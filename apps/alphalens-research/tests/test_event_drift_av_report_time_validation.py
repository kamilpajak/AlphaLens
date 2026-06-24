"""Unit tests for the §3.1 AV ``reportTime`` spot-check (launch gate #3).

The gate validates the COERCED ``report_time`` the engine actually consumes
(``av_earnings_ingestion._coerce_report_time`` defaults missing fields to
``post-market``) against curated ground truth for the five §3.1 anchor events.

Two mismatch classes are distinguished because only one is unsafe:
  * BENIGN — observed ``post-market`` where reality is ``pre-market`` (e.g. AV
    lacks the field → conservative default). Entry shifts one day LATER →
    no intraday lookahead, only lost drift capture.
  * DANGEROUS — observed ``pre-market`` where reality is ``post-market``. Entry
    on the announcement day's close would use not-yet-public information.

Acceptance: ≥ 4 of 5 anchors agree AND zero dangerous mismatches.
"""

import unittest
from datetime import date

from alphalens_research.screeners.event_drift.av_earnings_ingestion import (
    AVEarningsAnnouncement,
)
from alphalens_research.screeners.event_drift.av_report_time_validation import (
    REPORT_TIME_ANCHORS,
    evaluate_report_time_anchors,
)


def _ann(ticker: str, d: date, report_time: str) -> AVEarningsAnnouncement:
    return AVEarningsAnnouncement(
        ticker=ticker,
        period_end=d,
        reported_date=d,
        reported_eps=1.0,
        estimated_eps=0.9,
        report_time=report_time,  # type: ignore[arg-type]
    )


def _loaded_matching_all_anchors() -> dict[str, list[AVEarningsAnnouncement]]:
    """Every anchor present with its expected report_time → a perfect 5/5."""
    return {a.ticker: [_ann(a.ticker, a.reported_date, a.expected)] for a in REPORT_TIME_ANCHORS}


class TestReportTimeAnchorsContract(unittest.TestCase):
    def test_anchors_are_the_five_section_3_1_events(self) -> None:
        got = {(a.ticker, a.reported_date) for a in REPORT_TIME_ANCHORS}
        self.assertEqual(
            got,
            {
                ("AAPL", date(2018, 2, 1)),
                ("JPM", date(2018, 1, 12)),
                ("UNH", date(2018, 1, 16)),
                ("CAT", date(2018, 1, 25)),
                ("RSG", date(2018, 2, 8)),
            },
        )

    def test_each_anchor_carries_a_source_note(self) -> None:
        for a in REPORT_TIME_ANCHORS:
            self.assertTrue(a.source.strip(), f"{a.ticker} missing source")


class TestEvaluateReportTimeAnchors(unittest.TestCase):
    def test_all_agree_passes_five_of_five(self) -> None:
        res = evaluate_report_time_anchors(_loaded_matching_all_anchors())
        self.assertTrue(res.passed)
        self.assertEqual(res.n_agree, 5)
        self.assertEqual(res.n_total, 5)
        self.assertEqual(res.n_dangerous, 0)

    def test_benign_postmarket_default_disagreement_still_passes(self) -> None:
        # JPM reality = pre-market; AV defaults missing field to post-market.
        # Disagreement, but BENIGN (entry one day late, no lookahead) → PASS.
        loaded = _loaded_matching_all_anchors()
        loaded["JPM"] = [_ann("JPM", date(2018, 1, 12), "post-market")]
        res = evaluate_report_time_anchors(loaded)
        self.assertEqual(res.n_agree, 4)
        self.assertEqual(res.n_dangerous, 0)
        self.assertTrue(res.passed)
        jpm = next(v for v in res.verdicts if v.ticker == "JPM")
        self.assertFalse(jpm.agrees)
        self.assertFalse(jpm.dangerous)

    def test_dangerous_premarket_when_reality_postmarket_fails(self) -> None:
        # AAPL reality = post-market; AV wrongly says pre-market → lookahead risk.
        loaded = _loaded_matching_all_anchors()
        loaded["AAPL"] = [_ann("AAPL", date(2018, 2, 1), "pre-market")]
        res = evaluate_report_time_anchors(loaded)
        aapl = next(v for v in res.verdicts if v.ticker == "AAPL")
        self.assertTrue(aapl.dangerous)
        self.assertEqual(res.n_dangerous, 1)
        # Even though 4/5 still agree, a dangerous mismatch fails the gate.
        self.assertEqual(res.n_agree, 4)
        self.assertFalse(res.passed)

    def test_missing_event_is_none_observed_not_dangerous(self) -> None:
        loaded = _loaded_matching_all_anchors()
        loaded["CAT"] = []  # no event matching the anchor date
        res = evaluate_report_time_anchors(loaded)
        cat = next(v for v in res.verdicts if v.ticker == "CAT")
        self.assertIsNone(cat.observed)
        self.assertFalse(cat.agrees)
        self.assertFalse(cat.dangerous)
        self.assertEqual(res.n_agree, 4)
        self.assertTrue(res.passed)  # benign coverage gap, 4/5, 0 dangerous

    def test_two_benign_disagreements_drop_below_four_and_fail(self) -> None:
        loaded = _loaded_matching_all_anchors()
        loaded["JPM"] = [_ann("JPM", date(2018, 1, 12), "post-market")]
        loaded["UNH"] = []  # missing
        res = evaluate_report_time_anchors(loaded)
        self.assertEqual(res.n_agree, 3)
        self.assertEqual(res.n_dangerous, 0)
        self.assertFalse(res.passed)  # below the 4/5 floor

    def test_to_dict_is_json_serialisable_shape(self) -> None:
        import json

        res = evaluate_report_time_anchors(_loaded_matching_all_anchors())
        d = res.to_dict()
        json.dumps(d)  # must not raise
        self.assertEqual(d["passed"], True)
        self.assertEqual(d["n_agree"], 5)
        self.assertEqual(len(d["verdicts"]), 5)
        self.assertIn("acceptance_min_agree", d)


if __name__ == "__main__":
    unittest.main()
