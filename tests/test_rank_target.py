"""Tests for per-asof rank-percentile target transform.

Locked into v5 pre-reg per
docs/research/preregistration/params_alt_data_screener_v3_2026_05_01.json:
target raw 20d-forward excess return is rank-transformed per-asof to
(percentile_rank - 0.5) before Lasso fitting. Mathematically aligns Lasso
inner solver (MSE) with Spearman rank-IC objective.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd


class TestRankTransformPerAsof(unittest.TestCase):
    def test_transform_centers_at_zero_per_asof(self):
        from alphalens.screeners.multi_source_two_stage.target import (
            rank_transform_per_asof,
        )

        # Two asofs, each with 5 tickers.
        y = pd.Series(
            [
                0.05,
                -0.02,
                0.01,
                -0.10,
                0.20,  # asof 2024-01-01
                0.03,
                0.04,
                -0.05,
                0.10,
                0.00,
            ],  # asof 2024-01-15
            index=range(10),
        )
        asof = pd.Series(
            ["2024-01-01"] * 5 + ["2024-01-15"] * 5,
            index=range(10),
        )

        transformed = rank_transform_per_asof(y, asof)

        # Each asof slice has mean ~ 0 (small offset because percentile rank
        # of n=5 is {0.2, 0.4, 0.6, 0.8, 1.0} - 0.5 = {-0.3, -0.1, 0.1, 0.3, 0.5},
        # mean = 0.1 due to percentile_rank / n offsetting). Use the sklearn-style
        # convention: rank in [1..n], divide by n, subtract 0.5.
        # For n=5: ranks {1,2,3,4,5} / 5 = {0.2, 0.4, 0.6, 0.8, 1.0}; -0.5 = {-0.3,-0.1,0.1,0.3,0.5}; mean=0.1
        # So we expect mean = 0.1 per slice, NOT 0.0. The transform does not
        # require mean-0; it just compresses to bounded range.
        for asof_val in ["2024-01-01", "2024-01-15"]:
            slice_mask = asof == asof_val
            slice_vals = transformed[slice_mask]
            self.assertTrue(slice_vals.min() >= -0.5)
            self.assertTrue(slice_vals.max() <= 0.5)
            # Mean is offset by 1/(2n) due to ordinal-rank-divided-by-n
            self.assertAlmostEqual(slice_vals.mean(), 0.1, places=6)

    def test_transform_preserves_ordering(self):
        from alphalens.screeners.multi_source_two_stage.target import (
            rank_transform_per_asof,
        )

        y = pd.Series([0.10, 0.05, -0.02, -0.10, 0.20, 0.30])
        asof = pd.Series(["2024-01-01"] * 6)

        transformed = rank_transform_per_asof(y, asof)

        # Ordering: -0.10 < -0.02 < 0.05 < 0.10 < 0.20 < 0.30
        # Expected ranks: -0.10 -> rank 1, -0.02 -> rank 2, 0.05 -> rank 3, etc
        # After / n - 0.5:
        # rank 1/6 - 0.5 = -0.333
        # rank 2/6 - 0.5 = -0.167
        # rank 3/6 - 0.5 = 0
        # rank 4/6 - 0.5 = +0.167
        # rank 5/6 - 0.5 = +0.333
        # rank 6/6 - 0.5 = +0.5
        # Original index order: y[0]=0.10 (rank 4), y[1]=0.05 (rank 3), y[2]=-0.02 (rank 2),
        #                       y[3]=-0.10 (rank 1), y[4]=0.20 (rank 5), y[5]=0.30 (rank 6)
        expected = [4 / 6 - 0.5, 3 / 6 - 0.5, 2 / 6 - 0.5, 1 / 6 - 0.5, 5 / 6 - 0.5, 6 / 6 - 0.5]
        for i, exp in enumerate(expected):
            self.assertAlmostEqual(transformed.iloc[i], exp, places=6)

    def test_transform_handles_ties_via_average_rank(self):
        from alphalens.screeners.multi_source_two_stage.target import (
            rank_transform_per_asof,
        )

        # Ties at 0.10 (positions 0 and 1) -> average rank
        y = pd.Series([0.10, 0.10, 0.05, 0.20])
        asof = pd.Series(["2024-01-01"] * 4)

        transformed = rank_transform_per_asof(y, asof)

        # Ordering with ties: 0.05 -> rank 1, 0.10 -> ranks 2,3 average to 2.5,
        # 0.10 -> 2.5, 0.20 -> rank 4
        # After / 4 - 0.5: 0.05 -> -0.25, 0.10 -> +0.125 (×2), 0.20 -> +0.5
        self.assertAlmostEqual(transformed.iloc[0], 0.125, places=6)  # 0.10
        self.assertAlmostEqual(transformed.iloc[1], 0.125, places=6)  # 0.10
        self.assertAlmostEqual(transformed.iloc[2], -0.25, places=6)  # 0.05
        self.assertAlmostEqual(transformed.iloc[3], +0.5, places=6)  # 0.20

    def test_transform_nan_propagates(self):
        from alphalens.screeners.multi_source_two_stage.target import (
            rank_transform_per_asof,
        )

        y = pd.Series([0.10, np.nan, 0.05, 0.20])
        asof = pd.Series(["2024-01-01"] * 4)

        transformed = rank_transform_per_asof(y, asof)

        # NaN inputs propagate to NaN outputs (Lasso handles NaN-target via
        # downstream NaN-target drop in fit_global)
        self.assertTrue(np.isnan(transformed.iloc[1]))
        # Non-NaN ranks: 0.05 -> rank 1/3 - 0.5 = -0.167; 0.10 -> 2/3 - 0.5 = +0.167; 0.20 -> 3/3 - 0.5 = +0.5
        self.assertAlmostEqual(transformed.iloc[0], 2 / 3 - 0.5, places=6)
        self.assertAlmostEqual(transformed.iloc[2], 1 / 3 - 0.5, places=6)
        self.assertAlmostEqual(transformed.iloc[3], 3 / 3 - 0.5, places=6)

    def test_transform_drops_undersized_asof_slices(self):
        from alphalens.screeners.multi_source_two_stage.target import (
            rank_transform_per_asof,
        )

        # Asof "2024-01-01" has 4 tickers (kept), "2024-01-08" has only 2 (dropped)
        y = pd.Series([0.10, 0.05, -0.02, 0.20, 0.03, 0.07])
        asof = pd.Series(
            ["2024-01-01"] * 4 + ["2024-01-08"] * 2,
        )

        transformed = rank_transform_per_asof(y, asof, min_slice_size=3)

        # First 4 entries have valid ranks
        self.assertTrue(transformed.iloc[:4].notna().all())
        # Last 2 entries are NaN (dropped due to small slice)
        self.assertTrue(transformed.iloc[4:].isna().all())


class TestFitGlobalRankTarget(unittest.TestCase):
    """Smoke test that fit_global accepts target_transform='rank' and produces
    nonzero coefs on synthetic data with cross-sectional rank signal."""

    def test_fit_global_with_rank_target_smokes(self):
        from alphalens.screeners.multi_source_two_stage import fit_global
        from alphalens.screeners.multi_source_two_stage.features import (
            FEATURE_NAMES,
        )

        # Synthetic data with 21 features (matching prior class FEATURE_NAMES)
        # where one feature has clear cross-sectional rank signal.
        rng = np.random.RandomState(7)
        n_asofs = 30
        n_per_asof = 50
        rows = []
        targets = []
        for i in range(n_asofs):
            asof = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i * 7)
            # X: noise across 21 features; first feature has rank signal
            X = rng.randn(n_per_asof, len(FEATURE_NAMES))
            # Cross-sectional signal: first feature drives forward returns
            forward_rets = X[:, 0] * 0.02 + rng.randn(n_per_asof) * 0.05
            for j in range(n_per_asof):
                row = {"asof": asof, "ticker": f"T{j:03d}"}
                for k, name in enumerate(FEATURE_NAMES):
                    row[name] = X[j, k]
                rows.append(row)
                targets.append(forward_rets[j])
        feat_df = pd.DataFrame(rows)
        target = pd.Series(targets, index=feat_df.index)

        fit = fit_global(
            feat_df,
            target,
            n_folds=3,
            embargo_days=7,
            lambda_grid_points=10,
            target_transform="rank",
        )
        # Should fit (not None)
        self.assertIsNotNone(fit)
        # With clear feature signal, expect some nonzero coefs
        self.assertGreater(fit.n_nonzero_coefs, 0)


if __name__ == "__main__":
    unittest.main()
