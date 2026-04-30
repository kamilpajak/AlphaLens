"""Load OHLCV histories from Lean's zip-CSV equity-daily store.

Reads every zip under `<data_dir>/equity/usa/daily/` into a
`dict[ticker, pd.DataFrame]` with OHLCV columns and a `DatetimeIndex`.

The result is meant to be fed into `alphalens.backtest.HistoryStore`, which
knows nothing about Lean's data layout. Keeping loader + store separate means
a future backtest over, say, Polygon parquet or a pandas pickle can plug in
its own loader without touching the generic harness.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from .lean_csv_writer import LeanCsvWriter

logger = logging.getLogger(__name__)


def _bars_to_dataframe(bars: list) -> pd.DataFrame:
    """Convert list of `DailyBar` to a `DatetimeIndex` OHLCV DataFrame."""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        },
        index=pd.to_datetime([b.date for b in bars], format="%Y%m%d"),
    )
    return df.sort_index()


def load_lean_histories(data_dir: Path, tickers: Iterable[str]) -> dict[str, pd.DataFrame]:
    """Eagerly read zipped OHLCV bars for `tickers` from the Lean CSV store.

    Missing zips are skipped silently (delisted tickers with no bars, etc.).
    Tickers are upper-cased in the returned dict keys.
    """
    writer = LeanCsvWriter(data_dir)
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        up = ticker.upper()
        if up in out:
            continue
        bars = writer.read_bars(up)
        if bars:
            out[up] = _bars_to_dataframe(bars)
        else:
            logger.debug("lean_csv_loader: no bars for %s", up)
    return out
