"""并发占位、分批/部分确认与幂等性测试。"""
import threading
import unittest

from service_09252_003.errors import ConflictError, IdempotencyConflict
from service_09252_003.models import EventType

from helpers import (
    add_candidate,
    add_quota,
    apply,
    coord,
    event_types,
    make_service,
)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.coord = coord(self.svc)

    def test_concurrent_apply_single_capacity_exactly_one_hold(self) -> None:
        quota = add_quota(self.svc, capacity=1)
        candidates = [add_candidate(self.svc, name=f"教师{i}") for i in range(8)]
        barrier = threading.Barrier(len(candidates))
        results: list[dict] = []
        errors: list[Exception] = []

        def worker(candidate_id: str) -> None:
            try:
                barrier.wait(timeout=10)
                results.append(apply(self.svc, candidate_id, quota["id"]))
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(c["id"],))
                   for c in candidates]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        held = [r for r in results if not r["waitlisted"]]
        waitlisted = [r for r in results if r["waitlisted"]]
        self.assertEqual(len(held), 1, "单席位名额只能有一个占位")
        self.assertEqual(len(waitlisted), 7)
        slots = self.svc.get_quota(self.coord, quota["id"])["slots"]
        self.assertEqual([s["state"] for s in slots], ["held"])

    def test_concurrent_duplicate_apply_is_idempotent(self) -> None:
        quota = add_quota(self.svc, capacity=2)
        cand = add_candidate(self.svc)
        barrier = threading.Barrier(5)
        results: list[dict] = []

        def worker() -> None:
            barrier.wait(timeout=10)
            results.append(apply(self.svc, cand["id"], quota["id"]))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        app_ids = {r["application"]["id"] for r in results}
        self.assertEqual(len(app_ids), 1, "同人同名额并发申请必须收敛为一条")
        self.assertTrue(any(r["deduplicated"] for r in results))

    def test_concurrent_confirm_same_application_settles_once(self) -> None:
        quota = add_quota(self.svc, capacity=1)
        cand = add_candidate(self.svc)
        app = apply(self.svc, cand["id"], quota["id"])["application"]
        self.svc.review(self.coord, app["id"], "approve")
        barrier = threading.Barrier(4)
        outcomes: list[dict] = []

        def worker() -> None:
            barrier.wait(timeout=10)
            outcomes.append(self.svc.batch_lock(self.coord, quota["id"]))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        locked_events = [e for e in event_types(self.svc, "slot", app["slot_id"])
                         if e == EventType.SLOT_LOCKED]
        self.assertEqual(len(locked_events), 1, "并发锁定只能生效一次")
        self.assertTrue(all(o["locked_count"] == 1 for o in outcomes))
        self.assertTrue(any(o["items"][0].get("already") for o in outcomes))


class PartialBatchTests(unittest.TestCase):
    """分批确认：先确认一部分，剩余部分稍后再确认。"""

    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.coord = coord(self.svc)

    def test_partial_confirmation_across_batches(self) -> None:
        quota = add_quota(self.svc, capacity=2)
        cand_a = add_candidate(self.svc, name="甲")
        cand_b = add_candidate(self.svc, name="乙")
        app_a = apply(self.svc, cand_a["id"], quota["id"])["application"]
        app_b = apply(self.svc, cand_b["id"], quota["id"])["application"]
        # 只批准甲：第一批只锁定甲
        self.svc.review(self.coord, app_a["id"], "approve")
        first = self.svc.batch_lock(self.coord, quota["id"])
        self.assertEqual(first["locked_count"], 1)
        self.assertEqual(first["items"][0]["application_id"], app_a["id"])
        # 乙批准后：第二批锁定乙，甲幂等返回 already
        self.svc.review(self.coord, app_b["id"], "approve")
        second = self.svc.batch_lock(self.coord, quota["id"])
        self.assertEqual(second["locked_count"], 2)
        by_app = {i["application_id"]: i for i in second["items"]}
        self.assertTrue(by_app[app_a["id"]]["already"])
        self.assertFalse(by_app[app_b["id"]]["already"])
        slots = self.svc.get_quota(self.coord, quota["id"])["slots"]
        self.assertEqual([s["state"] for s in slots], ["locked", "locked"])

    def test_expired_hold_not_locked_and_replenished(self) -> None:
        quota = add_quota(self.svc, capacity=1)
        cand_a = add_candidate(self.svc, name="甲")
        cand_b = add_candidate(self.svc, name="乙")
        app_a = apply(self.svc, cand_a["id"], quota["id"])["application"]
        self.svc.review(self.coord, app_a["id"], "approve")
        apply(self.svc, cand_b["id"], quota["id"])  # 乙排队
        # 甲的锁定前占位超时：释放并自动补位给乙
        self.clock.advance(101)
        sweep = self.svc.release_expired()
        self.assertEqual(sweep["released"], [app_a["slot_id"]])
        self.assertEqual(len(sweep["promotions"]), 1)
        # 甲的申请已超时终结，批量锁定不再包含甲
        batch = self.svc.batch_lock(self.coord, quota["id"])
        self.assertEqual(batch["locked_count"], 0)
        app_a_after = self.svc.get_application(self.coord, app_a["id"])["application"]
        self.assertEqual(app_a_after["status"], "expired")


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.coord = coord(self.svc)

    def test_idempotency_key_replays_response(self) -> None:
        cand = add_candidate(self.svc)
        quota = add_quota(self.svc)
        payload = {"candidate_id": cand["id"], "quota_id": quota["id"]}
        first = self.svc.apply(self.coord, payload, idem_key="apply-1")
        second = self.svc.apply(self.coord, payload, idem_key="apply-1")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["application"]["id"], second["application"]["id"])
        # 重放不产生重复事件
        created = [e for e in event_types(self.svc, "application",
                                          first["application"]["id"])
                   if e == EventType.APPLICATION_CREATED]
        self.assertEqual(len(created), 1)

    def test_idempotency_key_conflict_on_different_payload(self) -> None:
        cand = add_candidate(self.svc)
        quota = add_quota(self.svc)
        other = add_quota(self.svc)
        self.svc.apply(self.coord, {"candidate_id": cand["id"],
                                    "quota_id": quota["id"]}, idem_key="k-1")
        with self.assertRaises(IdempotencyConflict):
            self.svc.apply(self.coord, {"candidate_id": cand["id"],
                                        "quota_id": other["id"]}, idem_key="k-1")

    def test_review_and_ticket_idempotent_by_state(self) -> None:
        cand = add_candidate(self.svc)
        quota = add_quota(self.svc)
        app = apply(self.svc, cand["id"], quota["id"])["application"]
        self.svc.review(self.coord, app["id"], "approve")
        again = self.svc.review(self.coord, app["id"], "approve")
        self.assertTrue(again["already"])
        with self.assertRaises(ConflictError):
            self.svc.review(self.coord, app["id"], "reject")  # 已批准不可改判
        self.svc.batch_lock(self.coord, quota["id"])
        first = self.svc.ticket(self.coord, app["id"])
        second = self.svc.ticket(self.coord, app["id"])
        self.assertFalse(first["already"])
        self.assertTrue(second["already"])
        ticketed = [e for e in event_types(self.svc, "slot", app["slot_id"])
                    if e == EventType.SLOT_TICKETED]
        self.assertEqual(len(ticketed), 1)

    def test_mutations_accept_idempotency_keys_end_to_end(self) -> None:
        cand = add_candidate(self.svc)
        quota = add_quota(self.svc)
        app = self.svc.apply(self.coord, {"candidate_id": cand["id"],
                                          "quota_id": quota["id"]},
                             idem_key="m-1")["application"]
        self.svc.review(self.coord, app["id"], "approve", idem_key="m-2")
        lock1 = self.svc.batch_lock(self.coord, quota["id"], idem_key="m-3")
        lock2 = self.svc.batch_lock(self.coord, quota["id"], idem_key="m-3")
        self.assertTrue(lock2["idempotent_replay"])
        self.assertEqual(lock1["locked_count"], lock2["locked_count"])


if __name__ == "__main__":
    unittest.main()
