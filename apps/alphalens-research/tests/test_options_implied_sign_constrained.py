"""Unit tests for v9 sign-constrained Lasso.

Locks the contract for `fit_sign_constrained_lasso(features, target, ...)`:
- Coefficients on options features (`ivp30, ivx30, ivx180_minus_ivx30,
  ivx30_over_hv20`) MUST be ≤ 0 in the returned GlobalLassoFit (Xing 2010
  prior enforced mechanically; v7's sign-flip surface eliminated).
- Equity-control coefficients (`reversal_1m, momentum_6m, rv_30d`) are
  unconstrained — Lasso fits the data freely.
- Returned fit object is the same `GlobalLassoFit` shape as v7's
  `fit_global_lasso`, so `predict_scores` works unchanged on ORIGINAL
  (un-negated) feature frames.
- Validation matches v7: raises on too few rows or missing features.

Mathematical contract: fit internally negates the 4 options columns of the
standardized feature matrix and runs sklearn's `LassoCV(positive=True)`,
then flips the options-column coefficients back to the original-feature
interpretation. Net effect is L1 regression with sign mask
`coef_options ≤ 0`.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd
from alphalens_research.screeners.options_implied import (
    FEATURE_NAMES,
    OPTIONS_FEATURES,
    fit_sign_constrained_lasso,
    predict_scores,
)


def _train_features(
    n_rows: int = 1000,
    seed: int = 0,
    *,
    options_signal_sign: float = -1.0,
    equity_signal_sign: float = +1.0,
) -> tuple[pd.DataFrame, pd.Series]:
    """Synthetic training panel — 7 random features + linear-combination target.

    `options_signal_sign` controls the TRUE sign of the data-generating
    process on the options features. Default −1 matches Xing prior;
    overriding to +1 lets us test that the sign constraint mechanically
    overrides the data-implied positive sign.
    """
    rng = np.random.default_rng(seed)
    data = {f: rng.normal(size=n_rows) for f in FEATURE_NAMES}
    df = pd.DataFrame(data)
    df.insert(0, "ticker", [f"T{i % 50}" for i in range(n_rows)])
    df.insert(0, "asof", "2023-06-01")
    target = (
        options_signal_sign * 0.3 * df["ivx30"]
        + equity_signal_sign * 0.15 * df["reversal_1m"]
        + 0.05 * rng.normal(size=n_rows)
    )
    return df, target


class TestFitSignConstrainedLasso(unittest.TestCase):
    def test_options_coefs_are_non_positive_when_data_agrees(self):
        """Data agrees with Xing (negative ivx30 signal). Sign-constrained
        Lasso recovers the negative direction; coef ≤ 0 trivially holds."""
        X, y = _train_features(n_rows=1000, options_signal_sign=-1.0)
        fit = fit_sign_constrained_lasso(X, y)
        for name in OPTIONS_FEATURES:
            idx = fit.feature_names.index(name)
            self.assertLessEqual(
                fit.coefficients[idx],
                1e-9,
                f"{name} coef must be ≤ 0 (got {fit.coefficients[idx]:+.4g})",
            )

    def test_options_coefs_are_zeroed_when_data_disagrees(self):
        """Data DISAGREES with Xing (positive ivx30 signal — like v7's
        regime-shift overfit). Sign constraint mechanically blocks the
        Lasso from fitting positive coef; output must be ≤ 0 (typically
        zeroed by L1 since fit can't move toward the data-implied positive)."""
        X, y = _train_features(n_rows=1000, options_signal_sign=+1.0)
        fit = fit_sign_constrained_lasso(X, y)
        for name in OPTIONS_FEATURES:
            idx = fit.feature_names.index(name)
            self.assertLessEqual(
                fit.coefficients[idx],
                1e-9,
                f"{name} coef must be ≤ 0 even when data wants positive "
                f"(got {fit.coefficients[idx]:+.4g}) — v7 sign-flip surface "
                f"is eliminated by construction",
            )

    def test_equity_control_coefs_unconstrained(self):
        """Equity controls have no sign constraint. Positive reversal_1m
        signal in the data should fit a positive coef."""
        X, y = _train_features(n_rows=1000, equity_signal_sign=+1.0)
        fit = fit_sign_constrained_lasso(X, y)
        idx = fit.feature_names.index("reversal_1m")
        self.assertGreater(
            fit.coefficients[idx],
            0,
            f"reversal_1m coef should be positive on +signal data "
            f"(got {fit.coefficients[idx]:+.4g})",
        )

    def test_equity_control_coefs_can_be_negative(self):
        """If equity-control signal is negative, the unconstrained fit
        should produce a negative coef. Confirms equity controls truly
        unconstrained, not silently bound to ≥ 0."""
        X, y = _train_features(n_rows=1000, equity_signal_sign=-1.0)
        fit = fit_sign_constrained_lasso(X, y)
        idx = fit.feature_names.index("reversal_1m")
        self.assertLess(
            fit.coefficients[idx],
            0,
            f"reversal_1m coef should be negative on −signal data "
            f"(got {fit.coefficients[idx]:+.4g})",
        )

    def test_returns_global_lasso_fit_shape(self):
        from alphalens_research.screeners.options_implied.model import GlobalLassoFit

        X, y = _train_features(n_rows=500)
        fit = fit_sign_constrained_lasso(X, y)
        self.assertIsInstance(fit, GlobalLassoFit)
        self.assertEqual(len(fit.feature_names), 7)
        self.assertEqual(fit.coefficients.shape, (7,))
        self.assertEqual(fit.scaler_means.shape, (7,))
        self.assertEqual(fit.scaler_stds.shape, (7,))
        self.assertEqual(fit.n_train_obs, 500)
        self.assertGreater(fit.chosen_alpha, 0)

    def test_raises_on_too_few_rows(self):
        X, y = _train_features(n_rows=50)
        with self.assertRaises(ValueError):
            fit_sign_constrained_lasso(X, y)

    def test_raises_when_features_missing(self):
        X, y = _train_features(n_rows=500)
        X = X.drop(columns=list(FEATURE_NAMES[1:]))
        with self.assertRaises(ValueError):
            fit_sign_constrained_lasso(X, y)

    def test_predict_scores_returns_xing_direction(self):
        """End-to-end: fit on negative-options data, predict on a small
        holdout grid. Since options coefs ≤ 0, holdout rows with HIGH IV
        features get LOW scores (= bottom decile = SHORT per Xing); rows
        with LOW IV features get HIGH scores (= top decile = LONG)."""
        X, y = _train_features(n_rows=1000, options_signal_sign=-1.0)
        fit = fit_sign_constrained_lasso(X, y)

        holdout = pd.DataFrame(
            {
                "ivp30": [0.0, 0.0, 0.0],
                "ivx30": [-3.0, 0.0, +3.0],  # low / mid / high IV
                "ivx180_minus_ivx30": [0.0, 0.0, 0.0],
                "ivx30_over_hv20": [0.0, 0.0, 0.0],
                "reversal_1m": [0.0, 0.0, 0.0],
                "momentum_6m": [0.0, 0.0, 0.0],
                "rv_30d": [0.0, 0.0, 0.0],
            }
        )
        scores = predict_scores(fit, holdout)
        self.assertGreater(
            scores.iloc[0], scores.iloc[2], "low-IV row should rank above high-IV row"
        )

    def test_n_nonzero_options_consistent_with_coefficients(self):
        X, y = _train_features(n_rows=1000, options_signal_sign=-1.0)
        fit = fit_sign_constrained_lasso(X, y)
        options_indices = [i for i, c in enumerate(fit.feature_names) if c in OPTIONS_FEATURES]
        actual_nonzero = sum(1 for i in options_indices if not np.isclose(fit.coefficients[i], 0.0))
        self.assertEqual(fit.n_nonzero_options, actual_nonzero)

    def test_all_options_zeroed_flag_consistent(self):
        """When the sign constraint forces ALL options coefs to zero
        (data wants opposite sign and Lasso shrinks them to 0), the flag
        should fire. Selection-mechanism gate same as v7."""
        # Strong positive ivx30 signal + heavy regularization context →
        # sign-constrained Lasso likely zeros all options coefs.
        X, y = _train_features(n_rows=1000, options_signal_sign=+1.0, seed=7)
        fit = fit_sign_constrained_lasso(X, y)
        if fit.n_nonzero_options == 0:
            self.assertTrue(fit.all_options_zeroed)
        else:
            self.assertFalse(fit.all_options_zeroed)


if __name__ == "__main__":
    unittest.main()
