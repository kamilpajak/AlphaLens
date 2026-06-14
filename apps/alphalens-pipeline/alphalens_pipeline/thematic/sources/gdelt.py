"""GDELT 2.0 DOC API client (free, no key, ~1 req/5s soft limit).

Strategy: one query per thematic bucket per day. Buckets are defined in
``alphalens_pipeline/thematic/config/gdelt_themes.yaml``. Each query returns up to
``MAXRECORDS`` articles in the last ``timespan`` window. Results across
buckets are deduplicated by article URL hash before caching.

The DOC API does not expose tickers directly; downstream Layer 2 LLM
extraction is responsible for mapping article text → tickers. Unlike Polygon
news, the unified-schema ``tickers`` column is left empty here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import logging
import re
import time
import urllib.error
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

from alphalens_pipeline.data.alt_data.gdelt_client import (
    DEFAULT_MAXRECORDS,
    GdeltMaxRetriesError,
    GdeltQueryError,
    get_default_gdelt_client,
)
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news" / "gdelt"
THEMES_PATH = Path(__file__).parent.parent / "config" / "gdelt_themes.yaml"
# 10s margin over GDELT's documented 5s/req soft limit. Pre-fix value was 8s,
# but bursts from per-bucket retries on broken queries were tripping HTTP 429
# on downstream buckets; with permanent-vs-transient distinction below the
# extra 2s/bucket buys headroom on the tail.
DEFAULT_INTER_QUERY_SLEEP_SEC = 10.0

# GDELT reconstructs titles from tokenized text, leaving spaces around
# punctuation (~25% of rows): "Alphabet ( Google )", "gas prices . ",
# "D - Wave", "33 %". The downstream pipeline + UI render the title verbatim,
# so normalise at ingest. Patterns match a SINGLE literal space (whitespace
# runs are collapsed first via str.split), so there is no quantifier to
# backtrack — sidesteps ReDoS.
_SPACE_BEFORE_PUNCT = re.compile(r" ([.,;:!?)\]}%])")
_SPACE_AFTER_OPENER = re.compile(r"([(\[{]) ")
# Compound-word hyphen: "D - Wave" → "D-Wave", "Coca - Cola" → "Coca-Cola",
# "2026 - 05 - 27" → "2026-05-27". Only collapses between two word characters
# (\w = [A-Za-z0-9_]) joined by a single ASCII hyphen — sentence em-dash
# separators use U+2014 ("—"), which doesn't match and stays untouched.
_SPACE_AROUND_HYPHEN = re.compile(r"(\w) - (\w)")


def unescape_entities(title: str) -> str:
    """Decode HTML character references (e.g. ``&#8216;`` → ``‘``, ``&amp;`` → ``&``).

    News titles from the RSS / GDELT / Polygon aggregators occasionally arrive
    with raw HTML entities that were never decoded upstream, so the dashboard
    renders the literal ``&#8216;reset&#8217;`` instead of ``‘reset’``.
    ``html.unescape`` is idempotent on already-decoded text, so applying it more
    than once (e.g. here and again in :func:`clean_title`) is safe.
    """
    return html.unescape(title)


def clean_title(title: str) -> str:
    """Decode HTML entities, strip GDELT's space-padding, collapse runs, trim.

    Public — also called by the legacy-title backfill (`alphalens thematic
    clean-titles`, see `alphalens_pipeline.thematic.clean_titles`), so the
    entity-decode step retro-cleans already-stored ``source_event_title`` rows.
    Idempotent on already-clean input.
    """
    # Decode first so any entity that resolves to whitespace (``&nbsp;`` →
    # U+00A0) is then normalized by the whitespace collapse below.
    title = unescape_entities(title)
    title = " ".join(title.split())  # collapse all whitespace runs + trim
    title = _SPACE_BEFORE_PUNCT.sub(r"\1", title)
    title = _SPACE_AFTER_OPENER.sub(r"\1", title)
    # Two passes: re.sub finds all non-overlapping matches left-to-right,
    # consuming the second \w of each match. For a single-character chain
    # like "A - B - C" the first pass produces "A-B - C" (B is consumed
    # before " - C" can be matched); a second pass then catches "B - C".
    # ISO-style chains like "2026 - 05 - 27" already converge in one pass
    # because the consumed \w sits before the next " - " in the string.
    # Two passes are provably sufficient for chains of any length: pass 1
    # collapses even-indexed pairs, pass 2 the remaining odd-indexed pairs.
    title = _SPACE_AROUND_HYPHEN.sub(r"\1-\2", title)
    title = _SPACE_AROUND_HYPHEN.sub(r"\1-\2", title)
    return title


@lru_cache(maxsize=1)
def load_theme_buckets() -> dict[str, str]:
    """Return the ``{theme_name: gdelt_query_string}`` map from YAML config."""
    with THEMES_PATH.open() as f:
        data = yaml.safe_load(f)
    return dict(data.get("themes") or {})


def _format_datetime_for_gdelt(dt_: dt.datetime) -> str:
    """Format a tz-aware datetime to GDELT API format ``YYYYMMDDHHMMSS`` (UTC).

    Rejects naive datetimes so window bounds are never silently misinterpreted
    as local time.
    """
    if dt_.tzinfo is None:
        raise ValueError(f"datetime must be timezone-aware; got {dt_!r}")
    return dt_.astimezone(dt.UTC).strftime("%Y%m%d%H%M%S")


def _parse_seendate(seendate: str) -> pd.Timestamp:
    """Parse GDELT seendate ``YYYYMMDDTHHMMSSZ`` to a UTC pd.Timestamp."""
    return pd.Timestamp(dt.datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC))


def _stable_id(url: str) -> str:
    # Content-addressing only — sha256 over sha1 to satisfy Sonar S4790.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def transform(raw_articles: Iterable[dict], *, theme: str) -> pd.DataFrame:
    """Normalise GDELT artlist rows to the unified ``NEWS_COLUMNS`` schema.

    Drops articles with a missing or unparseable ``seendate``: under the P1a
    explicit-window fetch the old ``pd.Timestamp.now(tz="UTC")`` fallback would
    inject an out-of-window timestamp (and a non-replay-stable one), so such
    rows cannot be trusted and are dropped instead.
    """
    rows: list[dict] = []
    for art in raw_articles:
        url = art.get("url") or ""
        if not url:
            continue
        seendate = art.get("seendate")
        if not seendate:
            logger.debug("gdelt article dropped (missing seendate): %s", url)
            continue
        try:
            ts = _parse_seendate(seendate)
        except ValueError:
            logger.debug("gdelt article dropped (invalid seendate %r): %s", seendate, url)
            continue
        extra = {
            "theme_bucket": theme,
            "domain": art.get("domain"),
            "language": art.get("language"),
            "sourcecountry": art.get("sourcecountry"),
            "socialimage": art.get("socialimage"),
        }
        rows.append(
            {
                "id": _stable_id(url),
                "source": "gdelt",
                "timestamp": ts,
                "tickers": [],  # GDELT doesn't tag tickers; Layer 2 LLM fills this
                "title": clean_title(art.get("title") or ""),
                "body": "",
                "url": url,
                "keywords": [],
                "extra": json.dumps(extra, ensure_ascii=False),
            }
        )

    if not rows:
        return empty_news_frame()
    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def fetch_theme(
    *,
    theme: str,
    query: str,
    start: dt.datetime,
    end: dt.datetime,
    maxrecords: int = DEFAULT_MAXRECORDS,
) -> pd.DataFrame:
    """Issue one GDELT DOC API query bounded to ``[start, end)`` UTC.

    The DOC API may over-return at the boundaries, so rows whose ``seendate``
    falls outside ``[start, end)`` are filtered post-fetch — the file is then
    free of T-0 bleed and replay-correct regardless of fetch wall-time.
    """
    data = get_default_gdelt_client().fetch_doc(
        query=query,
        startdatetime=_format_datetime_for_gdelt(start),
        enddatetime=_format_datetime_for_gdelt(end),
        maxrecords=maxrecords,
    )
    df = transform(data.get("articles") or [], theme=theme)
    if not df.empty:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] < end_ts)].reset_index(drop=True)
    return df


def fetch_daily_news(
    *,
    date: dt.date,
    theme_buckets: dict[str, str] | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    maxrecords: int = DEFAULT_MAXRECORDS,
    inter_query_sleep_sec: float = DEFAULT_INTER_QUERY_SLEEP_SEC,
    force: bool = False,
) -> pd.DataFrame:
    """Run all theme-bucket queries for the strict single UTC day, dedupe, cache.

    Window = ``[date 00:00 UTC, date+1 00:00 UTC)`` for every bucket, matching
    Polygon's strict single-day fetch.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)

    start = dt.datetime.combine(date, dt.time.min, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=1)

    buckets = theme_buckets if theme_buckets is not None else load_theme_buckets()
    frames: list[pd.DataFrame] = []
    for i, (theme, query) in enumerate(buckets.items()):
        if i > 0 and inter_query_sleep_sec > 0:
            time.sleep(inter_query_sleep_sec)
        try:
            df = fetch_theme(theme=theme, query=query, start=start, end=end, maxrecords=maxrecords)
            frames.append(df)
        except (GdeltQueryError, GdeltMaxRetriesError, urllib.error.URLError) as exc:
            logger.warning("gdelt bucket %s failed: %r", theme, exc)

    if not frames:
        merged = empty_news_frame()
    else:
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.drop_duplicates(subset=["id"], keep="first").reset_index(drop=True)

    merged.to_parquet(cache_path, index=False)
    return merged
