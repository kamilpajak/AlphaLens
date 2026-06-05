"""Unified news-item schema shared by Polygon / GDELT / RSS / EDGAR press-release adapters."""

from __future__ import annotations

import pandas as pd

NEWS_COLUMNS = [
    "id",  # str, source-stable unique identifier
    "source",  # str, one of: polygon | gdelt | rss | edgar_press_release
    "timestamp",  # pd.Timestamp, UTC, tz-aware — VALID-time (when the news was public)
    "tickers",  # list[str], filtered to input universe
    "title",  # str
    "body",  # str, may be empty
    "url",  # str
    "keywords",  # list[str], may be empty
    "extra",  # str, JSON-encoded source-specific payload (e.g. polygon insights)
    # TRANSACTION-time (P1b bitemporal lake): when ``ingest_daily`` recorded this
    # item into the canonical lake. Appended at the END so the existing column
    # offsets are unchanged. Stamped at the lake-entry point (ingest_daily), NOT
    # per source; every row of one ingest run shares the run's value. The
    # current-view ``{D}.parquet`` carries the last build's value (overwrite); the
    # P1c-raw append-only lake preserves every run's RAW union under its own value.
    "ingested_at",  # pd.Timestamp, UTC, tz-aware
]


def empty_news_frame() -> pd.DataFrame:
    """Return an empty DataFrame conforming to ``NEWS_COLUMNS``."""
    df = pd.DataFrame({col: pd.Series(dtype=object) for col in NEWS_COLUMNS})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    return df
