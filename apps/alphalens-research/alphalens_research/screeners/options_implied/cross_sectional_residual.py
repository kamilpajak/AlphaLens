"""v9 secondary — cross-sectional residual scorer.

Pre-committed in `params_v9_sign_constrained_options_implied_2026_05_03.json` as the
SECONDARY hypothesis to be run only if the primary (sign-constrained Lasso) FAILs.
Pre-commitment encoded in pre-reg JSON before any v9 run; no sequential p-hacking.

Per asof, fit OLS within asof:

  −ivp30 ~ reversal_1m + momentum_6m + rv_30d + intercept

Score = OLS residual (the −ivp30 signal orthogonal to equity controls).

Conceptually independent from the primary: A is time-series Lasso on pooled
cross-sections; D is per-asof cross-sectional regression. Different statistical
surfaces — independent test of the same Xing-direction hypothesis with the
equity-control variance explicitly removed.

Skips asofs with fewer rows than regressors+intercept (4) by emitting NaN scores
for those rows. NaN in any of (`ivp30, reversal_1m, momentum_6m, rv_30d`) for a
row also propagates to NaN score.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EQUITY_CONTROLS_FOR_RESIDUAL: tuple[str, ...] = ("reversal_1m", "momentum_6m", "rv_30d")
_MIN_ROWS_PER_ASOF = 4  # 3 regressors + 1 intercept


def score_cross_sectional_residual(features: pd.DataFrame) -> pd.Series:
    """Per-asof OLS residual of `−ivp30` on equity controls.

    Returns a Series aligned to `features.index` with name `score`. NaN rows
    (any of `ivp30/reversal_1m/momentum_6m/rv_30d` missing or asof too small)
    propagate to NaN scores so downstream `dropna(subset=["_score"])` excludes
    them from the cross-section.
    """
    out = pd.Series(np.nan, index=features.index, name="score", dtype=float)

    required = ("ivp30", *EQUITY_CONTROLS_FOR_RESIDUAL)
    valid_mask = features[list(required)].notna().all(axis=1)

    for _asof, group in features.loc[valid_mask].groupby("asof", sort=False):
        if len(group) < _MIN_ROWS_PER_ASOF:
            continue
        y = (-group["ivp30"]).to_numpy(dtype=float)
        X = group[list(EQUITY_CONTROLS_FOR_RESIDUAL)].to_numpy(dtype=float)
        ones = np.ones((X.shape[0], 1), dtype=float)
        Xb = np.hstack([ones, X])

        # Solve normal equations β = (Xb' Xb)^-1 Xb' y; numpy.linalg.lstsq
        # handles rank-deficient asofs gracefully.
        beta, *_ = np.linalg.lstsq(Xb, y, rcond=None)
        residuals = y - Xb @ beta
        out.loc[group.index] = residuals

    return out
