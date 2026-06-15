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


if __name__ == "__main__":
    unittest.main()
