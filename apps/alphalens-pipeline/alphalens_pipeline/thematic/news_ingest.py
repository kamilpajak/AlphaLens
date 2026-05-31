"""Daily unified news ingest — orchestrates Polygon / GDELT / RSS sources.

Output: one parquet per day at ``~/.alphalens/thematic_news/{YYYY-MM-DD}.parquet``
conforming to the shared ``NEWS_COLUMNS`` schema, capped at ``max_items``
per the §14 lock-7 LLM-budget envelope (~200 items/day → DeepSeek v4-flash
budget).

EDGAR signal lives in ``alphalens_pipeline/edgar_detector/`` Layer 1 (dedicated 8-K parsers).
Raw filing headers carry too little body content for thematic DeepSeek v4-flash
extraction to score them confidently (mean conf 0.36 vs 0.91 for Polygon/RSS),
so they are deliberately excluded here.

Two-stage dedup runs between fetch and cap:
1. URL canonicalisation collapses cross-source rows that differ only by
   tracking params (utm_source, ref=feed, etc.).
2. Tier 1 lexical clustering (Jaccard ≥ 0.6 AND ≥ 3 token overlap on
   normalized titles) collapses same-day syndication where source URLs
   differ but content is the same story. Cluster representative = earliest
   timestamp, with ``_SOURCE_PRIORITY`` as tie-break.

Cap-200 then sorts CLUSTERS by ``max(member.timestamp)`` so breaking news
without echoes still competes against amplified stories. The surviving row
carries its cluster size in the ``extra`` JSON blob — no schema change.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pandas as pd

from alphalens_pipeline.thematic import text_similarity
from alphalens_pipeline.thematic.sources import gdelt, polygon_news, rss
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news"
DEFAULT_MAX_ITEMS = 200

# Lower number = higher priority. Used both at URL-canonical dedup (richer
# source wins) and at Tier 1 cluster-representative selection (tie-break when
# two cluster members share a timestamp).
_SOURCE_PRIORITY = {"polygon": 0, "gdelt": 1, "rss": 2}


def _fetch_polygon(*, date: dt.date) -> pd.DataFrame:
    # PolygonClient reads POLYGON_API_KEY via get_default_polygon_client();
    # rate-limit + Bearer auth + retry are owned by the canonical client.
    return polygon_news.fetch_daily_news(date=date)


def _fetch_gdelt(*, date: dt.date) -> pd.DataFrame:
    return gdelt.fetch_daily_news(date=date)


def _fetch_rss(*, date: dt.date) -> pd.DataFrame:
    return rss.fetch_daily_news(date=date)


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


def _attach_cluster_size_to_extra(extra_str: str, cluster_size: int) -> str:
    """Merge ``{cluster_size: N}`` into the existing JSON-encoded ``extra`` payload."""
    try:
        payload = json.loads(extra_str) if extra_str else {}
        if not isinstance(payload, dict):
            # A future source adapter writing a JSON list / scalar / null
            # would otherwise lose that data silently. Surface the clobber.
            logger.warning(
                "overwriting non-dict extra JSON payload during cluster merge: %r",
                extra_str,
            )
            payload = {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    payload["cluster_size"] = int(cluster_size)
    return json.dumps(payload)


def _assign_cluster_ids(titles: list[str]) -> list[int]:
    """Greedy pairwise union-find on Jaccard-similar titles.

    Returns a list of cluster ids aligned to the input order. Each title is
    seeded as its own cluster; subsequent titles join the first earlier
    cluster whose seed is :func:`text_similarity.titles_similar` to them.
    """
    n = len(titles)
    cluster_id = [-1] * n
    next_cid = 0
    for i in range(n):
        if cluster_id[i] != -1:
            continue
        cluster_id[i] = next_cid
        for j in range(i + 1, n):
            if cluster_id[j] == -1 and text_similarity.titles_similar(titles[i], titles[j]):
                cluster_id[j] = next_cid
        next_cid += 1
    return cluster_id


def _collapse_cluster_to_representative(cluster: pd.DataFrame) -> dict:
    """Pick the (earliest, highest-priority-source) member as the cluster rep.

    Stamps ``cluster_size`` into the rep's ``extra`` JSON and records
    ``_cluster_rank_ts = max(member.timestamp)`` so downstream cap-by-recency
    lets breaking news compete against amplified stories.
    """
    cluster_sorted = cluster.sort_values(["timestamp", "_source_rank"], ascending=[True, True])
    rep = cluster_sorted.iloc[0].copy()
    rep["extra"] = _attach_cluster_size_to_extra(rep.get("extra", "{}"), len(cluster))
    rep["_cluster_rank_ts"] = cluster["timestamp"].max()
    return rep.to_dict()


def _cluster_same_day_lexical(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse syndicated echoes published within the same UTC date.

    Pairwise greedy union-find on Jaccard-similar titles, day-scoped so
    cross-day arcs stay separate (Tier 2 entity-overlap at the resolver
    handles those). Returns the surviving representatives plus a transient
    ``_cluster_rank_ts`` column equal to ``max(member.timestamp)`` so
    cap-by-recency lets unecho'd breaking news compete against amplified
    stories.
    """
    if df.empty:
        out = df.copy()
        out["_cluster_rank_ts"] = pd.Series(dtype="datetime64[ns, UTC]")
        return out

    df = df.reset_index(drop=True).copy()
    df["_date"] = df["timestamp"].dt.date
    df["_source_rank"] = df["source"].map(_SOURCE_PRIORITY).fillna(99).astype(int)

    out_rows: list[dict] = []
    for _, raw_day_group in df.groupby("_date", sort=False):
        day_group = raw_day_group.reset_index(drop=True)
        titles = day_group["title"].fillna("").tolist()
        day_group["_cluster_id"] = _assign_cluster_ids(titles)
        for _, cluster in day_group.groupby("_cluster_id"):
            out_rows.append(_collapse_cluster_to_representative(cluster))

    out = pd.DataFrame(out_rows)
    if out.empty:
        # Defensive: only reachable if groupby returned no rows (impossible given df not empty)
        out = df.iloc[0:0].copy()
        out["_cluster_rank_ts"] = pd.Series(dtype="datetime64[ns, UTC]")
        return out

    out = out.drop(columns=["_date", "_source_rank", "_cluster_id"], errors="ignore")
    out["_cluster_rank_ts"] = pd.to_datetime(out["_cluster_rank_ts"], utc=True)
    return out


def ingest_daily(
    *,
    date: dt.date,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_items: int = DEFAULT_MAX_ITEMS,
    polygon_api_key: str | None = None,
    force: bool = False,
) -> pd.DataFrame:
    """Aggregate Polygon/GDELT/RSS for one UTC day, dedupe, cluster, cap, persist, return."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)

    # ``polygon_api_key`` is accepted for backwards-compatibility with the CLI
    # signature, but the canonical PolygonClient reads ``POLYGON_API_KEY``
    # from the environment directly. Passing it explicitly is now a no-op
    # — kept so older callers don't blow up at the kwarg boundary.
    del polygon_api_key
    polygon_df = _safe_call("polygon", _fetch_polygon, date=date)
    gdelt_df = _safe_call("gdelt", _fetch_gdelt, date=date)
    rss_df = _safe_call("rss", _fetch_rss, date=date)

    frames = [df for df in (polygon_df, gdelt_df, rss_df) if len(df) > 0]
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
            .reset_index(drop=True)
        )
        merged = _cluster_same_day_lexical(merged)
        merged = (
            merged.sort_values("_cluster_rank_ts", ascending=False)
            .head(max_items)
            .drop(columns=["_cluster_rank_ts"])
            .sort_values("timestamp", ascending=False)
            .reset_index(drop=True)
        )

    merged = merged[NEWS_COLUMNS]
    merged.to_parquet(cache_path, index=False)
    return merged
