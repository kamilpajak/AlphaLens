"""Unified news-item schema shared by Polygon / GDELT / RSS / EDGAR adapters."""

from __future__ import annotations

import pandas as pd

NEWS_COLUMNS = [
    "id",  # str, source-stable unique identifier
    "source",  # str, one of: polygon | gdelt | rss | edgar
    "timestamp",  # pd.Timestamp, UTC, tz-aware
    "tickers",  # list[str], filtered to input universe
    "title",  # str
    "body",  # str, may be empty
    "url",  # str
    "keywords",  # list[str], may be empty
    "extra",  # str, JSON-encoded source-specific payload (e.g. polygon insights)
]


def empty_news_frame() -> pd.DataFrame:
    """Return an empty DataFrame conforming to ``NEWS_COLUMNS``."""
    df = pd.DataFrame({col: pd.Series(dtype=object) for col in NEWS_COLUMNS})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df
