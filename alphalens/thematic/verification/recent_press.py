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
    ticker: str | None,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    api_key: str,
    limit: int = DEFAULT_LIMIT,
    max_pages: int = 200,
) -> list[dict]:
    """Fetch Polygon news over ``[asof - lookback, asof]``, with pagination.

    Passing ``ticker=None`` retrieves the unfiltered firehose for the window —
    the orchestrator uses this single window-wide fetch to cover every
    candidate locally, avoiding the per-ticker 5-req/min Starter ceiling.
    """
    start = asof - dt.timedelta(days=lookback_days)
    end_excl = asof + dt.timedelta(days=1)
    params: dict[str, object] = {
        "published_utc.gte": start.isoformat(),
        "published_utc.lt": end_excl.isoformat(),
        "order": "desc",
        "sort": "published_utc",
        "limit": limit,
        "apiKey": api_key,
    }
    if ticker:
        params["ticker"] = ticker
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"

    items: list[dict] = []
    pages = 0
    while url and pages < max_pages:
        data = _http_get_json(url)
        items.extend(data.get("results") or [])
        nxt = data.get("next_url")
        if not nxt:
            break
        sep = "&" if "?" in nxt else "?"
        url = f"{nxt}{sep}apiKey={api_key}"
        pages += 1
    return items


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


def fetch_window_universe(
    *,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    api_key: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    force: bool = False,
) -> pd.DataFrame:
    """One unfiltered Polygon fetch over the lookback window, cached per ``asof``.

    Used by the orchestrator to amortise the cost of per-ticker press
    verification: one paginated fetch covers every candidate. Cache is
    independent from the per-ticker ``fetch_recent_news_cached`` cache.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"_universe_{asof.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)
    items = fetch_recent_news(ticker=None, asof=asof, lookback_days=lookback_days, api_key=api_key)
    df = _to_dataframe(items)
    df.to_parquet(cache_path, index=False)
    return df


def has_theme_in_press_frame(
    *,
    ticker: str,
    keywords: Iterable[str],
    press_df: pd.DataFrame,
) -> bool | None:
    """In-memory verification gate over a pre-fetched window DataFrame.

    Tri-state return (issue #149 fix):
    - ``True``  — ticker has at least one row in ``press_df`` AND at least
      one keyword appears in title / description / Polygon keywords field.
    - ``False`` — ticker has rows in ``press_df`` but none mention any of
      the keywords (real "no" — we checked, didn't find).
    - ``None``  — ticker has NO rows in ``press_df`` (we don't know; the
      caller should fall back to a per-ticker fetch). Also returned for an
      empty frame or an empty keyword iterable.

    The ``None`` case prevents silent false-negatives when Polygon's batch
    firehose fails to tag a ticker on articles that do mention it.
    """
    if press_df.empty:
        return None
    kw_lower = [k.lower() for k in keywords if k]
    if not kw_lower:
        return None
    mask_ticker = press_df["tickers"].apply(
        lambda x: ticker.upper() in (list(x) if x is not None else [])
    )
    rows = press_df[mask_ticker]
    if rows.empty:
        return None
    haystack = (
        rows["title"].fillna("").astype(str).str.lower()
        + " | "
        + rows["description"].fillna("").astype(str).str.lower()
        + " | "
        + rows["keywords"].apply(lambda x: " ".join(x) if x is not None else "").str.lower()
    )
    for kw in kw_lower:
        if haystack.str.contains(kw, regex=False).any():
            return True
    return False


def has_theme_in_recent_press(
    *,
    ticker: str,
    asof: dt.date,
    keywords: Iterable[str],
    api_key: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> bool | None:
    """Verification gate: does ticker's last-30d press mention any theme keyword?

    Tri-state: ``True`` (keyword hit), ``False`` (Polygon returned cleanly
    with no theme-tagged press), ``None`` (Polygon fetch failed — rate limit,
    network error — orchestrator records as ``gates_unknown``).
    """
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
        return None

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
    "fetch_window_universe",
    "has_theme_in_press_frame",
    "has_theme_in_recent_press",
]
