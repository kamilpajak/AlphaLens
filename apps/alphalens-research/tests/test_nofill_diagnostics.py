"""Unit tests for the NO_FILL root-cause reconstruction (pure, no I/O)."""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_research.diagnostics import nofill


class TestReconstruct(unittest.TestCase):
    def test_momentum_ran_when_low_never_reaches_e1(self):
        # E1=99, E2=97, E3=95; every session low stays above E1; no gap, no tail touch.
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(100.5, 105.0), (101.0, 106.0), (102.0, 107.0)],
            first_session_open=100.4,  # drift = +0.4% < 3% -> not a gap-up
            tail_min_low=103.0,  # tail also never dips to E1
        )
        self.assertEqual(r.cause, nofill.CAUSE_MOMENTUM_RAN)
        self.assertFalse(r.touched_e1)
        self.assertIsNone(r.days_to_first_touch)
        self.assertTrue(r.window_complete)
        self.assertEqual(r.e1, 99.0)
        self.assertAlmostEqual(r.min_low_in_window, 100.5)
        self.assertAlmostEqual(r.gap_to_e1, (100.5 - 99.0) / 99.0)

    def test_ambiguous_when_daily_low_reaches_e1(self):
        # Session 2 low dips to 98.5 < E1=99 -> daily path says fillable, yet the
        # row is NO_FILL -> AMBIGUOUS (daily-vs-minute discrepancy, escalate).
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(100.0, 105.0), (98.5, 101.0), (99.5, 103.0)],
            first_session_open=100.1,
            tail_min_low=None,
        )
        self.assertEqual(r.cause, nofill.CAUSE_AMBIGUOUS)
        self.assertTrue(r.touched_e1)
        self.assertFalse(r.touched_e3)  # 98.5 not <= 95*(1.0025)
        self.assertEqual(r.days_to_first_touch, 2)
        self.assertAlmostEqual(r.min_low_in_window, 98.5)

    def test_touched_after_ttl_when_only_tail_dips_to_e1(self):
        # Window never reaches E1=99 (min 100.0), but the post-window tail dips to
        # 98.0 -> the dip-buy would have filled just after the 7-session TTL.
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(100.0, 104.0), (100.5, 105.0), (101.0, 106.0)],
            first_session_open=100.2,
            tail_min_low=98.0,
        )
        self.assertEqual(r.cause, nofill.CAUSE_TOUCHED_AFTER_TTL)
        self.assertFalse(r.touched_e1)
        self.assertIsNone(r.days_to_first_touch)

    def test_gap_up_arrival_when_open_jumps_above_anchor(self):
        # Window never reaches E1, no tail touch, but arrival opened +5% vs anchor.
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(104.0, 108.0), (105.0, 109.0), (106.0, 110.0)],
            first_session_open=105.0,  # drift = +5% > 3%
            tail_min_low=104.0,
        )
        self.assertEqual(r.cause, nofill.CAUSE_GAP_UP_ARRIVAL)
        self.assertAlmostEqual(r.arrival_drift, 0.05)

    def test_data_gap_when_a_window_session_is_missing(self):
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(101.0, 105.0), None, (102.0, 106.0)],  # one snapshot absent
            first_session_open=100.5,
            tail_min_low=None,
        )
        self.assertEqual(r.cause, nofill.CAUSE_DATA_GAP)
        self.assertFalse(r.window_complete)

    def test_data_gap_when_no_entry_tier(self):
        r = nofill.reconstruct(
            tiers=[],
            stop=None,
            reference_close=100.0,
            window_lows_highs=[(101.0, 105.0)],
            first_session_open=100.5,
            tail_min_low=None,
        )
        self.assertEqual(r.cause, nofill.CAUSE_DATA_GAP)
        self.assertIsNone(r.e1)


class TestAnalyzeOutcomeRow(unittest.TestCase):
    def _grouped(self, low, high, open_=None):
        bar = {"o": open_ if open_ is not None else high, "h": high, "l": low, "c": high, "v": 1.0}
        return {"AAA": bar}

    def test_extracts_window_path_and_classifies_momentum(self):
        w = [dt.date(2026, 5, 4), dt.date(2026, 5, 5), dt.date(2026, 5, 6)]
        tail = [dt.date(2026, 5, 7)]
        grouped = {
            w[0]: self._grouped(100.5, 105.0, open_=100.4),
            w[1]: self._grouped(101.0, 106.0),
            w[2]: self._grouped(102.0, 107.0),
            tail[0]: self._grouped(103.0, 108.0),
        }
        r = nofill.analyze_outcome_row(
            ticker="aaa",
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_sessions=w,
            tail_sessions=tail,
            grouped_by_session=grouped,
        )
        self.assertEqual(r.cause, nofill.CAUSE_MOMENTUM_RAN)
        self.assertAlmostEqual(r.min_low_in_window, 100.5)

    def test_missing_snapshot_is_data_gap(self):
        w = [dt.date(2026, 5, 4), dt.date(2026, 5, 5)]
        grouped = {w[0]: self._grouped(101.0, 105.0), w[1]: None}  # second snapshot not on disk
        r = nofill.analyze_outcome_row(
            ticker="AAA",
            tiers=[99.0],
            stop=90.0,
            reference_close=100.0,
            window_sessions=w,
            tail_sessions=[],
            grouped_by_session=grouped,
        )
        self.assertEqual(r.cause, nofill.CAUSE_DATA_GAP)

    def test_ticker_absent_from_present_snapshot_is_missing(self):
        w = [dt.date(2026, 5, 4)]
        grouped = {w[0]: {"BBB": {"o": 10, "h": 11, "l": 9, "c": 10, "v": 1}}}  # AAA not traded
        r = nofill.analyze_outcome_row(
            ticker="AAA",
            tiers=[99.0],
            stop=90.0,
            reference_close=100.0,
            window_sessions=w,
            tail_sessions=[],
            grouped_by_session=grouped,
        )
        self.assertEqual(r.cause, nofill.CAUSE_DATA_GAP)


class TestPopulationSummary(unittest.TestCase):
    def test_rate_is_over_plannable_only(self):
        # 3 plannable (1 NO_FILL) + 2 non-plannable (the non-plannable NO_FILL is
        # spurious and must NOT count toward the rate).
        plannable = [True, True, True, False, False]
        classifications = ["NO_FILL", "TP_FULL", "OPEN", "NO_FILL", None]
        s = nofill.population_summary(plannable, classifications)
        self.assertEqual(s["total_raw"], 5)
        self.assertEqual(s["plannable"], 3)
        self.assertEqual(s["non_plannable"], 2)
        self.assertEqual(s["nofill"], 1)  # only the plannable NO_FILL
        self.assertAlmostEqual(s["nofill_rate_pct"], 100.0 / 3)

    def test_zero_plannable_gives_none_rate(self):
        s = nofill.population_summary([False, False], [None, "X"])
        self.assertEqual(s["plannable"], 0)
        self.assertEqual(s["nofill"], 0)
        self.assertIsNone(s["nofill_rate_pct"])

    def test_mismatched_lengths_raise(self):
        with self.assertRaises(ValueError):
            nofill.population_summary([True, False], ["NO_FILL"])


if __name__ == "__main__":
    unittest.main()
