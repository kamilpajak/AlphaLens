"""Data loader for Tactical Sector Rotation: yfinance ETFs + FRED macro.

Isolated behind a single `load_rotation_data()` function so the CLI and tests
can patch it at one point. Production callers hit yfinance + FRED; tests
substitute pre-built HistoryStore + SignalSet.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from alphalens.backtest.history_store import HistoryStore
from alphalens.macro.fred_client import FREDClient
from alphalens.macro.signals import SignalSet, build_signal_set

ETF_TICKERS = ("SPY", "QQQ", "IWM")


def load_rotation_data(
    *,
    start: pd.Timestamp | str,
    end: pd.Timestamp | str,
    cache_dir: Path | None = None,
) -> tuple[HistoryStore, SignalSet]:
    """Load ETF OHLCV + macro series in the range [start, end].

    Uses yfinance (daily adjusted OHLCV) and FREDClient (DGS10, DGS2, VIXCLS).
    Requires FRED_API_KEY in env. Disk cache at ~/.alphalens/macro/.
    """
    import yfinance as yf

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    histories: dict[str, pd.DataFrame] = {}
    for ticker in ETF_TICKERS:
        df = yf.download(
            ticker,
            start=start_ts,
            end=end_ts + pd.Timedelta(days=1),
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            raise RuntimeError(f"yfinance returned no data for {ticker}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)
        histories[ticker] = df[["open", "high", "low", "close", "volume"]]
    store = HistoryStore(histories)

    fred = FREDClient.from_env(cache_dir=cache_dir)
    dgs10 = fred.fetch_series("DGS10")
    dgs2 = fred.fetch_series("DGS2")
    vix = fred.fetch_series("VIXCLS")

    # Trim macro series to the backtest window
    mask = lambda s: s.loc[(s.index >= start_ts) & (s.index <= end_ts)]
    signals = build_signal_set(
        dgs10=mask(dgs10),
        dgs2=mask(dgs2),
        vix=mask(vix),
        qqq_close=store.full("QQQ")["close"],
        iwm_close=store.full("IWM")["close"],
    )
    return store, signals
