"""TDD red-phase tests for PIT replication probe (v7 pre-reg gate).

Tests cover pure functions only — fetches are mocked. The probe itself runs
against a live iVolatility API and is excluded from CI.

Hypothesis being tested by the probe:
  iVolatility's smd-endpoint IVP (1y rolling IV percentile) returned for
  (ticker, asof_t) was computed using only data available up to asof_t,
  i.e. strict backward-looking window. If vendor recomputes IVP using
  current-time history, querying historical asofs leaks future information
  into pre-reg backtests.

Gate: Pearson correlation between vendor IVP and empirically backward-
recomputed IVP across N test asofs must be >= 0.95.
"""

from __future__ import annotations

import itertools
import math
import unittest
from datetime import date

import pandas as pd
from scripts.probe_pit_replication import (
    DEFAULT_CORR_THRESHOLD,
    compute_empirical_iv_percentile,
    evaluate_pit_gate,
    pearson_correlation,
    pit_window_start,
    select_test_asofs,
)


class ComputeEmpiricalIvPercentileTests(unittest.TestCase):
    def test_min_value_returns_low_percentile(self):
        # Standard scipy percentileofscore(kind='mean') for [10,20,30],
        # current=10 returns 16.67 (mean of strict 0% and weak 33.33%).
        result = compute_empirical_iv_percentile([10.0, 20.0, 30.0], 10.0)
        self.assertAlmostEqual(result, 16.666666, places=4)

    def test_max_value_returns_high_percentile(self):
        # Mirror: [10,20,30], current=30 -> mean(66.67, 100) = 83.33.
        result = compute_empirical_iv_percentile([10.0, 20.0, 30.0], 30.0)
        self.assertAlmostEqual(result, 83.333333, places=4)

    def test_median_value_returns_around_50(self):
        result = compute_empirical_iv_percentile([10.0, 20.0, 30.0], 20.0)
        self.assertAlmostEqual(result, 50.0, places=4)

    def test_above_max_returns_100(self):
        result = compute_empirical_iv_percentile([10.0, 20.0, 30.0], 999.0)
        self.assertEqual(result, 100.0)

    def test_below_min_returns_0(self):
        result = compute_empirical_iv_percentile([10.0, 20.0, 30.0], -999.0)
        self.assertEqual(result, 0.0)

    def test_empty_history_returns_nan(self):
        result = compute_empirical_iv_percentile([], 10.0)
        self.assertTrue(math.isnan(result))

    def test_nan_current_returns_nan(self):
        result = compute_empirical_iv_percentile([10.0, 20.0, 30.0], float("nan"))
        self.assertTrue(math.isnan(result))

    def test_drops_nan_values_from_history(self):
        # NaN values in history must be excluded; otherwise scipy returns NaN.
        result = compute_empirical_iv_percentile([10.0, float("nan"), 30.0], 20.0)
        self.assertAlmostEqual(result, 50.0, places=4)


class PitWindowStartTests(unittest.TestCase):
    def test_subtracts_calendar_days(self):
        asof = pd.Timestamp("2023-03-08")
        start = pit_window_start(asof, lookback_days=365)
        self.assertEqual(start, pd.Timestamp("2022-03-08"))

    def test_default_lookback_is_365(self):
        asof = pd.Timestamp("2023-03-08")
        # Default 365 calendar days gives ~252 trading days, matching IVP
        # convention (1y rolling).
        self.assertEqual(pit_window_start(asof), pd.Timestamp("2022-03-08"))


class PearsonCorrelationTests(unittest.TestCase):
    def test_perfect_positive_returns_1(self):
        pairs = [(0.0, 0.0), (25.0, 25.0), (50.0, 50.0), (75.0, 75.0), (100.0, 100.0)]
        self.assertAlmostEqual(pearson_correlation(pairs), 1.0, places=6)

    def test_perfect_negative_returns_neg_1(self):
        pairs = [(0.0, 100.0), (25.0, 75.0), (50.0, 50.0), (75.0, 25.0), (100.0, 0.0)]
        self.assertAlmostEqual(pearson_correlation(pairs), -1.0, places=6)

    def test_zero_variance_returns_nan(self):
        # If all x values identical, correlation undefined.
        pairs = [(50.0, 0.0), (50.0, 25.0), (50.0, 50.0)]
        self.assertTrue(math.isnan(pearson_correlation(pairs)))

    def test_too_few_points_returns_nan(self):
        # Need at least 3 to get meaningful correlation.
        pairs = [(0.0, 0.0), (50.0, 50.0)]
        self.assertTrue(math.isnan(pearson_correlation(pairs)))

    def test_drops_pairs_with_nan(self):
        pairs = [
            (0.0, 0.0),
            (float("nan"), 25.0),
            (50.0, 50.0),
            (75.0, float("nan")),
            (100.0, 100.0),
        ]
        # After dropping NaN pairs: [(0,0),(50,50),(100,100)] -> perfect corr.
        self.assertAlmostEqual(pearson_correlation(pairs), 1.0, places=6)


class EvaluatePitGateTests(unittest.TestCase):
    def test_passes_at_threshold(self):
        result = evaluate_pit_gate(0.95)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["correlation"], 0.95)
        self.assertEqual(result["threshold"], DEFAULT_CORR_THRESHOLD)

    def test_passes_above_threshold(self):
        result = evaluate_pit_gate(0.99)
        self.assertEqual(result["verdict"], "PASS")

    def test_fails_below_threshold(self):
        result = evaluate_pit_gate(0.94)
        self.assertEqual(result["verdict"], "FAIL")

    def test_fails_on_nan_correlation(self):
        # Insufficient data or zero variance => structural FAIL.
        result = evaluate_pit_gate(float("nan"))
        self.assertEqual(result["verdict"], "FAIL")

    def test_negative_correlation_fails(self):
        # Vendor anti-correlated with backward computation => severely revised.
        result = evaluate_pit_gate(-0.8)
        self.assertEqual(result["verdict"], "FAIL")

    def test_custom_threshold_respected(self):
        result = evaluate_pit_gate(0.92, threshold=0.90)
        self.assertEqual(result["verdict"], "PASS")


class SelectTestAsofsTests(unittest.TestCase):
    def test_monthly_stride_returns_12_dates_in_year(self):
        asofs = select_test_asofs(date(2023, 1, 15), date(2023, 12, 31), stride_days=30)
        self.assertGreaterEqual(len(asofs), 11)
        self.assertLessEqual(len(asofs), 13)

    def test_first_asof_is_start(self):
        asofs = select_test_asofs(date(2023, 1, 15), date(2023, 6, 15), stride_days=30)
        self.assertEqual(asofs[0], date(2023, 1, 15))

    def test_dates_strictly_increasing(self):
        asofs = select_test_asofs(date(2023, 1, 15), date(2023, 12, 31), stride_days=30)
        for prev, curr in itertools.pairwise(asofs):
            self.assertLess(prev, curr)

    def test_no_asof_exceeds_end(self):
        asofs = select_test_asofs(date(2023, 1, 15), date(2023, 6, 15), stride_days=30)
        for asof in asofs:
            self.assertLessEqual(asof, date(2023, 6, 15))


if __name__ == "__main__":
    unittest.main()
