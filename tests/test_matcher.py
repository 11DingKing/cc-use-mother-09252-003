"""匹配引擎的可解释性测试。"""
import unittest

from service_09252_003 import matcher

from helpers import cand_payload, quota_payload
from service_09252_003.timeutil import parse_window


def _candidate(**over):
    payload = cand_payload(**{k: v for k, v in over.items()
                              if k in ("discipline", "visa_status",
                                       "budget_sources", "windows")})
    return {
        "id": over.get("id", "cand_1"),
        "name": "教师甲",
        "discipline": payload["discipline"],
        "home_org": "派出大学A",
        "windows": [parse_window(w) for w in payload["windows"]],
        "visa_status": payload["visa_status"],
        "budget_sources": payload["budget_sources"],
        "status": "active",
    }


def _quota(**over):
    payload = quota_payload(**{k: v for k, v in over.items()
                               if k in ("discipline", "budget_source", "window")})
    return {
        "id": over.get("id", "quota_1"),
        "host_org": "接收大学X",
        "discipline": payload["discipline"],
        "window": parse_window(payload["window"]),
        "capacity": 1,
        "budget_source": payload["budget_source"],
        "budget_total": 100000.0,
        "status": "open",
    }


class EvaluateTests(unittest.TestCase):
    def test_full_match_scores_100(self) -> None:
        result = matcher.evaluate(_candidate(), _quota())
        self.assertEqual(result["status"], matcher.ELIGIBLE)
        self.assertEqual(result["score"], 100.0)
        self.assertTrue(all(c["ok"] for c in result["checks"]))

    def test_visa_pending_scores_lower_with_reason(self) -> None:
        result = matcher.evaluate(_candidate(visa_status="pending"), _quota())
        self.assertEqual(result["status"], matcher.ELIGIBLE)
        self.assertEqual(result["score"], 95.0)
        self.assertTrue(any("降权" in r for r in result["reasons"]))

    def test_discipline_mismatch_explained(self) -> None:
        result = matcher.evaluate(_candidate(discipline="物理"), _quota())
        self.assertEqual(result["status"], matcher.INELIGIBLE)
        self.assertTrue(any("专业不匹配" in r for r in result["reasons"]))

    def test_budget_source_mismatch_explained(self) -> None:
        result = matcher.evaluate(
            _candidate(budget_sources=["国家基金"]), _quota())
        self.assertEqual(result["status"], matcher.INELIGIBLE)
        self.assertTrue(any("经费来源不符" in r for r in result["reasons"]))

    def test_window_mismatch_explains_in_utc(self) -> None:
        result = matcher.evaluate(
            _candidate(windows=[{"start": "2027-05-01T00:00:00+00:00",
                                 "end": "2027-06-01T00:00:00+00:00"}]),
            _quota())
        self.assertEqual(result["status"], matcher.INELIGIBLE)
        self.assertTrue(any("课程空档不重叠" in r and "UTC" in r
                            for r in result["reasons"]))

    def test_visa_rejected_ineligible(self) -> None:
        result = matcher.evaluate(_candidate(visa_status="rejected"), _quota())
        self.assertEqual(result["status"], matcher.INELIGIBLE)


class ProposalTests(unittest.TestCase):
    def test_ranking_and_recommendation(self) -> None:
        good = _candidate(id="cand_ok")
        pending = _candidate(id="cand_pending", visa_status="pending")
        bad = _candidate(id="cand_bad", discipline="物理")
        quota = _quota()
        proposal = matcher.generate_proposal([pending, bad, good], quota, 1)
        self.assertEqual(proposal["recommended"], ["cand_ok"])
        statuses = {e["candidate_id"]: e["status"] for e in proposal["evaluations"]}
        self.assertEqual(statuses["cand_bad"], matcher.INELIGIBLE)
        self.assertIn("空余 1 席", proposal["summary"])

    def test_cross_dateline_window_counts_as_overlap(self) -> None:
        candidate = _candidate(windows=[{
            "start": "2027-03-05T00:00:00", "end": "2027-03-08T00:00:00",
            "timezone": "Pacific/Kiritimati"}])
        quota = _quota(window={
            "start": "2027-03-04T00:00:00", "end": "2027-03-07T00:00:00",
            "timezone": "Pacific/Midway"})
        result = matcher.evaluate(candidate, quota)
        overlap = next(c for c in result["checks"]
                       if c["code"] == "window_overlap")
        self.assertTrue(overlap["ok"])
        self.assertGreater(overlap["overlap_seconds"], 2 * 24 * 3600)


if __name__ == "__main__":
    unittest.main()
