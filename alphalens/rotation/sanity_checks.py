"""IS sanity checks for Tactical Sector Rotation (Layer 2e).

Kill-gate diagnostics run on the IS backtest before committing to OOS. All
four must pass or OOS is statistically unjustified (per Perplexity R12
follow-up, 2026-04-24).

Distinction from ``gates.py``: those are R12 OOS *acceptance* gates.
``sanity_checks`` are *rejection* gates run on IS to falsify overfit
before paying the OOS query.

Gates:
  1. passive_correlation — strategy daily-return correlation with passive
     60/30/10 buy-and-hold MUST be < 0.95 (else the overlay has no
     tactical signal, just tracking error).
  2. rolling_sharpe_stability — no 252-day rolling Sharpe < 0.4 anywhere
     in IS (else regime-selection failure).
  3. per_regime_vs_passive — strategy must outperform passive in ≥ 2
     of 3 regimes (bull / bear / flat).
  4. overlay_alpha — regression of strategy on passive with HAC SE;
     annualised α > 20 bps AND |t| > 1.0, α positive.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from alphalens.backtest.factor_analysis import run_regression
from alphalens.backtest.history_store import HistoryStore
from alphalens.backtest.metrics import sharpe
from alphalens.backtest.regime import classify_regime


@dataclass(frozen=True)
class SanityCheckResult:
    name: str
    passed: bool
    value: float
    threshold: float
    detail: str


@dataclass(frozen=True)
class SanityCheckReport:
    checks: tuple[SanityCheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


def build_passive_benchmark(store: HistoryStore, *, core_weights: Mapping[str, float]) -> pd.Series:
    """Daily returns of a weighted buy-and-hold passive benchmark."""
    closes = pd.DataFrame(
        {t: store.full(t)["close"] for t in core_weights}
    )  # raises KeyError via store.full if ticker missing
    daily_rets = closes.pct_change().iloc[1:]
    weights = np.array([core_weights[t] for t in closes.columns])
    return (daily_rets * weights).sum(axis=1).rename("passive")


def check_passive_correlation(
    strategy_returns: pd.Series,
    passive_returns: pd.Series,
    *,
    threshold: float = 0.95,
) -> SanityCheckResult:
    aligned = pd.concat(
        [strategy_returns.rename("s"), passive_returns.rename("p")],
        axis=1,
        join="inner",
    ).dropna()
    corr = float(aligned["s"].corr(aligned["p"]))
    passed = corr < threshold
    detail = (
        f"Pearson(strategy, passive) = {corr:.3f} "
        f"({'below' if passed else 'at/above'} kill threshold {threshold})"
    )
    return SanityCheckResult(
        name="passive_correlation",
        passed=passed,
        value=corr,
        threshold=threshold,
        detail=detail,
    )


def check_rolling_sharpe_stability(
    strategy_returns: pd.Series,
    *,
    window: int = 252,
    min_sharpe: float = 0.4,
) -> SanityCheckResult:
    r = strategy_returns.dropna()
    if len(r) < window:
        return SanityCheckResult(
            name="rolling_sharpe_stability",
            passed=False,
            value=float("nan"),
            threshold=min_sharpe,
            detail=f"series too short ({len(r)} < {window})",
        )
    rolling = r.rolling(window).apply(lambda w: sharpe(w.tolist()), raw=False).dropna()
    worst = float(rolling.min())
    worst_date = rolling.idxmin()
    passed = worst > min_sharpe
    detail = (
        f"min {window}d rolling Sharpe = {worst:.2f} at {worst_date.date()} "
        f"(threshold {min_sharpe})"
    )
    return SanityCheckResult(
        name="rolling_sharpe_stability",
        passed=passed,
        value=worst,
        threshold=min_sharpe,
        detail=detail,
    )


def check_per_regime_vs_passive(
    strategy_returns: pd.Series,
    passive_returns: pd.Series,
    benchmark_close: pd.Series,
    *,
    lookback: int = 60,
    bull_threshold: float = 0.05,
    bear_threshold: float = -0.05,
) -> SanityCheckResult:
    labels = classify_regime(
        benchmark_close,
        lookback=lookback,
        bull_threshold=bull_threshold,
        bear_threshold=bear_threshold,
    )
    aligned = pd.concat(
        [
            strategy_returns.rename("s"),
            passive_returns.rename("p"),
            labels.rename("regime"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    summary: dict[str, float] = {}
    for label in ("bull", "bear", "flat"):
        slice_ = aligned[aligned["regime"] == label]
        if slice_.empty:
            continue
        # Excess mean daily return per regime (strategy - passive)
        summary[label] = float(slice_["s"].mean() - slice_["p"].mean())

    outperform = sum(1 for v in summary.values() if v > 0)
    # Strict kill-gate: docstring says "≥ 2 of 3 regimes". All three must
    # be exercised by the backtest; otherwise the gate would silently pass
    # strategies whose flat-regime behavior was never tested.
    passed = outperform >= 2 and len(summary) == 3
    detail = ", ".join(f"{k}: Δmean={v:+.5f}/d" for k, v in sorted(summary.items()))
    return SanityCheckResult(
        name="per_regime_vs_passive",
        passed=passed,
        value=float(outperform),
        threshold=2.0,
        detail=f"outperforms passive in {outperform}/{len(summary)} regimes ({detail})",
    )


def check_overlay_alpha(
    strategy_returns: pd.Series,
    passive_returns: pd.Series,
    *,
    min_alpha_bps: float = 20.0,
    min_t: float = 1.0,
    periods_per_year: int = 252,
) -> SanityCheckResult:
    """Regress strategy on passive + const with Newey-West HAC SE.

    α (intercept) is the daily tactical alpha orthogonal to passive exposure.
    Passes when annualised α > min_alpha_bps AND |t| > min_t AND α positive.
    """
    factors = pd.DataFrame({"passive": passive_returns, "RF": 0.0})
    alpha = run_regression(
        portfolio_returns=strategy_returns,
        factors=factors,
        factor_columns=["passive"],
        cov_type="HAC",
        spec_name="strategy ~ passive (HAC)",
        subtract_rf=False,
        periods_per_year=periods_per_year,
    )
    alpha_bps = alpha.alpha_annualized * 10_000.0  # annualised % → bps
    t = alpha.alpha_tstat
    passed = (alpha_bps > min_alpha_bps) and (abs(t) > min_t) and (alpha_bps > 0)
    detail = (
        f"α = {alpha_bps:.1f} bps/yr, t = {t:.2f} "
        f"(β_passive = {alpha.betas['passive']:.3f}, R² = {alpha.r_squared:.3f})"
    )
    return SanityCheckResult(
        name="overlay_alpha",
        passed=passed,
        value=alpha_bps,
        threshold=min_alpha_bps,
        detail=detail,
    )


def run_all_sanity_checks(
    *,
    strategy_returns: pd.Series,
    passive_returns: pd.Series,
    benchmark_close: pd.Series,
    corr_threshold: float = 0.95,
    rolling_window: int = 252,
    rolling_min_sharpe: float = 0.4,
    min_alpha_bps: float = 20.0,
    min_t: float = 1.0,
) -> SanityCheckReport:
    return SanityCheckReport(
        checks=(
            check_passive_correlation(strategy_returns, passive_returns, threshold=corr_threshold),
            check_rolling_sharpe_stability(
                strategy_returns, window=rolling_window, min_sharpe=rolling_min_sharpe
            ),
            check_per_regime_vs_passive(
                strategy_returns=strategy_returns,
                passive_returns=passive_returns,
                benchmark_close=benchmark_close,
            ),
            check_overlay_alpha(
                strategy_returns,
                passive_returns,
                min_alpha_bps=min_alpha_bps,
                min_t=min_t,
            ),
        )
    )
