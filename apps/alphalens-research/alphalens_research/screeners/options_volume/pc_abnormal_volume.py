"""P/C abnormal volume scorer (Pan-Poteshman 2006-inspired proxy).

Spec frozen in pre-reg ``pc_abnormal_volume_retrospective_pre_2018_2026_05_05``
(sha256 ``03ddf4b7906ed07049bbb74dcdd599afa29abda1e8c4f6551a1876c78e45e689``).

Pipeline per asof:
1. Per-ticker time-series: ``pcr_t = log(optVolPut_t / optVolCall_t)``; NaN on zero
   or non-positive volume.
2. Per-ticker time-series: ``abnormal_pcr_t = pcr_t - rolling_mean(pcr,
   window=60, min_periods=30)`` over t-60..t-1 (strictly past, no look-ahead).
3. Per-asof cross-sectional OLS: ``abnormal_pcr ~ log_marketCap + reversal_1m +
   momentum_6m + rv_30d``; ``score = -residual`` (negate so high score = abnormally
   low pcr = abnormal call activity, per Pan-Poteshman 2006 directional reading).
4. Long-only top decile by score, equal-weight, 5d rebalance.

Residualization is the pre-committed amendment (zen + Perplexity adversarial
review 2026-05-05): aggregated daily P/C ratios capture mechanical hedging,
vol scaling, and contemporaneous returns — Pan-Poteshman explicitly showed the
public-flow component lacks predictive power. Without controls the test
collapses to that null component.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

EQUITY_CONTROLS_FOR_RESIDUAL: tuple[str, ...] = (
    "reversal_1m",
    "momentum_6m",
    "rv_30d",
)
ROLLING_WINDOW_DAYS = 60
MIN_ROLLING_OBS = 30
MIN_ASOF_TICKERS = 50  # min cross-section size for OLS to be meaningful


def compute_pcr(opt_vol_put, opt_vol_call) -> float:
    """``log(put / call)``; ``NaN`` on zero or non-positive volume.

    Returns float NaN (not None) so callers can build a numeric Series cleanly.
    Both inputs accept ``None``, ``NaN``, or numeric scalars.
    """
    if opt_vol_put is None or opt_vol_call is None:
        return float("nan")
    try:
        p = float(opt_vol_put)
        c = float(opt_vol_call)
    except (TypeError, ValueError):
        return float("nan")
    if math.isnan(p) or math.isnan(c) or p <= 0.0 or c <= 0.0:
        return float("nan")
    return math.log(p / c)


def compute_abnormal_pcr_series(opt_vol_put: pd.Series, opt_vol_call: pd.Series) -> pd.Series:
    """Per-ticker time-series of abnormal log put-call ratio.

    Aligned to the input index. ``abnormal_pcr_t = pcr_t - rolling_mean_excluding_current``
    where the rolling mean spans ``t-60..t-1`` and requires ≥30 non-NaN obs to
    emit a value (else NaN).

    Both inputs must share the same index. Returns a Series of float dtype.
    """
    if len(opt_vol_put) != len(opt_vol_call):
        raise ValueError("opt_vol_put and opt_vol_call must have same length")

    pcr_values = [compute_pcr(p, c) for p, c in zip(opt_vol_put, opt_vol_call, strict=True)]
    pcr = pd.Series(pcr_values, index=opt_vol_put.index, dtype=float)

    # Rolling mean over t-60..t-1 (shift first to exclude current obs).
    rolling_mean = (
        pcr.shift(1).rolling(window=ROLLING_WINDOW_DAYS, min_periods=MIN_ROLLING_OBS).mean()
    )
    abnormal = pcr - rolling_mean
    abnormal.name = "abnormal_pcr"
    return abnormal


def score_pc_abnormal_residual(features: pd.DataFrame) -> pd.Series:
    """Per-asof OLS residual of abnormal_pcr on equity controls; score = -residual.

    Required columns in ``features``:
        ``asof``, ``ticker``, ``abnormal_pcr``, ``log_marketCap``, ``reversal_1m``,
        ``momentum_6m``, ``rv_30d``.

    Returns a Series aligned to ``features.index`` with name ``score``. NaN rows
    (any required column missing OR asof has fewer than ``MIN_ASOF_TICKERS``
    valid rows) propagate to NaN scores.
    """
    out = pd.Series(np.nan, index=features.index, name="score", dtype=float)

    required = ("abnormal_pcr", *EQUITY_CONTROLS_FOR_RESIDUAL)
    valid_mask = features[list(required)].notna().all(axis=1)

    for _asof, group in features.loc[valid_mask].groupby("asof", sort=False):
        if len(group) < MIN_ASOF_TICKERS:
            continue
        y = group["abnormal_pcr"].to_numpy(dtype=float)
        X = group[list(EQUITY_CONTROLS_FOR_RESIDUAL)].to_numpy(dtype=float)
        ones = np.ones((X.shape[0], 1), dtype=float)
        x_with_intercept = np.hstack([ones, X])

        # OLS via lstsq; rank-deficient asofs handled gracefully.
        beta, *_ = np.linalg.lstsq(x_with_intercept, y, rcond=None)
        residuals = y - x_with_intercept @ beta
        # Negate so that abnormally-LOW pcr (= bullish call activity) ranks HIGH.
        out.loc[group.index] = -residuals

    return out
