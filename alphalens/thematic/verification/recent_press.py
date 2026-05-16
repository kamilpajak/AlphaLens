"""Polygon news 30-day retrospective per ticker — Layer 3 verification gate.

For a candidate ticker, pull all Polygon-tracked news in the lookback window
filtered to that ticker, then check whether any item's title / description /
keywords / insights mention the target theme keywords. Cached per
``(ticker, asof)`` at ``~/.alphalens/thematic_press/{TICKER}_{asof}.parquet``.

This is distinct from the Phase A general news ingest: that one filters to
the input universe (S&P 100 + sector leaders) and pulls all news. Here we
pull a per-ticker history for any candidate (including the small/mid-caps
that the LLM-mapping path surfaces).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_press"
ENDPOINT = "https://api.polygon.io/v2/reference/news"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_LIMIT = 100


def _http_get_json(url: str, *, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "AlphaLens-thematic/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_recent_news(
    *,
    ticker: str,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    api_key: str,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Issue one Polygon news API call for ``ticker`` over ``[asof - lookback, asof]``."""
    start = asof - dt.timedelta(days=lookback_days)
    params = {
        "ticker": ticker,
        "published_utc.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ")
        .replace(f"{start.year}", start.isoformat().split("T")[0])
        .split("T")[0],
        "published_utc.lt": (asof + dt.timedelta(days=1)).isoformat(),
        "order": "desc",
        "sort": "published_utc",
        "limit": limit,
        "apiKey": api_key,
    }
    # rebuild params cleanly without the weird strftime trick
    params["published_utc.gte"] = start.isoformat()
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url)
    return list(data.get("results") or [])


def _to_dataframe(items: list[dict]) -> pd.DataFrame:
    rows = []
    for it in items:
        rows.append(
            {
                "id": it.get("id"),
                "published_utc": it.get("published_utc"),
                "title": it.get("title") or "",
                "description": it.get("description") or "",
                "url": it.get("article_url") or "",
                "tickers": list(it.get("tickers") or []),
                "keywords": list(it.get("keywords") or []),
                "publisher": (it.get("publisher") or {}).get("name", ""),
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "id",
            "published_utc",
            "title",
            "description",
            "url",
            "tickers",
            "keywords",
            "publisher",
        ],
    )


def fetch_recent_news_cached(
    *,
    ticker: str,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    api_key: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    force: bool = False,
) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{ticker.upper()}_{asof.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)
    items = fetch_recent_news(
        ticker=ticker, asof=asof, lookback_days=lookback_days, api_key=api_key
    )
    df = _to_dataframe(items)
    df.to_parquet(cache_path, index=False)
    return df


def has_theme_in_recent_press(
    *,
    ticker: str,
    asof: dt.date,
    keywords: Iterable[str],
    api_key: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Verification gate: does ticker's last-30d press mention any theme keyword?"""
    try:
        df = fetch_recent_news_cached(
            ticker=ticker,
            asof=asof,
            lookback_days=lookback_days,
            api_key=api_key,
            cache_dir=cache_dir,
        )
    except Exception as exc:
        logger.warning("recent press fetch failed for %s: %s", ticker, exc)
        return False

    if df.empty:
        return False
    kw_lower = [k.lower() for k in keywords if k]
    if not kw_lower:
        return False

    # Concatenate title + description + keywords list per row, lowercase, substring grep
    haystack = (
        df["title"].fillna("").astype(str).str.lower()
        + " | "
        + df["description"].fillna("").astype(str).str.lower()
        + " | "
        + df["keywords"].apply(lambda x: " ".join(x) if x is not None else "").str.lower()
    )
    for kw in kw_lower:
        if haystack.str.contains(kw, regex=False).any():
            return True
    return False


__all__ = [
    "DEFAULT_CACHE_DIR",
    "fetch_recent_news",
    "fetch_recent_news_cached",
    "has_theme_in_recent_press",
]
