"""签证拒绝、经费冻结、有序补位与超时释放测试。"""
import unittest
from datetime import timedelta

from service_09252_003.errors import StateConflictError
from service_09252_003.models import ResponsibleParty, Role, SlotStatus
from tests._fixtures import actor, io_of, seeded_plan, world


def lock_first_n(svc, io, n: int, ttl: int = 3600, key: str = "lk"):
    """只锁定当前方案中排名最靠前的 n 个主选槽位。"""
    plan = svc.db.latest_active_plan("Q1")
    open_slots = sorted(
        (s for s in plan.slots if s.status == SlotStatus.OPEN),
        key=lambda s: s.rank)
    wanted = [s.id for s in open_slots[:n]]
    r = svc.lock_slots(io, "Q1", {"slot_ids": wanted,
                                  "hold_ttl_seconds": ttl}, key)
    assert not r.data["errors"], r.data["errors"]
    return r.data["held"], wanted


class BackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.db, self.clock = world()
        seeded_plan(self.svc, [
            {"id": "C1", "budget": 7000},
            {"id": "C2", "budget": 8000},
            {"id": "C3", "budget": 9000},
            {"id": "C4", "budget": 9500},
        ], seats=2)
        self.io = io_of("MIT")
        self.fin = actor("fin-1", Role.FINANCE, name="财务")

    def test_visa_rejection_promotes_next_waitlist_in_order(self) -> None:
        held, _ = lock_first_n(self.svc, self.io, 2)
        self.svc.confirm_slot(self.io, held[0]["id"], {"ticketed": False}, "c1")
        self.svc.confirm_slot(self.io, held[1]["id"], {"ticketed": False}, "c2")
        res = self.svc.report_visa_rejection(self.io, held[0]["id"],
                                             {"note": "214b 条款"})
        replacement = res["replacement"]
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["candidate_id"], "C3")  # 替补第 1 位
        self.assertEqual(replacement["status"], "held")
        self.assertEqual(replacement["replaced_slot_id"], held[0]["id"])
        canceled = self.db.get_slot(held[0]["id"])
        self.assertEqual(canceled.status, SlotStatus.CANCELLED)
        self.assertEqual(canceled.cancel_reason, "visa_rejected")
        self.assertEqual(canceled.responsible_party,
                         ResponsibleParty.CONSULATE.value)
        # C3 再被拒 -> C4 按顺序递补
        res2 = self.svc.report_visa_rejection(
            self.io, replacement["id"], {"note": "再次被拒"})
        self.assertEqual(res2["replacement"]["candidate_id"], "C4")

    def test_backfill_explicit_is_idempotent_when_full(self) -> None:
        held, _ = lock_first_n(self.svc, self.io, 2)
        for i, h in enumerate(held):
            self.svc.confirm_slot(self.io, h["id"], {"ticketed": False}, f"c{i}")
        out = self.svc.backfill(self.io, "Q1")
        self.assertEqual(out["promoted"], [])
        self.assertEqual(out["active_seats"], 2)

    def test_funding_freeze_revokes_unticketed_and_promotes_on_unfreeze(self) -> None:
        held, _ = lock_first_n(self.svc, self.io, 2)
        # C1 已出票，C2 仅占位
        self.svc.confirm_slot(self.io, held[0]["id"], {"ticketed": True}, "c1")
        frozen = self.svc.set_funding_frozen(
            self.fin, "Q1", {"frozen": True, "note": "预算整改"}, "fr")
        self.assertEqual(frozen.data["ticketed_arrangements_kept"], [held[0]["id"]])
        self.assertEqual(frozen.data["revoked_to_waitlist"], [held[1]["id"]])
        self.assertEqual(self.db.get_slot(held[1]["id"]).status,
                         SlotStatus.WAITLISTED)
        self.assertEqual(self.db.get_slot(held[0]["id"]).status,
                         SlotStatus.CONFIRMED)
        # 冻结期间锁定/确认/补位均被拒绝
        with self.assertRaises(StateConflictError):
            self.svc.lock_slots(self.io, "Q1", {}, "lk2")
        with self.assertRaises(StateConflictError):
            self.svc.backfill(self.io, "Q1")
        # 解冻：空出 1 席，按分数顺序 C2（原占位者，rank2）最先递补
        unfrozen = self.svc.set_funding_frozen(
            self.fin, "Q1", {"frozen": False}, "uf")
        promoted = unfrozen.data["promotions_on_unfreeze"]
        self.assertEqual([p["candidate_id"] for p in promoted], ["C2"])
        self.assertEqual(self.db.get_slot(held[1]["id"]).status,
                         SlotStatus.HELD)

    def test_hold_expiry_releases_and_promotes(self) -> None:
        held, _ = lock_first_n(self.svc, self.io, 1, ttl=3600)
        self.clock.advance(timedelta(seconds=3601))
        out = self.svc.expire_holds()
        self.assertEqual(out["expired"], [held[0]["id"]])
        self.assertEqual([p["candidate_id"] for p in out["promoted"]], ["C3"])
        expired = self.db.get_slot(held[0]["id"])
        self.assertEqual(expired.status, SlotStatus.EXPIRED)
        self.assertEqual(expired.responsible_party,
                         ResponsibleParty.SYSTEM.value)

    def test_reaper_is_safe_when_nothing_due(self) -> None:
        lock_first_n(self.svc, self.io, 1, ttl=3600)
        out = self.svc.expire_holds()
        self.assertEqual(out["expired"], [])
        self.assertEqual(out["promoted"], [])

    def test_waitlist_candidate_losing_eligibility_is_skipped(self) -> None:
        held, _ = lock_first_n(self.svc, self.io, 2)
        self.svc.confirm_slot(self.io, held[0]["id"], {"ticketed": False}, "c1")
        self.svc.confirm_slot(self.io, held[1]["id"], {"ticketed": False}, "c2")
        # C3 签证材料失效 -> 跳过 C3，直接由 C4 递补
        c3 = self.db.get_candidate("C3")
        c3.visa_ready = False
        self.db.upsert_candidate(c3)
        res = self.svc.report_visa_rejection(self.io, held[0]["id"])
        self.assertEqual(res["replacement"]["candidate_id"], "C4")
        c3_slot = next(s for s in self.db.latest_active_plan("Q1").slots
                       if s.candidate_id == "C3")
        self.assertTrue(any("跳过递补" in x for x in c3_slot.explanation))


if __name__ == "__main__":
    unittest.main()
