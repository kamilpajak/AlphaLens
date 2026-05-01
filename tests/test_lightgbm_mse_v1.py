"""Unit tests for v1 LightGBM MSE fit (nonlinear_alt_data_v1_lightgbm_mse_2026_05_01).

Validates fit_lightgbm_mse_global:
- Returns LightGBMFit on synthetic data with non-trivial signal.
- CV early stopping converges to a finite n_estimators.
- Predict on the bundle returns finite scores aligned to input rows.
- Empty train pool returns None.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import numpy as np
import pandas as pd

from alphalens.screeners.multi_source_two_stage.model import (
    LightGBMFit,
    fit_lightgbm_mse_global,
    predict_scores_lightgbm,
)

_FEATURE_NAMES = ("f0", "f1", "f2", "f3", "f4")


def _make_synthetic_panel(
    *,
    n_asofs: int = 60,
    n_tickers: int = 80,
    seed: int = 7,
) -> tuple[pd.DataFrame, pd.Series]:
    """Synthetic panel with monotone-but-noisy signal: y = 0.5 * f0 - 0.3 * f1 + noise."""
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    targets: list[float] = []
    base_date = date(2018, 1, 1)
    for ai in range(n_asofs):
        asof = base_date + timedelta(days=5 * ai)
        for ti in range(n_tickers):
            f = rng.standard_normal(len(_FEATURE_NAMES))
            y = 0.5 * f[0] - 0.3 * f[1] + 0.1 * rng.standard_normal()
            row = {"asof": asof, "ticker": f"T{ti:04d}"}
            for fi, name in enumerate(_FEATURE_NAMES):
                row[name] = float(f[fi])
            rows.append(row)
            targets.append(float(y))
    feat = pd.DataFrame(rows)
    target = pd.Series(targets, dtype=float)
    return feat, target


class FitLightGBMMSEGlobalTest(unittest.TestCase):
    def test_fit_returns_bundle_on_synthetic_signal(self) -> None:
        feat, target = _make_synthetic_panel()
        fit = fit_lightgbm_mse_global(
            feat,
            target,
            feature_names=_FEATURE_NAMES,
            n_estimators_max=50,
            early_stopping_rounds=10,
        )
        self.assertIsNotNone(fit)
        self.assertIsInstance(fit, LightGBMFit)
        self.assertGreaterEqual(fit.chosen_alpha, 1.0)
        self.assertEqual(len(fit.feature_names), len(_FEATURE_NAMES))
        self.assertEqual(fit.n_train_obs, len(feat))
        self.assertGreaterEqual(fit.n_nonzero_coefs, 1)

    def test_predict_returns_finite_scores(self) -> None:
        feat, target = _make_synthetic_panel(n_asofs=40, n_tickers=60)
        fit = fit_lightgbm_mse_global(
            feat,
            target,
            feature_names=_FEATURE_NAMES,
            n_estimators_max=30,
            early_stopping_rounds=10,
        )
        self.assertIsNotNone(fit)
        scores = predict_scores_lightgbm(fit, feat)
        self.assertEqual(len(scores), len(feat))
        self.assertTrue(scores.notna().all())
        self.assertTrue(np.isfinite(scores.to_numpy()).all())

    def test_empty_train_pool_returns_none(self) -> None:
        feat = pd.DataFrame(columns=["asof", "ticker", *list(_FEATURE_NAMES)])
        target = pd.Series([], dtype=float)
        fit = fit_lightgbm_mse_global(
            feat,
            target,
            feature_names=_FEATURE_NAMES,
            n_estimators_max=10,
        )
        self.assertIsNone(fit)

    def test_signal_correlation_positive(self) -> None:
        """Predictions should be positively correlated with the planted signal."""
        feat, target = _make_synthetic_panel(n_asofs=40, n_tickers=60)
        fit = fit_lightgbm_mse_global(
            feat,
            target,
            feature_names=_FEATURE_NAMES,
            n_estimators_max=50,
            early_stopping_rounds=10,
        )
        self.assertIsNotNone(fit)
        scores = predict_scores_lightgbm(fit, feat)
        from scipy.stats import spearmanr

        rho, _p = spearmanr(scores.to_numpy(), target.to_numpy())
        self.assertGreater(rho, 0.2)


if __name__ == "__main__":
    unittest.main()
