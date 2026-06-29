"""v9 sign-constrained Lasso — Xing prior enforced mechanically on options
features only, equity controls free-sign.

Pre-registered as `v9_sign_constrained_options_implied_2026_05_03` per
`docs/research/preregistration/params_v9_sign_constrained_options_implied_2026_05_03.json`.

Built after v7 (Lasso, αt +2.60) and v8 (model-free −ivp30, αt +2.18) both
FAIL'd multi-phase audits on the burnt 2024-04-30 → 2026-04-30 holdout.
v7's Lasso fitted POSITIVE coefs on `ivx30`/`ivp30` (regime-shift overfit);
v8's pure ivp30 sort lacked the equity-control + IV/HV ratio amplification
that v7's bottom-decile demonstrated (L/S diagnostic αt = −3.25 → implied
bottom-decile αt ≈ +5.34 if direction had been correct).

v9 combines v8's sign safety with v7's equity-control magnitude lift:
mechanically force the 4 options-feature coefficients to ≤ 0 (Xing prior
enforced ex-ante), let the 3 equity controls (`reversal_1m`, `momentum_6m`,
`rv_30d`) fit freely.

Implementation note. `sklearn.linear_model.LassoCV(positive=True)` is
all-or-nothing: it forces ALL coefficients ≥ 0, with no per-feature mask.
We achieve a partial sign constraint via the standard "free-sign-as-pair"
trick: for each equity-control feature `x`, we add BOTH `+x` and `-x` to
the design matrix and fit `positive=True`; the recovered free-sign
coefficient is `β_pos - β_neg` (at the L1 optimum at most one of the pair
is nonzero per equity feature, so the joint L1 penalty equals the standard
Lasso L1 on the original equity coef). For options features we negate ONCE
so `positive=True` enforces the Xing-correct (negative-on-original) sign.

The returned `GlobalLassoFit` has coefficients/scaler in ORIGINAL-feature
standardized space (options coefs ≤ 0, equity coefs free) so the existing
`predict_scores` works unchanged on un-negated holdout frames.

Per perplexity Sonar Reasoning Pro adversarial review (2026-05-03), this is
the highest-likelihood v9 axis (~25-35% chance of clearing the program-Bonferroni
n=17 threshold |t|≥3.13). Underlying signal triangulated at ~+2.2 αt from v7+v8;
sign-constraining recovers Xing direction without overfit but does not
mechanically increase magnitude. Likely FAIL outcome documented as honest
expectation, not undesirable surprise.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn.linear_model import LassoCV
from sklearn.preprocessing import StandardScaler

from alphalens_research.screeners.options_implied.features import FEATURE_NAMES, OPTIONS_FEATURES
from alphalens_research.screeners.options_implied.model import (
    LAMBDA_GRID_POINTS_DEFAULT,
    LASSO_CV_EPS_DEFAULT,
    N_FOLDS_DEFAULT,
    GlobalLassoFit,
)


def fit_sign_constrained_lasso(
    train_features: pd.DataFrame,
    train_target: pd.Series,
    *,
    feature_names: Sequence[str] = FEATURE_NAMES,
    lambda_grid_points: int = LAMBDA_GRID_POINTS_DEFAULT,
    n_folds: int = N_FOLDS_DEFAULT,
    random_state: int = 42,
    eps: float = LASSO_CV_EPS_DEFAULT,
) -> GlobalLassoFit:
    """Fit Lasso with sign mask `coef_options ≤ 0` (Xing 2010 prior),
    equity controls free-sign.

    Mathematically:
    1. Standardize ORIGINAL features → mean μ, std σ, X_std (n × 7)
    2. Build augmented matrix X_aug (n × 10) with columns:
         - [0..3]   negated options:  -X_std[:, options_idx]
         - [4..6]   equity positive:  +X_std[:, equity_idx]
         - [7..9]   equity negative:  -X_std[:, equity_idx]
    3. Fit `LassoCV(positive=True)` on X_aug → β_aug ≥ 0 (10-vector)
    4. Map back to original-feature standardized space:
         - coefs[options[j]] = -β_aug[j]                              (≤ 0, Xing)
         - coefs[equity[j]]  = β_aug[4+j] - β_aug[7+j]                (free sign)

    The returned `GlobalLassoFit` has coefficients/scaler in ORIGINAL-feature
    standardized coordinates so `predict_scores(fit, original_features)`
    works unchanged.

    Raises
    ------
    ValueError
        If fewer than 2 known feature columns are present in `train_features`,
        or fewer than 100 training rows.
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
    X_std = scaler.fit_transform(X_raw)

    options_indices = [i for i, c in enumerate(feat_cols) if c in OPTIONS_FEATURES]
    equity_indices = [i for i, c in enumerate(feat_cols) if c not in OPTIONS_FEATURES]
    n_options = len(options_indices)
    n_equity = len(equity_indices)

    # Build augmented matrix: negated options + equity-positive + equity-negative.
    # The free-sign-as-pair encoding lets us use a single LassoCV(positive=True)
    # with a partial sign constraint.
    n_train = X_std.shape[0]
    X_aug = np.zeros((n_train, n_options + 2 * n_equity), dtype=float)
    for j, idx in enumerate(options_indices):
        X_aug[:, j] = -X_std[:, idx]
    for j, idx in enumerate(equity_indices):
        X_aug[:, n_options + j] = X_std[:, idx]
        X_aug[:, n_options + n_equity + j] = -X_std[:, idx]

    model = LassoCV(
        # scikit-learn 1.9 removed `n_alphas`; `alphas` now accepts an int
        # (number of alphas along the path) — same behavior.
        alphas=cast(Any, lambda_grid_points),
        cv=n_folds,
        eps=eps,
        random_state=random_state,
        selection="cyclic",
        max_iter=10_000,
        positive=True,
    )
    model.fit(X_aug, y)

    # Map β_aug back to original-feature standardized interpretation.
    coefs = np.zeros(len(feat_cols), dtype=float)
    for j, idx in enumerate(options_indices):
        coefs[idx] = -model.coef_[j]
    for j, idx in enumerate(equity_indices):
        coefs[idx] = model.coef_[n_options + j] - model.coef_[n_options + n_equity + j]

    nonzero_mask = ~np.isclose(coefs, 0.0)
    nonzero_options = sum(1 for i in options_indices if nonzero_mask[i])
    cv_mean_mse = float(np.min(np.mean(model.mse_path_, axis=1)))

    return GlobalLassoFit(
        feature_names=tuple(feat_cols),
        coefficients=coefs,
        intercept=float(model.intercept_),
        chosen_alpha=float(model.alpha_),
        cv_mean_mse=cv_mean_mse,
        n_train_obs=int(X_raw.shape[0]),
        n_nonzero_coefs=int(nonzero_mask.sum()),
        n_nonzero_options=nonzero_options,
        scaler_means=cast(np.ndarray, scaler.mean_),
        scaler_stds=cast(np.ndarray, scaler.scale_),
    )
