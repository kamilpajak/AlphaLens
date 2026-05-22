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
import logging
import math
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from alphalens_research.data.alt_data.polygon_client import (
    PolygonClient,
    get_default_polygon_client,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_press"
DEFAULT_LOOKBACK_DAYS = 30
# Polygon's hard max page size. The previous limit=100 throttled throughput
# 10x AND implicitly capped ``fetch_window_universe`` at ``max_pages * limit
# = 200 * 100 = 20,000`` rows — below the 30-day market-wide firehose volume.
# Raising to 1000 yields max_items=200,000 (safely covers a 30-day window)
# and cuts API calls 10x for the same coverage. Critical for the 5-req/min
# Starter quota that the canonical client now coordinates globally.
DEFAULT_LIMIT = 1000


def fetch_recent_news(
    *,
    ticker: str | None,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    client: PolygonClient | None = None,
    limit: int = DEFAULT_LIMIT,
    max_pages: int = 200,
) -> list[dict]:
    """Fetch Polygon news over ``[asof - lookback, asof]``, with pagination.

    Passing ``ticker=None`` retrieves the unfiltered firehose for the window —
    the orchestrator uses this single window-wide fetch to cover every
    candidate locally, avoiding the per-ticker 5-req/min Starter ceiling.

    HTTP, pagination, rate-limit (5 req/min Starter), Retry-After honoring,
    and Bearer auth are owned by the canonical PolygonClient. Pass
    ``client=None`` to use the process-wide default singleton.
    """
    if client is None:
        client = get_default_polygon_client()
    start = dt.datetime.combine(asof - dt.timedelta(days=lookback_days), dt.time.min, tzinfo=dt.UTC)
    end_excl = dt.datetime.combine(asof + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    return client.get_news_range(
        start=start,
        end=end_excl,
        ticker=ticker,
        order="desc",
        sort="published_utc",
        limit=limit,
        max_items=max_pages * limit,
        max_pages=max_pages,
    )


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
    client: PolygonClient | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    force: bool = False,
) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{ticker.upper()}_{asof.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)
    items = fetch_recent_news(ticker=ticker, asof=asof, lookback_days=lookback_days, client=client)
    df = _to_dataframe(items)
    df.to_parquet(cache_path, index=False)
    return df


def fetch_window_universe(
    *,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    client: PolygonClient | None = None,
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
    items = fetch_recent_news(ticker=None, asof=asof, lookback_days=lookback_days, client=client)
    df = _to_dataframe(items)
    df.to_parquet(cache_path, index=False)
    return df


def _is_listlike(x) -> bool:
    """True iff ``x`` is iterable and NOT a string / NaN scalar.

    Pandas / parquet round-trips can leave NaN floats in list-typed cells.
    ``x is not None`` lets NaN through (it's a float, not None), and
    ``list(nan)`` raises TypeError — which the orchestrator's ``_safe``
    wrapper would catch and silently degrade to ``gates_unknown`` for the
    affected candidate. Filter NaN / non-iterables / bare strings upfront.
    """
    if x is None:
        return False
    if isinstance(x, str):
        return False
    if isinstance(x, float) and math.isnan(x):
        return False
    return isinstance(x, Iterable)


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

    Ticker comparison is case-insensitive on both sides: caller passes any
    case; cell entries are upper-cased before matching. Defensive against
    NaN / non-list cells (parquet round-trip artefacts).
    """
    if press_df.empty:
        return None
    kw_lower = [k.lower() for k in keywords if k]
    if not kw_lower:
        return None
    needle = ticker.upper()
    mask_ticker = press_df["tickers"].apply(
        lambda x: needle in [str(t).upper() for t in x] if _is_listlike(x) else False
    )
    rows = press_df[mask_ticker]
    if rows.empty:
        return None
    haystack = (
        rows["title"].fillna("").astype(str).str.lower()
        + " | "
        + rows["description"].fillna("").astype(str).str.lower()
        + " | "
        + rows["keywords"]
        .apply(lambda x: " ".join(str(k) for k in x) if _is_listlike(x) else "")
        .str.lower()
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
    client: PolygonClient | None = None,
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
            client=client,
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

    # Concatenate title + description + keywords list per row, lowercase, substring grep.
    # Keywords lambda must use ``_is_listlike`` defence — pandas/parquet can put
    # NaN floats in list cells, which would crash ``" ".join`` and propagate up
    # to a silent ``gates_unknown`` via ``_safe`` in the orchestrator.
    haystack = (
        df["title"].fillna("").astype(str).str.lower()
        + " | "
        + df["description"].fillna("").astype(str).str.lower()
        + " | "
        + df["keywords"]
        .apply(lambda x: " ".join(str(k) for k in x) if _is_listlike(x) else "")
        .str.lower()
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
