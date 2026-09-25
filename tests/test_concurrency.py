"""并发占位与服务重启后的超时释放测试。"""
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from service_09252_003.models import SlotStatus
from service_09252_003.repository import Database
from service_09252_003.services import VisitExchangeService
from service_09252_003.timeutil import FakeClock
from tests._fixtures import io_of, seeded_plan, world


class ConcurrencyLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.db, self.clock = world()
        seeded_plan(self.svc, [
            {"id": "C1", "budget": 7000},
            {"id": "C2", "budget": 8000},
            {"id": "C3", "budget": 9000},
        ], seats=2)
        self.io = io_of("MIT")
        plan = self.db.latest_active_plan("Q1")
        self.open_ids = [s.id for s in plan.slots
                         if s.status == SlotStatus.OPEN]
        self.assertEqual(len(self.open_ids), 2)

    def test_concurrent_distinct_locks_both_win(self) -> None:
        results: list = []
        errors: list = []

        def worker(slot_id: str, key: str) -> None:
            try:
                r = self.svc.lock_slots(
                    self.io, "Q1", {"slot_ids": [slot_id],
                                    "hold_ttl_seconds": 3600}, key)
                results.append(r.data)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(self.open_ids[0], "k-a"))
        t2 = threading.Thread(target=worker, args=(self.open_ids[1], "k-b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(errors, [])
        held = [h for r in results for h in r["held"]]
        self.assertEqual(sorted(h["id"] for h in held), sorted(self.open_ids))
        roster = self.svc.roster(self.io, "Q1")
        self.assertEqual(roster["active_seats"], 2)

    def test_concurrent_race_for_single_seat_has_one_winner(self) -> None:
        # 多席名额上并发争抢同一槽位只能有一个赢家；再补一个单席场景
        svc, db, _ = world()
        seeded_plan(svc, [
            {"id": "E1", "budget": 7000},
            {"id": "E2", "budget": 8000},
        ], qid="Q3", seats=1)
        io = io_of("MIT")
        only = [s.id for s in db.latest_active_plan("Q3").slots
                if s.status == SlotStatus.OPEN][0]
        outcomes: list = []

        def worker(key: str) -> None:
            r = svc.lock_slots(io, "Q3", {"slot_ids": [only],
                                          "hold_ttl_seconds": 3600}, key)
            outcomes.append(len(r.data["held"]))

        threads = [threading.Thread(target=worker, args=(f"race-{i}",))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(outcomes), 1)  # 只占位成功一次，未超卖
        self.assertEqual(svc.roster(io, "Q3")["active_seats"], 1)


class RestartPersistenceTests(unittest.TestCase):
    def test_timeout_release_runs_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "restart.db")
            clock = FakeClock("2026-09-01T00:00:00Z")

            db = Database(db_path)
            svc = VisitExchangeService(db, clock)
            seeded_plan(svc, [
                {"id": "C1", "budget": 7000},
                {"id": "C2", "budget": 8000},
                {"id": "C3", "budget": 9000},
            ], seats=1)
            io = io_of("MIT")
            r = svc.lock_slots(io, "Q1", {"hold_ttl_seconds": 3600}, "lk")
            held_id = r.data["held"][0]["id"]
            # C1 已确认未出票；重启前先把时钟推过占位期无关——直接模拟崩溃
            db.close()

            # 服务重启：新对象、同文件库、同一时钟；先不做任何操作
            clock.advance(timedelta(seconds=3601))
            db2 = Database(db_path)
            svc2 = VisitExchangeService(db2, clock)
            self.assertEqual(db2.get_slot(held_id).status, SlotStatus.HELD)
            # 重启后执行一轮回收：逾期占位释放，C3... 实际替补为 C2（rank2）
            out = svc2.expire_holds()
            self.assertEqual(out["expired"], [held_id])
            self.assertEqual([p["candidate_id"] for p in out["promoted"]],
                             ["C2"])
            # 幂等记录也持久化，同键回放而非重复占位
            replay = svc2.lock_slots(io, "Q1", {"hold_ttl_seconds": 3600},
                                     "lk")
            self.assertTrue(replay.replayed)
            db2.close()

    def test_completed_arrangements_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "visit2.db")
            clock = FakeClock("2026-09-01T00:00:00Z")
            db = Database(db_path)
            svc = VisitExchangeService(db, clock)
            seeded_plan(svc, [{"id": "C1", "budget": 7000}], seats=1)
            io = io_of("MIT")
            r = svc.lock_slots(io, "Q1", {"hold_ttl_seconds": 7200}, "lk")
            sid = r.data["held"][0]["id"]
            svc.confirm_slot(io, sid, {"ticketed": True}, "cf")
            svc.check_in(io, sid)
            svc.complete(io, sid, {"note": "结项材料齐"})
            db.close()

            db2 = Database(db_path)
            svc2 = VisitExchangeService(db2, clock)
            slot = db2.get_slot(sid)
            self.assertEqual(slot.status, SlotStatus.COMPLETED)
            self.assertTrue(slot.ticketed)
            events = db2.list_events(slot_id=sid)
            self.assertIn("slot_completed", [e["type"] for e in events])
            db2.close()


    def test_pending_idempotency_record_cleared_on_startup(self) -> None:
        from service_09252_003.api import build_server
        from service_09252_003.timeutil import parse_instant

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "pending.db")
            db = Database(db_path)
            db.idem_put_pending("lock:orphan", "lock", "{}",
                                parse_instant("2026-09-01T00:00:00Z"))
            db.close()

            db2 = Database(db_path)
            svc2 = VisitExchangeService(db2, FakeClock("2026-09-02T00:00:00Z"))
            server = build_server(svc2, "127.0.0.1", 0,
                                  reaper_interval=None, startup_reap=False)
            self.assertIsNone(db2.idem_get("lock:orphan"))
            server.server_close()
            db2.close()


if __name__ == "__main__":
    unittest.main()
