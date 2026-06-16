"""Unit tests for the Kaplan-Meier time-to-fill + fill-rate (pure, no I/O)."""

from __future__ import annotations

import unittest

from alphalens_research.diagnostics import fill_survival as fs


class TestKaplanMeier(unittest.TestCase):
    def test_product_limit_with_censoring(self):
        # durations 2,3,3 filled; 7,7 censored. S drops at 2 (->0.8) and 3 (->0.4),
        # flat through the censored 7s (->0.4).
        curve = fs.kaplan_meier([2, 3, 3, 7, 7], [1, 1, 1, 0, 0])
        self.assertEqual([t for t, _ in curve], [2, 3, 7])
        self.assertAlmostEqual(dict(curve)[2], 0.8)
        self.assertAlmostEqual(dict(curve)[3], 0.4)
        self.assertAlmostEqual(dict(curve)[7], 0.4)

    def test_empty(self):
        self.assertEqual(fs.kaplan_meier([], []), [])


class TestFillRateCi(unittest.TestCase):
    def test_rate_and_deterministic_ci(self):
        lo, rate, hi = fs.fill_rate_ci(3, 5, n_resamples=2000, ci=0.90, seed=7)
        self.assertAlmostEqual(rate, 0.6)
        self.assertLessEqual(lo, rate)
        self.assertLessEqual(rate, hi)
        lo2, _, hi2 = fs.fill_rate_ci(3, 5, n_resamples=2000, ci=0.90, seed=7)
        self.assertEqual((lo, hi), (lo2, hi2))

    def test_zero_total(self):
        self.assertEqual(fs.fill_rate_ci(0, 0, n_resamples=10, seed=1), (None, None, None))


if __name__ == "__main__":
    unittest.main()
