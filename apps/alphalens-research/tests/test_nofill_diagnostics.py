"""Unit tests for the NO_FILL root-cause reconstruction (pure, no I/O)."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
