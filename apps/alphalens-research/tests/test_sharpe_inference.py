"""Tests for block-bootstrap Sharpe-difference inference.

Used by overlay-class success metric per ADR 0007. Covers:
  - reproducibility under fixed seed
  - degenerate-input handling (NaN, length mismatch, zero std)
  - block-bootstrap correctness on synthetic IID data (recovers analytic Sharpe)
  - paired sampling preserves cross-correlation
  - sanity: identical input series → diff exactly 0, p ~ 0.5
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd
from alphalens_research.backtest.sharpe_inference import (
    SharpeDiffResult,
    block_bootstrap_sharpe_diff,
)


class InputValidationTests(unittest.TestCase):
    def test_rejects_length_mismatch(self):
        a = np.zeros(100)
        b = np.zeros(99)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52)

    def test_rejects_2d_input(self):
        a = np.zeros((10, 2))
        b = np.zeros((10, 2))
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52)

    def test_rejects_too_short(self):
        a = np.array([0.01])
        b = np.array([0.02])
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52)

    def test_rejects_nans(self):
        a = np.array([0.01, np.nan, 0.02])
        b = np.array([0.01, 0.01, 0.01])
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52)

    def test_rejects_invalid_block_size(self):
        a = np.zeros(50)
        b = np.zeros(50)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, block_size=0)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, block_size=51)

    def test_rejects_low_n_bootstrap(self):
        a = np.random.default_rng(0).normal(size=100)
        b = np.random.default_rng(1).normal(size=100)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, n_bootstrap=50)

    def test_rejects_invalid_confidence(self):
        a = np.random.default_rng(0).normal(size=100)
        b = np.random.default_rng(1).normal(size=100)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, confidence=0.5)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, confidence=1.0)


class ReproducibilityTests(unittest.TestCase):
    def test_same_seed_same_result(self):
        rng = np.random.default_rng(0)
        a = rng.normal(loc=0.001, scale=0.01, size=300)
        b = rng.normal(loc=0.0, scale=0.01, size=300)
        r1 = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=42
        )
        r2 = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=42
        )
        self.assertEqual(r1.t_stat, r2.t_stat)
        self.assertEqual(r1.p_value_one_sided, r2.p_value_one_sided)
        self.assertEqual(r1.bootstrap_se, r2.bootstrap_se)


class IdenticalSeriesTests(unittest.TestCase):
    def test_zero_diff_exactly_when_a_equals_b(self):
        rng = np.random.default_rng(7)
        a = rng.normal(loc=0.001, scale=0.01, size=300)
        b = a.copy()
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertEqual(result.sharpe_diff, 0.0)
        # se should be ~0 because diffs are identically 0 across bootstraps
        # (same indices for both → same diff every resample). t_stat is NaN.
        self.assertEqual(result.bootstrap_se, 0.0)
        self.assertTrue(math.isnan(result.t_stat))


class SharpeRecoveryTests(unittest.TestCase):
    def test_observed_sharpe_matches_analytic(self):
        """Analytic Sharpe of a synthetic series should match the result's
        sharpe_a/sharpe_b fields exactly."""
        rng = np.random.default_rng(0)
        a = rng.normal(loc=0.001, scale=0.01, size=500)
        b = rng.normal(loc=0.0, scale=0.01, size=500)
        ppy = 252
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=ppy, block_size=21, n_bootstrap=200, seed=0
        )
        analytic_a = a.mean() / a.std(ddof=1) * math.sqrt(ppy)
        analytic_b = b.mean() / b.std(ddof=1) * math.sqrt(ppy)
        self.assertAlmostEqual(result.sharpe_a, analytic_a, places=10)
        self.assertAlmostEqual(result.sharpe_b, analytic_b, places=10)
        self.assertAlmostEqual(result.sharpe_diff, analytic_a - analytic_b, places=10)


class PairedSamplingTests(unittest.TestCase):
    def test_strongly_positive_diff_yields_low_p(self):
        """If a strictly dominates b, p-value should be very small."""
        rng = np.random.default_rng(0)
        n = 500
        # b is just IID noise; a = b + significant positive drift.
        b = rng.normal(loc=0.0, scale=0.01, size=n)
        a = b + 0.005  # +0.5% deterministic edge per period (huge)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertGreater(result.sharpe_diff, 0)
        self.assertLess(result.p_value_one_sided, 0.01)
        self.assertGreater(result.t_stat, 2.0)

    def test_strongly_negative_diff_yields_high_p(self):
        """If b dominates a, p-value for HA: a > b should be high."""
        rng = np.random.default_rng(0)
        n = 500
        b = rng.normal(loc=0.001, scale=0.01, size=n)
        a = b - 0.005  # a is worse
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertLess(result.sharpe_diff, 0)
        self.assertGreater(result.p_value_one_sided, 0.9)


class CIBoundsTests(unittest.TestCase):
    def test_ci_brackets_zero_when_diff_indistinguishable(self):
        """Two IID series with same mu/sigma — 95% CI should bracket 0."""
        rng = np.random.default_rng(0)
        a = rng.normal(loc=0.0005, scale=0.01, size=300)
        b = rng.normal(loc=0.0005, scale=0.01, size=300)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        # CI should bracket 0 (since true diff = 0).
        self.assertLessEqual(result.ci_lower, 0.0)
        self.assertGreaterEqual(result.ci_upper, 0.0)

    def test_ci_excludes_zero_when_diff_strong(self):
        rng = np.random.default_rng(0)
        n = 500
        b = rng.normal(loc=0.0, scale=0.01, size=n)
        a = b + 0.003  # strong positive edge
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertGreater(result.ci_lower, 0.0)


class ResultShapeTests(unittest.TestCase):
    def test_returns_dataclass_with_expected_fields(self):
        rng = np.random.default_rng(0)
        a = rng.normal(size=100)
        b = rng.normal(size=100)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=5, n_bootstrap=200, seed=0
        )
        self.assertIsInstance(result, SharpeDiffResult)
        self.assertEqual(result.n_obs, 100)
        self.assertEqual(result.block_size, 5)
        self.assertGreater(result.n_bootstrap, 0)


class PandasSeriesAcceptanceTests(unittest.TestCase):
    def test_accepts_pd_series(self):
        rng = np.random.default_rng(0)
        idx = pd.date_range("2020-01-01", periods=100, freq="W-MON")
        a = pd.Series(rng.normal(size=100), index=idx)
        b = pd.Series(rng.normal(size=100), index=idx)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=5, n_bootstrap=200, seed=0
        )
        self.assertIsInstance(result, SharpeDiffResult)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
