"""角色权限与数据隔离测试。"""
import unittest

from service_09252_003.errors import (
    AuthError,
    NotFoundError,
    PermissionError,
)

from helpers import (
    actor,
    add_candidate,
    add_quota,
    add_teacher_user,
    apply,
    cand_payload,
    coord,
    make_service,
    quota_payload,
)


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.coord = coord(self.svc)
        self.cand_a = add_candidate(self.svc, name="甲", home_org="派出大学A")
        self.cand_b = add_candidate(self.svc, name="乙", home_org="派出大学B")
        self.quota_x = add_quota(self.svc, host_org="接收大学X", capacity=2)
        self.quota_y = add_quota(self.svc, host_org="接收大学Y")
        self.teacher_a = add_teacher_user(self.svc, self.cand_a["id"], "teacherA")

    def test_invalid_token_rejected(self) -> None:
        with self.assertRaises(AuthError):
            self.svc.authenticate("no-such-token")
        with self.assertRaises(AuthError):
            self.svc.authenticate(None)

    def test_teacher_cannot_administer(self) -> None:
        with self.assertRaises(PermissionError):
            self.svc.create_candidate(self.teacher_a, cand_payload(name="丙"))
        with self.assertRaises(PermissionError):
            self.svc.create_quota(self.teacher_a, quota_payload())
        with self.assertRaises(PermissionError):
            self.svc.batch_lock(self.teacher_a, self.quota_x["id"])

    def test_teacher_applies_only_for_self(self) -> None:
        ok = self.svc.apply(self.teacher_a, {
            "candidate_id": self.cand_a["id"], "quota_id": self.quota_x["id"]})
        self.assertFalse(ok["deduplicated"])
        with self.assertRaises(PermissionError):
            self.svc.apply(self.teacher_a, {
                "candidate_id": self.cand_b["id"], "quota_id": self.quota_x["id"]})

    def test_teacher_sees_only_own_records(self) -> None:
        mine = apply(self.svc, self.cand_a["id"], self.quota_x["id"])["application"]
        other = apply(self.svc, self.cand_b["id"], self.quota_x["id"])["application"]
        listed = self.svc.list_applications(self.teacher_a)["applications"]
        self.assertEqual([a["id"] for a in listed], [mine["id"]])
        with self.assertRaises(NotFoundError):
            self.svc.get_application(self.teacher_a, other["id"])
        with self.assertRaises(NotFoundError):
            self.svc.get_candidate(self.teacher_a, self.cand_b["id"])
        # 事件流同样隔离
        events = self.svc.list_events(self.teacher_a, "application", other["id"])
        self.assertEqual(events["events"], [])
        own = self.svc.list_events(self.teacher_a, "application", mine["id"])
        self.assertTrue(own["events"])

    def test_home_admin_scoped_to_own_org(self) -> None:
        home_a = actor(self.svc, "tok-homeA")
        # 只能登记本院校候选人
        with self.assertRaises(PermissionError):
            self.svc.create_candidate(home_a, cand_payload(
                name="越权", home_org="派出大学B"))
        # 只能看到本院校候选人
        visible = self.svc.list_candidates(home_a)["candidates"]
        self.assertEqual({c["id"] for c in visible}, {self.cand_a["id"]})
        # 只能评审本院校候选人的申请
        app_a = apply(self.svc, self.cand_a["id"], self.quota_x["id"])["application"]
        app_b = apply(self.svc, self.cand_b["id"], self.quota_x["id"])["application"]
        self.svc.review(home_a, app_a["id"], "approve", "本校同意")
        with self.assertRaises(PermissionError):
            self.svc.review(home_a, app_b["id"], "approve")

    def test_host_admin_scoped_to_own_org(self) -> None:
        host_x = actor(self.svc, "tok-hostX")
        visible = self.svc.list_quotas(host_x)["quotas"]
        self.assertEqual({q["id"] for q in visible}, {self.quota_x["id"]})
        with self.assertRaises(NotFoundError):
            self.svc.get_quota(host_x, self.quota_y["id"])
        with self.assertRaises(PermissionError):
            self.svc.close_quota(host_x, self.quota_y["id"])
        # 可以锁定本院校名额
        app = apply(self.svc, self.cand_a["id"], self.quota_x["id"])["application"]
        self.svc.review(self.coord, app["id"], "approve")
        result = self.svc.batch_lock(host_x, self.quota_x["id"])
        self.assertEqual(result["locked_count"], 1)

    def test_finance_read_and_freeze_but_not_operate(self) -> None:
        fin = actor(self.svc, "tok-finance")
        with self.assertRaises(PermissionError):
            self.svc.apply(fin, {"candidate_id": self.cand_a["id"],
                                 "quota_id": self.quota_x["id"]})
        with self.assertRaises(PermissionError):
            self.svc.review(fin, "whatever", "approve")
        # 经费冻结是经费角色的职责
        result = self.svc.funding_frozen(fin, self.cand_b["id"], "冻结")
        self.assertEqual(result["candidate"]["status"], "frozen")
        # 经费角色可读全部事件（审计需要）
        self.assertTrue(self.svc.list_events(fin)["events"])

    def test_coordinator_sees_everything(self) -> None:
        apply(self.svc, self.cand_a["id"], self.quota_x["id"])
        apply(self.svc, self.cand_b["id"], self.quota_x["id"])
        apps = self.svc.list_applications(self.coord)["applications"]
        self.assertEqual(len(apps), 2)
        quotas = self.svc.list_quotas(self.coord)["quotas"]
        self.assertEqual(len(quotas), 2)


if __name__ == "__main__":
    unittest.main()
