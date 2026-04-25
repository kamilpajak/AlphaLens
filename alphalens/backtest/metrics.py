"""Backtest evaluation metrics — pure numpy/pandas.

Split into two conceptual tiers (per Perplexity's 2025-2026 guidance):

**Core** (ML-facing):
- Portfolio Sharpe (annualised, optional risk-free input)
- Rank IC (Spearman correlation, cross-sectional score vs forward return)
- IC t-statistic (mean/std-error) — required for significance claims
- IC rolling consistency (% of windows where IC is positive)

**Financial** (money-facing):
- Top-decile vs bottom-decile spread
- Hit rate (% days portfolio beats median universe)
- Turnover % between consecutive rebalances
- Max drawdown + Calmar ratio
- Concentration — weight of top-k names in the portfolio
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

DEFAULT_PERIODS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------


def sharpe(
    daily_returns: Sequence[float],
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
    risk_free: float = 0.0,
) -> float:
    """Annualised Sharpe ratio of `daily_returns`.

    `risk_free` is a per-period rate (not annualised). For daily data with
    4% annual RFR, pass risk_free=0.04/252. NaN or empty input → 0.
    """
    arr = np.asarray(list(daily_returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return 0.0
    excess = arr - risk_free
    std = excess.std(ddof=1)
    # numpy's std can leave a tiny float-point residual (~1e-18) for inputs that
    # are conceptually constant — treat those as zero-variance.
    if std < 1e-12:
        return 0.0
    return (excess.mean() / std) * math.sqrt(periods_per_year)


def sharpe_autocorr_adjusted(
    returns: Sequence[float],
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
    max_lag: int = 5,
    risk_free: float = 0.0,
) -> float:
    """Autocorrelation-adjusted annualised Sharpe (Lo 2002 Theorem 2).

    Naive ``sharpe()`` scales by ``sqrt(k)`` which assumes i.i.d. returns.
    Lo (2002) corrects for serial correlation via the variance ratio:

        VR(k) = 1 + 2 * Σ_{j=1..min(k-1, max_lag)} (1 - j/k) * ρ(j)
        k_eff = k / VR(k)
        SR_annualised = SR_per_period * sqrt(k_eff)

    Sign convention (verified against Lo 2002 §4.2):
    - ρ(j) < 0 (mean reversion): VR(k) < 1 → k_eff > k → scale > sqrt(k) →
      annualised Sharpe is HIGHER than naive sqrt(k) scaling. Realised annual
      vol grows slower than sqrt(k) × single-period vol, so the denominator
      in the annualised ratio is smaller, not larger.
    - ρ(j) > 0 (momentum/persistence): VR(k) > 1 → k_eff < k → scale < sqrt(k)
      → annualised Sharpe is LOWER than naive.
    - ρ(j) ≈ 0: VR(k) ≈ 1 → adjusted ≈ naive.

    An earlier implementation used ``k_eff = k + 2*Σ(1-j/k)*ρ(j)`` which
    inverted the direction for mean-reverting series. Zen code review
    (2026-04-24) caught the regression; this version matches Lo 2002.

    ``max_lag=5`` covers one-week of lag structure for daily returns; larger
    windows inflate noise on shorter series.
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < max(3, max_lag + 2):
        return 0.0

    excess = arr - risk_free
    std = excess.std(ddof=1)
    if std < 1e-12:
        return 0.0

    k = periods_per_year
    demean = excess - excess.mean()
    denom = float((demean * demean).sum())
    if denom <= 0:
        return (excess.mean() / std) * math.sqrt(max(k, 1))

    lags = min(max_lag, len(arr) - 1, k - 1)
    corr_sum = 0.0
    for j in range(1, lags + 1):
        num = float((demean[:-j] * demean[j:]).sum())
        rho = num / denom
        corr_sum += (1.0 - j / k) * rho

    # Lo 2002: annualised variance = k * σ² * VR(k) where VR = 1 + 2*Σ(1-j/k)ρ.
    # Guard against degenerate VR near 0 (pathological rho sequence) or
    # slightly-negative VR (numerical noise near zero-vol) — clamp to 0.01.
    var_ratio = 1.0 + 2.0 * corr_sum
    k_eff = k / max(var_ratio, 0.01)
    scale = math.sqrt(max(k_eff, 1.0))
    return (excess.mean() / std) * scale


def rank_ic(predicted_scores: Sequence[float], actual_returns: Sequence[float]) -> float:
    """Single-date Spearman rank correlation between predicted scores and realized returns.

    Returns 0 when input is empty or constant (no variance to measure).
    """
    p = np.asarray(list(predicted_scores), dtype=float)
    a = np.asarray(list(actual_returns), dtype=float)
    mask = ~(np.isnan(p) | np.isnan(a))
    if mask.sum() < 3:
        return 0.0
    p_valid, a_valid = p[mask], a[mask]
    # Spearman is undefined when either side is constant — short-circuit to
    # avoid scipy's ConstantInputWarning noise.
    if np.ptp(p_valid) == 0 or np.ptp(a_valid) == 0:
        return 0.0
    try:
        rho, _ = spearmanr(p_valid, a_valid)
    except ValueError:
        return 0.0
    if rho is None or np.isnan(rho):
        return 0.0
    return float(rho)


def rank_ic_tstat(ic_series: Sequence[float]) -> float:
    """Newey-West-less t-stat: mean(IC) / (std(IC) / sqrt(N)).

    Use on a series of daily ICs. A t-stat > 2 is the classic 95%-significance
    threshold. On short windows (<250 days) even a legitimate IC of 0.02-0.03
    will often fail this test — treat with domain nuance.
    """
    arr = np.asarray(list(ic_series), dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return 0.0
    std = arr.std(ddof=1)
    if std == 0:
        return 0.0
    return float(arr.mean() / (std / math.sqrt(len(arr))))


def rank_ic_rolling(ic_series: Sequence[float], window: int = 20) -> pd.Series:
    """Rolling-mean IC over a lookback window (default 20 trading days)."""
    s = pd.Series(list(ic_series), dtype=float)
    return s.rolling(window=window, min_periods=max(1, window // 2)).mean()


def rank_ic_positive_pct(ic_series: Sequence[float], window: int = 20) -> float:
    """Fraction of `window`-day rolling-IC windows with positive mean.

    Perplexity's rule of thumb: >60% → decent consistency, >80% → strong.
    """
    rolling = rank_ic_rolling(ic_series, window=window).dropna()
    if rolling.empty:
        return 0.0
    return float((rolling > 0).mean())


# ---------------------------------------------------------------------------
# Financial metrics
# ---------------------------------------------------------------------------


def decile_spread(
    scores: Sequence[float], forward_returns: Sequence[float], n_deciles: int = 10
) -> float:
    """Mean forward return of top `1/n_deciles` minus mean of bottom `1/n_deciles`.

    A positive spread means high-scored names outperformed low-scored ones —
    basic sanity check that the scorer is ordering things in the right direction.
    """
    s = np.asarray(list(scores), dtype=float)
    r = np.asarray(list(forward_returns), dtype=float)
    mask = ~(np.isnan(s) | np.isnan(r))
    s, r = s[mask], r[mask]
    if len(s) < n_deciles:
        return 0.0
    df = pd.DataFrame({"score": s, "ret": r}).sort_values("score")
    bucket = max(1, len(df) // n_deciles)
    bottom_mean = df.iloc[:bucket]["ret"].mean()
    top_mean = df.iloc[-bucket:]["ret"].mean()
    return float(top_mean - bottom_mean)


def hit_rate(portfolio_returns: Sequence[float], universe_median_returns: Sequence[float]) -> float:
    """Fraction of days where the portfolio beats the universe median.

    Both inputs must align by date. NaN rows are dropped pairwise.
    """
    p = np.asarray(list(portfolio_returns), dtype=float)
    u = np.asarray(list(universe_median_returns), dtype=float)
    mask = ~(np.isnan(p) | np.isnan(u))
    p, u = p[mask], u[mask]
    if len(p) == 0:
        return 0.0
    return float((p > u).mean())


def turnover_pct(daily_top_n_lists: Iterable[Iterable[str]]) -> float:
    """Average turnover between consecutive rebalance snapshots.

    Turnover = names exiting top-N / N. 100% means a full reshuffle each day.
    Returns 0 if fewer than two snapshots.
    """
    snapshots = [frozenset(s) for s in daily_top_n_lists]
    if len(snapshots) < 2:
        return 0.0
    turnovers = []
    for prev, nxt in zip(snapshots[:-1], snapshots[1:]):
        size = max(len(prev), 1)
        exits = prev - nxt
        turnovers.append(len(exits) / size)
    return float(np.mean(turnovers)) if turnovers else 0.0


def max_drawdown(cumulative_returns: Sequence[float]) -> float:
    """Largest peak-to-trough fractional loss in a cumulative-return series.

    Input is cumulative returns (starting typically at 1.0 or 0.0 for log).
    Returns a non-positive number (e.g. -0.18 for 18% drawdown), 0 if input empty.
    """
    arr = np.asarray(list(cumulative_returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return 0.0
    peak = np.maximum.accumulate(arr)
    drawdowns = (arr - peak) / np.where(peak == 0, 1, peak)
    return float(drawdowns.min())


def calmar_ratio(
    daily_returns: Sequence[float],
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
) -> float:
    """Annualised return divided by |max drawdown|.

    >1 is excellent; 0.3-0.5 is typical for mediocre strategies. Returns 0 if
    there's no drawdown (never goes underwater) or no history.
    """
    arr = np.asarray(list(daily_returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return 0.0
    cum = np.cumprod(1 + arr)
    mdd = max_drawdown(cum)
    if mdd == 0:
        return 0.0
    years = len(arr) / periods_per_year
    if years <= 0:
        return 0.0
    annual_ret = cum[-1] ** (1 / years) - 1
    return float(annual_ret / abs(mdd))


def concentration_top_k(weights: Sequence[float], k: int = 5) -> float:
    """Fraction of total absolute weight concentrated in the top-k positions.

    For an equal-weight top-30 portfolio with k=5: 5/30 = 0.167. Higher values
    indicate fragility to single-name shocks.
    """
    arr = np.asarray(list(weights), dtype=float)
    arr = np.abs(arr[~np.isnan(arr)])
    if len(arr) == 0 or k <= 0:
        return 0.0
    total = arr.sum()
    if total == 0:
        return 0.0
    k_eff = min(k, len(arr))
    top_sum = np.sort(arr)[-k_eff:].sum()
    return float(top_sum / total)


# ---------------------------------------------------------------------------
# Helpers used by multiple metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioSummary:
    """Tidy bundle of the headline portfolio metrics — for CLI printing + report."""

    sharpe: float
    annual_return: float
    max_drawdown: float
    calmar: float
    hit_rate: float
    days: int


def summarise_portfolio(
    daily_returns: Sequence[float],
    universe_median_returns: Sequence[float] | None = None,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
) -> PortfolioSummary:
    arr = np.asarray(list(daily_returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return PortfolioSummary(0.0, 0.0, 0.0, 0.0, 0.0, len(arr))
    cum = np.cumprod(1 + arr)
    years = len(arr) / periods_per_year
    annual = cum[-1] ** (1 / years) - 1 if years > 0 else 0.0
    hit = (
        hit_rate(daily_returns, universe_median_returns)
        if universe_median_returns is not None
        else 0.0
    )
    return PortfolioSummary(
        sharpe=sharpe(daily_returns, periods_per_year=periods_per_year),
        annual_return=float(annual),
        max_drawdown=max_drawdown(cum),
        calmar=calmar_ratio(daily_returns, periods_per_year=periods_per_year),
        hit_rate=hit,
        days=len(arr),
    )
