"""应用服务层：教师互访名额编排的全部用例。

事务与并发：每个命令在 :meth:`Storage.transaction`（BEGIN IMMEDIATE）内完成
“读-判-写”，槽位状态翻转走条件更新，因此并发占位、重复确认、重启恢复
都是安全的。所有 mutating 命令支持幂等键（``Idempotency-Key``）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from . import matcher
from .errors import (
    AuthError,
    ConflictError,
    DomainError,
    IdempotencyConflict,
    NotFoundError,
    PermissionError,
    TicketedImmovableError,
    ValidationError,
)
from .models import (
    DEFAULT_LOCK_TTL_SECONDS,
    DEFAULT_REVIEW_TTL_SECONDS,
    AppStatus,
    CandidateStatus,
    EventType,
    HoldReason,
    QuotaStatus,
    Responsibility,
    Role,
    SlotState,
    VisaStatus,
)
from .ports import Clock, IdGenerator, SystemClock, UuidIdGenerator
from .storage import Storage
from .timeutil import TimeParseError, parse_window

Actor = dict[str, Any]


def _require_str(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"缺少必填字段或类型错误: {field}")
    return value.strip()


def _require_int(payload: dict[str, Any], field: str, minimum: int) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"字段 {field} 必须是不小于 {minimum} 的整数")
    return value


class ExchangeService:
    """教师互访名额编排服务。"""

    def __init__(
        self,
        storage: Storage,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        review_ttl_seconds: int = DEFAULT_REVIEW_TTL_SECONDS,
        lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        recover: bool = True,
    ) -> None:
        self.storage = storage
        self.clock = clock or SystemClock()
        self.ids = ids or UuidIdGenerator()
        self.review_ttl_seconds = review_ttl_seconds
        self.lock_ttl_seconds = lock_ttl_seconds
        if recover:
            # 服务重启后继续执行超时释放（启动恢复）
            self.release_expired(actor="system")

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def authenticate(self, token: str | None) -> Actor:
        if not token:
            raise AuthError("缺少 X-Token 认证头")
        user = self.storage.user_by_token(token)
        if user is None:
            raise AuthError("无效的访问令牌")
        return user

    def seed_users(self, users: list[dict[str, Any]]) -> int:
        """初始化用户（仅当用户表为空时生效，便于测试与本地演示）。"""
        if self.storage.list_users():
            return 0
        for u in users:
            role = u.get("role")
            if role not in Role.ALL:
                raise ValidationError(f"未知角色: {role!r}")
            self.storage.add_user(
                token=_require_str(u, "token"),
                username=_require_str(u, "username"),
                role=role,
                org=str(u.get("org") or ""),
                candidate_id=u.get("candidate_id"),
            )
        return len(users)

    def add_user(self, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_role(actor, Role.COORDINATOR)
        role = payload.get("role")
        if role not in Role.ALL:
            raise ValidationError(f"未知角色: {role!r}")
        user = {
            "token": _require_str(payload, "token"),
            "username": _require_str(payload, "username"),
            "role": role,
            "org": str(payload.get("org") or ""),
            "candidate_id": payload.get("candidate_id"),
        }
        self.storage.add_user(**user)
        return user

    def list_users(self, actor: Actor) -> list[dict[str, Any]]:
        self._require_role(actor, Role.COORDINATOR)
        return self.storage.list_users()

    def _require_role(self, actor: Actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionError(
                f"角色 {actor['role']} 无权执行该操作，需要 {'/'.join(roles)}")

    def _emit(self, event_type: str, actor: Actor | str, responsibility: str,
              entity_type: str, entity_id: str,
              data: dict[str, Any] | None = None) -> None:
        if responsibility not in Responsibility.ALL:
            raise ValidationError(f"未知责任归属: {responsibility!r}")
        actor_name = actor if isinstance(actor, str) else actor["username"]
        self.storage.append_event({
            "id": self.ids.new_id("evt"),
            "type": event_type,
            "actor": actor_name,
            "responsibility": responsibility,
            "at": self.clock.now_us(),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "data": data or {},
        })

    def _idempotent(self, key: str | None, actor: Actor | str,
                    request_obj: dict[str, Any],
                    fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """幂等执行：同键同请求重放返回首个响应；同键不同请求报 409。"""
        if not key:
            return fn()
        actor_name = actor if isinstance(actor, str) else actor["username"]
        request_hash = hashlib.sha256(
            json.dumps(request_obj, sort_keys=True, ensure_ascii=False
                       ).encode("utf-8")).hexdigest()
        with self.storage.transaction():
            record = self.storage.get_idempotency(key)
            if record is not None:
                if record["request_hash"] != request_hash or record["actor"] != actor_name:
                    raise IdempotencyConflict(
                        "幂等键已被不同请求使用", details={"key": key})
                return {**record["response"], "idempotent_replay": True}
            response = fn()
            json.dumps(response)  # 保证可持久化
            self.storage.put_idempotency(
                key, actor_name, request_hash, response, self.clock.now_us())
            return response

    # ------------------------------------------------------------------
    # 候选人
    # ------------------------------------------------------------------
    def create_candidate(self, actor: Actor, payload: dict[str, Any],
                         idem_key: str | None = None) -> dict[str, Any]:
        self._require_role(actor, Role.COORDINATOR, Role.HOME_ADMIN)

        def core() -> dict[str, Any]:
            name = _require_str(payload, "name")
            discipline = _require_str(payload, "discipline")
            home_org = _require_str(payload, "home_org")
            if actor["role"] == Role.HOME_ADMIN and home_org != actor["org"]:
                raise PermissionError("派出院校管理员只能登记本院校候选人")
            visa_status = payload.get("visa_status", VisaStatus.PENDING)
            if visa_status not in VisaStatus.ALL:
                raise ValidationError(f"未知签证状态: {visa_status!r}")
            raw_windows = payload.get("windows") or []
            if not isinstance(raw_windows, list):
                raise ValidationError("windows 必须是时间窗数组")
            try:
                windows = [parse_window(w) for w in raw_windows]
            except TimeParseError as exc:
                raise ValidationError(f"时间窗无效: {exc}") from exc
            budget_sources = payload.get("budget_sources") or []
            if not isinstance(budget_sources, list) or not all(
                    isinstance(s, str) and s.strip() for s in budget_sources):
                raise ValidationError("budget_sources 必须是非空字符串数组")
            candidate = {
                "id": self.ids.new_id("cand"),
                "name": name,
                "discipline": discipline,
                "home_org": home_org,
                "windows": windows,
                "visa_status": visa_status,
                "budget_sources": [s.strip() for s in budget_sources],
                "status": CandidateStatus.ACTIVE,
                "created_at": self.clock.now_us(),
            }
            with self.storage.transaction():
                self.storage.insert_candidate(candidate)
                self._emit(EventType.CANDIDATE_CREATED, actor,
                           Responsibility.HOME_ORG, "candidate", candidate["id"],
                           {"name": name, "discipline": discipline})
            return {"candidate": candidate}

        return self._idempotent(idem_key, actor, {"op": "create_candidate", **payload}, core)

    def get_candidate(self, actor: Actor, candidate_id: str) -> dict[str, Any]:
        candidate = self.storage.get("candidates", candidate_id)
        if candidate is None or not self._can_see_candidate(actor, candidate):
            raise NotFoundError(f"候选人不存在: {candidate_id}")
        return {"candidate": candidate}

    def list_candidates(self, actor: Actor) -> dict[str, Any]:
        all_candidates = self.storage.query("candidates", order_by="created_at, id")
        visible = [c for c in all_candidates if self._can_see_candidate(actor, c)]
        return {"candidates": visible}

    def _can_see_candidate(self, actor: Actor, candidate: dict[str, Any]) -> bool:
        role = actor["role"]
        if role in (Role.COORDINATOR, Role.FINANCE, Role.HOST_ADMIN):
            return True
        if role == Role.HOME_ADMIN:
            return candidate["home_org"] == actor["org"]
        if role == Role.TEACHER:
            return candidate["id"] == actor.get("candidate_id")
        return False

    # ------------------------------------------------------------------
    # 名额
    # ------------------------------------------------------------------
    def create_quota(self, actor: Actor, payload: dict[str, Any],
                     idem_key: str | None = None) -> dict[str, Any]:
        self._require_role(actor, Role.COORDINATOR, Role.HOST_ADMIN)

        def core() -> dict[str, Any]:
            host_org = _require_str(payload, "host_org")
            if actor["role"] == Role.HOST_ADMIN and host_org != actor["org"]:
                raise PermissionError("接收院校管理员只能登记本院校名额")
            discipline = _require_str(payload, "discipline")
            try:
                window = parse_window(payload.get("window"))
            except TimeParseError as exc:
                raise ValidationError(f"名额时间窗无效: {exc}") from exc
            capacity = _require_int(payload, "capacity", 1)
            budget_source = _require_str(payload, "budget_source")
            budget_total = payload.get("budget_total", 0)
            if isinstance(budget_total, bool) or not isinstance(
                    budget_total, (int, float)) or budget_total < 0:
                raise ValidationError("budget_total 必须是非负数字")
            review_ttl = payload.get("review_ttl_seconds", self.review_ttl_seconds)
            lock_ttl = payload.get("lock_ttl_seconds", self.lock_ttl_seconds)
            for name, ttl in (("review_ttl_seconds", review_ttl),
                              ("lock_ttl_seconds", lock_ttl)):
                if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
                    raise ValidationError(f"{name} 必须是正整数秒")
            now = self.clock.now_us()
            quota = {
                "id": self.ids.new_id("quota"),
                "host_org": host_org,
                "discipline": discipline,
                "window": window,
                "capacity": capacity,
                "budget_source": budget_source,
                "budget_total": float(budget_total),
                "review_ttl_seconds": review_ttl,
                "lock_ttl_seconds": lock_ttl,
                "status": QuotaStatus.OPEN,
                "created_at": now,
            }
            with self.storage.transaction():
                self.storage.insert_quota(quota)
                for seq in range(1, capacity + 1):
                    self.storage.insert_slot({
                        "id": self.ids.new_id("slot"),
                        "quota_id": quota["id"], "seq": seq,
                        "state": SlotState.AVAILABLE, "updated_at": now,
                    })
                self._emit(EventType.QUOTA_CREATED, actor, Responsibility.HOST_ORG,
                           "quota", quota["id"],
                           {"host_org": host_org, "capacity": capacity})
            return {"quota": quota,
                    "slots": self.storage.list_slots(quota["id"])}

        return self._idempotent(idem_key, actor, {"op": "create_quota", **payload}, core)

    def close_quota(self, actor: Actor, quota_id: str,
                    reason: str = "") -> dict[str, Any]:
        quota = self._get_quota(quota_id)
        self._require_quota_admin(actor, quota)
        with self.storage.transaction():
            if quota["status"] == QuotaStatus.CLOSED:
                return {"quota": quota, "already": True}
            self.storage.update("quotas", quota_id, {"status": QuotaStatus.CLOSED})
            self._emit(EventType.QUOTA_CLOSED, actor, Responsibility.COORDINATOR,
                       "quota", quota_id, {"reason": reason})
        quota = self._get_quota(quota_id)
        return {"quota": quota}

    def get_quota(self, actor: Actor, quota_id: str) -> dict[str, Any]:
        quota = self._get_quota(quota_id)
        if not self._can_see_quota(actor, quota):
            raise NotFoundError(f"名额不存在: {quota_id}")
        return {"quota": quota, "slots": self.storage.list_slots(quota_id)}

    def list_quotas(self, actor: Actor) -> dict[str, Any]:
        quotas = self.storage.query("quotas", order_by="created_at, id")
        return {"quotas": [q for q in quotas if self._can_see_quota(actor, q)]}

    def _get_quota(self, quota_id: str) -> dict[str, Any]:
        quota = self.storage.get("quotas", quota_id)
        if quota is None:
            raise NotFoundError(f"名额不存在: {quota_id}")
        return quota

    def _can_see_quota(self, actor: Actor, quota: dict[str, Any]) -> bool:
        if actor["role"] == Role.HOST_ADMIN:
            return quota["host_org"] == actor["org"]
        return actor["role"] in (Role.COORDINATOR, Role.FINANCE, Role.HOME_ADMIN,
                                 Role.TEACHER)

    def _require_quota_admin(self, actor: Actor, quota: dict[str, Any]) -> None:
        if actor["role"] == Role.COORDINATOR:
            return
        if actor["role"] == Role.HOST_ADMIN and quota["host_org"] == actor["org"]:
            return
        raise PermissionError("需要国际处或本接收院校管理员权限")

    # ------------------------------------------------------------------
    # 方案（可解释、纯只读）
    # ------------------------------------------------------------------
    def proposal(self, actor: Actor, quota_id: str) -> dict[str, Any]:
        self._require_role(actor, Role.COORDINATOR, Role.HOST_ADMIN,
                           Role.HOME_ADMIN, Role.FINANCE)
        quota = self._get_quota(quota_id)
        if not self._can_see_quota(actor, quota):
            raise NotFoundError(f"名额不存在: {quota_id}")
        slots = self.storage.list_slots(quota_id)
        free = sum(1 for s in slots if s["state"] == SlotState.AVAILABLE)
        candidates = self.storage.query("candidates", order_by="created_at, id")
        active = [c for c in candidates if c["status"] == CandidateStatus.ACTIVE]
        result = matcher.generate_proposal(active, quota, free)
        self._emit(EventType.PROPOSAL_VIEWED, actor, Responsibility.COORDINATOR,
                   "quota", quota_id, {"free_slots": free})
        return result

    # ------------------------------------------------------------------
    # 申请
    # ------------------------------------------------------------------
    def apply(self, actor: Actor, payload: dict[str, Any],
              idem_key: str | None = None) -> dict[str, Any]:
        self._require_role(actor, Role.COORDINATOR, Role.HOME_ADMIN, Role.TEACHER)

        def core() -> dict[str, Any]:
            candidate_id = _require_str(payload, "candidate_id")
            quota_id = _require_str(payload, "quota_id")
            note = str(payload.get("note") or "")
            with self.storage.transaction():
                candidate = self.storage.get("candidates", candidate_id)
                if candidate is None:
                    raise NotFoundError(f"候选人不存在: {candidate_id}")
                quota = self.storage.get("quotas", quota_id)
                if quota is None:
                    raise NotFoundError(f"名额不存在: {quota_id}")
                self._check_apply_permission(actor, candidate)
                if candidate["status"] != CandidateStatus.ACTIVE:
                    raise ConflictError("候选人经费已冻结，无法申请",
                                        code="candidate_frozen")
                if quota["status"] != QuotaStatus.OPEN:
                    raise ConflictError("名额已关闭，无法申请", code="quota_closed")
                now = self.clock.now_us()
                if int(quota["window"]["end_us"]) <= now:
                    raise ConflictError("名额时间窗已结束", code="window_passed")
                existing = self.storage.open_application(candidate_id, quota_id)
                if existing is not None:
                    # 天然幂等：同人同名额的开放申请直接返回
                    return {"application": existing,
                            "slot": self._slot_of(existing),
                            "waitlisted": existing["slot_id"] is None,
                            "deduplicated": True}
                evaluation = matcher.evaluate(candidate, quota)
                if evaluation["status"] == matcher.INELIGIBLE:
                    raise ConflictError("候选人不满足名额约束",
                                        code="ineligible", details=evaluation)
                app_id = self.ids.new_id("app")
                slot = self._first_available_slot(quota_id)
                waitlisted = slot is None
                application = {
                    "id": app_id, "candidate_id": candidate_id,
                    "quota_id": quota_id,
                    "slot_id": slot["id"] if slot else None,
                    "status": AppStatus.PENDING, "note": note,
                    "eval": evaluation, "decide_reason": None,
                    "created_at": now, "decided_at": None, "version": 0,
                }
                self.storage.insert_application(application)
                self._emit(EventType.APPLICATION_CREATED, actor,
                           Responsibility.CANDIDATE, "application", app_id,
                           {"candidate_id": candidate_id, "quota_id": quota_id})
                held_slot = None
                if slot is not None:
                    held_slot = self._hold_slot(
                        slot, candidate_id, app_id, quota, now, actor,
                        HoldReason.REVIEW, quota["review_ttl_seconds"])
                else:
                    self._emit(EventType.APPLICATION_WAITLISTED, actor,
                               Responsibility.SYSTEM, "application", app_id,
                               {"quota_id": quota_id,
                                "reason": "无空缺席位，进入替补队列"})
                return {"application": application, "slot": held_slot,
                        "waitlisted": waitlisted, "deduplicated": False}

        return self._idempotent(idem_key, actor, {"op": "apply", **payload}, core)

    def _check_apply_permission(self, actor: Actor, candidate: dict[str, Any]) -> None:
        if actor["role"] == Role.COORDINATOR:
            return
        if actor["role"] == Role.HOME_ADMIN:
            if candidate["home_org"] != actor["org"]:
                raise PermissionError("只能为本院校候选人提交申请")
            return
        if actor["role"] == Role.TEACHER:
            if candidate["id"] != actor.get("candidate_id"):
                raise PermissionError("教师只能为本人提交申请")
            return
        raise PermissionError("无权提交申请")

    def _first_available_slot(self, quota_id: str) -> dict[str, Any] | None:
        slots = self.storage.list_slots(quota_id, (SlotState.AVAILABLE,))
        return slots[0] if slots else None

    def _hold_slot(self, slot: dict[str, Any], candidate_id: str, app_id: str,
                   quota: dict[str, Any], now: int, actor: Actor | str,
                   reason: str, ttl_seconds: int) -> dict[str, Any]:
        expires = now + ttl_seconds * 1_000_000
        ok = self.storage.slot_transition(
            slot["id"], (SlotState.AVAILABLE,),
            state=SlotState.HELD, candidate_id=candidate_id,
            application_id=app_id, hold_reason=reason,
            hold_expires_at=expires, updated_at=now,
            version=slot["version"] + 1)
        if not ok:
            raise ConflictError("席位已被并发占用", code="slot_race")
        held = self.storage.get("quota_slots", slot["id"])
        self._emit(EventType.SLOT_HELD, actor, Responsibility.SYSTEM,
                   "slot", slot["id"],
                   {"quota_id": quota["id"], "candidate_id": candidate_id,
                    "application_id": app_id, "hold_reason": reason,
                    "hold_expires_at": expires})
        return held

    def _slot_of(self, application: dict[str, Any]) -> dict[str, Any] | None:
        if application.get("slot_id"):
            return self.storage.get("quota_slots", application["slot_id"])
        return None

    # ------------------------------------------------------------------
    # 评审
    # ------------------------------------------------------------------
    def review(self, actor: Actor, app_id: str, decision: str,
               reason: str = "", idem_key: str | None = None) -> dict[str, Any]:
        self._require_role(actor, Role.COORDINATOR, Role.HOME_ADMIN)
        if decision not in ("approve", "reject"):
            raise ValidationError("decision 必须是 approve 或 reject")

        def core() -> dict[str, Any]:
            with self.storage.transaction():
                app = self._get_application(app_id)
                self._check_review_permission(actor, app)
                now = self.clock.now_us()
                if app["status"] != AppStatus.PENDING:
                    if decision == "approve" and app["status"] == AppStatus.APPROVED:
                        return {"application": app, "already": True,
                                "promotions": []}
                    raise ConflictError(
                        f"申请当前状态为 {app['status']}，不可评审",
                        code="invalid_state")
                released_slot = None
                promotions: list[dict[str, Any]] = []
                if decision == "approve":
                    self.storage.update_application(
                        app_id, status=AppStatus.APPROVED, decided_at=now,
                        decide_reason=reason or None,
                        version=app["version"] + 1)
                    self._emit(EventType.APPLICATION_DECIDED, actor,
                               Responsibility.COORDINATOR, "application", app_id,
                               {"decision": "approve", "reason": reason})
                    if app["slot_id"] is None:
                        # 替补队列中的申请获批：立即尝试补位
                        promotions = self._promote(app["quota_id"], now, actor)
                else:
                    self.storage.update_application(
                        app_id, status=AppStatus.REJECTED, decided_at=now,
                        decide_reason=reason or None,
                        version=app["version"] + 1)
                    self._emit(EventType.APPLICATION_DECIDED, actor,
                               Responsibility.COORDINATOR, "application", app_id,
                               {"decision": "reject", "reason": reason})
                    if app["slot_id"]:
                        released_slot = self._release_slot(
                            app["slot_id"], now, actor,
                            Responsibility.COORDINATOR, trigger="review_reject")
                        promotions = self._promote(app["quota_id"], now, actor)
                return {"application": self._get_application(app_id),
                        "already": False, "released_slot": released_slot,
                        "promotions": promotions}

        return self._idempotent(
            idem_key, actor, {"op": "review", "app": app_id,
                              "decision": decision, "reason": reason}, core)

    def _check_review_permission(self, actor: Actor, app: dict[str, Any]) -> None:
        if actor["role"] == Role.COORDINATOR:
            return
        candidate = self.storage.get("candidates", app["candidate_id"])
        if actor["role"] == Role.HOME_ADMIN and candidate \
                and candidate["home_org"] == actor["org"]:
            return
        raise PermissionError("只能评审本院校候选人的申请")

    def _get_application(self, app_id: str) -> dict[str, Any]:
        app = self.storage.get("applications", app_id)
        if app is None:
            raise NotFoundError(f"申请不存在: {app_id}")
        return app

    # ------------------------------------------------------------------
    # 分批锁定
    # ------------------------------------------------------------------
    def batch_lock(self, actor: Actor, quota_id: str,
                   ttl_seconds: int | None = None,
                   idem_key: str | None = None) -> dict[str, Any]:
        """把该名额下“已批准且仍占位”的申请分批锁定。

        每条申请独立结算：成功、已锁定（幂等）或失败原因逐项返回，
        支持部分确认。
        """
        def core() -> dict[str, Any]:
            with self.storage.transaction():
                quota = self._get_quota(quota_id)
                self._require_quota_admin(actor, quota)
                if ttl_seconds is not None and (
                        isinstance(ttl_seconds, bool)
                        or not isinstance(ttl_seconds, int) or ttl_seconds <= 0):
                    raise ValidationError("ttl_seconds 必须是正整数秒")
                now = self.clock.now_us()
                ttl = ttl_seconds or quota["lock_ttl_seconds"]
                apps = self.storage.query(
                    "applications",
                    "quota_id = ? AND status = ? AND slot_id IS NOT NULL",
                    (quota_id, AppStatus.APPROVED), order_by="created_at, id")
                items: list[dict[str, Any]] = []
                for app in apps:
                    slot = self.storage.get("quota_slots", app["slot_id"])
                    if slot is None:
                        items.append({"application_id": app["id"], "locked": False,
                                      "error": "席位记录缺失"})
                        continue
                    if slot["state"] == SlotState.LOCKED:
                        items.append({"application_id": app["id"],
                                      "slot_id": slot["id"], "locked": True,
                                      "already": True})
                        continue
                    ok = self.storage.slot_transition(
                        slot["id"], (SlotState.HELD,),
                        state=SlotState.LOCKED, hold_reason=HoldReason.LOCK,
                        hold_expires_at=now + ttl * 1_000_000,
                        locked_at=now, updated_at=now,
                        version=slot["version"] + 1)
                    if ok:
                        self._emit(EventType.SLOT_LOCKED, actor,
                                   Responsibility.COORDINATOR, "slot", slot["id"],
                                   {"quota_id": quota_id,
                                    "application_id": app["id"],
                                    "hold_expires_at": now + ttl * 1_000_000})
                        items.append({"application_id": app["id"],
                                      "slot_id": slot["id"], "locked": True,
                                      "already": False})
                    else:
                        fresh = self.storage.get("quota_slots", slot["id"])
                        items.append({"application_id": app["id"],
                                      "slot_id": slot["id"], "locked": False,
                                      "error": f"席位状态为 {fresh['state']}，无法锁定"})
                locked_count = sum(1 for i in items if i.get("locked"))
                return {"quota_id": quota_id, "items": items,
                        "locked_count": locked_count}

        return self._idempotent(
            idem_key, actor, {"op": "batch_lock", "quota": quota_id,
                              "ttl": ttl_seconds}, core)

    # ------------------------------------------------------------------
    # 出票 / 报到 / 结项
    # ------------------------------------------------------------------
    def ticket(self, actor: Actor, app_id: str,
               idem_key: str | None = None) -> dict[str, Any]:
        def core() -> dict[str, Any]:
            with self.storage.transaction():
                app = self._get_application(app_id)
                slot = self._require_slot_for(app)
                self._require_quota_admin(actor, self._get_quota(app["quota_id"]))
                now = self.clock.now_us()
                if slot["state"] == SlotState.TICKETED:
                    return {"application": app, "slot": slot, "already": True}
                if slot["state"] != SlotState.LOCKED:
                    raise ConflictError(
                        f"仅锁定状态可出票，当前席位状态为 {slot['state']}",
                        code="invalid_state")
                self.storage.slot_transition(
                    slot["id"], (SlotState.LOCKED,),
                    state=SlotState.TICKETED, ticketed_at=now,
                    hold_expires_at=None, updated_at=now,
                    version=slot["version"] + 1)
                self._emit(EventType.SLOT_TICKETED, actor,
                           Responsibility.COORDINATOR, "slot", slot["id"],
                           {"application_id": app_id, "quota_id": app["quota_id"]})
                return {"application": app,
                        "slot": self.storage.get("quota_slots", slot["id"]),
                        "already": False}

        return self._idempotent(idem_key, actor, {"op": "ticket", "app": app_id}, core)

    def checkin(self, actor: Actor, app_id: str, at: int | None = None,
                idem_key: str | None = None) -> dict[str, Any]:
        def core() -> dict[str, Any]:
            with self.storage.transaction():
                app = self._get_application(app_id)
                slot = self._require_slot_for(app)
                self._check_report_permission(actor, app)
                now = self.clock.now_us()
                checkin_at = at if at is not None else now
                if slot["state"] == SlotState.CHECKED_IN:
                    return {"application": app, "slot": slot, "already": True}
                if slot["state"] != SlotState.TICKETED:
                    raise ConflictError(
                        f"仅已出票安排可报到，当前席位状态为 {slot['state']}",
                        code="invalid_state")
                self.storage.slot_transition(
                    slot["id"], (SlotState.TICKETED,),
                    state=SlotState.CHECKED_IN, checkin_at=checkin_at,
                    updated_at=now, version=slot["version"] + 1)
                self._emit(EventType.SLOT_CHECKED_IN, actor,
                           Responsibility.CANDIDATE, "slot", slot["id"],
                           {"application_id": app_id, "checkin_at": checkin_at})
                return {"application": app,
                        "slot": self.storage.get("quota_slots", slot["id"]),
                        "already": False}

        return self._idempotent(idem_key, actor, {"op": "checkin", "app": app_id}, core)

    def closeout(self, actor: Actor, app_id: str, report: str = "",
                 idem_key: str | None = None) -> dict[str, Any]:
        def core() -> dict[str, Any]:
            with self.storage.transaction():
                app = self._get_application(app_id)
                slot = self._require_slot_for(app)
                self._require_quota_admin(actor, self._get_quota(app["quota_id"]))
                now = self.clock.now_us()
                if slot["state"] == SlotState.COMPLETED:
                    return {"application": app, "slot": slot, "already": True}
                if slot["state"] != SlotState.CHECKED_IN:
                    raise ConflictError(
                        f"仅已报到安排可结项，当前席位状态为 {slot['state']}",
                        code="invalid_state")
                self.storage.slot_transition(
                    slot["id"], (SlotState.CHECKED_IN,),
                    state=SlotState.COMPLETED, closeout_at=now,
                    closeout_report=report or None, updated_at=now,
                    version=slot["version"] + 1)
                self._emit(EventType.SLOT_CLOSED_OUT, actor,
                           Responsibility.HOST_ORG, "slot", slot["id"],
                           {"application_id": app_id, "report": report})
                return {"application": app,
                        "slot": self.storage.get("quota_slots", slot["id"]),
                        "already": False}

        return self._idempotent(idem_key, actor,
                                {"op": "closeout", "app": app_id, "report": report},
                                core)

    def _require_slot_for(self, app: dict[str, Any]) -> dict[str, Any]:
        if not app.get("slot_id"):
            raise ConflictError("该申请没有关联席位（可能在替补队列中）",
                                code="no_slot")
        slot = self.storage.get("quota_slots", app["slot_id"])
        if slot is None:
            raise NotFoundError(f"席位不存在: {app['slot_id']}")
        return slot

    def _check_report_permission(self, actor: Actor, app: dict[str, Any]) -> None:
        if actor["role"] == Role.COORDINATOR:
            return
        if actor["role"] == Role.TEACHER \
                and app["candidate_id"] == actor.get("candidate_id"):
            return
        quota = self._get_quota(app["quota_id"])
        if actor["role"] == Role.HOST_ADMIN and quota["host_org"] == actor["org"]:
            return
        raise PermissionError("无权为该安排报到")

    # ------------------------------------------------------------------
    # 取消（主动释放）
    # ------------------------------------------------------------------
    def cancel(self, actor: Actor, app_id: str, reason: str = "",
               responsibility: str = Responsibility.COORDINATOR,
               idem_key: str | None = None) -> dict[str, Any]:
        if responsibility not in Responsibility.ALL:
            raise ValidationError(f"未知责任归属: {responsibility!r}")

        def core() -> dict[str, Any]:
            with self.storage.transaction():
                app = self._get_application(app_id)
                self._check_cancel_permission(actor, app)
                now = self.clock.now_us()
                if app["status"] == AppStatus.CANCELLED:
                    return {"application": app, "already": True,
                            "released_slot": None, "promotions": []}
                if app["status"] in (AppStatus.REJECTED, AppStatus.EXPIRED):
                    raise ConflictError(
                        f"申请已终结（{app['status']}），不可取消",
                        code="invalid_state")
                protected_slot: dict[str, Any] | None = None
                if app["slot_id"]:
                    slot = self.storage.get("quota_slots", app["slot_id"])
                    if slot and slot["state"] in SlotState.IMMOVABLE:
                        protected_slot = slot
                        # 责任记录随事务提交；异常在提交后抛出，避免回滚掉记录
                        self._emit(EventType.TICKETED_PROTECTED, actor,
                                   responsibility, "slot", slot["id"],
                                   {"application_id": app_id,
                                    "attempt": "cancel",
                                    "detail": "已出票安排不可自动挪动，需人工处理"})
                if protected_slot is not None:
                    result: dict[str, Any] = {"__protected__": True}
                else:
                    self.storage.update_application(
                        app_id, status=AppStatus.CANCELLED, decided_at=now,
                        decide_reason=reason or None, version=app["version"] + 1)
                    self._emit(EventType.APPLICATION_DECIDED, actor, responsibility,
                               "application", app_id,
                               {"decision": "cancel", "reason": reason})
                    released_slot = None
                    promotions: list[dict[str, Any]] = []
                    if app["slot_id"]:
                        released_slot = self._release_slot(
                            app["slot_id"], now, actor, responsibility,
                            trigger="cancel")
                        promotions = self._promote(app["quota_id"], now, actor)
                    result = {"application": self._get_application(app_id),
                              "already": False, "released_slot": released_slot,
                              "promotions": promotions}
            if result.pop("__protected__", False):
                raise TicketedImmovableError(
                    "已出票（及之后）的安排不可自动挪动，需人工线下处理")
            return result

        return self._idempotent(
            idem_key, actor, {"op": "cancel", "app": app_id, "reason": reason}, core)

    def _check_cancel_permission(self, actor: Actor, app: dict[str, Any]) -> None:
        if actor["role"] == Role.COORDINATOR:
            return
        if actor["role"] == Role.TEACHER \
                and app["candidate_id"] == actor.get("candidate_id"):
            return
        candidate = self.storage.get("candidates", app["candidate_id"])
        if actor["role"] == Role.HOME_ADMIN and candidate \
                and candidate["home_org"] == actor["org"]:
            return
        raise PermissionError("无权取消该申请")

    # ------------------------------------------------------------------
    # 签证拒绝 / 经费冻结（级联 + 有序补位 + 责任记录）
    # ------------------------------------------------------------------
    def visa_rejected(self, actor: Actor, candidate_id: str,
                      reason: str = "") -> dict[str, Any]:
        self._require_role(actor, Role.COORDINATOR)
        return self._candidate_cascade(
            actor, candidate_id, reason,
            new_visa_status=VisaStatus.REJECTED,
            new_candidate_status=None,
            event_type=EventType.VISA_REJECTED,
            responsibility=Responsibility.VISA_AUTHORITY)

    def funding_frozen(self, actor: Actor, candidate_id: str,
                       reason: str = "") -> dict[str, Any]:
        self._require_role(actor, Role.COORDINATOR, Role.FINANCE)
        return self._candidate_cascade(
            actor, candidate_id, reason,
            new_visa_status=None,
            new_candidate_status=CandidateStatus.FROZEN,
            event_type=EventType.FUNDING_FROZEN,
            responsibility=Responsibility.FINANCE)

    def _candidate_cascade(self, actor: Actor, candidate_id: str, reason: str,
                           new_visa_status: str | None,
                           new_candidate_status: str | None,
                           event_type: str,
                           responsibility: str) -> dict[str, Any]:
        with self.storage.transaction():
            candidate = self.storage.get("candidates", candidate_id)
            if candidate is None:
                raise NotFoundError(f"候选人不存在: {candidate_id}")
            now = self.clock.now_us()
            updates: dict[str, Any] = {}
            if new_visa_status:
                updates["visa_status"] = new_visa_status
            if new_candidate_status:
                updates["status"] = new_candidate_status
            if updates:
                self.storage.update_candidate(candidate_id, **updates)
            self._emit(event_type, actor, responsibility,
                       "candidate", candidate_id, {"reason": reason})
            cascades: list[dict[str, Any]] = []
            promotions: list[dict[str, Any]] = []
            open_apps = self.storage.query(
                "applications",
                "candidate_id = ? AND status IN ('pending', 'approved')",
                (candidate_id,), order_by="created_at, id")
            for app in open_apps:
                if app["slot_id"]:
                    slot = self.storage.get("quota_slots", app["slot_id"])
                    if slot is not None and slot["state"] in SlotState.IMMOVABLE:
                        # 已出票安排不可自动挪动：申请与席位都保持原样，
                        # 只记录责任事件，等待人工线下处理。
                        self._emit(EventType.TICKETED_PROTECTED, actor,
                                   responsibility, "slot", slot["id"],
                                   {"application_id": app["id"],
                                    "trigger": event_type,
                                    "detail": "已出票安排不可自动挪动，保留原安排，需人工处理"})
                        cascades.append({"application_id": app["id"],
                                         "slot_id": slot["id"],
                                         "action": "ticketed_protected"})
                        continue
                self.storage.update_application(
                    app["id"], status=AppStatus.CANCELLED, decided_at=now,
                    decide_reason=reason or event_type,
                    version=app["version"] + 1)
                self._emit(EventType.APPLICATION_DECIDED, actor, responsibility,
                           "application", app["id"],
                           {"decision": "cancel", "reason": reason or event_type})
                if not app["slot_id"]:
                    cascades.append({"application_id": app["id"],
                                     "action": "cancelled_waitlisted"})
                    continue
                slot = self.storage.get("quota_slots", app["slot_id"])
                if slot is None:
                    continue
                released = self._release_slot(slot["id"], now, actor,
                                              responsibility, trigger=event_type)
                cascades.append({"application_id": app["id"],
                                 "slot_id": released, "action": "released"})
                promotions.extend(self._promote(app["quota_id"], now, actor))
            return {"candidate": self.storage.get("candidates", candidate_id),
                    "cascades": cascades, "promotions": promotions}

    # ------------------------------------------------------------------
    # 超时释放（有序补位）与启动恢复
    # ------------------------------------------------------------------
    def release_expired(self, actor: Actor | str = "system") -> dict[str, Any]:
        """释放所有到期的占位/锁定席位，并按替补顺序补位。

        由后台回收器周期调用，也在服务启动时调用一次（重启恢复）。
        """
        now = self.clock.now_us()
        released: list[str] = []
        promotions: list[dict[str, Any]] = []
        with self.storage.transaction():
            for slot in self.storage.expired_holds(now):
                ok = self.storage.slot_transition(
                    slot["id"], (SlotState.HELD, SlotState.LOCKED),
                    state=SlotState.AVAILABLE, candidate_id=None,
                    application_id=None, hold_reason=None,
                    hold_expires_at=None, locked_at=None, updated_at=now,
                    version=slot["version"] + 1)
                if not ok:
                    continue  # 已被并发命令处理
                if slot.get("application_id"):
                    app = self.storage.get("applications", slot["application_id"])
                    if app and app["status"] in AppStatus.OPEN_STATES:
                        self.storage.update_application(
                            app["id"], status=AppStatus.EXPIRED, decided_at=now,
                            decide_reason="占位/锁定超时",
                            version=app["version"] + 1)
                self._emit(EventType.HOLD_EXPIRED, actor, Responsibility.TIMEOUT,
                           "slot", slot["id"],
                           {"quota_id": slot["quota_id"],
                            "application_id": slot.get("application_id"),
                            "hold_reason": slot.get("hold_reason")})
                released.append(slot["id"])
                promotions.extend(self._promote(slot["quota_id"], now, actor))
        return {"released": released, "promotions": promotions}

    def _release_slot(self, slot_id: str, now: int, actor: Actor | str,
                      responsibility: str, trigger: str) -> str:
        """把占位/锁定席位释放为空缺，返回槽 id。调用方须持有事务。"""
        slot = self.storage.get("quota_slots", slot_id)
        if slot is None:
            raise NotFoundError(f"席位不存在: {slot_id}")
        if slot["state"] in SlotState.IMMOVABLE:
            raise TicketedImmovableError("已出票安排不可自动挪动")
        ok = self.storage.slot_transition(
            slot_id, (SlotState.HELD, SlotState.LOCKED),
            state=SlotState.AVAILABLE, candidate_id=None, application_id=None,
            hold_reason=None, hold_expires_at=None, locked_at=None,
            updated_at=now, version=slot["version"] + 1)
        if not ok:
            fresh = self.storage.get("quota_slots", slot_id)
            raise ConflictError(f"席位状态已变化（{fresh['state']}），释放失败",
                                code="slot_race")
        self._emit(EventType.SLOT_CANCELLED, actor, responsibility,
                   "slot", slot_id,
                   {"quota_id": slot["quota_id"], "trigger": trigger,
                    "previous_state": slot["state"]})
        return slot_id

    def _promote(self, quota_id: str, now: int, actor: Actor | str,
                 tried: set[str] | None = None) -> list[dict[str, Any]]:
        """有序补位：空缺席位优先给“已批准”的排队申请，其次“待评审”。

        每次补位前重新做匹配评估（签证/经费状态可能已变化），
        不合格的跳过并记录 ``substitution_skipped``。
        """
        tried = tried if tried is not None else set()
        promotions: list[dict[str, Any]] = []
        quota = self.storage.get("quotas", quota_id)
        if quota is None or quota["status"] != QuotaStatus.OPEN:
            return promotions
        while True:
            slot = self._first_available_slot(quota_id)
            if slot is None:
                break
            queue = self._waitlist(quota_id)
            placed = False
            for app in queue:
                if app["id"] in tried:
                    continue
                candidate = self.storage.get("candidates", app["candidate_id"])
                if candidate is None \
                        or candidate["status"] != CandidateStatus.ACTIVE:
                    tried.add(app["id"])
                    self._emit(EventType.SUBSTITUTION_SKIPPED, actor,
                               Responsibility.SYSTEM, "application", app["id"],
                               {"quota_id": quota_id,
                                "reason": "候选人经费冻结或已注销"})
                    continue
                evaluation = matcher.evaluate(candidate, quota)
                if evaluation["status"] != matcher.ELIGIBLE:
                    tried.add(app["id"])
                    self._emit(EventType.SUBSTITUTION_SKIPPED, actor,
                               Responsibility.SYSTEM, "application", app["id"],
                               {"quota_id": quota_id,
                                "reason": "; ".join(evaluation["reasons"])})
                    continue
                held = self._hold_slot(slot, candidate["id"], app["id"], quota,
                                       now, actor, HoldReason.REVIEW,
                                       quota["review_ttl_seconds"])
                self.storage.update_application(
                    app["id"], slot_id=slot["id"], eval=evaluation,
                    version=app["version"] + 1)
                self._emit(EventType.SUBSTITUTION_PROMOTED, actor,
                           Responsibility.SYSTEM, "application", app["id"],
                           {"quota_id": quota_id, "slot_id": slot["id"],
                            "candidate_id": candidate["id"]})
                promotions.append({"application_id": app["id"],
                                   "slot_id": held["id"],
                                   "candidate_id": candidate["id"]})
                placed = True
                break
            if not placed:
                break
        return promotions

    def _waitlist(self, quota_id: str) -> list[dict[str, Any]]:
        """替补队列：已批准优先，其次待评审；各自按申请时间先后排序。"""
        apps = self.storage.query(
            "applications",
            "quota_id = ? AND slot_id IS NULL AND status IN ('pending', 'approved')",
            (quota_id,))
        approved = sorted((a for a in apps if a["status"] == AppStatus.APPROVED),
                          key=lambda a: (a["created_at"], a["id"]))
        pending = sorted((a for a in apps if a["status"] == AppStatus.PENDING),
                         key=lambda a: (a["created_at"], a["id"]))
        return approved + pending

    # ------------------------------------------------------------------
    # 查询（含权限隔离）
    # ------------------------------------------------------------------
    def get_application(self, actor: Actor, app_id: str) -> dict[str, Any]:
        app = self._get_application(app_id)
        if not self._can_see_application(actor, app):
            raise NotFoundError(f"申请不存在: {app_id}")
        return {"application": app, "slot": self._slot_of(app)}

    def list_applications(self, actor: Actor, quota_id: str | None = None,
                          status: str | None = None,
                          candidate_id: str | None = None) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if quota_id:
            clauses.append("quota_id = ?")
            params.append(quota_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if candidate_id:
            clauses.append("candidate_id = ?")
            params.append(candidate_id)
        apps = self.storage.query(
            "applications", " AND ".join(clauses), tuple(params),
            order_by="created_at, id")
        visible = [a for a in apps if self._can_see_application(actor, a)]
        return {"applications": visible}

    def _can_see_application(self, actor: Actor, app: dict[str, Any]) -> bool:
        role = actor["role"]
        if role in (Role.COORDINATOR, Role.FINANCE):
            return True
        if role == Role.TEACHER:
            return app["candidate_id"] == actor.get("candidate_id")
        candidate = self.storage.get("candidates", app["candidate_id"])
        if role == Role.HOME_ADMIN:
            return bool(candidate) and candidate["home_org"] == actor["org"]
        if role == Role.HOST_ADMIN:
            quota = self.storage.get("quotas", app["quota_id"])
            return bool(quota) and quota["host_org"] == actor["org"]
        return False

    def get_slot(self, actor: Actor, slot_id: str) -> dict[str, Any]:
        slot = self.storage.get("quota_slots", slot_id)
        if slot is None:
            raise NotFoundError(f"席位不存在: {slot_id}")
        quota = self._get_quota(slot["quota_id"])
        role = actor["role"]
        if role in (Role.COORDINATOR, Role.FINANCE):
            return {"slot": slot}
        if role == Role.HOST_ADMIN and quota["host_org"] == actor["org"]:
            return {"slot": slot}
        if role == Role.TEACHER and slot.get("candidate_id") == actor.get("candidate_id"):
            return {"slot": slot}
        if role == Role.HOME_ADMIN and slot.get("candidate_id"):
            candidate = self.storage.get("candidates", slot["candidate_id"])
            if candidate and candidate["home_org"] == actor["org"]:
                return {"slot": slot}
        raise NotFoundError(f"席位不存在: {slot_id}")

    def list_events(self, actor: Actor, entity_type: str | None = None,
                    entity_id: str | None = None) -> dict[str, Any]:
        events = self.storage.list_events(entity_type, entity_id)
        visible = [e for e in events
                   if self._can_see_event(actor, e)]
        return {"events": visible}

    def _can_see_event(self, actor: Actor, event: dict[str, Any]) -> bool:
        role = actor["role"]
        if role in (Role.COORDINATOR, Role.FINANCE):
            return True
        etype, eid = event["entity_type"], event["entity_id"]
        if role == Role.TEACHER:
            cid = actor.get("candidate_id")
            if not cid:
                return False
            if etype == "candidate":
                return eid == cid
            if etype == "application":
                app = self.storage.get("applications", eid)
                return bool(app) and app["candidate_id"] == cid
            if etype == "slot":
                slot = self.storage.get("quota_slots", eid)
                return bool(slot) and slot.get("candidate_id") == cid
            return False
        if role == Role.HOME_ADMIN:
            if etype == "candidate":
                candidate = self.storage.get("candidates", eid)
                return bool(candidate) and candidate["home_org"] == actor["org"]
            if etype == "application":
                app = self.storage.get("applications", eid)
                candidate = self.storage.get(
                    "candidates", app["candidate_id"]) if app else None
                return bool(candidate) and candidate["home_org"] == actor["org"]
            return False
        if role == Role.HOST_ADMIN:
            if etype == "quota":
                quota = self.storage.get("quotas", eid)
                return bool(quota) and quota["host_org"] == actor["org"]
            if etype == "slot":
                slot = self.storage.get("quota_slots", eid)
                quota = self.storage.get("quotas", slot["quota_id"]) if slot else None
                return bool(quota) and quota["host_org"] == actor["org"]
            if etype == "application":
                app = self.storage.get("applications", eid)
                quota = self.storage.get("quotas", app["quota_id"]) if app else None
                return bool(quota) and quota["host_org"] == actor["org"]
            return False
        return False
