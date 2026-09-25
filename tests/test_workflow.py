"""申请、评审、方案与分批确认的服务层流程测试。"""
import unittest

from service_09252_003.errors import (
    PermissionError,
    StateConflictError,
    ValidationError,
)
from service_09252_003.models import Role, SlotStatus
from tests._fixtures import (
    ADMIN,
    actor,
    add_candidate,
    add_quota,
    apply_and_approve,
    io_of,
    seeded_plan,
    world,
)


class ApplicationReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.db, self.clock = world()
        add_quota(self.svc, "Q1")
        add_candidate(self.svc, "C1")

    def test_applicant_can_only_submit_own_application(self) -> None:
        with self.assertRaises(PermissionError):
            self.svc.apply(actor("C2", Role.APPLICANT),
                           {"candidate_id": "C1", "quota_id": "Q1",
                            "requested_budget": 100}, None)

    def test_duplicate_application_is_idempotent(self) -> None:
        a1 = self.svc.apply(actor("C1", Role.APPLICANT),
                            {"candidate_id": "C1", "quota_id": "Q1",
                             "requested_budget": 100}, "k1")
        a2 = self.svc.apply(actor("C1", Role.APPLICANT),
                            {"candidate_id": "C1", "quota_id": "Q1",
                             "requested_budget": 100}, "k1")
        self.assertEqual(a1.data["id"], a2.data["id"])
        self.assertTrue(a2.replayed)

    def test_idempotency_key_rejects_different_payload(self) -> None:
        self.svc.apply(actor("C1", Role.APPLICANT),
                       {"candidate_id": "C1", "quota_id": "Q1",
                        "requested_budget": 100}, "k1")
        with self.assertRaises(ValidationError):
            self.svc.apply(actor("C1", Role.APPLICANT),
                           {"candidate_id": "C1", "quota_id": "Q1",
                            "requested_budget": 999}, "k1")

    def test_review_lifecycle_and_cross_school_forbidden(self) -> None:
        res = self.svc.apply(actor("C1", Role.APPLICANT),
                             {"candidate_id": "C1", "quota_id": "Q1",
                              "requested_budget": 8000}, None)
        # 伯克利国际处不能评审 MIT 的名额
        with self.assertRaises(PermissionError):
            self.svc.review(io_of("Berkeley"), res.data["id"],
                            {"decision": "approve"})
        self.svc.review(io_of("MIT"), res.data["id"],
                        {"decision": "start"})
        self.assertEqual(self.svc.get_application(io_of("MIT"), res.data["id"])
                         ["status"], "under_review")
        self.svc.review(io_of("MIT"), res.data["id"], {"decision": "approve"})
        got = self.svc.db.get_application(res.data["id"])
        self.assertIs(got.status.value, "approved")

    def test_reject_then_reapply_allowed(self) -> None:
        res = self.svc.apply(actor("C1", Role.APPLICANT),
                             {"candidate_id": "C1", "quota_id": "Q1",
                              "requested_budget": 8000}, None)
        self.svc.review(io_of("MIT"), res.data["id"], {"decision": "reject"})
        # 被拒后同键不可再次提交（终态冲突），但不带键同样冲突
        with self.assertRaises(StateConflictError):
            self.svc.apply(actor("C1", Role.APPLICANT),
                           {"candidate_id": "C1", "quota_id": "Q1",
                            "requested_budget": 8000}, None)


class PlanAndConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.db, self.clock = world()
        seeded_plan(self.svc, [
            {"id": "C1", "budget": 7000},
            {"id": "C2", "budget": 9000},
            {"id": "C3", "budget": 9500},
        ], seats=2)
        self.io = io_of("MIT")

    def _held_slots(self):
        r = self.svc.lock_slots(self.io, "Q1", {"hold_ttl_seconds": 3600},
                                "lock-1")
        self.assertEqual(r.data["errors"], [])
        return r.data["held"]

    def test_plan_ranks_primary_and_waitlist(self) -> None:
        roster = self.svc.roster(self.io, "Q1")
        self.assertEqual(roster["free_seats"], 2)
        plan = self.svc.db.latest_active_plan("Q1")
        primaries = [s for s in plan.slots if s.status == SlotStatus.OPEN]
        waits = [s for s in plan.slots if s.status == SlotStatus.WAITLISTED]
        self.assertEqual([s.candidate_id for s in primaries], ["C1", "C2"])
        self.assertEqual([s.candidate_id for s in waits], ["C3"])
        # 解释信息存在且可读
        self.assertTrue(any("评分" in line for line in waits[0].explanation))

    def test_lock_is_idempotent(self) -> None:
        first = self.svc.lock_slots(self.io, "Q1", {"hold_ttl_seconds": 3600},
                                    "lk")
        second = self.svc.lock_slots(self.io, "Q1", {"hold_ttl_seconds": 3600},
                                     "lk")
        self.assertTrue(second.replayed)
        self.assertEqual(len(first.data["held"]), len(second.data["held"]))

    def test_partial_batch_confirmation_records_per_item_failure(self) -> None:
        held = self._held_slots()
        # 让第二个槽位先过期，批量确认时第一项成功、第二项失败（部分确认）
        self.clock.advance(3601)
        res = self.svc.confirm_batch(
            self.io, {"slot_ids": [s["id"] for s in held],
                      "ticketed": False}, "batch-1")
        self.assertEqual(res.data["confirmed_count"], 0)  # 两者都过期
        self.assertEqual(res.data["failed_count"], 2)
        # 重试同一批次键为回放，不产生新状态
        again = self.svc.confirm_batch(
            self.io, {"slot_ids": [s["id"] for s in held],
                      "ticketed": False}, "batch-1")
        self.assertTrue(again.replayed)

    def test_partial_batch_with_one_bad_state(self) -> None:
        held = self._held_slots()
        ids = [s["id"] for s in held]
        # 先确认第一项（单飞），再批量确认两项
        self.svc.confirm_slot(self.io, ids[0], {"ticketed": False}, "c0")
        res = self.svc.confirm_batch(self.io, {"slot_ids": ids}, "cb")
        self.assertEqual(res.data["confirmed_count"], 2)
        notes = " ".join(c.get("note", "") for c in res.data["confirmed"])
        self.assertIn("幂等", notes)

    def test_ticketed_slot_cannot_be_auto_moved(self) -> None:
        held = self._held_slots()
        self.svc.confirm_slot(self.io, held[0]["id"],
                              {"ticketed": True}, "t1")
        with self.assertRaises(StateConflictError):
            self.svc.cancel_slot(self.io, held[0]["id"],
                                 {"reason": "applicant_cancel"})
        # 签证拒绝只登记责任，不挪动
        rec = self.svc.report_visa_rejection(
            self.io, held[0]["id"], {"note": "214b"})
        self.assertTrue(rec["manual_action_required"])
        self.assertEqual(self.svc.db.get_slot(held[0]["id"]).status,
                         SlotStatus.CONFIRMED)
        events = self.svc.db.list_events(slot_id=held[0]["id"])
        self.assertTrue(any(e["type"] == "ticketed_visa_rejection_recorded"
                            and e["payload"]["responsible_party"] == "consulate"
                            for e in events))

    def test_check_in_requires_confirmation(self) -> None:
        held = self._held_slots()
        with self.assertRaises(StateConflictError):
            self.svc.check_in(self.io, held[0]["id"])

    def test_full_happy_path_to_completion(self) -> None:
        held = self._held_slots()
        self.svc.confirm_slot(self.io, held[0]["id"],
                              {"ticketed": True}, "t1")
        self.svc.check_in(self.io, held[0]["id"])
        done = self.svc.complete(self.io, held[0]["id"], {"note": "访学顺利"})
        self.assertEqual(done["status"], "completed")
        # 结项幂等
        again = self.svc.complete(self.io, held[0]["id"])
        self.assertIn("幂等", again["note"])


if __name__ == "__main__":
    unittest.main()
