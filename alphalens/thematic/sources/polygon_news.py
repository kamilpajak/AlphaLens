"""Polygon /v2/reference/news client for the thematic tool.

Pulls news for a UTC date range, filters tickers to the thematic input universe,
and normalises to the unified ``NEWS_COLUMNS`` schema. Output cache lives at
``~/.alphalens/thematic_news/polygon/{YYYY-MM-DD}.parquet``.

The Polygon Starter tier rate-limits at 5 req/min; the public news endpoint is
included in that pool. Pagination is handled via ``next_url`` until the cursor
is exhausted or ``max_items`` is reached.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from alphalens.thematic.config.universe import load_input_universe
from alphalens.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

ENDPOINT = "https://api.polygon.io/v2/reference/news"
DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news" / "polygon"
DEFAULT_PAGE_LIMIT = 1000  # Polygon hard max
DEFAULT_MAX_ITEMS = 1000
RATE_LIMIT_SLEEP_SEC = 13.0  # 5 req/min => >= 12s spacing, +1s safety


def _http_get_json(url: str, *, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "AlphaLens-thematic/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_news_range(
    *,
    api_key: str,
    start: dt.datetime,
    end: dt.datetime,
    max_items: int = DEFAULT_MAX_ITEMS,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    sleep_between_pages: float = 0.0,
) -> list[dict]:
    """Fetch raw Polygon news items for ``[start, end)``, paginating until exhausted."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")
    params = {
        "published_utc.gte": start.date().isoformat(),
        "published_utc.lt": end.date().isoformat(),
        "order": "asc",
        "sort": "published_utc",
        "limit": page_limit,
        "apiKey": api_key,
    }
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"

    items: list[dict] = []
    while url and len(items) < max_items:
        data = _http_get_json(url)
        results = data.get("results") or []
        items.extend(results)
        next_url = data.get("next_url")
        if not next_url:
            break
        # Polygon's next_url omits the apiKey
        sep = "&" if "?" in next_url else "?"
        url = f"{next_url}{sep}apiKey={api_key}"
        if sleep_between_pages > 0:
            time.sleep(sleep_between_pages)
    return items[:max_items]


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
            "off_universe_tickers": [t for t in raw_tickers if t not in universe_set],
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
    api_key: str,
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
    raw = fetch_news_range(
        api_key=api_key,
        start=start,
        end=end,
        max_items=max_items,
        sleep_between_pages=RATE_LIMIT_SLEEP_SEC,
    )
    universe_set = universe if universe is not None else load_input_universe()
    df = transform(raw, universe=universe_set)
    df.to_parquet(cache_path, index=False)
    return df
