"""Layer 2d live/ad-hoc pipeline: scan a universe, emit cluster candidates.

Where the backtest adapter (:mod:`alphalens.archive.screeners.insider.backtest_adapter`)
scores a universe supplied by the engine as OHLCV histories, this pipeline is
the live counterpart invoked from the CLI and launchd. Universe comes from the
injected loader (typically :func:`alphalens.alt_data.russell_universe.load_iwm_current`),
not from price histories.

Mirrors the themed pipeline contract: ``run(curr_date, top_n) -> DataFrame`` +
``to_candidates(df, weighting) -> list[Candidate]``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Protocol

import pandas as pd

from alphalens.candidates import Candidate

logger = logging.getLogger(__name__)


class _ScorerProto(Protocol):
    def features_as_of(self, ticker: str, asof: date) -> dict | None: ...


_OUTPUT_COLUMNS = ["ticker", "insider_count", "aggregate_dollar", "asof"]


class InsiderPipeline:
    def __init__(
        self,
        scorer: _ScorerProto,
        universe_loader: Callable[[], list[str]],
        source_name: str = "insider",
    ):
        self._scorer = scorer
        self._universe_loader = universe_loader
        self.source_name = source_name

    def run(self, curr_date: date, top_n: int = 10) -> pd.DataFrame:
        tickers = self._universe_loader()
        if not tickers:
            return pd.DataFrame(columns=_OUTPUT_COLUMNS)

        rows: list[dict] = []
        total = len(tickers)
        log_every = max(1, total // 20)  # ~5% progress granularity
        for idx, ticker in enumerate(tickers, start=1):
            feat = self._scorer.features_as_of(ticker, curr_date)
            if feat:
                rows.append(
                    {
                        "ticker": ticker,
                        "insider_count": feat["insider_count"],
                        "aggregate_dollar": feat["aggregate_dollar"],
                        "asof": feat.get("asof", curr_date.isoformat()),
                    }
                )
            if idx % log_every == 0 or idx == total:
                logger.info(
                    "insider scan %d/%d (%.0f%%) — %d clusters so far",
                    idx,
                    total,
                    idx / total * 100,
                    len(rows),
                )

        if not rows:
            return pd.DataFrame(columns=_OUTPUT_COLUMNS)
        return (
            pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)
            .sort_values("insider_count", ascending=False)
            .head(top_n)
            .reset_index(drop=True)
        )

    def to_candidates(self, df: pd.DataFrame, weighting: str = "linear") -> list[Candidate]:
        if df.empty:
            return []

        try:
            from alphalens.backtest.weighting import compute_position_weights

            weights = compute_position_weights(len(df), weighting).tolist()
        except (ImportError, ValueError):
            weights = [1.0 / len(df)] * len(df)

        now = datetime.now(UTC)
        discriminator = now.date().isoformat()

        return [
            Candidate.from_screener(
                ticker=row["ticker"],
                source=self.source_name,
                priority=12,
                payload={
                    "insider_count": int(row["insider_count"]),
                    "aggregate_dollar": float(row["aggregate_dollar"]),
                    "weight": float(weights[idx]),
                    "weighting_scheme": weighting,
                    "scorer": self.source_name,
                },
                discriminator=discriminator,
                detected_at=now,
            )
            for idx, (_, row) in enumerate(df.iterrows())
        ]
