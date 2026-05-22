"""Market-regime classification based on benchmark-trend thresholds.

Per Perplexity's recommendation, regime breakdown is a must-have metric:
splits the backtest period into bull / bear / flat windows driven by the
benchmark (default SPY) and reports per-regime Sharpe/IC so you can see
whether the strategy's edge is regime-dependent.

Default thresholds (on 60-day trailing return):
  - bull:  +5% or higher
  - bear:  −5% or lower
  - flat:  everything in between
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import pandas as pd

Regime = Literal["bull", "bear", "flat"]

_DEFAULT_LOOKBACK = 60
_DEFAULT_BULL_THRESHOLD = 0.05
_DEFAULT_BEAR_THRESHOLD = -0.05


def classify_regime(
    benchmark_close: pd.Series,
    lookback: int = _DEFAULT_LOOKBACK,
    bull_threshold: float = _DEFAULT_BULL_THRESHOLD,
    bear_threshold: float = _DEFAULT_BEAR_THRESHOLD,
) -> pd.Series:
    """Classify each date by trailing `lookback`-day return of the benchmark.

    Returns a Series of string labels ("bull", "bear", "flat") indexed by the
    same dates as the input (minus the warmup window).
    """
    if lookback < 2:
        raise ValueError("lookback must be >= 2")
    closes = benchmark_close.dropna()
    if closes.empty:
        return pd.Series(dtype=str)
    trailing = closes / closes.shift(lookback) - 1.0
    labels = pd.Series(index=trailing.index, dtype=object)
    labels[trailing >= bull_threshold] = "bull"
    labels[trailing <= bear_threshold] = "bear"
    labels[labels.isna()] = "flat"
    return labels.dropna()


@dataclass(frozen=True)
class RegimeStats:
    regime: Regime
    days: int
    sharpe: float
    annual_return: float
    mean_ic: float
    hit_rate: float


def regime_breakdown(
    portfolio_returns: pd.Series,
    ic_series: pd.Series,
    universe_median_returns: pd.Series,
    regime_labels: pd.Series,
    periods_per_year: int = 252,
) -> Mapping[Regime, RegimeStats]:
    """Group metrics by regime — returns dict keyed by "bull"/"bear"/"flat".

    All four series must share aligned DatetimeIndex; the function takes the
    intersection before slicing.
    """
    from alphalens_research.backtest.metrics import hit_rate, sharpe

    # Align by intersection of all inputs.
    idx = (
        portfolio_returns.index.intersection(ic_series.index)
        .intersection(universe_median_returns.index)
        .intersection(regime_labels.index)
    )
    port = portfolio_returns.loc[idx]
    ic = ic_series.loc[idx]
    median = universe_median_returns.loc[idx]
    regimes = regime_labels.loc[idx]

    out: dict[str, RegimeStats] = {}
    for label in ("bull", "bear", "flat"):
        mask = regimes == label
        if not mask.any():
            continue
        port_slice = port[mask]
        ic_slice = ic[mask]
        median_slice = median[mask]
        cum = (1 + port_slice).prod()
        years = max(len(port_slice) / periods_per_year, 1e-9)
        annual = float(cum ** (1 / years) - 1) if years > 0 else 0.0
        out[label] = RegimeStats(
            regime=label,  # type: ignore[arg-type]
            days=len(port_slice),
            sharpe=sharpe(port_slice.tolist(), periods_per_year=periods_per_year),
            annual_return=annual,
            mean_ic=float(ic_slice.mean()) if not ic_slice.empty else 0.0,
            hit_rate=hit_rate(port_slice.tolist(), median_slice.tolist()),
        )
    return out
