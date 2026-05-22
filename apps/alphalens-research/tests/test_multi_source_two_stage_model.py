"""Phase B unit tests for multi_source_two_stage Lasso model.

Locks the contract for `alphalens_research.screeners.multi_source_two_stage.model`:
- fit_two_stage produces one fit per regime present in train data
- _expanding_splits_with_embargo enforces embargo days strictly
- prediction shape matches input rows
- regimes absent from training receive NaN predictions
- λ grid is glmnet-style log-spaced
- recovers a known linear signal under low noise (smoke test on synthetic Q1 regime)
- NaN feature inputs are imputed (not dropped)
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import numpy as np
import pandas as pd
from alphalens_research.screeners.multi_source_two_stage.features import (
    FEATURE_NAMES,
    REGIME_LABELS,
)
from alphalens_research.screeners.multi_source_two_stage.model import (
    EMBARGO_DAYS_DEFAULT,
    GLOBAL_REGIME_LABEL,
    _expanding_splits_with_embargo,
    _lambda_grid,
    fit_global,
    fit_two_stage,
    predict_scores,
    predict_scores_global,
)


def _synthetic_features(
    n_per_regime: int,
    *,
    seed: int = 0,
    signal_feature: str = "ret_60d",
    coef: float = 0.5,
    noise_sd: float = 0.05,
) -> tuple[pd.DataFrame, pd.Series]:
    """Random features in a known scale; target = coef × signal + noise (Q1 only).

    Other regimes get pure noise targets so Lasso should zero out their coefs.
    """
    rng = np.random.default_rng(seed)
    rows = []
    targets = []
    asof_base = date(2020, 1, 1)
    for ri, regime in enumerate(REGIME_LABELS):
        for i in range(n_per_regime):
            asof = asof_base + timedelta(days=ri * n_per_regime + i)
            row = {fn: float(rng.standard_normal()) for fn in FEATURE_NAMES}
            row["asof"] = asof
            row["ticker"] = f"T{ri}_{i:04d}"
            row["regime"] = regime
            rows.append(row)
            if regime == "Q1_calm":
                t = coef * row[signal_feature] + noise_sd * rng.standard_normal()
            else:
                t = noise_sd * rng.standard_normal()
            targets.append(t)
    return pd.DataFrame(rows), pd.Series(targets, dtype=float)


class TestExpandingSplitsEmbargo(unittest.TestCase):
    def test_basic_split_count(self):
        idx = pd.bdate_range("2020-01-01", periods=200)
        s = pd.Series(idx.date, index=range(200))
        splits = _expanding_splits_with_embargo(s, n_folds=3, embargo_days=0)
        self.assertEqual(len(splits), 3)

    def test_embargo_strictly_excludes_recent_train(self):
        idx = pd.bdate_range("2020-01-01", periods=200)
        s = pd.Series(idx.date, index=range(200))
        splits = _expanding_splits_with_embargo(s, n_folds=3, embargo_days=60)
        self.assertEqual(len(splits), 3)
        for train_idx, val_idx in splits:
            train_max = s.loc[train_idx].max()
            val_min = s.loc[val_idx].min()
            gap_days = (val_min - train_max).days
            # Embargo cuts at val_min - 60d strictly. Train rows are < cutoff,
            # so train_max < val_min - 60d ⇒ gap > 60d.
            self.assertGreater(gap_days, 60)

    def test_too_few_rows_returns_empty(self):
        idx = pd.bdate_range("2020-01-01", periods=2)
        s = pd.Series(idx.date, index=range(2))
        self.assertEqual(_expanding_splits_with_embargo(s, n_folds=3, embargo_days=0), [])


class TestLambdaGrid(unittest.TestCase):
    def test_descending_log_spaced(self):
        rng = np.random.default_rng(0)
        X = rng.standard_normal((100, 5))
        y = X @ np.array([0.3, 0.0, 0.0, 0.0, 0.0]) + 0.05 * rng.standard_normal(100)
        grid = _lambda_grid(X, y, n_points=25, min_ratio=1e-3)
        self.assertEqual(len(grid), 25)
        self.assertTrue(np.all(np.diff(grid) <= 0))  # descending
        self.assertAlmostEqual(grid[-1] / grid[0], 1e-3, places=6)


class TestFitTwoStage(unittest.TestCase):
    def test_fits_one_per_regime(self):
        feat, y = _synthetic_features(n_per_regime=120, seed=1)
        # Use small embargo so synthetic 120-row regimes still admit splits.
        fits = fit_two_stage(feat, y, embargo_days=2, n_folds=3, lambda_grid_points=10)
        self.assertEqual(set(fits.keys()), set(REGIME_LABELS))

    def test_q1_regime_recovers_signal_feature(self):
        feat, y = _synthetic_features(
            n_per_regime=400, signal_feature="ret_60d", coef=0.8, noise_sd=0.02, seed=42
        )
        fits = fit_two_stage(feat, y, embargo_days=2, n_folds=3, lambda_grid_points=15)
        q1 = fits["Q1_calm"]
        sig_idx = list(FEATURE_NAMES).index("ret_60d")
        # In Q1: signal coef should be the largest absolute Lasso coefficient.
        self.assertEqual(int(np.argmax(np.abs(q1.model.coef_))), sig_idx)
        # Non-Q1 regime: most coefs should be zero (pure noise).
        q3 = fits["Q3"]
        nonzero = int(np.sum(np.abs(q3.model.coef_) > 1e-6))
        self.assertLess(nonzero, len(FEATURE_NAMES))  # at least some sparsity

    def test_predict_scores_shape_matches_input(self):
        feat, y = _synthetic_features(n_per_regime=120, seed=1)
        fits = fit_two_stage(feat, y, embargo_days=2, n_folds=3, lambda_grid_points=10)
        preds = predict_scores(fits, feat)
        self.assertEqual(len(preds), len(feat))
        self.assertTrue(preds.notna().all())

    def test_unfit_regime_yields_nan(self):
        feat, y = _synthetic_features(n_per_regime=120, seed=2)
        # Drop Q4 from training entirely
        train_mask = feat["regime"] != "Q4_stress"
        fits = fit_two_stage(
            feat.loc[train_mask],
            y.loc[train_mask],
            embargo_days=2,
            n_folds=3,
            lambda_grid_points=10,
        )
        self.assertNotIn("Q4_stress", fits)
        preds = predict_scores(fits, feat)
        self.assertTrue(preds.loc[feat["regime"] == "Q4_stress"].isna().all())
        self.assertTrue(preds.loc[feat["regime"] == "Q1_calm"].notna().all())

    def test_nan_features_imputed_not_dropped(self):
        feat, y = _synthetic_features(n_per_regime=120, seed=3)
        # Inject NaN into one feature for some Q1 rows
        feat.loc[feat["regime"] == "Q1_calm", "vol_realized_20d"] = np.nan
        fits = fit_two_stage(feat, y, embargo_days=2, n_folds=3, lambda_grid_points=10)
        # Q1 should still be fit (NaN imputed via train-median, not row-drop)
        self.assertIn("Q1_calm", fits)
        preds = predict_scores(fits, feat)
        # No row was dropped; predictions exist for all Q1 rows
        q1_preds = preds.loc[feat["regime"] == "Q1_calm"]
        self.assertTrue(q1_preds.notna().all())

    def test_default_embargo_constant_matches_prereg(self):
        # Pre-registration says 60 days. If this changes, must re-register.
        self.assertEqual(EMBARGO_DAYS_DEFAULT, 60)


# ---------------------------------------------------------------------------
# v2 ablation tests — single global Lasso (multi_source_global_lasso_2026_04_30)


class TestFitGlobal(unittest.TestCase):
    def test_returns_single_regime_fit(self):
        feat, y = _synthetic_features(n_per_regime=200, seed=11)
        fit = fit_global(feat, y, embargo_days=2, n_folds=3, lambda_grid_points=10)
        self.assertIsNotNone(fit)
        self.assertEqual(fit.regime, GLOBAL_REGIME_LABEL)

    def test_uses_full_train_pool(self):
        feat, y = _synthetic_features(n_per_regime=150, seed=12)
        fit = fit_global(feat, y, embargo_days=2, n_folds=3, lambda_grid_points=10)
        # All 4 regimes × 150 rows = 600 rows; NaN-target drop yields 600 here.
        self.assertEqual(fit.n_train_obs, len(feat))

    def test_predict_scores_global_covers_all_regimes(self):
        feat, y = _synthetic_features(n_per_regime=150, seed=13)
        fit = fit_global(feat, y, embargo_days=2, n_folds=3, lambda_grid_points=10)
        preds = predict_scores_global(fit, feat)
        # No NaNs anywhere — single model covers every regime.
        self.assertEqual(len(preds), len(feat))
        self.assertTrue(preds.notna().all())

    def test_recovers_global_signal(self):
        # GLOBAL signal (every regime), low noise — fit_global must put the
        # signal feature at top-|coef|. Different from the per-regime test which
        # used a Q1-only signal.
        rng = np.random.default_rng(99)
        rows = []
        targets = []
        sig_feat = "ret_60d"
        coef = 0.8
        noise_sd = 0.02
        asof_base = date(2020, 1, 1)
        for ri, regime in enumerate(REGIME_LABELS):
            for i in range(300):
                row = {fn: float(rng.standard_normal()) for fn in FEATURE_NAMES}
                row["asof"] = asof_base + timedelta(days=ri * 300 + i)
                row["ticker"] = f"T{ri}_{i:04d}"
                row["regime"] = regime
                rows.append(row)
                targets.append(coef * row[sig_feat] + noise_sd * rng.standard_normal())
        feat = pd.DataFrame(rows)
        y = pd.Series(targets, dtype=float)
        fit = fit_global(feat, y, embargo_days=2, n_folds=3, lambda_grid_points=15)
        sig_idx = list(FEATURE_NAMES).index(sig_feat)
        self.assertEqual(int(np.argmax(np.abs(fit.model.coef_))), sig_idx)

    def test_predict_scores_global_handles_none_fit(self):
        # If fit_global returns None (e.g. empty train pool), predictions are NaN.
        feat, _ = _synthetic_features(n_per_regime=10, seed=15)
        preds = predict_scores_global(None, feat)
        self.assertEqual(len(preds), len(feat))
        self.assertTrue(preds.isna().all())

    def test_empty_train_returns_none(self):
        empty_feat = pd.DataFrame(columns=["asof", "ticker", "regime", *FEATURE_NAMES])
        fit = fit_global(empty_feat, pd.Series(dtype=float))
        self.assertIsNone(fit)


if __name__ == "__main__":
    unittest.main()
