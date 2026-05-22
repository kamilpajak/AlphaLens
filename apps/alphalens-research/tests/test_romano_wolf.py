"""TDD tests for :mod:`alphalens_research.backtest.romano_wolf`.

Romano-Wolf (2005) step-down family-wise-error-rate control with
Politis-Romano (1994) stationary block-bootstrap. Power gain over
naive Bonferroni grows with positive correlation between test
statistics; reduces to Bonferroni at zero correlation, to unadjusted
at perfect correlation.
"""

from __future__ import annotations

import unittest

import numpy as np
from alphalens_research.backtest.romano_wolf import (
    RomanoWolfResult,
    romano_wolf_step_down,
    stationary_bootstrap_indices,
)


class StationaryBootstrapIndicesTests(unittest.TestCase):
    def test_shape_matches_n_bootstrap_by_n_obs(self) -> None:
        rng = np.random.default_rng(seed=42)
        idx = stationary_bootstrap_indices(n_obs=100, mean_block_length=5, n_bootstrap=50, rng=rng)
        self.assertEqual(idx.shape, (50, 100))

    def test_indices_within_valid_range(self) -> None:
        rng = np.random.default_rng(seed=42)
        idx = stationary_bootstrap_indices(n_obs=100, mean_block_length=4, n_bootstrap=20, rng=rng)
        self.assertGreaterEqual(int(idx.min()), 0)
        self.assertLess(int(idx.max()), 100)

    def test_seed_reproducibility(self) -> None:
        rng_a = np.random.default_rng(seed=42)
        rng_b = np.random.default_rng(seed=42)
        idx_a = stationary_bootstrap_indices(
            n_obs=80, mean_block_length=4, n_bootstrap=30, rng=rng_a
        )
        idx_b = stationary_bootstrap_indices(
            n_obs=80, mean_block_length=4, n_bootstrap=30, rng=rng_b
        )
        np.testing.assert_array_equal(idx_a, idx_b)

    def test_mean_block_length_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            stationary_bootstrap_indices(
                n_obs=100,
                mean_block_length=0.0,
                n_bootstrap=10,
                rng=np.random.default_rng(),
            )
        with self.assertRaises(ValueError):
            stationary_bootstrap_indices(
                n_obs=100,
                mean_block_length=-1.0,
                n_bootstrap=10,
                rng=np.random.default_rng(),
            )

    def test_block_length_one_acts_like_iid_resample(self) -> None:
        # mean_block_length=1 means probability 1.0 of starting a new block at
        # each step → indices are independent uniform[0, n_obs).
        rng = np.random.default_rng(seed=42)
        idx = stationary_bootstrap_indices(
            n_obs=1000, mean_block_length=1.0, n_bootstrap=1, rng=rng
        )
        # Successive indices should not be highly autocorrelated.
        diffs = np.diff(idx[0])
        # Even random walks with steps in [-n,+n] would have some autocorr;
        # but for IID resample, the lag-1 autocorrelation of indices is ~0.
        autocorr = np.corrcoef(idx[0, :-1], idx[0, 1:])[0, 1]
        self.assertLess(abs(float(autocorr)), 0.15)


class RomanoWolfStepDownTests(unittest.TestCase):
    def test_single_strategy_threshold_close_to_unadjusted(self) -> None:
        # n=1 family: no multiplicity penalty, threshold ≈ 1.96 for α=0.05
        rng = np.random.default_rng(seed=42)
        # Generate IID returns with mean 0 (under H_0)
        returns = rng.standard_normal(size=(500, 1))
        result = romano_wolf_step_down(
            returns, alpha=0.05, mean_block_length=1, n_bootstrap=2000, rng=rng
        )
        # Bootstrap of mean-0 IID gaussian → 95th-percentile of |t| ≈ 1.96
        self.assertAlmostEqual(float(result.adjusted_critical[0]), 1.96, delta=0.20)

    def test_independent_strategies_approach_bonferroni(self) -> None:
        # 5 INDEPENDENT IID gaussian columns under H_0
        # Bonferroni at n=5, α=0.05 → |t|≥2.81; Romano-Wolf should be ≤ 2.81
        # but not far below (independence means little correlation gain).
        rng = np.random.default_rng(seed=42)
        n_obs = 500
        n_strats = 5
        returns = rng.standard_normal(size=(n_obs, n_strats))
        result = romano_wolf_step_down(
            returns, alpha=0.05, mean_block_length=1, n_bootstrap=2000, rng=rng
        )
        # Critical value for the LARGEST observed |t| should be in Bonferroni
        # ballpark — between unadjusted (~1.96) and Bonferroni (~2.81).
        max_critical = float(result.adjusted_critical.max())
        self.assertGreater(max_critical, 1.96)
        self.assertLess(max_critical, 3.5)

    def test_perfectly_correlated_collapses_to_unadjusted(self) -> None:
        # 5 IDENTICAL columns: max-stat distribution = single-stat distribution
        # → adjusted threshold ≈ unadjusted 1.96 for α=0.05
        rng = np.random.default_rng(seed=42)
        base = rng.standard_normal(size=(500, 1))
        returns = np.tile(base, (1, 5))
        result = romano_wolf_step_down(
            returns, alpha=0.05, mean_block_length=1, n_bootstrap=2000, rng=rng
        )
        max_critical = float(result.adjusted_critical.max())
        # With perfect correlation, max-of-S ≈ single |t|
        self.assertLess(max_critical, 2.30)

    def test_strong_signal_rejected_after_step_down(self) -> None:
        # One strategy has +2.0 mean shift → t ≈ +2*sqrt(500) ≈ 44.7 huge
        # Should be rejected at any reasonable α.
        rng = np.random.default_rng(seed=42)
        signal = rng.standard_normal(size=(500, 1)) + 2.0
        noise = rng.standard_normal(size=(500, 4))
        returns = np.hstack([signal, noise])
        result = romano_wolf_step_down(
            returns, alpha=0.05, mean_block_length=1, n_bootstrap=1000, rng=rng
        )
        self.assertTrue(bool(result.rejected[0]))
        # Other 4 should not be rejected (centered around 0)
        self.assertFalse(bool(result.rejected[1:].any()))

    def test_no_signal_no_rejections(self) -> None:
        rng = np.random.default_rng(seed=42)
        # Pure noise; no signal anywhere
        returns = rng.standard_normal(size=(500, 5))
        result = romano_wolf_step_down(
            returns, alpha=0.05, mean_block_length=1, n_bootstrap=1000, rng=rng
        )
        # Most or all should fail to reject under H_0
        self.assertLessEqual(int(result.rejected.sum()), 1)

    def test_returns_result_dataclass(self) -> None:
        rng = np.random.default_rng(seed=42)
        returns = rng.standard_normal(size=(100, 3))
        result = romano_wolf_step_down(
            returns, alpha=0.05, mean_block_length=1, n_bootstrap=200, rng=rng
        )
        self.assertIsInstance(result, RomanoWolfResult)
        self.assertEqual(result.observed_tstats.shape, (3,))
        self.assertEqual(result.adjusted_critical.shape, (3,))
        self.assertEqual(result.rejected.shape, (3,))

    def test_rejects_invalid_inputs(self) -> None:
        rng = np.random.default_rng(seed=42)
        with self.assertRaises(ValueError):
            romano_wolf_step_down(
                np.array([]).reshape(0, 0),
                alpha=0.05,
                mean_block_length=1,
                n_bootstrap=10,
                rng=rng,
            )
        with self.assertRaises(ValueError):
            romano_wolf_step_down(
                np.zeros((100, 3)),
                alpha=0.0,
                mean_block_length=1,
                n_bootstrap=10,
                rng=rng,
            )
        with self.assertRaises(ValueError):
            romano_wolf_step_down(
                np.zeros((100, 3)),
                alpha=1.5,
                mean_block_length=1,
                n_bootstrap=10,
                rng=rng,
            )
        with self.assertRaises(ValueError):
            romano_wolf_step_down(
                np.zeros((100, 3)),
                alpha=0.05,
                mean_block_length=1,
                n_bootstrap=0,
                rng=rng,
            )


if __name__ == "__main__":
    unittest.main()
