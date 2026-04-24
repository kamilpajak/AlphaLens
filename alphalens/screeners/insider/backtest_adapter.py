"""Adapter bridging the Layer 2d insider scorer into BacktestEngine.

The engine expects a Scorer conforming to::

    Scorer = Callable[[Mapping[str, pd.DataFrame], Mapping], pd.DataFrame]

and returning a frame with at least ``ticker`` and ``score`` columns. Unlike
the Layer 2b scorers, Layer 2d does not consume OHLCV prices — the signal
comes from SEC EDGAR Form 4 filings. The histories dict is still useful for
two things: inferring the current rebalance date (``asof = max(index)``)
and enumerating the universe the engine wants scored at this step.

Callers wire a ready-to-query ``InsiderScorer`` (or any object exposing
``features_as_of(ticker, asof)``) via ``config['_insider_store']``. The
adapter raises ``KeyError`` when the store is missing rather than silently
returning an empty frame — that's a CLI wiring bug and should fail loud.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping

import pandas as pd


_OUTPUT_COLUMNS = ["ticker", "score", "insider_count", "aggregate_dollar"]


def _latest_asof(histories: Mapping[str, pd.DataFrame]) -> date | None:
    dates: list[date] = []
    for df in histories.values():
        if df is None or df.empty:
            continue
        try:
            dates.append(df.index.max().date())
        except Exception:  # noqa: BLE001
            continue
    return max(dates) if dates else None


def insider_scorer_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:
    config = dict(config or {})
    if "_insider_store" not in config:
        raise KeyError(
            "insider_scorer_adapter requires config['_insider_store'] — "
            "wire an InsiderScorer instance via CLI backtest setup"
        )
    store = config["_insider_store"]

    asof = _latest_asof(histories)
    if asof is None:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    benchmark = config.get("benchmark")

    rows: list[dict] = []
    for ticker in histories:
        if ticker == benchmark:
            continue
        feat = store.features_as_of(ticker, asof)
        if not feat:
            continue
        rows.append(
            {
                "ticker": ticker,
                "score": float(feat["insider_count"]),
                "insider_count": feat["insider_count"],
                "aggregate_dollar": feat["aggregate_dollar"],
            }
        )

    if not rows:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)
    return (
        pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)
        .sort_values("score", ascending=False)
        .reset_index(drop=True)
    )


# Signal derives from EDGAR filings, not price history — no warmup bars needed.
insider_scorer_adapter.MIN_BARS_REQUIRED = 0
