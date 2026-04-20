"""Per-ticker rolling market characteristics for cost modelling.

Exposes effective spread (via EDGE), close-to-close volatility, and average
daily dollar volume (ADV) as point-in-time lookups. Populated once via
`prime(tickers, start, end)`; subsequent `*_at(ticker, asof_date)` queries
are O(1).

Backed by an upstream `HistoryStore`. Strict point-in-time contract — a
query at date `t` uses only bars with index <= `t` (i.e. pre-computation
aligns each characteristic to the as-of bar, not the centre of the rolling
window).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from .history_store import HistoryStore
from .spread_estimator import edge_spread


@dataclass
class _TickerSeries:
    spread: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    volatility: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    adv_dollar: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


class MarketCharacteristicsStore:
    """Pre-computed per-ticker rolling series used by the per-trade cost model."""

    def __init__(
        self,
        history_store: HistoryStore,
        spread_window: int = 21,
        vol_window: int = 20,
        adv_window: int = 20,
    ):
        self._history = history_store
        self._spread_window = int(spread_window)
        self._vol_window = int(vol_window)
        self._adv_window = int(adv_window)
        self._cache: dict[str, _TickerSeries] = {}

    def prime(self, tickers: Iterable[str], start: date, end: date) -> None:
        """Pre-compute rolling series for each ticker over the full history span.

        `start` and `end` are informational (current implementation computes
        for each ticker's entire bar history so queries anywhere in range
        work). They are reserved for future slicing optimisations.
        """
        _ = (start, end)  # reserved
        for ticker in tickers:
            up = ticker.upper()
            if up in self._cache:
                continue
            try:
                df = self._history.full(up)
            except KeyError:
                continue
            if df.empty:
                continue
            self._cache[up] = _TickerSeries(
                spread=self._compute_spread(df),
                volatility=self._compute_volatility(df),
                adv_dollar=self._compute_adv_dollar(df),
            )

    def _compute_spread(self, df: pd.DataFrame) -> pd.Series:
        return edge_spread(
            df["open"], df["high"], df["low"], df["close"],
            window=self._spread_window,
        )

    def _compute_volatility(self, df: pd.DataFrame) -> pd.Series:
        log_returns = np.log(df["close"].astype(float)).diff()
        return log_returns.rolling(window=self._vol_window, min_periods=self._vol_window).std()

    def _compute_adv_dollar(self, df: pd.DataFrame) -> pd.Series:
        dollar_vol = df["close"].astype(float) * df["volume"].astype(float)
        return dollar_vol.rolling(window=self._adv_window, min_periods=self._adv_window).mean()

    def spread_at(self, ticker: str, asof: date) -> float | None:
        return self._lookup(ticker, asof, attr="spread")

    def volatility_at(self, ticker: str, asof: date) -> float | None:
        return self._lookup(ticker, asof, attr="volatility")

    def adv_dollar_at(self, ticker: str, asof: date) -> float | None:
        return self._lookup(ticker, asof, attr="adv_dollar")

    def _lookup(self, ticker: str, asof: date, attr: str) -> float | None:
        series = self._cache.get(ticker.upper())
        if series is None:
            return None
        s: pd.Series = getattr(series, attr)
        if s.empty:
            return None
        ts = pd.Timestamp(asof)
        truncated = s.loc[:ts]
        if truncated.empty:
            return None
        value = truncated.iloc[-1]
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)
