"""Merton distance-to-default (naive KMV).

Pure functions. Naive KMV per Bharath-Shumway 2008: V = equity_mcap +
total_liabilities; sigma_V approximated as sigma_E (documented limitation,
rho ~ 0.95 vs full iterative for cross-sectional rank).

    d2 = (ln(V/D) + (r - 0.5*sigma^2)*T) / (sigma*sqrt(T))
    PD = N(-d2)

Score (in adapter) = -PD so engine's ``top_n`` selection picks lowest-PD
names = safest equity (long leg of distress puzzle).
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats

_MIN_BARS_VOL = 65  # 60d window + small buffer


def merton_d2(
    equity_mcap: float,
    total_liabilities: float,
    sigma_equity: float,
    rf: float,
    horizon_years: float = 1.0,
) -> float | None:
    """Naive Merton d2.

    Returns ``None`` if any input is non-finite, V <= 0, D <= 0, or sigma
    <= 0 (degenerate states). Defensive against missing data — adapter
    drops the ticker rather than ranking it on bogus PD.
    """
    inputs = (equity_mcap, total_liabilities, sigma_equity, rf, horizon_years)
    if any(not math.isfinite(x) for x in inputs):
        return None
    if equity_mcap <= 0.0 or total_liabilities <= 0.0 or sigma_equity <= 0.0:
        return None
    if horizon_years <= 0.0:
        return None
    V = equity_mcap + total_liabilities
    D = total_liabilities
    sigma = sigma_equity
    T = horizon_years
    return (math.log(V / D) + (rf - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def merton_pd(d2: float) -> float:
    """Implied probability of default = N(-d2)."""
    return float(stats.norm.cdf(-d2))


def realised_vol_60d(closes: np.ndarray) -> float | None:
    """Annualised stdev of 60d daily log-returns.

    Requires at least 61 closes (60 returns + the starting bar). Returns
    ``None`` for non-finite inputs, constant-price degenerate windows, or
    insufficient bars.
    """
    if closes.size < _MIN_BARS_VOL - 4:  # at minimum 61 closes for 60 log-returns
        return None
    window_closes = closes[-(_MIN_BARS_VOL - 4) :]
    if not np.all(np.isfinite(window_closes)) or np.any(window_closes <= 0):
        return None
    log_rets = np.diff(np.log(window_closes))
    if log_rets.size < 60:
        return None
    if not np.all(np.isfinite(log_rets)):
        return None
    std = float(np.std(log_rets, ddof=1))
    if std <= 0.0 or not math.isfinite(std):
        return None
    return std * math.sqrt(252)
