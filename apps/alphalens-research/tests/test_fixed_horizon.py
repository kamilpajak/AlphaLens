"""Unit tests for fixed-horizon CAR + bootstrap (pure, no I/O)."""

from __future__ import annotations

import unittest

from alphalens_research.diagnostics import fixed_horizon as fh


class TestCarForEvent(unittest.TestCase):
    def test_market_adjusted_bhar(self):
        # stock +10%, SPY +4% -> CAR +6%.
        car = fh.car_for_event(
            stock_anchor=100.0, stock_horizon=110.0, spy_anchor=100.0, spy_horizon=104.0
        )
        self.assertAlmostEqual(car, 0.06)

    def test_none_on_missing_or_nonpositive(self):
        self.assertIsNone(
            fh.car_for_event(
                stock_anchor=None, stock_horizon=110.0, spy_anchor=100.0, spy_horizon=104.0
            )
        )
        self.assertIsNone(
            fh.car_for_event(
                stock_anchor=0.0, stock_horizon=110.0, spy_anchor=100.0, spy_horizon=104.0
            )
        )


class TestBootstrapCi(unittest.TestCase):
    def test_deterministic_and_brackets_mean(self):
        vals = [0.01, -0.02, 0.05, 0.03, -0.01, 0.04]
        lo, mean, hi = fh.bootstrap_ci(vals, n_resamples=2000, ci=0.90, seed=42)
        self.assertAlmostEqual(mean, sum(vals) / len(vals))
        self.assertLessEqual(lo, mean)
        self.assertLessEqual(mean, hi)
        # reproducible
        lo2, _, hi2 = fh.bootstrap_ci(vals, n_resamples=2000, ci=0.90, seed=42)
        self.assertEqual((lo, hi), (lo2, hi2))

    def test_empty_and_singleton(self):
        self.assertEqual(fh.bootstrap_ci([], n_resamples=100, seed=1), (None, None, None))
        self.assertEqual(fh.bootstrap_ci([0.07], n_resamples=100, seed=1), (0.07, 0.07, 0.07))

    def test_filters_none(self):
        _lo, mean, _hi = fh.bootstrap_ci([0.02, None, 0.04], n_resamples=500, seed=3)
        self.assertAlmostEqual(mean, 0.03)


if __name__ == "__main__":
    unittest.main()
