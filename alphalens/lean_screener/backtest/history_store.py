"""In-memory OHLCV store for backtesting.

Loads every zip under `<data_dir>/equity/usa/daily/` once into a
`dict[ticker, pd.DataFrame]`, then serves point-in-time truncated slices and
forward-return lookups without hitting disk again. The backtest loop calls
`.truncate_to(ticker, asof)` thousands of times, so avoiding I/O per call is
critical for calibration speed.

All DataFrames are sorted ascending by date and use a `DatetimeIndex`.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from ..lean_csv_writer import LeanCsvWriter

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
    df = df.sort_index()
    return df


class HistoryStore:
    def __init__(self, data_dir: Path):
        self._writer = LeanCsvWriter(data_dir)
        self._cache: dict[str, pd.DataFrame] = {}

    def load(self, tickers: Iterable[str]) -> None:
        """Eagerly populate the in-memory cache for the given tickers.

        Missing zips are skipped silently (delisted tickers with no bars, etc.).
        """
        for ticker in tickers:
            up = ticker.upper()
            if up in self._cache:
                continue
            bars = self._writer.read_bars(up)
            if bars:
                self._cache[up] = _bars_to_dataframe(bars)
            else:
                logger.debug("history_store: no bars for %s", up)

    def tickers(self) -> list[str]:
        return sorted(self._cache.keys())

    def full(self, ticker: str) -> pd.DataFrame:
        """Full OHLCV history for `ticker`. Raises KeyError if unknown."""
        up = ticker.upper()
        if up not in self._cache:
            raise KeyError(f"no history cached for {ticker!r} — did you call load()?")
        return self._cache[up]

    def truncate_to(self, ticker: str, asof: date) -> pd.DataFrame:
        """Return OHLCV with index <= asof (inclusive). Point-in-time contract.

        Used by the backtest loop to avoid leaking future bars into scoring.
        Returns empty DataFrame if ticker is unknown or has no bars before asof.
        """
        up = ticker.upper()
        df = self._cache.get(up)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.loc[:pd.Timestamp(asof)]

    def forward_return(
        self, ticker: str, entry_date: date, holding_period: int
    ) -> float | None:
        """Forward return from entry at next-close after `entry_date` to exit
        `holding_period` bars later.

        Convention: a screener ranks on EOD bar `entry_date`; we enter at the
        NEXT trading day's close (earliest realistic fill) and exit at the close
        `holding_period` bars later. Returns None if there's not enough history
        (e.g. ticker got delisted mid-holding).
        """
        up = ticker.upper()
        df = self._cache.get(up)
        if df is None or df.empty:
            return None
        ts_asof = pd.Timestamp(entry_date)
        # Find the first trading day strictly after asof.
        future = df.loc[df.index > ts_asof]
        if len(future) < holding_period + 1:
            return None
        entry_price = float(future.iloc[0]["close"])
        exit_price = float(future.iloc[holding_period]["close"])
        if entry_price == 0.0:
            return None
        return exit_price / entry_price - 1.0

    def trading_days_between(
        self, ticker: str, start: date, end: date
    ) -> list[pd.Timestamp]:
        """Trading days for `ticker` in [start, end] inclusive.

        Uses a specific ticker's index as the trading-day proxy. Pass a
        known-complete benchmark (e.g. SPY) for the authoritative calendar.
        """
        up = ticker.upper()
        df = self._cache.get(up)
        if df is None:
            return []
        mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
        return list(df.loc[mask].index)

    @staticmethod
    def benchmark_calendar(
        store: "HistoryStore", benchmark: str, start: date, end: date
    ) -> list[pd.Timestamp]:
        """Canonical trading calendar derived from a benchmark ticker's bars.

        Convenience wrapper so backtest callers don't have to remember which
        ticker is reliable for calendar use (e.g. SPY covers 1998+).
        """
        return store.trading_days_between(benchmark, start, end)
