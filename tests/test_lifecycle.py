"""申请→评审→锁定→出票→报到→结项全生命周期，以及取消/签证/经费级联补位。"""
import unittest

from service_09252_003.errors import ConflictError, TicketedImmovableError
from service_09252_003.models import EventType, Responsibility

from helpers import (
    add_candidate,
    add_quota,
    apply,
    approve_all,
    coord,
    event_types,
    finance,
    make_service,
)


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.coord = coord(self.svc)

    def _setup_held(self, capacity: int = 1, candidates: int = 1):
        quota = add_quota(self.svc, capacity=capacity)
        apps = []
        for i in range(candidates):
            cand = add_candidate(self.svc, name=f"教师{i}")
            result = apply(self.svc, cand["id"], quota["id"])
            apps.append((cand, result))
        return quota, apps

    def test_full_happy_path(self) -> None:
        quota, [(cand, result)] = self._setup_held()
        app = result["application"]
        slot = result["slot"]
        self.assertEqual(app["status"], "pending")
        self.assertEqual(slot["state"], "held")
        self.assertEqual(slot["hold_reason"], "review")

        self.svc.review(self.coord, app["id"], "approve", "同意")
        locked = self.svc.batch_lock(self.coord, quota["id"])
        self.assertEqual(locked["locked_count"], 1)
        ticketed = self.svc.ticket(self.coord, app["id"])
        self.assertEqual(ticketed["slot"]["state"], "ticketed")
        self.assertIsNone(ticketed["slot"]["hold_expires_at"])  # 出票后不再超时

        checked = self.svc.checkin(self.coord, app["id"])
        self.assertEqual(checked["slot"]["state"], "checked_in")
        closed = self.svc.closeout(self.coord, app["id"], "访学完成，成果良好")
        self.assertEqual(closed["slot"]["state"], "completed")
        self.assertEqual(closed["slot"]["closeout_report"], "访学完成，成果良好")

        types = event_types(self.svc, "slot", slot["id"])
        for expected in (EventType.SLOT_HELD, EventType.SLOT_LOCKED,
                         EventType.SLOT_TICKETED, EventType.SLOT_CHECKED_IN,
                         EventType.SLOT_CLOSED_OUT):
            self.assertIn(expected, types)

    def test_apply_ineligible_explained(self) -> None:
        quota = add_quota(self.svc, discipline="物理")
        cand = add_candidate(self.svc, discipline="计算机")
        with self.assertRaises(ConflictError) as ctx:
            apply(self.svc, cand["id"], quota["id"])
        self.assertEqual(ctx.exception.code, "ineligible")
        self.assertTrue(any("专业不匹配" in r
                            for r in ctx.exception.details["reasons"]))

    def test_duplicate_apply_deduplicated(self) -> None:
        quota, [(cand, first)] = self._setup_held()
        second = apply(self.svc, cand["id"], quota["id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["application"]["id"], first["application"]["id"])

    def test_state_machine_guards(self) -> None:
        quota, [(cand, result)] = self._setup_held()
        app = result["application"]
        with self.assertRaises(ConflictError):
            self.svc.ticket(self.coord, app["id"])  # 未锁定不可出票
        with self.assertRaises(ConflictError):
            self.svc.checkin(self.coord, app["id"])  # 未出票不可报到
        with self.assertRaises(ConflictError):
            self.svc.closeout(self.coord, app["id"])  # 未报到不可结项

    def test_close_quota_blocks_new_applications(self) -> None:
        quota, _ = self._setup_held()
        self.svc.close_quota(self.coord, quota["id"], "计划调整")
        cand = add_candidate(self.svc, name="后来者")
        with self.assertRaises(ConflictError) as ctx:
            apply(self.svc, cand["id"], quota["id"])
        self.assertEqual(ctx.exception.code, "quota_closed")


class SubstitutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.coord = coord(self.svc)

    def _quota_with_waitlist(self):
        """capacity=1：A 占位，B、C 依次进入替补队列。"""
        quota = add_quota(self.svc, capacity=1)
        cand_a = add_candidate(self.svc, name="A")
        cand_b = add_candidate(self.svc, name="B")
        cand_c = add_candidate(self.svc, name="C")
        app_a = apply(self.svc, cand_a["id"], quota["id"])["application"]
        app_b = apply(self.svc, cand_b["id"], quota["id"])["application"]
        app_c = apply(self.svc, cand_c["id"], quota["id"])["application"]
        self.assertIsNotNone(app_a["slot_id"])
        self.assertIsNone(app_b["slot_id"])
        self.assertIsNone(app_c["slot_id"])
        return quota, (cand_a, cand_b, cand_c), (app_a, app_b, app_c)

    def test_cancel_promotes_next_in_order(self) -> None:
        quota, cands, (app_a, app_b, app_c) = self._quota_with_waitlist()
        result = self.svc.cancel(self.coord, app_a["id"], "候选人临时取消")
        self.assertEqual(result["released_slot"], app_a["slot_id"])
        self.assertEqual(len(result["promotions"]), 1)
        self.assertEqual(result["promotions"][0]["application_id"], app_b["id"])
        slot = self.svc.get_slot(self.coord, app_a["slot_id"])["slot"]
        self.assertEqual(slot["state"], "held")
        self.assertEqual(slot["candidate_id"], cands[1]["id"])
        self.assertIn(EventType.SUBSTITUTION_PROMOTED,
                      event_types(self.svc, "application", app_b["id"]))

    def test_approved_waitlist_jumps_ahead_of_pending(self) -> None:
        quota, cands, (app_a, app_b, app_c) = self._quota_with_waitlist()
        # C 先获批（仍在队列中），B 仍待评审；释放后 C 优先补位
        self.svc.review(self.coord, app_c["id"], "approve")
        result = self.svc.cancel(self.coord, app_a["id"], "取消")
        self.assertEqual(result["promotions"][0]["application_id"], app_c["id"])

    def test_review_reject_releases_slot_and_promotes(self) -> None:
        quota, cands, (app_a, app_b, _) = self._quota_with_waitlist()
        result = self.svc.review(self.coord, app_a["id"], "reject", "材料不全")
        self.assertEqual(result["promotions"][0]["application_id"], app_b["id"])
        app = self.svc.get_application(self.coord, app_a["id"])["application"]
        self.assertEqual(app["status"], "rejected")

    def test_visa_rejected_cascades_and_records_responsibility(self) -> None:
        quota, cands, (app_a, app_b, _) = self._quota_with_waitlist()
        result = self.svc.visa_rejected(self.coord, cands[0]["id"], "拒签")
        self.assertEqual(result["candidate"]["visa_status"], "rejected")
        self.assertEqual(result["promotions"][0]["application_id"], app_b["id"])
        events = self.svc.list_events(self.coord, "candidate", cands[0]["id"])["events"]
        visa_events = [e for e in events if e["type"] == EventType.VISA_REJECTED]
        self.assertEqual(len(visa_events), 1)
        self.assertEqual(visa_events[0]["responsibility"],
                         Responsibility.VISA_AUTHORITY)
        self.assertEqual(visa_events[0]["data"]["reason"], "拒签")

    def test_visa_rejected_protects_ticketed_arrangement(self) -> None:
        quota = add_quota(self.svc, capacity=1)
        cand = add_candidate(self.svc, name="已出票者")
        app = apply(self.svc, cand["id"], quota["id"])["application"]
        self.svc.review(self.coord, app["id"], "approve")
        self.svc.batch_lock(self.coord, quota["id"])
        self.svc.ticket(self.coord, app["id"])

        result = self.svc.visa_rejected(self.coord, cand["id"], "拒签")
        # 已出票安排不可自动挪动：席位与申请保持原样，仅记录责任
        slot = self.svc.get_slot(self.coord, app["slot_id"])["slot"]
        self.assertEqual(slot["state"], "ticketed")
        self.assertEqual(slot["candidate_id"], cand["id"])
        app_after = self.svc.get_application(self.coord, app["id"])["application"]
        self.assertEqual(app_after["status"], "approved")
        self.assertEqual(result["cascades"][0]["action"], "ticketed_protected")
        self.assertEqual(result["promotions"], [])
        self.assertIn(EventType.TICKETED_PROTECTED,
                      event_types(self.svc, "slot", app["slot_id"]))

    def test_cancel_on_ticketed_is_refused_and_recorded(self) -> None:
        quota = add_quota(self.svc, capacity=1)
        cand = add_candidate(self.svc)
        app = apply(self.svc, cand["id"], quota["id"])["application"]
        self.svc.review(self.coord, app["id"], "approve")
        self.svc.batch_lock(self.coord, quota["id"])
        self.svc.ticket(self.coord, app["id"])
        with self.assertRaises(TicketedImmovableError):
            self.svc.cancel(self.coord, app["id"], "想取消")
        slot = self.svc.get_slot(self.coord, app["slot_id"])["slot"]
        self.assertEqual(slot["state"], "ticketed")
        self.assertIn(EventType.TICKETED_PROTECTED,
                      event_types(self.svc, "slot", app["slot_id"]))

    def test_funding_frozen_cascades_and_blocks_new_applications(self) -> None:
        quota, cands, (app_a, app_b, _) = self._quota_with_waitlist()
        fin = finance(self.svc)
        result = self.svc.funding_frozen(fin, cands[0]["id"], "经费冻结通知")
        self.assertEqual(result["candidate"]["status"], "frozen")
        self.assertEqual(result["promotions"][0]["application_id"], app_b["id"])
        events = self.svc.list_events(fin, "candidate", cands[0]["id"])["events"]
        freeze = [e for e in events if e["type"] == EventType.FUNDING_FROZEN]
        self.assertEqual(freeze[0]["responsibility"], Responsibility.FINANCE)
        # 冻结后不可再申请
        quota2 = add_quota(self.svc)
        with self.assertRaises(ConflictError) as ctx:
            apply(self.svc, cands[0]["id"], quota2["id"])
        self.assertEqual(ctx.exception.code, "candidate_frozen")

    def test_frozen_candidate_skipped_during_promotion(self) -> None:
        quota, cands, (app_a, app_b, app_c) = self._quota_with_waitlist()
        # B 被冻结但申请仍在队列；释放 A 后应跳过 B 补位 C
        self.svc.funding_frozen(finance(self.svc), cands[1]["id"], "冻结")
        # B 的排队申请已被级联取消；再冻结场景下 C 顶上
        result = self.svc.cancel(self.coord, app_a["id"], "取消")
        promoted_ids = [p["application_id"] for p in result["promotions"]]
        self.assertEqual(promoted_ids, [app_c["id"]])


if __name__ == "__main__":
    unittest.main()
