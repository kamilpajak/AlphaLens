# pyright: reportAttributeAccessIssue=false, reportCallIssue=false
"""Daily-OHLC bid-ask spread estimators (Phase 1 of issue #3).

Two published low-frequency spread estimators that work on daily high/low/close
bars, no tick-level quotes required. Both return the **proportional full
bid-ask spread in basis points**, rolling-window smoothed. The half-spread (the
quantity consumed by ``RealisticCostModel.primary_one_way_bps``) is the output
divided by 2 — kept out of this module so the math stays auditable directly
against the source papers.

References:
- Abdi, F., & Ranaldo, A. (2017). A simple estimation of bid-ask spreads from
  daily close, high, and low prices. *Review of Financial Studies*, 30(12),
  4437-4480.
- Corwin, S. A., & Schultz, P. (2012). A simple way to estimate bid-ask
  spreads from daily high and low prices. *Journal of Finance*, 67(2),
  719-760.

Phase 1 of issue #3 ships estimator math only — no rolling store, no cost
model integration. Phase 2 will wire the output through a
``MarketCharacteristicsStore`` into ``alphalens_research/attribution/cost_model.py``.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

_SQRT2 = math.sqrt(2.0)
_K = 3.0 - 2.0 * _SQRT2  # Corwin-Schultz constant denominator


def abdi_ranaldo_spread_bps(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    window: int = 21,
    min_periods: int = 10,
) -> pd.Series:
    """Abdi-Ranaldo (2017) proportional full bid-ask spread, in basis points.

    Per-day mid ``η_t = (log H_t + log L_t) / 2``. The 2-day estimator
    ``S²_t = 4 · (log C_t − η_t) · (log C_t − η_{t+1})`` is unbiased for the
    squared proportional spread. Averaging S²_t over a rolling window, clipping
    negative values to zero (overlap correction), and taking the square root
    gives the estimated full proportional spread.

    Output is in basis points: ``sqrt(max(0, mean(S²))) × 10_000``.

    NaN propagation: any row with non-positive high, low, or close becomes
    NaN in the intermediate S²_t series; the rolling mean uses ``min_periods``
    valid daily estimates per window.
    """
    if window <= 0:
        raise ValueError(f"window must be > 0, got {window}")
    if min_periods <= 0 or min_periods > window:
        raise ValueError(
            f"min_periods must be in (0, window]; got min_periods={min_periods}, window={window}"
        )

    log_h = np.log(high.where(high > 0))
    log_l = np.log(low.where(low > 0))
    log_c = np.log(close.where(close > 0))
    eta = (log_h + log_l) / 2.0
    # S²_t = 4 · (log C_t − η_t) · (log C_t − η_{t+1})
    s_squared = 4.0 * (log_c - eta) * (log_c - eta.shift(-1))
    # Rolling mean over `window` daily estimates, clip negative (overlap), sqrt.
    mean_s_sq = s_squared.rolling(window=window, min_periods=min_periods).mean()
    mean_s_sq = mean_s_sq.clip(lower=0.0)
    full_spread = np.sqrt(mean_s_sq)
    return (full_spread * 10_000.0).astype(float)


def corwin_schultz_spread_bps(
    high: pd.Series,
    low: pd.Series,
    *,
    window: int = 21,
    min_periods: int = 10,
) -> pd.Series:
    """Corwin-Schultz (2012) proportional full bid-ask spread, in basis points.

    Daily proportional spread from consecutive single-day and 2-day high-low ranges:

        β_t = (log(H_t / L_t))² + (log(H_{t+1} / L_{t+1}))²
        γ_t = (log(max(H_t, H_{t+1}) / min(L_t, L_{t+1})))²
        α_t = (sqrt(2β_t) − sqrt(β_t)) / (3 − 2√2) − sqrt(γ_t / (3 − 2√2))
        S_t = 2(e^α_t − 1) / (1 + e^α_t)

    Negative daily ``S_t`` is clipped to zero (per CS paper, overlap correction).
    Output averages S_t over a rolling window and converts to basis points
    (``mean × 10_000``).
    """
    if window <= 0:
        raise ValueError(f"window must be > 0, got {window}")
    if min_periods <= 0 or min_periods > window:
        raise ValueError(
            f"min_periods must be in (0, window]; got min_periods={min_periods}, window={window}"
        )

    h = high.where(high > 0)
    low_ = low.where(low > 0)
    h_next = h.shift(-1)
    l_next = low_.shift(-1)

    log_hl_t = np.log(h / low_)
    log_hl_t1 = np.log(h_next / l_next)
    beta = log_hl_t**2 + log_hl_t1**2

    h_pair = pd.concat([h, h_next], axis=1).max(axis=1)
    l_pair = pd.concat([low_, l_next], axis=1).min(axis=1)
    gamma = np.log(h_pair / l_pair) ** 2

    alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _K - np.sqrt(gamma / _K)
    s_t = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    s_t = s_t.clip(lower=0.0)

    mean_s = s_t.rolling(window=window, min_periods=min_periods).mean()
    return (mean_s * 10_000.0).astype(float)


__all__ = ["abdi_ranaldo_spread_bps", "corwin_schultz_spread_bps"]
