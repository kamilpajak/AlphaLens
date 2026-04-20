"""Pulls tick-level trades from Polygon and caches them as parquet files."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ..screeners.lean.polygon_client import PolygonClient, Trade

logger = logging.getLogger(__name__)


_SCHEMA_COLUMNS = (
    "sip_timestamp_ns",
    "price",
    "size",
    "conditions",
    "exchange",
    "trf_id",
)


class TickLoader:
    """Fetches Polygon v3/trades and writes one parquet per ticker-day.

    `cache_dir` is typically `~/.alphalens/tick_samples/`.
    """

    def __init__(self, client: PolygonClient, cache_dir: Path):
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, ticker: str, trade_date: date) -> Path:
        return self._cache_dir / ticker.upper() / f"{trade_date.isoformat()}.parquet"

    def fetch_day(self, ticker: str, trade_date: date, force: bool = False) -> Path:
        """Fetch one ticker-day. Idempotent unless force=True."""
        path = self.path_for(ticker, trade_date)
        if path.exists() and not force:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)

        trades = self._client.trades(ticker, trade_date)
        df = _trades_to_frame(trades)
        df.to_parquet(path, index=False)
        logger.debug("wrote %d trades for %s %s -> %s", len(df), ticker, trade_date, path)
        return path

    def fetch_range(
        self, ticker: str, start: date, end: date, force: bool = False
    ) -> list[Path]:
        """Fetch every weekday in [start, end] (inclusive). Skips Sat/Sun.

        Does NOT consult a trading-day calendar — weekend rejection only.
        Market holidays are fetched as empty frames (Polygon returns no rows).
        """
        paths: list[Path] = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                paths.append(self.fetch_day(ticker, cursor, force=force))
            cursor += timedelta(days=1)
        return paths


def _trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame({col: [] for col in _SCHEMA_COLUMNS}).astype(
            {
                "sip_timestamp_ns": "int64",
                "price": "float64",
                "size": "int64",
                "exchange": "int64",
                # trf_id and conditions handled specially below.
            }
        )
    rows = [
        {
            "sip_timestamp_ns": t.sip_timestamp_ns,
            "price": t.price,
            "size": t.size,
            "conditions": t.conditions,
            "exchange": t.exchange,
            "trf_id": t.trf_id,
        }
        for t in trades
    ]
    df = pd.DataFrame(rows, columns=list(_SCHEMA_COLUMNS))
    return df
