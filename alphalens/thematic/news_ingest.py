"""Daily unified news ingest — orchestrates Polygon / GDELT / RSS / EDGAR.

Output: one parquet per day at ``~/.alphalens/thematic_news/{YYYY-MM-DD}.parquet``
conforming to the shared ``NEWS_COLUMNS`` schema, capped at ``max_items``
per the §14 lock-7 LLM-budget envelope (~200 items/day → Gemini Flash free
tier).
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from alphalens.thematic.sources import edgar_adapter, gdelt, polygon_news, rss
from alphalens.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news"
DEFAULT_MAX_ITEMS = 200

# Lower number = higher priority when deduping by URL. Polygon wins because it
# tags tickers natively and exposes sentiment insights; other sources rely on
# the downstream LLM to tag.
_SOURCE_PRIORITY = {"polygon": 0, "edgar": 1, "gdelt": 2, "rss": 3}


def _fetch_polygon(*, date: dt.date, api_key: str) -> pd.DataFrame:
    return polygon_news.fetch_daily_news(date=date, api_key=api_key)


def _fetch_gdelt(*, date: dt.date) -> pd.DataFrame:
    return gdelt.fetch_daily_news(date=date)


def _fetch_rss(*, date: dt.date) -> pd.DataFrame:
    return rss.fetch_daily_news(date=date)


def _fetch_edgar(*, date: dt.date) -> pd.DataFrame:
    return edgar_adapter.fetch_daily_news(date=date)


def _safe_call(name: str, fn, **kwargs) -> pd.DataFrame:
    try:
        return fn(**kwargs)
    except Exception as exc:
        logger.warning("thematic ingest source %s failed: %s", name, exc, exc_info=True)
        return empty_news_frame()


def _canonical_url(url: str) -> str:
    """Drop query string + fragment + trailing slash so cross-source dedup matches.

    Tracking params (``?utm_source=...``, ``?ref=feed``) on syndicated articles
    routinely differ between Polygon, GDELT and RSS feeds, defeating exact-URL
    dedup. Canonicalising lets the same article collapse to one row.
    """
    base = url.split("#", 1)[0].split("?", 1)[0]
    return base.rstrip("/")


def ingest_daily(
    *,
    date: dt.date,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_items: int = DEFAULT_MAX_ITEMS,
    polygon_api_key: str | None = None,
    force: bool = False,
) -> pd.DataFrame:
    """Aggregate all four sources for one UTC day, dedupe, cap, persist, return."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)

    polygon_df = _safe_call("polygon", _fetch_polygon, date=date, api_key=polygon_api_key or "")
    gdelt_df = _safe_call("gdelt", _fetch_gdelt, date=date)
    rss_df = _safe_call("rss", _fetch_rss, date=date)
    edgar_df = _safe_call("edgar", _fetch_edgar, date=date)

    frames = [df for df in (polygon_df, gdelt_df, rss_df, edgar_df) if len(df) > 0]
    if not frames:
        merged = empty_news_frame()
    else:
        merged = pd.concat(frames, ignore_index=True)
        merged["_source_rank"] = merged["source"].map(_SOURCE_PRIORITY).fillna(99).astype(int)
        merged["_url_canon"] = merged["url"].map(_canonical_url)
        merged = (
            merged.sort_values(["_url_canon", "_source_rank"])
            .drop_duplicates(subset=["_url_canon"], keep="first")
            .drop(columns=["_source_rank", "_url_canon"])
            .sort_values("timestamp", ascending=False)
            .head(max_items)
            .reset_index(drop=True)
        )

    merged = merged[NEWS_COLUMNS]
    merged.to_parquet(cache_path, index=False)
    return merged
