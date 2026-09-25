"""超时释放与服务重启后的恢复。"""
import os
import tempfile
import unittest

from service_09252_003 import (
    ExchangeService,
    Storage,
    UuidIdGenerator,
)
from service_09252_003.models import EventType, Responsibility

from helpers import (
    LOCK_TTL,
    REVIEW_TTL,
    USERS,
    add_candidate,
    add_quota,
    apply,
    coord,
    event_types,
    make_service,
)


class TimeoutReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.coord = coord(self.svc)

    def test_review_hold_expires_and_waitlist_promoted(self) -> None:
        quota = add_quota(self.svc, capacity=1)
        cand_a = add_candidate(self.svc, name="甲")
        cand_b = add_candidate(self.svc, name="乙")
        app_a = apply(self.svc, cand_a["id"], quota["id"])["application"]
        app_b = apply(self.svc, cand_b["id"], quota["id"])["application"]
        self.assertIsNone(app_b["slot_id"])

        self.clock.advance(REVIEW_TTL + 1)
        result = self.svc.release_expired()
        self.assertEqual(result["released"], [app_a["slot_id"]])
        self.assertEqual(result["promotions"][0]["application_id"], app_b["id"])

        slot = self.svc.get_slot(self.coord, app_a["slot_id"])["slot"]
        self.assertEqual(slot["state"], "held")  # 已补位给乙
        self.assertEqual(slot["candidate_id"], cand_b["id"])
        app_a_after = self.svc.get_application(self.coord, app_a["id"])["application"]
        self.assertEqual(app_a_after["status"], "expired")
        events = self.svc.list_events(self.coord, "slot", app_a["slot_id"])["events"]
        expired = [e for e in events if e["type"] == EventType.HOLD_EXPIRED]
        self.assertEqual(expired[0]["responsibility"], Responsibility.TIMEOUT)

    def test_locked_hold_expires_too(self) -> None:
        quota = add_quota(self.svc, capacity=1)
        cand = add_candidate(self.svc)
        app = apply(self.svc, cand["id"], quota["id"])["application"]
        self.svc.review(self.coord, app["id"], "approve")
        self.svc.batch_lock(self.coord, quota["id"])
        slot_before = self.svc.get_slot(self.coord, app["slot_id"])["slot"]
        self.assertEqual(slot_before["state"], "locked")

        self.clock.advance(LOCK_TTL + 1)
        result = self.svc.release_expired()
        self.assertEqual(result["released"], [app["slot_id"]])
        slot_after = self.svc.get_slot(self.coord, app["slot_id"])["slot"]
        self.assertEqual(slot_after["state"], "available")
        app_after = self.svc.get_application(self.coord, app["id"])["application"]
        self.assertEqual(app_after["status"], "expired")

    def test_ticketed_never_expires(self) -> None:
        quota = add_quota(self.svc, capacity=1)
        cand = add_candidate(self.svc)
        app = apply(self.svc, cand["id"], quota["id"])["application"]
        self.svc.review(self.coord, app["id"], "approve")
        self.svc.batch_lock(self.coord, quota["id"])
        self.svc.ticket(self.coord, app["id"])
        self.clock.advance(10 * REVIEW_TTL)
        result = self.svc.release_expired()
        self.assertEqual(result["released"], [])
        slot = self.svc.get_slot(self.coord, app["slot_id"])["slot"]
        self.assertEqual(slot["state"], "ticketed")


class RestartRecoveryTests(unittest.TestCase):
    """服务重启后继续执行超时释放：启动时先清扫一轮。"""

    def test_restart_releases_expired_and_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "exchange.db")
            svc1, clock = make_service(db_path, recover=False)
            c1 = coord(svc1)
            quota = add_quota(svc1, capacity=1)
            cand_a = add_candidate(svc1, name="甲")
            cand_b = add_candidate(svc1, name="乙")
            app_a = apply(svc1, cand_a["id"], quota["id"])["application"]
            apply(svc1, cand_b["id"], quota["id"])
            slot_id = app_a["slot_id"]
            self.assertEqual(
                svc1.get_slot(c1, slot_id)["slot"]["state"], "held")

            # 时钟走过占位 TTL，随后“进程退出”（关闭库连接）
            clock.advance(REVIEW_TTL + 1)
            svc1.storage.close()

            # 重启：同一数据库、同一时钟来源，recover=True 触发启动清扫；
            # 生产环境用 UUID 生成器，重启后不会与持久化的 ID 冲突
            svc2 = ExchangeService(Storage(db_path), clock,
                                   UuidIdGenerator(),
                                   review_ttl_seconds=REVIEW_TTL,
                                   lock_ttl_seconds=LOCK_TTL, recover=True)
            svc2.seed_users(USERS)
            c2 = coord(svc2)
            slot = svc2.get_slot(c2, slot_id)["slot"]
            self.assertEqual(slot["state"], "held",
                             "释放后应立即按替补顺序补位")
            self.assertEqual(slot["candidate_id"], cand_b["id"])
            app_a_after = svc2.get_application(c2, app_a["id"])["application"]
            self.assertEqual(app_a_after["status"], "expired")
            self.assertIn(EventType.HOLD_EXPIRED,
                          event_types(svc2, "slot", slot_id))
            svc2.storage.close()

    def test_restart_without_expiry_keeps_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "exchange.db")
            svc1, clock = make_service(db_path, recover=False)
            quota = add_quota(svc1, capacity=1)
            cand = add_candidate(svc1)
            app = apply(svc1, cand["id"], quota["id"])["application"]
            svc1.storage.close()

            svc2 = ExchangeService(Storage(db_path), clock,
                                   UuidIdGenerator(),
                                   review_ttl_seconds=REVIEW_TTL,
                                   lock_ttl_seconds=LOCK_TTL, recover=True)
            svc2.seed_users(USERS)
            slot = svc2.get_slot(coord(svc2), app["slot_id"])["slot"]
            self.assertEqual(slot["state"], "held", "未超时的占位重启后必须保留")
            self.assertEqual(slot["candidate_id"], cand["id"])
            svc2.storage.close()


if __name__ == "__main__":
    unittest.main()
