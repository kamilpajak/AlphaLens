"""In-memory OHLCV store for backtesting.

Takes a pre-loaded mapping of ticker → `DatetimeIndex` OHLCV DataFrame and
serves point-in-time truncated slices and forward-return lookups without
hitting disk. The backtest loop calls `.truncate_to(ticker, asof)` thousands
of times, so avoiding I/O per call is critical for calibration speed.

Loading is the caller's responsibility (see
`alphalens.lean_screener.lean_csv_loader.load_lean_histories` for the Lean
zip-CSV implementation). Keeping I/O out of this class means the generic
harness has no knowledge of the Lean data layout.

All DataFrames must be sorted ascending by date and use a `DatetimeIndex`.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping

import pandas as pd


class HistoryStore:
    def __init__(self, histories: Mapping[str, pd.DataFrame]):
        self._cache: dict[str, pd.DataFrame] = {
            ticker.upper(): df for ticker, df in histories.items()
        }

    def tickers(self) -> list[str]:
        return sorted(self._cache.keys())

    def full(self, ticker: str) -> pd.DataFrame:
        """Full OHLCV history for `ticker`. Raises KeyError if unknown."""
        up = ticker.upper()
        if up not in self._cache:
            raise KeyError(f"no history cached for {ticker!r}")
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
