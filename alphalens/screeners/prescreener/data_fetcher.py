"""Batch data fetching for the pre-screener."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

from .config import PRESCREENER_DEFAULTS

logger = logging.getLogger(__name__)


class BatchDataFetcher:
    def __init__(self, tickers: list[str], curr_date: str, config: dict | None = None):
        self.tickers = tickers
        self.curr_date = curr_date
        self.config = config or PRESCREENER_DEFAULTS
        self._price_cache: dict[str, pd.DataFrame] | None = None
        self._fundamentals_cache: dict[str, dict] | None = None

    def fetch_prices(self) -> dict[str, pd.DataFrame]:
        """Batch-download OHLCV for all tickers.

        Returns {ticker: DataFrame} with look-ahead bias prevention.
        """
        if self._price_cache is not None:
            return self._price_cache

        batch_size = self.config["batch_size"]
        lookback = self.config["price_lookback_days"]
        end_date = pd.Timestamp(self.curr_date)
        start_date = end_date - pd.Timedelta(days=int(lookback * 1.5))

        all_data: dict[str, pd.DataFrame] = {}

        for i in range(0, len(self.tickers), batch_size):
            chunk = self.tickers[i : i + batch_size]
            try:
                raw = yf.download(
                    " ".join(chunk),
                    start=start_date.strftime("%Y-%m-%d"),
                    end=(end_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    progress=False,
                    threads=True,
                    auto_adjust=True,
                )
                if raw.empty:
                    continue

                if len(chunk) == 1:
                    ticker = chunk[0]
                    # yfinance may return MultiIndex columns even for 1 ticker
                    if isinstance(raw.columns, pd.MultiIndex):
                        raw = raw.droplevel("Ticker", axis=1)
                    filtered = raw[raw.index <= end_date]
                    if not filtered.empty:
                        all_data[ticker] = filtered
                else:
                    for ticker in chunk:
                        try:
                            ticker_df = raw.xs(ticker, level=1, axis=1)
                            filtered = ticker_df[ticker_df.index <= end_date].dropna(
                                subset=["Close"]
                            )
                            if not filtered.empty:
                                all_data[ticker] = filtered
                        except (KeyError, ValueError):
                            continue
            except Exception:
                logger.warning("Batch download failed for chunk %d", i, exc_info=True)

        self._price_cache = all_data
        return all_data

    def fetch_fundamentals(self) -> dict[str, dict]:
        """Fetch .info for each ticker with threading.

        Returns {ticker: {field: value}}.
        """
        if self._fundamentals_cache is not None:
            return self._fundamentals_cache

        result: dict[str, dict] = {}

        def _fetch_one(ticker: str) -> tuple[str, dict]:
            try:
                info = yf.Ticker(ticker).info or {}
                return ticker, info
            except Exception:
                logger.debug("Failed to fetch info for %s", ticker)
                return ticker, {}

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_one, t): t for t in self.tickers}
            for future in as_completed(futures):
                try:
                    ticker, info = future.result()
                    result[ticker] = info
                except Exception:
                    ticker = futures[future]
                    result[ticker] = {}

        self._fundamentals_cache = result
        return result
