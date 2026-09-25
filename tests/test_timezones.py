"""跨时区与国际日期变更线的时间窗测试。"""
import unittest
from datetime import timedelta

from service_09252_003.timeutil import (
    FakeClock,
    TimeWindow,
    local_day_bounds,
    parse_instant,
)


class TimezoneTests(unittest.TestCase):
    def test_date_line_adjacent_local_days_overlap_in_utc(self) -> None:
        # 奥克兰 10/1 全天（UTC+13）= 09-30 11:00Z ~ 10-01 11:00Z
        auckland = local_day_bounds("2026-10-01", "Pacific/Auckland")
        # 洛杉矶 9/30 全天（UTC-7）= 09-30 07:00Z ~ 10-01 07:00Z
        la = local_day_bounds("2026-09-30", "America/Los_Angeles")
        w1 = TimeWindow(*auckland, "Pacific/Auckland", "Pacific/Auckland")
        w2 = TimeWindow(*la, "America/Los_Angeles", "America/Los_Angeles")
        # 两地“本地日期不同”，但绝对时间有 20 小时重叠
        self.assertAlmostEqual(w1.overlap(w2), 20.0, places=6)
        self.assertIsNotNone(w1.intersect(w2))

    def test_no_overlap_when_half_world_apart_same_local_day(self) -> None:
        # 同为“10 月 1 日白天”，奥克兰与洛杉矶的白天在绝对时间上完全错开
        akl = TimeWindow(
            parse_instant("2026-10-01T09:00", "Pacific/Auckland"),
            parse_instant("2026-10-01T17:00", "Pacific/Auckland"),
            "Pacific/Auckland", "Pacific/Auckland")
        la = TimeWindow(
            parse_instant("2026-10-01T09:00", "America/Los_Angeles"),
            parse_instant("2026-10-01T17:00", "America/Los_Angeles"),
            "America/Los_Angeles", "America/Los_Angeles")
        self.assertEqual(akl.overlap(la), 0.0)
        self.assertIsNone(akl.intersect(la))

    def test_naive_datetime_localized_with_dst(self) -> None:
        # 2026 年美国夏令时：3/8 春拨（当地自然日 23h）、11/1 秋拨（25h），
        # 本地日期换算为 UTC 绝对区间时长度随之变化，区间本身始终有效。
        spring_lo, spring_hi = local_day_bounds("2026-03-08",
                                                "America/Los_Angeles")
        fall_lo, fall_hi = local_day_bounds("2026-11-01",
                                            "America/Los_Angeles")
        self.assertEqual(spring_hi - spring_lo, timedelta(hours=23))
        self.assertEqual(fall_hi - fall_lo, timedelta(hours=25))
        # 普通日期仍是 24 小时
        normal_lo, normal_hi = local_day_bounds("2026-06-15",
                                                "America/Los_Angeles")
        self.assertEqual(normal_hi - normal_lo, timedelta(hours=24))

    def test_explicit_offset_naive_combinations(self) -> None:
        self.assertEqual(
            parse_instant("2026-10-01T00:00:00+13:00"),
            parse_instant("2026-09-30T11:00:00Z"))
        with self.assertRaises(ValueError):
            parse_instant("2026-10-01T00:00:00")  # 朴素时间且无时区

    def test_fake_clock_advance(self) -> None:
        clk = FakeClock("2026-09-01T00:00:00Z")
        clk.advance(timedelta(days=2, hours=3))
        self.assertEqual(clk.now().isoformat(),
                         "2026-09-03T03:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
