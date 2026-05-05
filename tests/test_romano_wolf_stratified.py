"""Tests for stratified Romano-Wolf step-down (per-sub-period bootstrap).

Bug 2 fix per zen review 2026-05-05: when family-of-strategies data spans
calendar-disjoint sub-periods (e.g. v9D retrospective: GFC_recovery,
mid_cycle_eu_debt, late_cycle_china_shock), naive concat of returns
violates stationarity assumption of stationary block bootstrap — blocks
sampled across sub-period seams (Friday 2009 → Monday 2014) carry no
real-world correlation. Stratified bootstrap performs independent
within-stratum block resampling, then concatenates replicate-by-replicate.
"""

from __future__ import annotations

import unittest

import numpy as np

from alphalens.backtest.romano_wolf import (
    romano_wolf_step_down,
    romano_wolf_step_down_stratified,
)


class TestStratifiedRomanoWolf(unittest.TestCase):
    def test_reduces_to_unstratified_when_n_strata_one(self):
        """Single stratum should match unstratified result on same input."""
        rng = np.random.default_rng(7)
        returns = rng.normal(0.001, 0.02, size=(200, 4))

        rng_a = np.random.default_rng(42)
        unstratified = romano_wolf_step_down(
            returns,
            mean_block_length=4.0,
            n_bootstrap=500,
            rng=rng_a,
        )

        rng_b = np.random.default_rng(42)
        stratified = romano_wolf_step_down_stratified(
            [returns],
            mean_block_length=4.0,
            n_bootstrap=500,
            rng=rng_b,
        )

        # Observed t-stats identical (same data)
        np.testing.assert_allclose(
            stratified.observed_tstats, unstratified.observed_tstats, rtol=1e-12
        )
        # n_obs is sum across strata = 200
        self.assertEqual(stratified.n_obs, 200)
        self.assertEqual(stratified.n_strategies, 4)

    def test_three_strata_dimensions(self):
        """Three strata × 5 strategies should yield n_obs = sum and n_strategies = 5."""
        rng = np.random.default_rng(8)
        s1 = rng.normal(0.001, 0.02, size=(180, 5))
        s2 = rng.normal(0.001, 0.02, size=(150, 5))
        s3 = rng.normal(0.001, 0.02, size=(170, 5))
        result = romano_wolf_step_down_stratified(
            [s1, s2, s3],
            mean_block_length=4.0,
            n_bootstrap=200,
            rng=np.random.default_rng(0),
        )
        self.assertEqual(result.n_obs, 500)
        self.assertEqual(result.n_strategies, 5)
        self.assertEqual(len(result.observed_tstats), 5)
        self.assertEqual(len(result.adjusted_critical), 5)

    def test_rejects_strategy_with_clear_signal(self):
        """A high-mean strategy should be rejected; null strategies should not."""
        rng = np.random.default_rng(11)
        n_strata = 3
        n_obs_per = 200
        n_strats = 4
        # Strategy 0 has clear positive mean (αt ~10), others null
        strata = []
        for k in range(n_strata):
            arr = rng.normal(0.0, 0.005, size=(n_obs_per, n_strats))
            arr[:, 0] += 0.005  # large signal in strategy 0
            strata.append(arr)
        result = romano_wolf_step_down_stratified(
            strata,
            mean_block_length=4.0,
            n_bootstrap=1000,
            rng=np.random.default_rng(99),
        )
        self.assertTrue(result.rejected[0], f"strategy 0 should reject; got {result}")
        # Other strategies should not reject (null data)
        for s in range(1, n_strats):
            self.assertFalse(result.rejected[s], f"strategy {s} should not reject")

    def test_raises_on_inconsistent_n_strats(self):
        s1 = np.zeros((100, 4))
        s2 = np.zeros((100, 5))
        with self.assertRaises(ValueError):
            romano_wolf_step_down_stratified(
                [s1, s2],
                mean_block_length=4.0,
                n_bootstrap=100,
            )

    def test_raises_on_empty_list(self):
        with self.assertRaises(ValueError):
            romano_wolf_step_down_stratified([], mean_block_length=4.0, n_bootstrap=100)

    def test_seam_independence_smoke(self):
        """Sanity: bootstrap should NOT couple sub-period boundaries.

        We construct two strata where stratum 1 has high values and stratum 2
        has low values. A naive concat-then-bootstrap would produce blocks
        crossing the seam and yield distorted distributions. Stratified
        bootstrap should sample within-stratum only — observed pooled mean
        equals true pooled mean, but bootstrap distribution preserves
        within-stratum variance structure.
        """
        rng = np.random.default_rng(31)
        s1 = rng.normal(0.01, 0.005, size=(100, 2))
        s2 = rng.normal(-0.01, 0.005, size=(100, 2))
        result = romano_wolf_step_down_stratified(
            [s1, s2],
            mean_block_length=4.0,
            n_bootstrap=500,
            rng=np.random.default_rng(31),
        )
        # Pooled mean should be near zero (s1+s2 cancels), so observed t ~ 0
        # → no rejections
        self.assertFalse(result.rejected[0])
        self.assertFalse(result.rejected[1])
        self.assertEqual(result.n_obs, 200)


if __name__ == "__main__":
    unittest.main()
