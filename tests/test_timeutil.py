"""时间窗与跨时区（含国际日期变更线）解析的单元测试。"""
import unittest

from service_09252_003.timeutil import (
    TimeParseError,
    format_instant,
    local_date,
    overlap_seconds,
    parse_instant,
    parse_window,
    windows_overlap,
)


class ParseInstantTests(unittest.TestCase):
    def test_z_and_offset_equivalent(self) -> None:
        a = parse_instant("2027-03-01T00:00:00Z")
        b = parse_instant("2027-03-01T08:00:00+08:00")
        self.assertEqual(a, b)

    def test_naive_without_offset_rejected(self) -> None:
        with self.assertRaises(TimeParseError):
            parse_instant("2027-03-01T00:00:00")

    def test_int_passthrough_microseconds(self) -> None:
        self.assertEqual(parse_instant(1_800_000_000_000_000), 1_800_000_000_000_000)

    def test_format_roundtrip(self) -> None:
        instant = parse_instant("2027-03-01T00:00:00Z")
        self.assertEqual(format_instant(instant), "2027-03-01T00:00:00.000000Z")


class WindowTests(unittest.TestCase):
    def test_window_with_timezone_field(self) -> None:
        w = parse_window({"start": "2027-03-01T00:00:00",
                          "end": "2027-03-10T00:00:00",
                          "timezone": "Pacific/Kiritimati"})
        # UTC+14：当地 3 月 1 日 0 点 = UTC 2 月 28 日 10 点
        self.assertEqual(format_instant(w["start_us"]), "2027-02-28T10:00:00.000000Z")
        self.assertEqual(w["tz"], "Pacific/Kiritimati")

    def test_end_must_follow_start(self) -> None:
        with self.assertRaises(TimeParseError):
            parse_window({"start": "2027-03-02T00:00:00Z",
                          "end": "2027-03-01T00:00:00Z"})

    def test_unknown_timezone_rejected(self) -> None:
        with self.assertRaises(TimeParseError):
            parse_window({"start": "2027-03-01T00:00:00",
                          "end": "2027-03-02T00:00:00",
                          "timezone": "Mars/Olympus"})

    def test_naive_requires_timezone(self) -> None:
        with self.assertRaises(TimeParseError):
            parse_window({"start": "2027-03-01T00:00:00",
                          "end": "2027-03-02T00:00:00"})


class DateLineTests(unittest.TestCase):
    """跨日界线：同一当地日期在 UTC+14 与 UTC-11 下相差 25 小时。"""

    def _window(self, day: str, tz: str) -> dict:
        return parse_window({"start": f"2027-03-{day}T00:00:00",
                             "end": f"2027-03-{int(day) + 1:02d}T00:00:00",
                             "timezone": tz})

    def test_same_local_day_barely_overlaps_across_dateline(self) -> None:
        early = self._window("05", "Pacific/Kiritimati")   # UTC+14
        late = self._window("04", "Pacific/Midway")        # UTC-11
        # Kiritimati 3/5 = UTC 3/4 10:00 ~ 3/5 10:00
        # Midway    3/4 = UTC 3/4 11:00 ~ 3/5 11:00
        self.assertEqual(overlap_seconds(early, late), 23 * 3600)

    def test_adjacent_local_days_one_hour_overlap(self) -> None:
        early = self._window("05", "Pacific/Kiritimati")
        late = self._window("03", "Pacific/Midway")
        # 只差一个小时：UTC 3/4 10:00 ~ 11:00
        self.assertEqual(overlap_seconds(early, late), 3600)

    def test_two_local_days_apart_no_overlap(self) -> None:
        early = self._window("06", "Pacific/Kiritimati")
        late = self._window("03", "Pacific/Midway")
        self.assertFalse(windows_overlap(early, late))

    def test_local_date_depends_on_timezone(self) -> None:
        instant = parse_instant("2027-03-01T02:00:00Z")
        self.assertEqual(local_date(instant, "Pacific/Kiritimati"), "2027-03-01")
        self.assertEqual(local_date(instant, "Pacific/Midway"), "2027-02-28")


if __name__ == "__main__":
    unittest.main()
