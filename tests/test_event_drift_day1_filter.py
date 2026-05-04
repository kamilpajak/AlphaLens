"""Tests for the Bernard-Thomas (1989) day-1 sign-confirmation gate.

Pre-reg ``event_drift_v3_pead_quality_clean`` requires the sign of the
market's day-1 reaction to match the sign of the SUE before a name enters
the long-only PEAD portfolio. Filters "bull-trap" cases where forward
guidance destroyed the historical earnings surprise (positive SUE on
fundamentals but market priced in bad guidance -> negative day-1 return).
"""

from __future__ import annotations

import unittest

import pandas as pd


class TestDay1SignConfirmed(unittest.TestCase):
    """Sign-confirmation rule applied to a (sue, day1_return) tuple."""

    def setUp(self):
        from alphalens.screeners.event_drift.day1_filter import day1_sign_confirmed

        self._fn = day1_sign_confirmed

    def test_positive_sue_positive_day1_kept(self):
        self.assertTrue(self._fn(sue=2.5, day1_return=0.03))

    def test_negative_sue_negative_day1_kept(self):
        self.assertTrue(self._fn(sue=-1.8, day1_return=-0.02))

    def test_positive_sue_negative_day1_filtered_bull_trap(self):
        self.assertFalse(self._fn(sue=2.5, day1_return=-0.04))

    def test_negative_sue_positive_day1_filtered(self):
        self.assertFalse(self._fn(sue=-1.8, day1_return=0.05))

    def test_zero_day1_return_filtered(self):
        # Ambiguous reaction -> filter out conservatively.
        self.assertFalse(self._fn(sue=2.5, day1_return=0.0))

    def test_zero_sue_filtered(self):
        # No earnings surprise at all -> nothing to confirm.
        self.assertFalse(self._fn(sue=0.0, day1_return=0.05))

    def test_nan_day1_return_filtered(self):
        self.assertFalse(self._fn(sue=2.5, day1_return=float("nan")))

    def test_none_day1_return_filtered(self):
        self.assertFalse(self._fn(sue=2.5, day1_return=None))

    def test_inf_day1_return_filtered(self):
        self.assertFalse(self._fn(sue=2.5, day1_return=float("inf")))


class TestDay1Return(unittest.TestCase):
    """Compute close-to-close day-1 return at the market-announcement day."""

    def setUp(self):
        from alphalens.screeners.event_drift.day1_filter import day1_return

        self._fn = day1_return

    def _series(self, prices: dict) -> pd.Series:
        idx = pd.to_datetime(sorted(prices.keys()))
        return pd.Series([prices[d.date()] for d in idx], index=idx)

    def test_basic_positive_return(self):
        prices = self._series(
            {
                pd.Timestamp("2024-08-06").date(): 100.0,
                pd.Timestamp("2024-08-07").date(): 103.0,
            }
        )
        ret = self._fn(prices=prices, market_day=pd.Timestamp("2024-08-07").date())
        self.assertAlmostEqual(ret, 0.03, places=6)

    def test_basic_negative_return(self):
        prices = self._series(
            {
                pd.Timestamp("2024-08-06").date(): 100.0,
                pd.Timestamp("2024-08-07").date(): 96.0,
            }
        )
        ret = self._fn(prices=prices, market_day=pd.Timestamp("2024-08-07").date())
        self.assertAlmostEqual(ret, -0.04, places=6)

    def test_market_day_missing_from_series_returns_none(self):
        prices = self._series({pd.Timestamp("2024-08-06").date(): 100.0})
        ret = self._fn(prices=prices, market_day=pd.Timestamp("2024-08-07").date())
        self.assertIsNone(ret)

    def test_no_prior_close_returns_none(self):
        # Only one entry in the series -> no prior close -> None
        prices = self._series({pd.Timestamp("2024-08-07").date(): 100.0})
        ret = self._fn(prices=prices, market_day=pd.Timestamp("2024-08-07").date())
        self.assertIsNone(ret)

    def test_zero_prior_close_returns_none(self):
        prices = self._series(
            {
                pd.Timestamp("2024-08-06").date(): 0.0,
                pd.Timestamp("2024-08-07").date(): 1.0,
            }
        )
        ret = self._fn(prices=prices, market_day=pd.Timestamp("2024-08-07").date())
        self.assertIsNone(ret)

    def test_uses_last_close_before_market_day(self):
        # Friday -> Mon: market_day=Mon, prev_close should be Fri.
        prices = self._series(
            {
                pd.Timestamp("2024-08-08").date(): 100.0,  # Thu (older)
                pd.Timestamp("2024-08-09").date(): 102.0,  # Fri (prev close)
                pd.Timestamp("2024-08-12").date(): 110.0,  # Mon (market_day)
            }
        )
        ret = self._fn(prices=prices, market_day=pd.Timestamp("2024-08-12").date())
        # day1 return = 110 / 102 - 1
        self.assertAlmostEqual(ret, 110.0 / 102.0 - 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
