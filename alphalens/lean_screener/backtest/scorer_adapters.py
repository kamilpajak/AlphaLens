"""Adapters so the BacktestEngine can run Layer 2b (MomentumScorer) on the same
5-year harness as Layer 2c (Lean scorer). Enables the 2×2 cross-test
(scorer × universe) that isolates which dimension is broken.

The engine expects a scorer with signature:
    scorer(histories: Mapping[str, pd.DataFrame], config: Mapping) -> pd.DataFrame
where each history has columns [open, high, low, close, volume] and the output
DataFrame carries at least `ticker` and `score` columns.

Layer 2b's `MomentumScorer` uses capitalised columns and a separate benchmark
DataFrame — both handled here.
"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from alphalens.momentum_screener.config import MOMENTUM_DEFAULTS
from alphalens.momentum_screener.momentum_scorer import MomentumScorer


_COLUMN_RENAME = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
}


def _to_capitalised(df: pd.DataFrame) -> pd.DataFrame:
    """Lean/pandas lowercase → capitalised (Layer 2b convention)."""
    return df.rename(columns=_COLUMN_RENAME)


def momentum_scorer_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:
    """Call Layer 2b's MomentumScorer with the column rename + benchmark wiring.

    Config may override `benchmark` (default "SPY") and the seven metric weights.
    Returns a DataFrame with the full breakdown plus a single `score` column
    that the BacktestEngine consumes.
    """
    config = dict(config or {})
    merged_config = dict(MOMENTUM_DEFAULTS)
    merged_config.update({k: v for k, v in config.items() if k in MOMENTUM_DEFAULTS})

    benchmark_ticker = config.get("benchmark", merged_config.get("benchmark", "SPY"))
    prices: dict[str, pd.DataFrame] = {
        ticker: _to_capitalised(df) for ticker, df in histories.items()
    }

    # The benchmark may or may not be in the scored universe — the engine can
    # pass it in through the histories dict.
    if benchmark_ticker not in prices:
        # Without a benchmark series, rel_strength collapses to 0.5 (neutral).
        bench_series = None
    else:
        bench_series = benchmark_ticker

    tickers = [t for t in prices.keys() if t != benchmark_ticker]

    scorer = MomentumScorer(merged_config)
    scored = scorer.score_all(tickers, prices, benchmark_ticker=bench_series)
    if scored.empty:
        return pd.DataFrame(columns=["ticker", "score"])
    # Normalise column name for the engine — engine sorts by "score" descending.
    scored = scored.rename(columns={"momentum_score": "score"})
    return scored
