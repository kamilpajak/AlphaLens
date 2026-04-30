"""LeanScreenerPipeline — the `registry.SCREENERS["lean"]` entry point.

`run()` orchestrates the daily path: Polygon sync → Lean Docker run → DataFrame.
`to_candidates(df)` converts the ranked frame into queue-ready `Candidate`s.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from alphalens.core.candidates import Candidate

from .config import LEAN_DEFAULTS
from .runner import LeanDockerRunner
from .schema import LeanOutput

logger = logging.getLogger(__name__)


def lean_output_to_dataframe(output: LeanOutput) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in output.rankings:
        rows.append(
            {
                "ticker": row.ticker,
                "rank": row.rank,
                "score": row.score,
                "roc5": row.roc5,
                "roc20": row.roc20,
                "roc60": row.roc60,
                "volume_surprise": row.volume_surprise,
                "trend_strength": row.trend_strength,
                "breakout": row.breakout,
                "near_high": row.near_high,
                "last_close": row.last_close,
                "avg_dollar_volume": row.avg_dollar_volume,
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "ticker",
                "rank",
                "score",
                "roc5",
                "roc20",
                "roc60",
                "volume_surprise",
                "trend_strength",
                "breakout",
                "near_high",
                "last_close",
                "avg_dollar_volume",
            ]
        )
    return pd.DataFrame(rows)


class LeanScreenerPipeline:
    def __init__(
        self,
        config: dict | None = None,
        sync=None,  # PolygonLeanSync | None
        runner: LeanDockerRunner | None = None,
    ):
        self.config = config or LEAN_DEFAULTS
        self._sync = sync
        self._runner = runner

    def run(
        self,
        *,
        today: date | None = None,
        top_n: int | None = None,
    ) -> pd.DataFrame:
        if self._sync is not None:
            today = today or datetime.now(UTC).date()
            report = self._sync.incremental_sync(
                today=today,
                bootstrap_days=self.config["history_bootstrap_days"],
            )
            logger.info(
                "polygon sync: dates=%d tickers=%d bars=%d",
                len(report.dates_synced),
                report.tickers_written,
                report.bars_written,
            )

        if self._runner is None:
            raise RuntimeError("LeanScreenerPipeline needs a runner to produce a DataFrame")

        output = self._runner.run()
        df = lean_output_to_dataframe(output)
        n = top_n if top_n is not None else self.config["top_n"]
        return df.head(n)

    def to_candidates(self, df: pd.DataFrame) -> list[Candidate]:
        if df.empty:
            return []
        now = datetime.now(UTC)
        discriminator = now.date().isoformat()
        return [
            Candidate.from_screener(
                ticker=row["ticker"],
                source="lean",
                priority=15,
                payload={
                    "score": float(row["score"]),
                    "rank": int(row["rank"]),
                    "roc20": float(row["roc20"]),
                    "roc60": float(row["roc60"]),
                    "volume_surprise": float(row["volume_surprise"]),
                    "trend_strength": float(row["trend_strength"]),
                    "breakout": bool(row["breakout"]),
                    "near_high": float(row["near_high"]),
                    "last_close": float(row["last_close"]),
                },
                discriminator=discriminator,
                detected_at=now,
            )
            for _, row in df.iterrows()
        ]
