"""Tests for per-strategy Romano-Wolf step-down (issue #66).

When phase strategies have stride-shifted disjoint asof calendars within a
sub-period (e.g. v9D + PC retrospective aggregators), neither unstratified
concat nor stratified-by-sub-period bootstrap fits — both require row-aligned
panels. Per-strategy independent block bootstrap aggregates only at the
t-statistic-distribution level, with no calendar alignment required.

Trade-off vs stratified: per-strategy independence destroys cross-strategy
correlation that would tighten the joint-max critical value (closer to
Bonferroni than the stratified variant).
"""

from __future__ import annotations

import unittest

import numpy as np

from alphalens.backtest.romano_wolf import (
    romano_wolf_step_down,
    romano_wolf_step_down_per_strategy,
)


class TestPerStrategyRomanoWolf(unittest.TestCase):
    def test_equivalent_observed_tstats_when_equal_lengths(self):
        """Observed t-stats identical to unstratified on equal-length input
        (computational equivalence). Adjusted criticals will differ because
        per-strategy uses independent bootstraps (destroys cross-strategy
        correlation) — that divergence is by design, not a bug.
        """
        rng = np.random.default_rng(7)
        returns = rng.normal(0.001, 0.02, size=(200, 5))

        rng_a = np.random.default_rng(42)
        unstratified = romano_wolf_step_down(
            returns, mean_block_length=4.0, n_bootstrap=500, rng=rng_a
        )

        rng_b = np.random.default_rng(42)
        per_strategy = romano_wolf_step_down_per_strategy(
            [returns[:, s] for s in range(5)],
            mean_block_length=4.0,
            n_bootstrap=500,
            rng=rng_b,
        )

        np.testing.assert_allclose(
            per_strategy.observed_tstats, unstratified.observed_tstats, rtol=1e-12
        )
        self.assertEqual(per_strategy.n_strategies, 5)
        self.assertEqual(per_strategy.n_obs, 200)

    def test_variable_lengths_run_to_completion(self):
        """Strategies with different lengths produce a valid RomanoWolfResult."""
        rng = np.random.default_rng(123)
        arrays = [rng.normal(0.0, 1.0, size=n) for n in (80, 120, 200, 50, 150)]
        result = romano_wolf_step_down_per_strategy(
            arrays, mean_block_length=4.0, n_bootstrap=300, rng=np.random.default_rng(1)
        )
        self.assertEqual(result.n_strategies, 5)
        self.assertEqual(result.n_obs, 200)  # max of lengths
        self.assertEqual(result.observed_tstats.shape, (5,))
        self.assertEqual(result.adjusted_critical.shape, (5,))
        self.assertEqual(result.rejected.shape, (5,))

    def test_rejects_strategy_with_clear_signal(self):
        """Strategy with strong positive signal is rejected; noise strategies are not."""
        rng = np.random.default_rng(0)
        arrays = [rng.normal(0.0, 1.0, size=200) for _ in range(4)]
        # Strong signal: mean 0.5 stdev 1.0 n=200 → t ≈ 7.07
        arrays.append(rng.normal(0.5, 1.0, size=200))

        result = romano_wolf_step_down_per_strategy(
            arrays,
            mean_block_length=4.0,
            n_bootstrap=1000,
            rng=np.random.default_rng(42),
        )
        self.assertTrue(bool(result.rejected[4]))
        self.assertGreater(
            float(abs(result.observed_tstats[4])), float(result.adjusted_critical[4])
        )

    def test_seed_reproducibility(self):
        """Same seed twice → identical observed + adjusted vectors."""
        rng = np.random.default_rng(5)
        arrays = [rng.normal(0.0, 1.0, size=100) for _ in range(3)]

        a = romano_wolf_step_down_per_strategy(
            arrays, mean_block_length=4.0, n_bootstrap=200, rng=np.random.default_rng(99)
        )
        b = romano_wolf_step_down_per_strategy(
            arrays, mean_block_length=4.0, n_bootstrap=200, rng=np.random.default_rng(99)
        )
        np.testing.assert_array_equal(a.observed_tstats, b.observed_tstats)
        np.testing.assert_array_equal(a.adjusted_critical, b.adjusted_critical)
        np.testing.assert_array_equal(a.rejected, b.rejected)

    def test_disjoint_calendars_do_not_break_bootstrap(self):
        """The motivating case: variable-length disjoint-calendar strategies
        (e.g. stride-shifted phases). Result must be fully populated with no NaN."""
        rng = np.random.default_rng(11)
        # Strategy lengths simulate 5 phases × 5 sub-periods of varying coverage.
        arrays = [rng.normal(0.001, 0.02, size=n) for n in (40, 65, 80, 95, 110)]
        result = romano_wolf_step_down_per_strategy(
            arrays, mean_block_length=4.0, n_bootstrap=500, rng=np.random.default_rng(7)
        )
        self.assertFalse(np.isnan(result.observed_tstats).any())
        # adjusted_critical may contain inf for halted strategies — that's OK;
        # at minimum the largest-t strategy gets a finite critical.
        finite_crits = result.adjusted_critical[~np.isinf(result.adjusted_critical)]
        self.assertGreater(finite_crits.size, 0)

    def test_critical_is_close_to_bonferroni_under_independence(self):
        """With independent per-strategy bootstrap and no real signals, family-max
        critical approaches Bonferroni-style ~ Φ⁻¹(1 - α/(2k)). For α=0.05, k=5
        the two-sided Bonferroni critical is ~2.58. Sanity-check the RW result
        falls in [2.0, 3.0]."""
        rng = np.random.default_rng(33)
        arrays = [rng.normal(0.0, 1.0, size=200) for _ in range(5)]
        result = romano_wolf_step_down_per_strategy(
            arrays,
            mean_block_length=4.0,
            n_bootstrap=2000,
            rng=np.random.default_rng(101),
        )
        finite_crits = result.adjusted_critical[~np.isinf(result.adjusted_critical)]
        max_crit = float(finite_crits.max())
        self.assertGreater(max_crit, 2.0)
        self.assertLess(max_crit, 3.0)


class TestValidationBranches(unittest.TestCase):
    def test_rejects_empty_list(self):
        with self.assertRaises(ValueError):
            romano_wolf_step_down_per_strategy([])

    def test_rejects_zero_length_strategy(self):
        with self.assertRaises(ValueError):
            romano_wolf_step_down_per_strategy([np.array([], dtype=np.float64)])

    def test_rejects_non_1d_array_element(self):
        with self.assertRaises(ValueError):
            romano_wolf_step_down_per_strategy([np.zeros((10, 2))])

    def test_rejects_invalid_alpha(self):
        with self.assertRaises(ValueError):
            romano_wolf_step_down_per_strategy([np.ones(10)], alpha=1.5)

    def test_rejects_invalid_n_bootstrap(self):
        with self.assertRaises(ValueError):
            romano_wolf_step_down_per_strategy([np.ones(10)], n_bootstrap=0)

    def test_rejects_invalid_mean_block_length(self):
        with self.assertRaises(ValueError):
            romano_wolf_step_down_per_strategy([np.ones(10)], mean_block_length=0)

    def test_rng_default_path(self):
        """rng=None should not raise; results vary between runs (unseeded)."""
        arrays = [np.random.default_rng(5).normal(size=50) for _ in range(2)]
        result = romano_wolf_step_down_per_strategy(arrays, n_bootstrap=100)
        self.assertEqual(result.n_strategies, 2)


if __name__ == "__main__":
    unittest.main()
