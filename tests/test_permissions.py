"""角色与院校隔离的权限测试。"""
import unittest

from service_09252_003.errors import AuthError, PermissionError
from service_09252_003.models import Role, SlotStatus
from tests._fixtures import (
    actor,
    add_candidate,
    add_quota,
    apply_and_approve,
    io_of,
    seeded_plan,
    world,
)


class PermissionIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.db, self.clock = world()
        # 两个院校的名额
        seeded_plan(self.svc, [
            {"id": "C1", "budget": 7000},
            {"id": "C2", "budget": 8000},
        ], qid="Q-MIT", host="MIT", seats=1)
        seeded_plan(self.svc, [
            {"id": "C3", "budget": 7000},
        ], qid="Q-STA", host="Stanford", seats=1)
        self.mit = io_of("MIT")
        self.sta = io_of("Stanford")

    def test_cross_school_io_cannot_lock_review_or_read(self) -> None:
        plan_mit = self.db.latest_active_plan("Q-MIT")
        slot = next(s for s in plan_mit.slots if s.status == SlotStatus.OPEN)
        with self.assertRaises(PermissionError):
            self.svc.lock_slots(self.sta, "Q-MIT",
                                {"slot_ids": [slot.id]}, "x")
        with self.assertRaises(PermissionError):
            self.svc.get_slot(self.sta, slot.id)
        with self.assertRaises(PermissionError):
            self.svc.match_report(self.sta, "Q-MIT")
        with self.assertRaises(PermissionError):
            self.svc.list_events(self.sta, quota_id="Q-MIT")
        # 本校国际处全链路放行
        self.svc.lock_slots(self.mit, "Q-MIT", {"slot_ids": [slot.id]}, "ok")

    def test_applicant_sees_only_own_resources(self) -> None:
        apps_c1 = self.svc.list_applications(actor("C1", Role.APPLICANT))
        self.assertTrue(all(a["candidate_id"] == "C1"
                            for a in apps_c1["applications"]))
        # 申请列表中不能看到 C2
        self.assertNotIn("C2", {a["candidate_id"]
                                for a in apps_c1["applications"]})
        # 不能替别人申请
        with self.assertRaises(PermissionError):
            self.svc.apply(actor("C2", Role.APPLICANT),
                           {"candidate_id": "C1", "quota_id": "Q-MIT",
                            "requested_budget": 1}, None)
        # 方案里只能看到本人槽位
        plan_id = self.db.latest_active_plan("Q-MIT").id
        view = self.svc.get_plan(actor("C1", Role.APPLICANT), plan_id)
        self.assertTrue(view["slots"])
        self.assertTrue(all(s["candidate_id"] == "C1" for s in view["slots"]))
        # 无关候选人连方案都看不到
        with self.assertRaises(PermissionError):
            self.svc.get_plan(actor("C9", Role.APPLICANT), plan_id)
        # 申请人不能看责任流水
        with self.assertRaises(PermissionError):
            self.svc.list_events(actor("C1", Role.APPLICANT),
                                 quota_id="Q-MIT")

    def test_finance_cannot_lock_or_review(self) -> None:
        fin = actor("fin", Role.FINANCE)
        with self.assertRaises(PermissionError):
            self.svc.review(fin, "whatever", {"decision": "approve"})
        with self.assertRaises(PermissionError):
            self.svc.lock_slots(fin, "Q-MIT", {}, "k")
        # 财务可以冻结经费
        out = self.svc.set_funding_frozen(fin, "Q-MIT", {"frozen": True}, "f")
        self.assertTrue(out.data["funding_frozen"])

    def test_applicant_cannot_register(self) -> None:
        with self.assertRaises(PermissionError):
            self.svc.register_quota(actor("C1", Role.APPLICANT), {})

    def test_actor_header_parsing(self) -> None:
        from service_09252_003.api import actor_from_headers

        class H(dict):
            def get(self, k, d=None):
                return super().get(k, d)

        with self.assertRaises(AuthError):
            actor_from_headers(H({}))
        with self.assertRaises(AuthError):
            actor_from_headers(H({"X-Actor-Id": "a", "X-Actor-Role": "wizard"}))
        # system 不得用于外部请求
        with self.assertRaises(PermissionError):
            actor_from_headers(H({"X-Actor-Id": "s",
                                  "X-Actor-Role": "system"}))


if __name__ == "__main__":
    unittest.main()
