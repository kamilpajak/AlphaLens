"""BacktestEngine adapter for the EV/FCFF-yield scorer.

The engine expects ``Callable[[Mapping[str, pd.DataFrame], Mapping], pd.DataFrame]``
(see ``alphalens_research.backtest.engine.Scorer``). This module wraps the pure
``score_ev_fcff_yield`` primitive together with an ``EdgarFundamentalsStore``
into an instance whose ``__call__`` matches that protocol.

The scorer derives ``asof`` from the maximum trailing-index across the
histories dict — same convention as the compound insider/P/C adapter and
the v9D options-implied adapter (see ``scripts/experiment_insider_pc_compound.py::_CompoundInsiderPcScorer``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import pandas as pd
from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore
from alphalens_pipeline.scorers.fcff_yield import score_ev_fcff_yield

logger = logging.getLogger(__name__)


class EvFcffYieldScorer:
    """Adapter — composes EDGAR fundamentals + pure scorer for one rebalance."""

    # FCFF / EV ranking doesn't need any price history bars to score (the
    # market-cap input comes from EDGAR-store + yfinance snapshot prices,
    # not from the engine's yfinance histories). 1 satisfies the engine's
    # "histories must be non-empty" precondition while not gating tickers
    # on warm-up bars.
    MIN_BARS_REQUIRED = 1

    def __init__(self, fundamentals_store: EdgarFundamentalsStore):
        self._store = fundamentals_store

    def __call__(
        self,
        histories: Mapping[str, pd.DataFrame],
        config: Mapping | None = None,
    ) -> pd.DataFrame:
        cfg = dict(config or {})
        asof = cfg.get("asof") or _derive_asof(histories)
        if asof is None:
            return pd.DataFrame(columns=["ticker", "score"])
        asof_date = asof.date() if hasattr(asof, "date") else asof

        snapshots: dict[str, dict] = {}
        for ticker in histories:
            snap = self._store.ev_fcff_features_as_of(ticker, asof_date)
            if snap is not None:
                snapshots[ticker] = snap

        if not snapshots:
            logger.debug(
                "No EDGAR snapshots for asof %s across %d tickers", asof_date, len(histories)
            )
            return pd.DataFrame(columns=["ticker", "score"])

        scores = score_ev_fcff_yield(snapshots)
        if scores.empty:
            return pd.DataFrame(columns=["ticker", "score"])

        return pd.DataFrame({"ticker": scores.index.tolist(), "score": scores.values.astype(float)})


def _derive_asof(histories: Mapping[str, pd.DataFrame]) -> pd.Timestamp | None:
    """Pick the latest index timestamp across non-empty histories."""
    latest: pd.Timestamp | None = None
    for df in histories.values():
        if df is None or len(df) == 0:
            continue
        candidate = df.index[-1]
        if latest is None or candidate > latest:
            latest = candidate
    return latest
