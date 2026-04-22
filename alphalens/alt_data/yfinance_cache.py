"""yfinance bulk-download cache for PIT price reconstruction.

Phase 2.5 price-side foundation: combine ``shares × close`` to derive PIT
market cap at each historical rebalance date. Survivorship-bias caveat
documented in ``docs/research/layer2d_alt_data_design.md`` §3 (R6 / R7):
yfinance has partial delisted-ticker coverage; net backtest bias ~100-
150 bps/y (to be evaluated under the R8 three-scenario sensitivity).

Cache is per-ticker parquet under ``cache_dir``. ``download_and_cache``
skips tickers already present on disk so the one-shot ~6h build is
resumable — rerun after a crash and only missing tickers refetch.

Fetcher is injected as a callable so tests don't hit Yahoo. Default
implementation uses ``yfinance.Ticker(t).history`` with
``auto_adjust=False`` (raw prices; split/div adjustments applied at
analysis time, not at fetch time).
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

FetcherFn = Callable[[str, date, date], pd.DataFrame]

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_YFINANCE_RENAME = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase OHLCV columns and drop extras (Dividends, Stock Splits).

    Raises ``KeyError`` if any required column is missing — a silent drop
    would mask upstream data corruption.
    """
    renamed = df.rename(columns=_YFINANCE_RENAME)
    missing = [c for c in _OHLCV_COLUMNS if c not in renamed.columns]
    if missing:
        raise KeyError(f"yfinance DataFrame missing OHLCV columns: {missing}")
    return renamed[list(_OHLCV_COLUMNS)]


def _default_yfinance_fetcher(ticker: str, start: date, end: date) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.Ticker(ticker).history(
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=False,
    )
    if raw.empty:
        return raw
    return _normalize_ohlcv(raw)


def download_and_cache(
    tickers: list[str],
    start: date,
    end: date,
    cache_dir: Path,
    *,
    fetcher: FetcherFn | None = None,
    sleep_between: float = 0.5,
) -> int:
    """Fetch missing tickers into ``cache_dir`` as parquet. Returns new-count.

    Skips tickers whose parquet already exists — allows the multi-hour
    universe build to resume after crashes. Fetch failures are logged
    (WARNING) and skipped, not propagated.
    """
    fetch = fetcher or _default_yfinance_fetcher
    cache_dir.mkdir(parents=True, exist_ok=True)

    new_count = 0
    for ticker in tickers:
        path = cache_dir / f"{ticker.upper()}.parquet"
        if path.exists():
            continue
        try:
            df = fetch(ticker, start, end)
        except Exception as exc:  # noqa: BLE001 — log-and-continue on any fetch error
            logger.warning("yfinance fetch %s failed: %s", ticker, exc)
            continue
        if df is None or df.empty:
            logger.info("yfinance returned empty frame for %s (likely delisted)", ticker)
            continue
        df.to_parquet(path)
        new_count += 1
        if sleep_between > 0:
            time.sleep(sleep_between)
    return new_count


def load_cached_histories(
    tickers: list[str], cache_dir: Path
) -> dict[str, pd.DataFrame]:
    """Load pre-cached parquets into a dict compatible with ``HistoryStore``.

    Tickers without a parquet on disk are silently skipped — caller's
    responsibility to ensure coverage before backtest.
    """
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        path = cache_dir / f"{ticker.upper()}.parquet"
        if path.exists():
            out[ticker] = pd.read_parquet(path)
    return out
