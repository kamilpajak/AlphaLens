"""Adapters bridging Layer 2b scorers into the generic BacktestEngine.

The engine expects a scorer with signature:
    scorer(histories: Mapping[str, pd.DataFrame], config: Mapping) -> pd.DataFrame
where each history has columns [open, high, low, close, volume] and the output
DataFrame carries at least `ticker` and `score` columns.

Layer 2b's scorers use capitalised OHLCV columns and a separate benchmark
DataFrame — both handled here.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping

import pandas as pd

from .config import THEMED_DEFAULTS
from .early_stage_scorer import EARLY_STAGE_DEFAULTS, EarlyStageScorer
from .momentum_scorer import MomentumScorer

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


def _latest_asof(histories: Mapping[str, pd.DataFrame]) -> date | None:
    """Infer the backtest engine's current date from the truncated histories.

    BacktestEngine calls `store.truncate_to(ticker, asof)` before passing
    prices in — so max(index) across any non-empty frame equals asof. Cheapest
    PIT hook without modifying the Scorer protocol signature.
    """
    for df in histories.values():
        if df is None or df.empty:
            continue
        try:
            return df.index.max().date()
        except Exception:  # noqa: BLE001
            continue
    return None


def _fundamentals_for_backtest(
    tickers: list[str],
    histories: Mapping[str, pd.DataFrame],
    config: Mapping,
) -> dict[str, dict]:
    """Pull PIT feature dicts from the HistoricalFundamentalsStore attached to
    config (CLI wiring adds it under `_fundamentals_store`). Returns {} when
    the store isn't wired or gate is disabled."""
    if not config.get("fundamental_gate_enabled", False):
        return {}
    store = config.get("_fundamentals_store")
    if store is None:
        return {}
    asof = _latest_asof(histories)
    if asof is None:
        return {}
    out: dict[str, dict] = {}
    for t in tickers:
        features = store.features_as_of(t, asof)
        if features:
            out[t] = features
    return out


def momentum_scorer_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:  # MIN_BARS_REQUIRED attached below at module scope

    """Call Layer 2b's MomentumScorer with the column rename + benchmark wiring.

    Config may override `benchmark` (default "SPY") and the seven metric weights.
    Returns a DataFrame with the full breakdown plus a single `score` column
    that the BacktestEngine consumes.
    """
    config = dict(config or {})
    merged_config = dict(THEMED_DEFAULTS)
    merged_config.update({k: v for k, v in config.items() if k in THEMED_DEFAULTS})

    benchmark_ticker = config.get("benchmark", merged_config.get("benchmark", "SPY"))
    prices: dict[str, pd.DataFrame] = {
        ticker: _to_capitalised(df) for ticker, df in histories.items()
    }

    if benchmark_ticker not in prices:
        bench_series = None
    else:
        bench_series = benchmark_ticker

    tickers = [t for t in prices.keys() if t != benchmark_ticker]
    fundamentals = _fundamentals_for_backtest(tickers, histories, config)

    scorer = MomentumScorer(merged_config)
    scored = scorer.score_all(
        tickers, prices, benchmark_ticker=bench_series, fundamentals=fundamentals,
    )
    if scored.empty:
        return pd.DataFrame(columns=["ticker", "score"])
    scored = scored.rename(columns={"momentum_score": "score"})
    return scored


def early_stage_scorer_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:  # MIN_BARS_REQUIRED attached below at module scope

    """Call EarlyStageScorer with the column rename (lowercase → capitalised).

    Unlike MomentumScorer, the early-stage scorer ignores the benchmark (no
    relative-strength metric in the 7-metric set), so benchmark wiring is a
    no-op here.
    """
    config = dict(config or {})
    merged_config = dict(EARLY_STAGE_DEFAULTS)
    merged_config.update({k: v for k, v in config.items() if k in EARLY_STAGE_DEFAULTS})

    prices: dict[str, pd.DataFrame] = {
        ticker: _to_capitalised(df) for ticker, df in histories.items()
    }

    benchmark_ticker = config.get("benchmark", merged_config.get("benchmark", "SPY"))
    tickers = [t for t in prices.keys() if t != benchmark_ticker]

    fundamentals = _fundamentals_for_backtest(tickers, histories, config)

    scorer = EarlyStageScorer(merged_config)
    scored = scorer.score_all(
        tickers, prices, benchmark_ticker=None, fundamentals=fundamentals,
    )
    if scored.empty:
        return pd.DataFrame(columns=["ticker", "score"])
    scored = scored.rename(columns={"early_stage_score": "score"})
    return scored


# Propagate scorer warmup requirements to the adapter functions so BacktestEngine
# (which receives the adapter, not the underlying scorer class) can getattr them.
momentum_scorer_adapter.MIN_BARS_REQUIRED = MomentumScorer.MIN_BARS_REQUIRED
early_stage_scorer_adapter.MIN_BARS_REQUIRED = EarlyStageScorer.MIN_BARS_REQUIRED
