"""应用服务层：编排领域对象、持久化、权限、幂等与补位。

所有写操作都在单一线程化事务（``BEGIN IMMEDIATE``）内完成“读—判定—写—记事件”，
因此并发占位不会超卖；命令携带 ``idempotency_key`` 时，重试/重复请求安全回放。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .errors import (
    NotFoundError,
    PermissionError,
    StateConflictError,
    ValidationError,
)
from .matching import rank_candidates, score_pair
from .models import (
    Application,
    ApplicationStatus,
    CancelReason,
    Candidate,
    Plan,
    Quota,
    ResponsibleParty,
    Role,
    Slot,
    SlotStatus,
)
from .repository import Database
from .timeutil import Clock, SystemClock, TimeWindow, iso, now_utc, parse_instant

ACTIVE_SLOT_STATUSES = (
    SlotStatus.HELD,
    SlotStatus.CONFIRMED,
    SlotStatus.CHECKED_IN,
    SlotStatus.COMPLETED,
)
DEFAULT_HOLD_TTL = timedelta(hours=72)


@dataclass
class Actor:
    """请求身份。role 决定权限，school 用于校级隔离，申请人 id 即候选人 id。"""

    id: str
    role: Role
    school: str = ""
    name: str = ""

    @property
    def candidate_id(self) -> str:
        return self.id


@dataclass
class ServiceResult:
    """命令返回：业务数据 + 是否为幂等回放。"""

    data: dict[str, Any]
    replayed: bool = False


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class VisitExchangeService:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------- infra helpers
    def _now(self) -> datetime:
        return self.clock.now()

    def _event(self, at: datetime, actor: Actor, event_type: str, **kw: Any) -> None:
        self.db.append_event(at, actor.id, event_type, **kw)

    def _idem_run(
        self,
        actor: Actor,
        scope: str,
        key: str | None,
        fingerprint: dict,
        action: Callable[[], dict[str, Any]],
    ) -> ServiceResult:
        """在同一事务内登记幂等键并执行业务；重复键回放首次结果。"""
        if not key:
            with self.db.transaction():
                return ServiceResult(action(), replayed=False)

        full_key = f"{scope}:{key}"
        with self.db.transaction():
            rec = self.db.idem_get(full_key)
            fp = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)
            if rec is not None:
                if rec["fingerprint"] != fp:
                    raise ValidationError(
                        "幂等键被用于不同的请求体", details={"key": key}
                    )
                if rec["status"] == "pending":
                    from .errors import IdempotencyConflictError

                    raise IdempotencyConflictError(
                        "同一幂等键的前序请求仍在处理或曾中断", details={"key": key}
                    )
                if rec["status"] == "done":
                    return ServiceResult(rec["response"], replayed=True)
            self.db.idem_put_pending(full_key, scope, fp, self._now())

        try:
            with self.db.transaction():
                response = action()
                self.db.idem_finish(full_key, "done", response, self._now())
        except BaseException:
            # 业务失败：业务事务已回滚；清除 pending 记录，允许同键稍后重试。
            with self.db.transaction():
                self.db.idem_delete_pending(full_key)
            raise
        return ServiceResult(response, replayed=False)

    # ------------------------------------------------------------------ catalog
    def register_candidate(self, actor: Actor, data: dict) -> dict:
        self._require_role(actor, Role.ADMIN)
        cid = str(data.get("id") or "").strip()
        if not cid:
            raise ValidationError("候选人 id 必填")
        windows = self._parse_windows(data.get("availability_windows", []))
        candidate = Candidate(
            id=cid,
            name=str(data.get("name", "")),
            sending_school=str(data.get("sending_school", "")),
            disciplines=[str(d).strip() for d in data.get("disciplines", []) if str(d).strip()],
            availability_windows=windows,
            passport_no=str(data.get("passport_no", "")),
            visa_ready=bool(data.get("visa_ready", False)),
        )
        if not candidate.name or not candidate.sending_school:
            raise ValidationError("候选人姓名与所属院校必填")
        with self.db.transaction():
            self.db.upsert_candidate(candidate)
            self._event(self._now(), actor, "candidate_registered",
                        candidate_id=cid, payload={"fields": list(data.keys())})
        return candidate.to_dict()

    def register_quota(self, actor: Actor, data: dict) -> dict:
        self._require_role(actor, Role.ADMIN)
        qid = str(data.get("id") or "").strip()
        if not qid:
            raise ValidationError("名额 id 必填")
        win = self._parse_single_window(data)
        seats = int(data.get("seats", 0))
        budget = float(data.get("budget_per_seat", 0))
        if seats <= 0 or budget < 0:
            raise ValidationError("名额数须为正整数，每人预算不可为负")
        quota = Quota(
            id=qid,
            host_school=str(data.get("host_school", "")),
            discipline=str(data.get("discipline", "")).strip(),
            window=win,
            seats=seats,
            budget_per_seat=budget,
            funding_source=str(data.get("funding_source", "")),
        )
        if not quota.host_school or not quota.discipline or not quota.funding_source:
            raise ValidationError("接收院校、专业与经费来源必填")
        with self.db.transaction():
            existing = self.db.get_quota(qid)
            if existing and self.db.list_slots(
                quota_id=qid, statuses=ACTIVE_SLOT_STATUSES
            ):
                raise StateConflictError("名额已有进行中的安排，不可改写建档信息")
            self.db.upsert_quota(quota)
            self._event(self._now(), actor, "quota_registered", quota_id=qid)
        return quota.to_dict()

    def _parse_windows(self, raw: list) -> list[TimeWindow]:
        out: list[TimeWindow] = []
        for i, item in enumerate(raw):
            try:
                tz = str(item.get("tz", "UTC"))
                start = parse_instant(item["start"], tz)
                end = parse_instant(item["end"], item.get("end_tz", tz))
                out.append(TimeWindow(start, end, tz, item.get("end_tz", tz),
                                      label=str(item.get("label", f"window-{i+1}"))))
            except (KeyError, ValueError) as exc:
                raise ValidationError(f"第 {i+1} 个时间窗非法: {exc}") from exc
        return out

    def _parse_single_window(self, data: dict) -> TimeWindow:
        if "window" in data:
            w = data["window"]
            tz = str(w.get("tz", "UTC"))
            return TimeWindow(
                parse_instant(w["start"], tz),
                parse_instant(w["end"], w.get("end_tz", tz)),
                tz, w.get("end_tz", tz), label=str(w.get("label", "host_window")),
            )
        tz = str(data.get("tz", "UTC"))
        return TimeWindow(
            parse_instant(data["window_start"], tz),
            parse_instant(data["window_end"], data.get("end_tz", tz)),
            tz, data.get("end_tz", tz), label="host_window",
        )

    # --------------------------------------------------------------------- apply
    def apply(self, actor: Actor, data: dict, idem_key: str | None) -> ServiceResult:
        self._require_role(actor, Role.APPLICANT)
        candidate_id = str(data.get("candidate_id", "")).strip()
        quota_id = str(data.get("quota_id", "")).strip()
        if candidate_id != actor.candidate_id:
            raise PermissionError("申请人只能提交本人的申请")
        try:
            budget = float(data.get("requested_budget", 0))
        except (TypeError, ValueError) as exc:
            raise ValidationError("申请预算非法") from exc
        if budget < 0:
            raise ValidationError("申请预算不可为负")

        def action() -> dict[str, Any]:
            candidate = self.db.get_candidate(candidate_id)
            quota = self.db.get_quota(quota_id)
            if not candidate or not quota:
                raise NotFoundError("候选人或名额不存在")
            existing = self.db.find_application(candidate_id, quota_id)
            if existing:
                if existing.status in (ApplicationStatus.SUBMITTED,
                                       ApplicationStatus.UNDER_REVIEW,
                                       ApplicationStatus.APPROVED):
                    return {**existing.to_dict(), "note": "已存在有效申请，幂等返回"}
                raise StateConflictError(
                    "该候选人对该名额已有终态申请，不可重复提交",
                    details={"existing_application": existing.id,
                             "status": existing.status.value},
                )
            app = Application(
                id=_new_id("app"), candidate_id=candidate_id, quota_id=quota_id,
                status=ApplicationStatus.SUBMITTED, requested_budget=budget,
                submitted_at=self._now(), updated_at=self._now(),
            )
            self.db.insert_application(app)
            self._event(self._now(), actor, "application_submitted",
                        candidate_id=candidate_id, quota_id=quota_id,
                        payload={"application_id": app.id, "budget": budget})
            return app.to_dict()

        return self._idem_run(actor, "apply", idem_key,
                              {"candidate_id": candidate_id, "quota_id": quota_id,
                               "requested_budget": budget}, action)

    def withdraw(self, actor: Actor, application_id: str) -> dict:
        with self.db.transaction():
            app = self._require_application(application_id)
            if actor.role == Role.APPLICANT and app.candidate_id != actor.candidate_id:
                raise PermissionError("只能撤回本人的申请")
            if actor.role not in (Role.APPLICANT, Role.ADMIN):
                raise PermissionError("无权撤回申请")
            if app.status not in (ApplicationStatus.SUBMITTED,
                                  ApplicationStatus.UNDER_REVIEW):
                raise StateConflictError(f"申请处于 {app.status.value}，不可撤回")
            app.status = ApplicationStatus.WITHDRAWN
            app.updated_at = self._now()
            self.db.update_application(app)
            self._event(self._now(), actor, "application_withdrawn",
                        candidate_id=app.candidate_id, quota_id=app.quota_id,
                        payload={"application_id": app.id})
            return app.to_dict()

    # -------------------------------------------------------------------- review
    def review(self, actor: Actor, application_id: str, data: dict) -> dict:
        self._require_role(actor, Role.INTERNATIONAL_OFFICE)
        decision = str(data.get("decision", "")).strip()
        note = str(data.get("note", ""))
        if decision not in ("approve", "reject", "start"):
            raise ValidationError("decision 须为 approve / reject / start")
        with self.db.transaction():
            app = self._require_application(application_id)
            quota = self._require_quota(app.quota_id)
            self._require_host_scope(actor, quota)
            if decision == "start":
                if app.status != ApplicationStatus.SUBMITTED:
                    raise StateConflictError("仅 submitted 申请可进入评审")
                app.status = ApplicationStatus.UNDER_REVIEW
            elif decision == "approve":
                if app.status not in (ApplicationStatus.SUBMITTED,
                                      ApplicationStatus.UNDER_REVIEW):
                    raise StateConflictError(f"申请处于 {app.status.value}，不可通过")
                app.status = ApplicationStatus.APPROVED
            else:
                if app.status not in (ApplicationStatus.SUBMITTED,
                                      ApplicationStatus.UNDER_REVIEW):
                    raise StateConflictError(f"申请处于 {app.status.value}，不可拒绝")
                app.status = ApplicationStatus.REJECTED
            app.reviewer = actor.id
            app.review_note = note
            app.updated_at = self._now()
            self.db.update_application(app)
            event_name = {"start": "application_review_started",
                          "approve": "application_approved",
                          "reject": "application_rejected"}[decision]
            self._event(self._now(), actor, event_name,
                        candidate_id=app.candidate_id, quota_id=quota.id,
                        payload={"application_id": app.id, "decision": decision,
                                 "note": note})
            return app.to_dict()

    def list_applications(self, actor: Actor, quota_id: str | None = None) -> dict:
        with self.db.transaction():
            if actor.role == Role.APPLICANT:
                apps = self.db.list_applications(candidate_id=actor.candidate_id)
            elif actor.role == Role.INTERNATIONAL_OFFICE:
                apps = [a for a in self.db.list_applications(quota_id=quota_id)
                        if self._scope_allows(actor, self._require_quota(a.quota_id))]
            elif actor.role == Role.ADMIN:
                apps = self.db.list_applications(quota_id=quota_id)
            else:
                raise PermissionError("该角色无权查看申请列表")
            return {"applications": [a.to_dict() for a in apps]}

    # ------------------------------------------------------------- match & plan
    def match_report(self, actor: Actor, quota_id: str) -> dict:
        """返回全部候选的硬约束判定与评分分解（含落选解释）。"""
        with self.db.transaction():
            quota = self._require_quota(quota_id)
            self._require_host_scope(actor, quota)
            candidates = {c.id: c for c in self.db.list_candidates()}
            quotas = {q.id: q for q in self.db.list_quotas()}
            results = rank_candidates(candidates, quotas, self.db.list_applications())
            rows = results.get(quota_id, [])
            return {
                "quota_id": quota_id,
                "seats": quota.seats,
                "funding_frozen": quota.funding_frozen,
                "results": [self._explain(r, candidates[r.candidate_id], quota)
                            for r in rows],
            }

    @staticmethod
    def _explain(result, candidate: Candidate, quota: Quota) -> dict:
        d = result.to_dict()
        d["candidate_name"] = candidate.name
        if result.eligible:
            b = result.breakdown
            d["explanation"] = [
                f"综合评分 {result.score:.1f}/100",
                f"时间窗重叠得分 {b['time_overlap']:.1f}/40（已按双方时区换算绝对时间）",
                f"专业匹配得分 {b['discipline']:.1f}/25（名额方向：{quota.discipline}）",
                f"预算余量得分 {b['budget_headroom']:.1f}/20"
                f"（申请额未超每人预算 {quota.budget_per_seat:.2f}，来源：{quota.funding_source}）",
                f"签证材料齐备 {b['visa_ready']:.1f}/15",
            ] + [f"附加说明：{note}" for note in result.notes]
        else:
            lines = ["未通过硬性约束："] + [f"- {r}" for r in result.reasons]
            if result.notes:
                lines.append("附加说明：" + "；".join(result.notes))
            d["explanation"] = lines
        return d

    def generate_plan(self, actor: Actor, quota_id: str) -> dict:
        self._require_role(actor, Role.ADMIN, Role.INTERNATIONAL_OFFICE)
        with self.db.transaction():
            quota = self._require_quota(quota_id)
            self._require_host_scope(actor, quota, allow_roles=(Role.ADMIN,))
            existing = self.db.latest_active_plan(quota_id)
            if existing:
                blocking = [s for s in existing.slots if s.status in ACTIVE_SLOT_STATUSES]
                if blocking:
                    raise StateConflictError(
                        "名额已有进行中的安排；如需重排，请先终结或取消现有占用",
                        details={"plan_id": existing.id},
                    )
                # 旧方案中未落位的主选与替补一律失效，避免游离槽位干扰补位
                for stale in existing.slots:
                    if stale.status in (SlotStatus.OPEN, SlotStatus.WAITLISTED):
                        stale.status = SlotStatus.EXPIRED
                        stale.explanation.append("方案被新一轮匹配取代，槽位失效")
                        self.db.update_slot(stale)
                self.db.close_plan(existing.id)
            candidates = {c.id: c for c in self.db.list_candidates()}
            quotas = {q.id: q for q in self.db.list_quotas()}
            ranked = rank_candidates(candidates, quotas,
                                     self.db.list_applications()).get(quota_id, [])
            eligible = [r for r in ranked if r.eligible]
            plan = Plan(id=_new_id("plan"), quota_id=quota_id,
                        created_at=self._now(), created_by=actor.id)
            self.db.insert_plan(plan)
            for rank, result in enumerate(eligible, start=1):
                status = SlotStatus.OPEN if rank <= quota.seats else SlotStatus.WAITLISTED
                explanation = self._explain(
                    result, candidates[result.candidate_id], quota
                )["explanation"]
                slot = Slot(
                    id=_new_id("slot"), plan_id=plan.id, quota_id=quota_id,
                    candidate_id=result.candidate_id, rank=rank, status=status,
                    score=result.score, score_breakdown=result.breakdown,
                    explanation=explanation + (
                        [f"主选第 {rank} 位，可在名额释放后锁定"]
                        if status == SlotStatus.OPEN
                        else [f"替补第 {rank - quota.seats} 位，按分数顺序等候递补"]
                    ),
                )
                self.db.insert_slot(slot)
            self._event(self._now(), actor, "plan_generated", plan_id=plan.id,
                        quota_id=quota_id,
                        payload={"eligible": len(eligible), "seats": quota.seats,
                                 "primary": min(quota.seats, len(eligible)),
                                 "waitlist": max(0, len(eligible) - quota.seats)})
            return self.db.get_plan(plan.id).to_dict()

    def get_plan(self, actor: Actor, plan_id: str) -> dict:
        with self.db.transaction():
            plan = self._require_plan(plan_id)
            quota = self._require_quota(plan.quota_id)
            self._require_host_scope(actor, quota, allow_candidate=True, plan=plan)
            if actor.role == Role.APPLICANT:
                # 申请人只能看到方案中与本人相关的槽位与替补位次信息
                data = plan.to_dict(include_slots=False)
                data["slots"] = [s.to_dict() for s in plan.slots
                                 if s.candidate_id == actor.candidate_id]
                return data
            return plan.to_dict()

    # -------------------------------------------------------------- lock (占位)
    def lock_slots(self, actor: Actor, quota_id: str, data: dict,
                   idem_key: str | None) -> ServiceResult:
        self._require_role(actor, Role.INTERNATIONAL_OFFICE)
        slot_ids = data.get("slot_ids")
        ttl_seconds = int(data.get("hold_ttl_seconds", DEFAULT_HOLD_TTL.total_seconds()))
        if ttl_seconds <= 0:
            raise ValidationError("占位有效期必须为正数")

        def action() -> dict[str, Any]:
            quota = self._require_quota(quota_id)
            self._require_host_scope(actor, quota)
            if quota.funding_frozen:
                raise StateConflictError("经费来源已冻结，不可新增占位")
            plan = self.db.latest_active_plan(quota_id)
            if not plan:
                raise NotFoundError("该名额尚无匹配方案")
            open_slots = [s for s in plan.slots if s.status == SlotStatus.OPEN]
            if slot_ids is not None:
                wanted = []
                by_id = {s.id: s for s in plan.slots}
                for sid in slot_ids:
                    slot = by_id.get(str(sid))
                    if not slot:
                        raise NotFoundError(f"槽位 {sid} 不属于名额 {quota_id} 的当前方案")
                    wanted.append(slot)
            else:
                wanted = sorted(open_slots, key=lambda s: s.rank)
            active = self.db.list_slots(quota_id=quota_id, statuses=ACTIVE_SLOT_STATUSES)
            free_seats = quota.seats - len(active)
            held, errors = [], []
            deadline = self._now() + timedelta(seconds=ttl_seconds)
            for slot in wanted:
                if slot.status != SlotStatus.OPEN:
                    errors.append({"slot_id": slot.id,
                                   "error": "state_conflict",
                                   "message": f"槽位当前状态 {slot.status.value}，不可占位"})
                    continue
                if free_seats <= 0:
                    errors.append({"slot_id": slot.id,
                                   "error": "capacity_conflict",
                                   "message": f"名额 {quota_id} 已占满（{quota.seats} 席）"})
                    continue
                slot.status = SlotStatus.HELD
                slot.held_until = deadline
                slot.explanation.append(
                    f"由 {actor.name or actor.id} 于 {iso(self._now())} 占位，"
                    f"须在 {iso(deadline)} 前确认，逾期自动释放并顺延替补"
                )
                self.db.update_slot(slot)
                self._event(self._now(), actor, "slot_held", slot_id=slot.id,
                            plan_id=plan.id, quota_id=quota_id,
                            candidate_id=slot.candidate_id,
                            payload={"held_until": iso(deadline)})
                held.append(slot.to_dict())
                free_seats -= 1
            return {"quota_id": quota_id, "held": held, "errors": errors,
                    "remaining_seats": free_seats}

        return self._idem_run(
            actor, "lock", idem_key,
            {"quota_id": quota_id, "slot_ids": slot_ids, "ttl": ttl_seconds}, action,
        )

    # ------------------------------------------------------------- confirm (锁定)
    def confirm_slot(self, actor: Actor, slot_id: str, data: dict,
                     idem_key: str | None) -> ServiceResult:
        self._require_role(actor, Role.INTERNATIONAL_OFFICE)
        ticketed = bool(data.get("ticketed", False))
        note = str(data.get("note", ""))

        def action() -> dict[str, Any]:
            slot = self._require_slot(slot_id)
            quota = self._require_quota(slot.quota_id)
            self._require_host_scope(actor, quota)
            if quota.funding_frozen:
                raise StateConflictError("经费来源已冻结，不可确认安排")
            if slot.status == SlotStatus.CONFIRMED:
                # 幂等的状态层：已确认即成功回放；出票标记不允许被下调
                if ticketed and not slot.ticketed:
                    raise StateConflictError("槽位已确认但未出票，出票请走单独流程")
                return {**slot.to_dict(), "note": "槽位已确认，幂等返回"}
            if slot.status != SlotStatus.HELD:
                raise StateConflictError(
                    f"槽位状态 {slot.status.value}，仅占位中（held）可确认")
            if slot.held_until and self._now() >= slot.held_until:
                raise StateConflictError(
                    "占位已过期；请由超时回收器或 POST /quotas/{id}/backfill "
                    "完成释放与替补后再安排")
            slot.status = SlotStatus.CONFIRMED
            slot.confirmed_at = self._now()
            slot.held_until = None
            if ticketed:
                slot.ticketed = True
                self.db.adjust_ticketed(quota.id, +1)
            slot.explanation.append(
                f"由 {actor.name or actor.id} 确认锁定"
                + ("，已出票；已出票安排不参与自动挪动" if ticketed else "，尚未出票")
            )
            self.db.update_slot(slot)
            self._event(self._now(), actor,
                        "ticket_issued" if ticketed else "slot_confirmed",
                        slot_id=slot.id, quota_id=quota.id, plan_id=slot.plan_id,
                        candidate_id=slot.candidate_id, payload={"note": note})
            return slot.to_dict()

        return self._idem_run(actor, "confirm", idem_key,
                              {"slot_id": slot_id, "ticketed": ticketed}, action)

    def confirm_batch(self, actor: Actor, data: dict,
                      idem_key: str | None) -> ServiceResult:
        """分批确认：逐项汇报成败，不因单项失败回滚整批（部分确认）。"""
        self._require_role(actor, Role.INTERNATIONAL_OFFICE)
        slot_ids = [str(x) for x in data.get("slot_ids", [])]
        ticketed = bool(data.get("ticketed", False))
        if not slot_ids:
            raise ValidationError("slot_ids 不能为空")

        def action() -> dict[str, Any]:
            confirmed, failed = [], []
            for sid in slot_ids:
                try:
                    slot = self._require_slot(sid)
                    quota = self._require_quota(slot.quota_id)
                    self._require_host_scope(actor, quota)
                    if quota.funding_frozen:
                        raise StateConflictError("经费来源已冻结，不可确认安排")
                    if slot.status == SlotStatus.CONFIRMED:
                        confirmed.append({**slot.to_dict(), "note": "已确认，幂等跳过"})
                        continue
                    if slot.status != SlotStatus.HELD:
                        raise StateConflictError(
                            f"槽位状态 {slot.status.value}，不可确认")
                    if slot.held_until and self._now() >= slot.held_until:
                        raise StateConflictError("占位已过期")
                    slot.status = SlotStatus.CONFIRMED
                    slot.confirmed_at = self._now()
                    slot.held_until = None
                    if ticketed:
                        slot.ticketed = True
                        self.db.adjust_ticketed(quota.id, +1)
                    slot.explanation.append(
                        f"批量确认（{'已出票' if ticketed else '未出票'}），"
                        f"确认人 {actor.name or actor.id}")
                    self.db.update_slot(slot)
                    self._event(self._now(), actor,
                                "ticket_issued" if ticketed else "slot_confirmed",
                                slot_id=slot.id, quota_id=quota.id,
                                plan_id=slot.plan_id, candidate_id=slot.candidate_id,
                                payload={"batch": True})
                    confirmed.append(slot.to_dict())
                except Exception as exc:  # noqa: BLE001 - 单项失败记录到结果而非中断
                    code = getattr(exc, "code", "domain_error")
                    failed.append({"slot_id": sid, "error": code,
                                   "message": str(exc)})
            return {"confirmed": confirmed, "failed": failed,
                    "confirmed_count": len(confirmed), "failed_count": len(failed)}

        return self._idem_run(actor, "confirm_batch", idem_key,
                              {"slot_ids": slot_ids, "ticketed": ticketed}, action)

    # --------------------------------------------------------- cancel & backfill
    def cancel_slot(self, actor: Actor, slot_id: str, data: dict) -> dict:
        reason = str(data.get("reason", "")).strip()
        responsible = str(data.get("responsible_party", "")).strip()
        note = str(data.get("note", ""))
        try:
            reason_enum = CancelReason(reason)
        except ValueError as exc:
            raise ValidationError(
                f"非法取消原因，可选：{[r.value for r in CancelReason]}") from exc
        with self.db.transaction():
            slot = self._require_slot(slot_id)
            quota = self._require_quota(slot.quota_id)
            if actor.role == Role.APPLICANT:
                if slot.candidate_id != actor.candidate_id:
                    raise PermissionError("只能取消本人的安排")
            elif actor.role == Role.INTERNATIONAL_OFFICE:
                self._require_host_scope(actor, quota)
            elif actor.role != Role.ADMIN:
                raise PermissionError("无权取消安排")
            # 状态层幂等：同一原因的重复取消直接回放既成结果，不重复触发补位
            if slot.status in (SlotStatus.CANCELLED, SlotStatus.EXPIRED):
                if slot.cancel_reason == reason_enum.value:
                    replacement = self.db.list_slots(
                        quota_id=slot.quota_id, status=SlotStatus.HELD)
                    rep = next((s for s in replacement
                                if s.replaced_slot_id == slot.id), None)
                    return {"canceled": slot.to_dict(),
                            "replacement": rep.to_dict() if rep else None,
                            "backfill_deferred": quota.funding_frozen,
                            "note": "槽位已取消，幂等返回"}
                raise StateConflictError(
                    f"槽位已处于 {slot.status.value}（原因 {slot.cancel_reason}），"
                    "不可再次取消")
            return self._cancel_and_backfill(slot, reason_enum, responsible or None,
                                             actor, note)

    def report_visa_rejection(self, actor: Actor, slot_id: str,
                              data: dict | None = None) -> dict:
        """签证拒绝：国际处登记，默认责任记领事馆（可在 data 中更正责任方）。

        已出票安排不可自动挪动：仅记录责任事件，槽位保留，须人工走改票/废票流程。
        """
        self._require_role(actor, Role.INTERNATIONAL_OFFICE)
        data = data or {}
        with self.db.transaction():
            slot = self._require_slot(slot_id)
            quota = self._require_quota(slot.quota_id)
            self._require_host_scope(actor, quota)
            responsible = str(data.get("responsible_party",
                                       ResponsibleParty.CONSULATE.value))
            note = str(data.get("note", "签证被拒"))
            if slot.ticketed:
                already = self.db.list_events(
                    slot_id=slot.id,
                    event_type="ticketed_visa_rejection_recorded")
                if already:
                    return {"slot": slot.to_dict(), "replacement": None,
                            "manual_action_required": True,
                            "note": "签证拒绝已登记，幂等返回"}
                slot.explanation.append(
                    f"签证拒绝已登记（责任方：{responsible}，备注：{note}）；"
                    "因已出票，安排不自动挪动，等待人工改票/废票流程，名额暂不释放")
                self.db.update_slot(slot)
                self._event(self._now(), actor, "ticketed_visa_rejection_recorded",
                            slot_id=slot.id, plan_id=slot.plan_id, quota_id=quota.id,
                            candidate_id=slot.candidate_id,
                            payload={"responsible_party": responsible, "note": note})
                return {"slot": slot.to_dict(), "replacement": None,
                        "manual_action_required": True,
                        "message": "已出票安排不自动挪动，责任已记录，待人工处理"}
            if slot.status in (SlotStatus.CANCELLED, SlotStatus.EXPIRED):
                if slot.cancel_reason == CancelReason.VISA_REJECTED.value:
                    rep = next((s for s in self.db.list_slots(
                        quota_id=quota.id, status=SlotStatus.HELD)
                        if s.replaced_slot_id == slot.id), None)
                    return {"canceled": slot.to_dict(),
                            "replacement": rep.to_dict() if rep else None,
                            "backfill_deferred": quota.funding_frozen,
                            "note": "签证拒绝已登记，幂等返回"}
                raise StateConflictError(
                    f"槽位已处于 {slot.status.value}（原因 {slot.cancel_reason}）")
            return self._cancel_and_backfill(
                slot, CancelReason.VISA_REJECTED, responsible, actor, note)

    def _cancel_and_backfill(self, slot: Slot, reason: CancelReason,
                             responsible: str | None, actor: Actor,
                             note: str) -> dict[str, Any]:
        quota = self._require_quota(slot.quota_id)
        if slot.ticketed:
            raise StateConflictError(
                "该安排已出票，不可自动挪动或取消；须走线下改票流程并人工记录",
                details={"slot_id": slot.id})
        if slot.status not in (SlotStatus.OPEN, SlotStatus.HELD,
                               SlotStatus.CONFIRMED, SlotStatus.WAITLISTED):
            raise StateConflictError(
                f"槽位状态 {slot.status.value}，无可释放的占用")
        # OPEN（尚未占位的主选）取消同样腾出座位；替补退出不影响座位。
        frees_seat = slot.status in (SlotStatus.OPEN, SlotStatus.HELD,
                                     SlotStatus.CONFIRMED)
        if reason == CancelReason.VISA_REJECTED:
            responsible = responsible or ResponsibleParty.CONSULATE.value
        elif reason == CancelReason.FUNDING_FROZEN:
            responsible = responsible or ResponsibleParty.FUNDING_BODY.value
        elif reason == CancelReason.HOLD_EXPIRED:
            responsible = responsible or ResponsibleParty.SYSTEM.value
        else:
            responsible = responsible or ResponsibleParty.APPLICANT.value
        slot.status = SlotStatus.CANCELLED
        slot.cancelled_at = self._now()
        slot.cancel_reason = reason.value
        slot.responsible_party = responsible
        slot.held_until = None
        slot.explanation.append(
            f"取消：{reason.value}；责任方记录：{responsible}；备注：{note or '无'}")
        self.db.update_slot(slot)
        self._event(self._now(), actor, "slot_canceled", slot_id=slot.id,
                    plan_id=slot.plan_id, quota_id=quota.id,
                    candidate_id=slot.candidate_id,
                    payload={"reason": reason.value,
                             "responsible_party": responsible, "note": note,
                             "frees_seat": frees_seat})
        promotion = None
        deferred = False
        # 只有释放了实际座位才触发补位；替补自身退出不影响座位。
        if frees_seat and quota.funding_frozen:
            deferred = True
            self._event(self._now(), actor, "backfill_deferred",
                        plan_id=slot.plan_id, quota_id=quota.id,
                        payload={"reason": "funding_frozen", "canceled_slot": slot.id})
        elif frees_seat:
            promotion = self._promote_next(quota.id, actor, reason_canceled=slot.id)
        return {"canceled": slot.to_dict(),
                "replacement": promotion.to_dict() if promotion else None,
                "backfill_deferred": deferred}

    def _promote_next(self, quota_id: str, actor: Actor,
                      reason_canceled: str | None = None) -> Slot | None:
        """按替补顺序递补第一名仍合格的候选人；返回其槽位。"""
        quota = self._require_quota(quota_id)
        if quota.funding_frozen:
            return None
        plan = self.db.latest_active_plan(quota_id)
        if not plan:
            return None
        candidates = {c.id: c for c in self.db.list_candidates()}
        waitlist = sorted(
            (s for s in plan.slots if s.status == SlotStatus.WAITLISTED),
            key=lambda s: s.rank)
        apps = {a.candidate_id: a for a in
                self.db.list_applications(quota_id=quota_id,
                                          status=ApplicationStatus.APPROVED)}
        for slot in waitlist:
            candidate = candidates.get(slot.candidate_id)
            app = apps.get(slot.candidate_id)
            if not candidate or not app:
                continue
            check = score_pair(candidate, quota, app)
            if not check.eligible:
                slot.explanation.append(
                    f"跳过递补：{self._now().date()} 复核硬性约束未通过——"
                    + "；".join(check.reasons))
                self.db.update_slot(slot)
                self._event(self._now(), actor, "backfill_skipped",
                            slot_id=slot.id, quota_id=quota_id, plan_id=plan.id,
                            payload={"reasons": check.reasons})
                continue
            slot.status = SlotStatus.HELD
            slot.held_until = self._now() + DEFAULT_HOLD_TTL
            if reason_canceled:
                slot.replaced_slot_id = reason_canceled
            slot.explanation.append(
                f"按替补顺序递补（替补第 {slot.rank - quota.seats} 位）；"
                + (f"接替因取消而释放的槽位 {reason_canceled}；" if reason_canceled else "")
                + f"复核评分 {check.score:.1f}/100；须在 {iso(slot.held_until)} 前确认")
            slot.score_breakdown = check.breakdown
            self.db.update_slot(slot)
            self._event(self._now(), actor, "slot_replaced", slot_id=slot.id,
                        plan_id=plan.id, quota_id=quota_id,
                        candidate_id=slot.candidate_id,
                        payload={"replaced_slot": reason_canceled,
                                 "held_until": iso(slot.held_until)})
            return slot
        return None

    def backfill(self, actor: Actor, quota_id: str) -> dict:
        """显式触发有序补位（幂等：无空位则不产生变化）。"""
        with self.db.transaction():
            quota = self._require_quota(quota_id)
            self._require_host_scope(actor, quota, allow_roles=(Role.ADMIN,))
            if quota.funding_frozen:
                raise StateConflictError("经费冻结中，补位已顺延，解冻后自动执行")
            active = self.db.list_slots(quota_id=quota_id,
                                        statuses=ACTIVE_SLOT_STATUSES)
            promoted = []
            while len(active) + len(promoted) < quota.seats:
                slot = self._promote_next(quota_id, actor)
                if not slot:
                    break
                promoted.append(slot.to_dict())
            return {"quota_id": quota_id, "promoted": promoted,
                    "active_seats": len(active) + len(promoted),
                    "seats": quota.seats}

    # ------------------------------------------------------------------ funding
    def set_funding_frozen(self, actor: Actor, quota_id: str,
                           data: dict, idem_key: str | None) -> ServiceResult:
        self._require_role(actor, Role.FINANCE, Role.ADMIN)
        frozen = bool(data.get("frozen", True))
        note = str(data.get("note", ""))

        def action() -> dict[str, Any]:
            quota = self._require_quota(quota_id)
            if quota.funding_frozen == frozen:
                return {**quota.to_dict(), "note": "经费状态未变化，幂等返回"}
            # 写冻结位（整个判断链在单事务内，并发冻结被串行化）
            self.db.set_funding_flag(quota_id, frozen)
            self._event(self._now(), actor,
                        "funding_frozen" if frozen else "funding_unfrozen",
                        quota_id=quota_id, payload={"note": note})
            revoked, ticketed_kept = [], []
            if frozen:
                # 已出票安排不可自动挪动，原样保留；未出票的占位/确认退回替补序列，
                # 责任记 funding_body，冻结期间不得锁定，解冻后按分数顺序有序补位。
                live = self.db.list_slots(
                    quota_id=quota_id,
                    statuses=(SlotStatus.HELD, SlotStatus.CONFIRMED))
                for slot in live:
                    if slot.ticketed:
                        ticketed_kept.append(slot.id)
                        self._event(self._now(), actor, "ticketed_arrangement_kept",
                                    slot_id=slot.id, plan_id=slot.plan_id,
                                    quota_id=quota_id, candidate_id=slot.candidate_id,
                                    payload={"reason": "funding_frozen"})
                        continue
                    prev_status = slot.status.value
                    slot.status = SlotStatus.WAITLISTED
                    slot.cancelled_at = None
                    slot.cancel_reason = ""
                    slot.confirmed_at = None
                    slot.held_until = None
                    slot.explanation.append(
                        f"经费来源 {quota.funding_source} 冻结，{prev_status} 安排"
                        "退回替补序列；冻结期间不得锁定，解冻后按分数顺序有序递补；"
                        "责任方：funding_body")
                    self.db.update_slot(slot)
                    self._event(self._now(), actor, "arrangement_revoked_funding",
                                slot_id=slot.id, plan_id=slot.plan_id,
                                quota_id=quota_id, candidate_id=slot.candidate_id,
                                payload={"previous_status": prev_status,
                                         "responsible_party":
                                             ResponsibleParty.FUNDING_BODY.value})
                    revoked.append(slot.id)
                    self._event(self._now(), actor, "backfill_deferred",
                                plan_id=slot.plan_id, quota_id=quota_id,
                                payload={"reason": "funding_frozen",
                                         "revoked_slot": slot.id})
                promotions = []
            else:
                # 解冻：把冻结期间空位（含退回替补者）按替补顺序逐位补上
                promotions = []
                quota_refreshed = self._require_quota(quota_id)
                active = self.db.list_slots(quota_id=quota_id,
                                            statuses=ACTIVE_SLOT_STATUSES)
                while len(active) + len(promotions) < quota_refreshed.seats:
                    slot = self._promote_next(quota_id, actor)
                    if not slot:
                        break
                    promotions.append(slot.to_dict())
            return {"quota_id": quota_id, "funding_frozen": frozen,
                    "revoked_to_waitlist": revoked,
                    "ticketed_arrangements_kept": ticketed_kept,
                    "promotions_on_unfreeze": promotions}

        return self._idem_run(actor, "funding", idem_key,
                              {"quota_id": quota_id, "frozen": frozen}, action)

    # ------------------------------------------------------- check-in & complete
    def check_in(self, actor: Actor, slot_id: str, data: dict | None = None) -> dict:
        self._require_role(actor, Role.INTERNATIONAL_OFFICE)
        data = data or {}
        with self.db.transaction():
            slot = self._require_slot(slot_id)
            quota = self._require_quota(slot.quota_id)
            self._require_host_scope(actor, quota)
            if slot.status == SlotStatus.CHECKED_IN:
                return {**slot.to_dict(), "note": "已报到，幂等返回"}
            if slot.status != SlotStatus.CONFIRMED:
                raise StateConflictError(
                    f"槽位状态 {slot.status.value}，仅已确认安排可报到")
            at = parse_instant(data["at"], quota.window.start_tz) if data.get("at") \
                else self._now()
            slot.status = SlotStatus.CHECKED_IN
            slot.checked_in_at = at
            slot.explanation.append(f"于 {iso(at)} 报到")
            self.db.update_slot(slot)
            self._event(self._now(), actor, "slot_checked_in", slot_id=slot.id,
                        quota_id=quota.id, plan_id=slot.plan_id,
                        candidate_id=slot.candidate_id, payload={"at": iso(at)})
            return slot.to_dict()

    def complete(self, actor: Actor, slot_id: str, data: dict | None = None) -> dict:
        data = data or {}
        with self.db.transaction():
            slot = self._require_slot(slot_id)
            quota = self._require_quota(slot.quota_id)
            if actor.role not in (Role.ADMIN, Role.INTERNATIONAL_OFFICE):
                raise PermissionError("仅国际处或管理员可登记结项")
            if actor.role == Role.INTERNATIONAL_OFFICE:
                self._require_host_scope(actor, quota)
            if slot.status == SlotStatus.COMPLETED:
                return {**slot.to_dict(), "note": "已结项，幂等返回"}
            if slot.status != SlotStatus.CHECKED_IN:
                raise StateConflictError(
                    f"槽位状态 {slot.status.value}，仅已报到安排可结项")
            slot.status = SlotStatus.COMPLETED
            slot.completed_at = self._now()
            note = str(data.get("note", ""))
            slot.explanation.append(f"结项；备注：{note or '无'}")
            self.db.update_slot(slot)
            self._event(self._now(), actor, "slot_completed", slot_id=slot.id,
                        quota_id=quota.id, plan_id=slot.plan_id,
                        candidate_id=slot.candidate_id, payload={"note": note})
            return slot.to_dict()

    # ------------------------------------------------------------- hold timeouts
    def expire_holds(self) -> dict:
        """扫描并释放所有逾期占位，逐名额触发有序补位。重启后可安全重放。"""
        promoted, expired = [], []
        with self.db.transaction():
            now = self._now()
            held = self.db.list_slots(status=SlotStatus.HELD)
            due = [s for s in held if s.held_until and s.held_until <= now]
            system = Actor("system:reaper", Role.SYSTEM, name="超时回收器")
            by_quota: dict[str, list[Slot]] = {}
            for slot in due:
                by_quota.setdefault(slot.quota_id, []).append(slot)
            for quota_id, slots in by_quota.items():
                quota = self._require_quota(quota_id)
                for slot in slots:
                    self._expire_slot(slot, system)
                    expired.append(slot.id)
                # 每个被释放的座位顺延一次替补；新递补者获得全新占位期限，
                # 不会在本轮再次判定为逾期。
                if quota.funding_frozen:
                    for slot in slots:
                        self._event(self._now(), system, "backfill_deferred",
                                    plan_id=slot.plan_id, quota_id=quota_id,
                                    payload={"reason": "funding_frozen",
                                             "expired_slot": slot.id})
                    continue
                for freed_slot in slots:
                    promoted_slot = self._promote_next(
                        quota_id, system, reason_canceled=freed_slot.id)
                    if not promoted_slot:
                        break
                    promoted.append(promoted_slot.to_dict())
        return {"expired": expired, "promoted": promoted, "at": iso(self._now())}

    def _expire_slot(self, slot: Slot, actor: Actor) -> None:
        slot.status = SlotStatus.EXPIRED
        slot.cancelled_at = self._now()
        slot.cancel_reason = CancelReason.HOLD_EXPIRED.value
        slot.responsible_party = ResponsibleParty.SYSTEM.value
        slot.held_until = None
        slot.explanation.append(
            "占位超过确认期限，系统自动释放并按替补顺序补位；责任记录：system")
        self.db.update_slot(slot)
        self._event(self._now(), actor, "hold_expired", slot_id=slot.id,
                    plan_id=slot.plan_id, quota_id=slot.quota_id,
                    candidate_id=slot.candidate_id,
                    payload={"held_until": slot.held_until})

    # -------------------------------------------------------------- read models
    def get_slot(self, actor: Actor, slot_id: str) -> dict:
        with self.db.transaction():
            slot = self._require_slot(slot_id)
            quota = self._require_quota(slot.quota_id)
            self._require_host_scope(actor, quota,
                                     allow_roles=(Role.ADMIN, Role.FINANCE),
                                     allow_candidate=True, slot=slot)
            return slot.to_dict()

    def get_application(self, actor: Actor, application_id: str) -> dict:
        with self.db.transaction():
            app = self._require_application(application_id)
            if actor.role == Role.APPLICANT and app.candidate_id != actor.candidate_id:
                raise PermissionError("只能查看本人的申请")
            if actor.role == Role.INTERNATIONAL_OFFICE:
                self._require_host_scope(actor, self._require_quota(app.quota_id))
            elif actor.role not in (Role.ADMIN, Role.APPLICANT):
                raise PermissionError("该角色无权查看申请详情")
            return app.to_dict()

    def list_events(self, actor: Actor, quota_id: str | None = None,
                    slot_id: str | None = None) -> dict:
        if actor.role == Role.APPLICANT:
            raise PermissionError("申请人无权查看完整责任流水")
        with self.db.transaction():
            if actor.role == Role.INTERNATIONAL_OFFICE:
                # 院校隔离：国际处只能带本校名额查询，禁止全量拉取流水
                if not quota_id:
                    raise PermissionError("国际处须指定本校 quota_id 查询流水")
                self._require_host_scope(actor, self._require_quota(quota_id))
            events = self.db.list_events(quota_id=quota_id, slot_id=slot_id)
            return {"events": events}

    def roster(self, actor: Actor, quota_id: str) -> dict:
        """名额当前总览：活跃占用、空位、替补顺序与终态（用于业务人员核对）。"""
        with self.db.transaction():
            quota = self._require_quota(quota_id)
            self._require_host_scope(actor, quota,
                                     allow_roles=(Role.ADMIN, Role.FINANCE))
            plan = self.db.latest_active_plan(quota_id)
            slots = self.db.list_slots(quota_id=quota_id)
            active = [s for s in slots if s.status in ACTIVE_SLOT_STATUSES]
            waitlist = [s for s in (plan.slots if plan else [])
                        if s.status == SlotStatus.WAITLISTED]
            return {
                "quota": quota.to_dict(),
                "plan_id": plan.id if plan else None,
                "active_seats": len(active),
                "free_seats": max(0, quota.seats - len(active)),
                "active": [s.to_dict() for s in sorted(active, key=lambda s: s.rank)],
                "waitlist": [s.to_dict() for s in sorted(waitlist, key=lambda s: s.rank)],
                "ticketed_seats": quota.ticketed_seats,
            }

    # --------------------------------------------------------------- assertions
    def _require_role(self, actor: Actor, *roles: Role) -> None:
        if actor.role not in roles:
            raise PermissionError(
                f"该操作需要角色 {[r.value for r in roles]}，当前 {actor.role.value}")

    def _scope_allows(self, actor: Actor, quota: Quota) -> bool:
        if actor.role == Role.ADMIN:
            return True
        if actor.role == Role.INTERNATIONAL_OFFICE:
            return actor.school == quota.host_school
        return False

    def _require_host_scope(
        self, actor: Actor, quota: Quota, *,
        allow_roles: tuple[Role, ...] = (),
        allow_candidate: bool = False,
        plan: Plan | None = None,
        slot: Slot | None = None,
    ) -> None:
        if actor.role in allow_roles or actor.role == Role.ADMIN:
            return
        if actor.role == Role.INTERNATIONAL_OFFICE:
            if actor.school != quota.host_school:
                raise PermissionError(
                    f"院校隔离：{actor.school} 国际处无权操作 {quota.host_school} 的名额")
            return
        if actor.role == Role.APPLICANT and allow_candidate:
            cid = actor.candidate_id
            if slot is not None:
                if slot.candidate_id != cid:
                    raise PermissionError("只能查看与本人相关的安排")
                return
            if plan is not None and any(s.candidate_id == cid for s in plan.slots):
                return
        if actor.role == Role.APPLICANT:
            raise PermissionError("无权访问该资源")
        raise PermissionError("无权访问该资源")

    def _require_quota(self, quota_id: str) -> Quota:
        quota = self.db.get_quota(quota_id)
        if not quota:
            raise NotFoundError(f"名额 {quota_id} 不存在")
        return quota

    def _require_application(self, app_id: str) -> Application:
        app = self.db.get_application(app_id)
        if not app:
            raise NotFoundError(f"申请 {app_id} 不存在")
        return app

    def _require_plan(self, plan_id: str) -> Plan:
        plan = self.db.get_plan(plan_id)
        if not plan:
            raise NotFoundError(f"方案 {plan_id} 不存在")
        return plan

    def _require_slot(self, slot_id: str) -> Slot:
        slot = self.db.get_slot(slot_id)
        if not slot:
            raise NotFoundError(f"槽位 {slot_id} 不存在")
        return slot
