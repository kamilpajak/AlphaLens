"""v7 Phase B — single global Lasso fit on 7-feature stack.

Per pre-reg `model_class: Lasso` and `fit_strategy: single global model fitted
on 7-feature stack (4 options + 3 equity controls) across train tranches`.

Lasso α selected via sklearn LassoCV (default 3-fold CV with embargo not
required for global fit per pre-reg — train tranches are pooled, no
panel-leakage issue because target is 20d-forward and stride is 5d, so
embargo would only affect ~4 lookahead asofs if we wanted strict embargo;
for v7 v1 we accept the small embargo gap and rely on multi-phase audit).

If the Lasso zeros out ALL 4 options features → pre-reg auto_pivot trigger
"selection-mechanism artifact" → caller raises FAIL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn.linear_model import LassoCV
from sklearn.preprocessing import StandardScaler

from alphalens_research.screeners.options_implied.features import FEATURE_NAMES, OPTIONS_FEATURES

LAMBDA_GRID_POINTS_DEFAULT = 25
N_FOLDS_DEFAULT = 3
# sklearn LassoCV default eps=1e-3 yields alpha grid [max_alpha*1e-3, max_alpha].
# For high-noise targets (20d-forward returns with stdev ~40% on wide universe),
# max_alpha is large → CV cannot find a small enough α to retain coefs and
# zeros everything. eps=1e-6 widens the grid to [max_alpha*1e-6, max_alpha],
# letting CV find the bias-variance trade-off at lower regularization.
LASSO_CV_EPS_DEFAULT = 1e-6


@dataclass(frozen=True)
class GlobalLassoFit:
    """Trained model + diagnostic stats."""

    feature_names: tuple[str, ...]
    coefficients: np.ndarray  # aligned to feature_names
    intercept: float
    chosen_alpha: float
    cv_mean_mse: float
    n_train_obs: int
    n_nonzero_coefs: int
    n_nonzero_options: int  # subset of nonzero_coefs that are options features
    scaler_means: np.ndarray
    scaler_stds: np.ndarray

    @property
    def all_options_zeroed(self) -> bool:
        """Pre-reg auto_pivot trigger: Lasso zeros all 4 options features."""
        return self.n_nonzero_options == 0


def fit_global_lasso(
    train_features: pd.DataFrame,
    train_target: pd.Series,
    *,
    feature_names: Sequence[str] = FEATURE_NAMES,
    lambda_grid_points: int = LAMBDA_GRID_POINTS_DEFAULT,
    n_folds: int = N_FOLDS_DEFAULT,
    random_state: int = 42,
    eps: float = LASSO_CV_EPS_DEFAULT,
) -> GlobalLassoFit:
    """Fit single global Lasso with CV-tuned α on standardized features.

    `eps` controls the LassoCV alpha grid range: grid spans
    [max_alpha * eps, max_alpha]. Default eps=1e-6 is intentionally wider than
    sklearn's 1e-3 default to avoid zero-coef degenerate fit on noisy targets
    (see LASSO_CV_EPS_DEFAULT docstring).
    """
    feat_cols = [c for c in feature_names if c in train_features.columns]
    if len(feat_cols) < 2:
        raise ValueError(
            f"Insufficient feature columns in train_features: {feat_cols}. "
            f"Expected subset of FEATURE_NAMES."
        )

    X_raw = train_features[feat_cols].astype(float).to_numpy()
    y = train_target.astype(float).to_numpy()
    if X_raw.shape[0] != y.shape[0]:
        raise ValueError(f"X/y row mismatch: {X_raw.shape[0]} vs {y.shape[0]}")
    if X_raw.shape[0] < 100:
        raise ValueError(f"Insufficient train rows for Lasso CV: {X_raw.shape[0]} < 100")

    scaler = StandardScaler()
    X = scaler.fit_transform(X_raw)

    model = LassoCV(
        n_alphas=cast(Any, lambda_grid_points),
        cv=n_folds,
        eps=eps,
        random_state=random_state,
        selection="cyclic",
        max_iter=10_000,
    )
    model.fit(X, y)

    options_indices = [i for i, c in enumerate(feat_cols) if c in OPTIONS_FEATURES]
    coefs = model.coef_
    nonzero_mask = ~np.isclose(coefs, 0.0)
    nonzero_options = sum(1 for i in options_indices if nonzero_mask[i])

    cv_mean_mse = float(np.min(np.mean(model.mse_path_, axis=1)))

    return GlobalLassoFit(
        feature_names=tuple(feat_cols),
        coefficients=coefs,
        intercept=float(model.intercept_),
        chosen_alpha=float(model.alpha_),
        cv_mean_mse=cv_mean_mse,
        n_train_obs=int(X.shape[0]),
        n_nonzero_coefs=int(nonzero_mask.sum()),
        n_nonzero_options=nonzero_options,
        scaler_means=cast(np.ndarray, scaler.mean_),
        scaler_stds=cast(np.ndarray, scaler.scale_),
    )


def lasso_sign_alignment(
    fit: GlobalLassoFit,
    *,
    expected_negative_features: tuple[str, ...] = OPTIONS_FEATURES,
) -> dict[str, str | bool]:
    """Classify each options-feature coefficient against the literature prior
    (Xing 2010 et al.: vol-level features predict NEGATIVE forward returns).

    Returns a dict with one entry per options feature:
      - "agrees"  → coef is negative
      - "flipped" → coef is positive (contradicts literature prior)
      - "zero"   → Lasso zeroed it
    Plus a summary key `any_options_flipped: bool`.

    Pre-reg auto_pivot trigger: if `any_options_flipped` AND alpha-pass holdout,
    this is a diagnostic flag — document deviation, do NOT pivot strategy
    direction post-hoc.
    """
    out: dict[str, str | bool] = {}
    any_flipped = False
    for name in expected_negative_features:
        if name not in fit.feature_names:
            out[name] = "absent"
            continue
        idx = fit.feature_names.index(name)
        coef = float(fit.coefficients[idx])
        if coef == 0:
            out[name] = "zero"
        elif coef < 0:
            out[name] = "agrees"
        else:
            out[name] = "flipped"
            any_flipped = True
    out["any_options_flipped"] = any_flipped
    return out


def predict_scores(fit: GlobalLassoFit, features: pd.DataFrame) -> pd.Series:
    """Predict per-row Lasso score for `features`. Returns Series indexed by
    features.index. Rows with any NaN feature get NaN score.
    """
    feat_cols = list(fit.feature_names)
    X_raw = features[feat_cols].astype(float).to_numpy()
    valid = ~np.isnan(X_raw).any(axis=1)
    out = np.full(X_raw.shape[0], np.nan, dtype=float)
    if valid.any():
        X = (X_raw[valid] - fit.scaler_means) / fit.scaler_stds
        out[valid] = X @ fit.coefficients + fit.intercept
    return pd.Series(out, index=features.index, name="score")
