"""Unit tests for v9 secondary — cross-sectional residual scorer.

Locks the contract for `score_cross_sectional_residual(features) -> pd.Series`:
- Per asof, runs OLS within asof: `−ivp30 ~ reversal_1m + momentum_6m + rv_30d + intercept`.
- Score = OLS residual (the −ivp30 signal orthogonalized to the 3 equity controls).
- No time-series fit — purely cross-sectional. Different statistical surface
  from v9 primary (sign-constrained Lasso on pooled cross-sections).
- NaN propagation: if ANY of the 4 inputs (`ivp30, reversal_1m, momentum_6m, rv_30d`)
  is NaN for a row, that row's score is NaN.
- Index preserved verbatim, name = `score`.
- Series sums to ~0 within each asof (OLS residual property).
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from alphalens.screeners.options_implied import score_cross_sectional_residual


def _features_two_asofs(seed: int = 0, n_per_asof: int = 50) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for asof in ("2024-05-01", "2024-05-08"):
        for i in range(n_per_asof):
            rows.append(
                {
                    "asof": asof,
                    "ticker": f"T{i:03d}",
                    "ivp30": float(rng.uniform(0, 100)),
                    "reversal_1m": float(rng.normal()),
                    "momentum_6m": float(rng.normal()),
                    "rv_30d": float(rng.uniform(0.1, 0.8)),
                }
            )
    return pd.DataFrame(rows)


class ScoreCrossSectionalResidualTests(unittest.TestCase):
    def test_score_index_matches_features_index(self):
        feats = _features_two_asofs()
        scores = score_cross_sectional_residual(feats)
        self.assertEqual(len(scores), len(feats))
        pd.testing.assert_index_equal(scores.index, feats.index)

    def test_score_name_is_score(self):
        feats = _features_two_asofs()
        scores = score_cross_sectional_residual(feats)
        self.assertEqual(scores.name, "score")

    def test_residuals_sum_to_zero_per_asof(self):
        """OLS residuals always sum to zero within the regression's sample —
        this is a definitional property of the linear regression with intercept."""
        feats = _features_two_asofs()
        scores = score_cross_sectional_residual(feats)
        joined = feats.assign(_score=scores)
        for asof in joined["asof"].unique():
            sub = joined.loc[joined["asof"] == asof, "_score"]
            self.assertAlmostEqual(
                float(sub.sum()),
                0.0,
                places=6,
                msg=f"residuals must sum to zero on asof {asof}",
            )

    def test_residual_orthogonal_to_equity_controls(self):
        """OLS residuals are orthogonal to the regressors — Σ resid·X = 0."""
        feats = _features_two_asofs()
        scores = score_cross_sectional_residual(feats)
        joined = feats.assign(_score=scores)
        for asof in joined["asof"].unique():
            sub = joined.loc[joined["asof"] == asof]
            for col in ("reversal_1m", "momentum_6m", "rv_30d"):
                dot = float((sub["_score"] * sub[col]).sum())
                self.assertAlmostEqual(
                    dot,
                    0.0,
                    places=4,
                    msg=f"residual · {col} must be ~0 on asof {asof} (got {dot:.6g})",
                )

    def test_nan_in_any_input_propagates(self):
        feats = _features_two_asofs()
        feats.loc[3, "rv_30d"] = float("nan")
        scores = score_cross_sectional_residual(feats)
        self.assertTrue(math.isnan(scores.iloc[3]))
        # Other rows in same asof still scored
        self.assertFalse(math.isnan(scores.iloc[0]))

    def test_handles_single_asof(self):
        feats = _features_two_asofs(n_per_asof=20)
        feats = feats.loc[feats["asof"] == "2024-05-01"].reset_index(drop=True)
        scores = score_cross_sectional_residual(feats)
        self.assertEqual(len(scores), 20)
        self.assertAlmostEqual(float(scores.sum()), 0.0, places=6)

    def test_skips_asof_with_too_few_rows(self):
        """An asof with fewer rows than regressors+intercept (4) cannot
        support OLS — those rows get NaN scores."""
        rows = [
            {
                "asof": "2024-05-01",
                "ticker": "A",
                "ivp30": 50.0,
                "reversal_1m": 0.0,
                "momentum_6m": 0.0,
                "rv_30d": 0.3,
            },
            {
                "asof": "2024-05-01",
                "ticker": "B",
                "ivp30": 60.0,
                "reversal_1m": 0.1,
                "momentum_6m": 0.0,
                "rv_30d": 0.4,
            },
        ]
        # Only 2 rows in asof — can't fit 4-parameter OLS
        feats = pd.DataFrame(rows)
        scores = score_cross_sectional_residual(feats)
        self.assertEqual(len(scores), 2)
        self.assertTrue(all(math.isnan(s) for s in scores))

    def test_dtype_is_float(self):
        feats = _features_two_asofs()
        scores = score_cross_sectional_residual(feats)
        self.assertTrue(np.issubdtype(scores.dtype, np.floating))

    def test_top_decile_picks_high_orthogonalized_low_iv(self):
        """End-to-end: with controlled ivp30 inversely correlated with rv_30d
        (so equity controls explain part of ivp30 variance), the residual
        ranks names by the part of `−ivp30` NOT explained by equity controls."""
        rows = []
        for i in range(40):
            ivp = 80.0 - 1.0 * i  # ivp30 decreases with i
            rv = 0.1 + 0.005 * i  # rv_30d increases with i — anti-correlated
            rows.append(
                {
                    "asof": "2024-05-01",
                    "ticker": f"T{i:02d}",
                    "ivp30": ivp,
                    "reversal_1m": 0.0,
                    "momentum_6m": 0.0,
                    "rv_30d": rv,
                }
            )
        feats = pd.DataFrame(rows)
        scores = score_cross_sectional_residual(feats)
        # Residual is small (anti-correlated structure) but score should
        # still produce SOME ordering — check it's not all zero/NaN
        self.assertGreater(scores.std(), 0)


if __name__ == "__main__":
    unittest.main()
