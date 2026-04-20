"""Reads cached tick parquet files and serves per-day aggregation queries."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd


_SCHEMA_COLUMNS = (
    "sip_timestamp_ns",
    "price",
    "size",
    "conditions",
    "exchange",
    "trf_id",
)


class TickStore:
    """Read-only view over the tick cache written by `TickLoader`."""

    def __init__(self, cache_dir: Path):
        self._cache_dir = Path(cache_dir)

    def path_for(self, ticker: str, trade_date: date) -> Path:
        return self._cache_dir / ticker.upper() / f"{trade_date.isoformat()}.parquet"

    def get_trades(self, ticker: str, trade_date: date) -> pd.DataFrame:
        path = self.path_for(ticker, trade_date)
        if not path.exists():
            return _empty_frame()
        df = pd.read_parquet(path)
        # Ensure schema columns always present (empty frames written without
        # some nullable columns round-trip through pyarrow oddly).
        for col in _SCHEMA_COLUMNS:
            if col not in df.columns:
                df[col] = pd.Series([], dtype="object")
        return df

    def dark_pool_trades(self, ticker: str, trade_date: date) -> pd.DataFrame:
        """Return only prints where `trf_id` is populated (off-exchange / TRF)."""
        df = self.get_trades(ticker, trade_date)
        if df.empty:
            return df
        return df[df["trf_id"].notna()].reset_index(drop=True)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({col: [] for col in _SCHEMA_COLUMNS})
