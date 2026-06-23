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


class TestDayBlockBootstrapCi(unittest.TestCase):
    def test_empty_returns_none_triple(self):
        self.assertEqual(fh.day_block_bootstrap_ci({}), (None, None, None))

    def test_all_none_values_returns_none_triple(self):
        self.assertEqual(
            fh.day_block_bootstrap_ci({"d1": [None, None], "d2": [None]}),
            (None, None, None),
        )

    def test_single_non_empty_day_is_degenerate(self):
        # n_eff = 1 because resampling 1 day always draws the same day
        lo, mean, hi = fh.day_block_bootstrap_ci(
            {"d1": [0.01, 0.02, 0.03, 0.04, 0.05]}, n_resamples=1000, seed=7
        )
        expected = (0.01 + 0.02 + 0.03 + 0.04 + 0.05) / 5
        import math

        self.assertTrue(math.isclose(lo, expected))
        self.assertTrue(math.isclose(mean, expected))
        self.assertTrue(math.isclose(hi, expected))

    def test_grand_mean_equals_bootstrap_ci_mean(self):
        import math

        d1 = [0.01, 0.02, 0.03]
        d2 = [0.05, 0.06]
        flat = d1 + d2
        _, db_mean, _ = fh.day_block_bootstrap_ci({"d1": d1, "d2": d2}, n_resamples=500, seed=0)
        _, bs_mean, _ = fh.bootstrap_ci(flat, n_resamples=500, seed=0)
        # grand mean must be equal (NOT mean-of-day-means)
        self.assertTrue(math.isclose(db_mean, bs_mean))

    def test_ci_width_contrast_single_day_vs_multi_row(self):
        # 5 rows all in ONE day: day_block is degenerate; bootstrap_ci is not
        vals = [0.01, 0.02, 0.03, 0.04, 0.05]
        db_lo, _, db_hi = fh.day_block_bootstrap_ci({"d1": vals}, n_resamples=5000, seed=0)
        bs_lo, _, bs_hi = fh.bootstrap_ci(vals, n_resamples=5000, seed=0)
        # day_block degenerate
        import math

        self.assertTrue(math.isclose(db_lo, db_hi))
        # bootstrap_ci is non-degenerate
        self.assertLess(bs_lo, bs_hi)

    def test_two_single_row_days_gives_real_ci(self):
        # 2 days, each 1 row — resampling draws either day, so CI is non-degenerate
        lo, _mean, hi = fh.day_block_bootstrap_ci(
            {"d1": [0.10], "d2": [-0.10]}, n_resamples=5000, seed=0
        )
        self.assertLess(lo, hi)

    def test_determinism_same_seed(self):
        data = {"d1": [0.01, 0.02], "d2": [0.03, 0.04, 0.05]}
        r1 = fh.day_block_bootstrap_ci(data, n_resamples=1000, seed=42)
        r2 = fh.day_block_bootstrap_ci(data, n_resamples=1000, seed=42)
        self.assertEqual(r1, r2)

    def test_different_seeds_differ(self):
        # Use 5 days with distinct mean values so the CI endpoints are seed-sensitive.
        data = {
            "d1": [0.10, 0.12],
            "d2": [-0.05, -0.08],
            "d3": [0.20, 0.22],
            "d4": [-0.15, -0.12],
            "d5": [0.30, 0.28],
        }
        r1 = fh.day_block_bootstrap_ci(data, n_resamples=1000, seed=1)
        r2 = fh.day_block_bootstrap_ci(data, n_resamples=1000, seed=2)
        self.assertNotEqual(r1, r2)

    def test_none_dropped_within_day(self):
        import math

        # day with [1.0, None, 3.0] should contribute 1.0 and 3.0 (mean=2.0)
        # single day → degenerate (lo==mean==hi==2.0)
        lo, mean, hi = fh.day_block_bootstrap_ci({"d1": [1.0, None, 3.0]}, n_resamples=500, seed=0)
        self.assertTrue(math.isclose(mean, 2.0))
        self.assertTrue(math.isclose(lo, 2.0))
        self.assertTrue(math.isclose(hi, 2.0))


if __name__ == "__main__":
    unittest.main()
