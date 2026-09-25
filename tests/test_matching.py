"""匹配引擎：四项硬约束与评分解释。"""
import unittest

from service_09252_003.matching import score_pair
from service_09252_003.models import Application, ApplicationStatus, Candidate, Quota
from service_09252_003.timeutil import TimeWindow, parse_instant
from tests._fixtures import window


def quota_window() -> TimeWindow:
    w = window("2026-10-01T00:00", "2026-10-15T00:00", "America/Los_Angeles")
    return TimeWindow(parse_instant(w["start"], w["tz"]),
                      parse_instant(w["end"], w["end_tz"]),
                      w["tz"], w["end_tz"])


def make_candidate(cid="C1", *, disciplines=("cs",), windows=None,
                   visa_ready=True) -> Candidate:
    windows = windows if windows is not None else [
        TimeWindow(parse_instant(s, tz), parse_instant(e, tz), tz, tz)
        for s, e, tz in [("2026-09-25T00:00", "2026-10-31T00:00", "UTC")]]
    return Candidate(id=cid, name=cid, sending_school="THU",
                     disciplines=list(disciplines),
                     availability_windows=windows, visa_ready=visa_ready)


def make_app(candidate="C1", budget=8000.0) -> Application:
    return Application(id="app1", candidate_id=candidate, quota_id="Q1",
                       status=ApplicationStatus.APPROVED, requested_budget=budget,
                       submitted_at=parse_instant("2026-09-01T00:00Z"),
                       updated_at=parse_instant("2026-09-01T00:00Z"))


class MatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.quota = Quota(id="Q1", host_school="MIT", discipline="cs",
                           window=quota_window(), seats=2, budget_per_seat=10000,
                           funding_source="CSC")

    def test_perfect_match_scores_high_and_explains(self) -> None:
        r = score_pair(make_candidate(), self.quota, make_app())
        self.assertTrue(r.eligible, r.reasons)
        self.assertGreaterEqual(r.score, 80)
        self.assertEqual(set(r.breakdown),
                         {"time_overlap", "discipline", "budget_headroom",
                          "visa_ready"})
        self.assertEqual(r.breakdown["visa_ready"], 15.0)
        self.assertEqual(r.breakdown["discipline"], 25.0)

    def test_visa_not_ready_rejected(self) -> None:
        r = score_pair(make_candidate(visa_ready=False), self.quota, make_app())
        self.assertFalse(r.eligible)
        self.assertTrue(any("签证" in x for x in r.reasons))
        self.assertEqual(r.score, 0.0)

    def test_discipline_mismatch_rejected(self) -> None:
        r = score_pair(make_candidate(disciplines=("music",)), self.quota,
                       make_app())
        self.assertFalse(r.eligible)
        self.assertTrue(any("专业不匹配" in x for x in r.reasons))

    def test_budget_overrun_rejected(self) -> None:
        r = score_pair(make_candidate(), self.quota, make_app(budget=12000))
        self.assertFalse(r.eligible)
        self.assertTrue(any("预算" in x for x in r.reasons))

    def test_no_time_overlap_across_date_line_rejected(self) -> None:
        # 候选人空档按奥克兰本地“白天”给出，与洛杉矶 10 月上半月窗口无交集
        w = window("2026-10-02T09:00", "2026-10-02T17:00", "Pacific/Auckland")
        tw = TimeWindow(parse_instant(w["start"], w["tz"]),
                        parse_instant(w["end"], w["end_tz"]),
                        w["tz"], w["end_tz"])
        # 该区间换算 UTC 为 2026-10-01 20:00Z ~ 2026-10-02 04:00Z
        # 洛杉矶窗口为 10-01 07:00Z ~ 10-15 07:00Z，存在交集 -> 换一个完全错开的
        winter = window("2026-12-01T00:00", "2026-12-10T00:00",
                        "Pacific/Auckland")
        tw2 = TimeWindow(parse_instant(winter["start"], winter["tz"]),
                         parse_instant(winter["end"], winter["end_tz"]),
                         winter["tz"], winter["end_tz"])
        r = score_pair(make_candidate(windows=[tw, tw2]), self.quota, make_app())
        # tw 与窗口有 8 小时重叠（< 最短 24h），仍不合格；tw2 完全不重叠
        self.assertFalse(r.eligible)
        self.assertTrue(any("最短访问" in x or "无交集" in x for x in r.reasons))

    def test_date_line_overlap_accepted(self) -> None:
        # 奥克兰 9/30 当地晚间出发的空档，与洛杉矶窗口在 UTC 上交集 >=24h
        w = window("2026-09-30T20:00", "2026-10-03T20:00", "Pacific/Auckland")
        tw = TimeWindow(parse_instant(w["start"], w["tz"]),
                        parse_instant(w["end"], w["end_tz"]),
                        w["tz"], w["end_tz"])
        r = score_pair(make_candidate(windows=[tw]), self.quota, make_app())
        self.assertTrue(r.eligible, r.reasons)
        self.assertGreater(r.breakdown["time_overlap"], 0)

    def test_family_discipline_partial_score(self) -> None:
        r = score_pair(make_candidate(disciplines=("software",)), self.quota,
                       make_app())
        self.assertTrue(r.eligible)
        self.assertEqual(r.breakdown["discipline"], 15.0)

    def test_cheaper_budget_scores_higher(self) -> None:
        cheap = score_pair(make_candidate("A"), self.quota, make_app(budget=1000))
        pricey = score_pair(make_candidate("B"), self.quota, make_app(budget=9900))
        self.assertGreater(
            cheap.breakdown["budget_headroom"], pricey.breakdown["budget_headroom"])


if __name__ == "__main__":
    unittest.main()
