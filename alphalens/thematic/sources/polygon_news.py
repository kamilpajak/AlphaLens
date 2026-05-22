"""Polygon /v2/reference/news client for the thematic tool.

Pulls news for a UTC date range, filters tickers to the thematic input universe,
and normalises to the unified ``NEWS_COLUMNS`` schema. Output cache lives at
``~/.alphalens/thematic_news/polygon/{YYYY-MM-DD}.parquet``.

HTTP, pagination, rate limiting, and 429 / Retry-After handling are owned by
the canonical :class:`alphalens.data.alt_data.polygon_client.PolygonClient` —
this module is now a thin domain wrapper that adds the per-day parquet cache
and the universe-filter transform on top of ``client.get_news_range()``.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from alphalens.data.alt_data.polygon_client import PolygonClient, get_default_polygon_client
from alphalens.thematic.config.universe import load_input_universe
from alphalens.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news" / "polygon"
DEFAULT_PAGE_LIMIT = 1000  # Polygon hard max
# Daily firehose on a heavy market day exceeds the previous 1000 ceiling; with
# the canonical client now enforcing a strict ``max_items`` cap, that ceiling
# silently dropped late-day articles when ``order=asc``. 50k comfortably covers
# the busiest observed sessions (~3-5k items) with margin.
DEFAULT_MAX_ITEMS = 50000


def fetch_news_range(
    *,
    start: dt.datetime,
    end: dt.datetime,
    client: PolygonClient | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    page_limit: int = DEFAULT_PAGE_LIMIT,
) -> list[dict]:
    """Fetch raw Polygon news items for ``[start, end)``, paginating until exhausted.

    Delegates HTTP + pagination + rate-limit + retry to the canonical client.
    Pass ``client=None`` to use the process-wide default
    (:func:`get_default_polygon_client`).
    """
    if client is None:
        client = get_default_polygon_client()
    return client.get_news_range(
        start=start,
        end=end,
        order="asc",
        limit=page_limit,
        max_items=max_items,
    )


def transform(raw_items: Iterable[dict], *, universe: Iterable[str]) -> pd.DataFrame:
    """Normalise Polygon response rows to ``NEWS_COLUMNS`` schema.

    Filters out items whose ``tickers`` field has no intersection with ``universe``.
    Per-item ``tickers`` are restricted to the universe overlap (off-universe
    co-mentioned names are dropped).
    """
    universe_set = {t.upper() for t in universe}
    rows: list[dict] = []
    for item in raw_items:
        raw_tickers = [t.upper() for t in (item.get("tickers") or [])]
        kept = [t for t in raw_tickers if t in universe_set]
        if not kept:
            continue
        extra = {
            "publisher": (item.get("publisher") or {}).get("name"),
            "author": item.get("author"),
            "image_url": item.get("image_url"),
            "insights": item.get("insights") or [],
        }
        rows.append(
            {
                "id": item.get("id"),
                "source": "polygon",
                "timestamp": pd.Timestamp(item.get("published_utc")),
                "tickers": kept,
                "title": item.get("title") or "",
                "body": item.get("description") or "",
                "url": item.get("article_url") or "",
                "keywords": list(item.get("keywords") or []),
                "extra": json.dumps(extra, ensure_ascii=False),
            }
        )

    if not rows:
        return empty_news_frame()

    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def fetch_daily_news(
    *,
    date: dt.date,
    client: PolygonClient | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    universe: Iterable[str] | None = None,
    force: bool = False,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> pd.DataFrame:
    """Fetch and cache one day's news, returning the normalised frame.

    Read-through cache: returns the parquet immediately if it exists and
    ``force=False``. Otherwise, pulls the full day from Polygon, normalises,
    writes parquet, then returns.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)

    start = dt.datetime.combine(date, dt.time.min, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=1)
    raw = fetch_news_range(start=start, end=end, client=client, max_items=max_items)
    universe_set = universe if universe is not None else load_input_universe()
    df = transform(raw, universe=universe_set)
    df.to_parquet(cache_path, index=False)
    return df
