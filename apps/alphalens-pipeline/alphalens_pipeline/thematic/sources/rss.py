"""RSS aggregator (feedparser-based).

Polls a YAML-configured list of tech / finance / sector feeds, transforms
each entry to the unified ``NEWS_COLUMNS`` schema, deduplicates by URL hash,
and caches a daily snapshot.

Like the GDELT source, the RSS path does not tag tickers itself — the
downstream Layer 2 LLM is responsible for ticker extraction from headlines
and summaries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import feedparser
import pandas as pd
import yaml

from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news" / "rss"
FEEDS_PATH = Path(__file__).parent.parent / "config" / "rss_feeds.yaml"
# P1a: strict single UTC day. 0 encodes "the target day only" — the window math
# below computes ``[date 00:00, date+1 00:00)`` regardless, so an explicit
# ``window_days > 0`` no longer widens the window (kept in the signature only so
# older callers do not break at the kwarg boundary).
DEFAULT_WINDOW_DAYS = 0


def _parse_feed(url: str):
    """Wrapper around ``feedparser.parse`` so tests can patch a single seam."""
    return feedparser.parse(url, agent="AlphaLens-thematic/0.1")


def _stable_id(url: str) -> str:
    # Content-addressing only — sha256 over sha1 to satisfy Sonar S4790.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _entry_timestamp(entry) -> pd.Timestamp | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    try:
        return pd.Timestamp(dt.datetime(*parsed[:6], tzinfo=dt.UTC))
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def load_feeds() -> list[dict[str, str]]:
    """Return ``[{name, url, domain}, ...]`` from the YAML config."""
    with FEEDS_PATH.open() as f:
        data = yaml.safe_load(f)
    return list(data.get("feeds") or [])


def transform(
    entries: Iterable,
    *,
    feed_name: str,
    domain: str,
    fallback_date: dt.date | None = None,
) -> pd.DataFrame:
    # No now()-fallback: an entry with no parseable timestamp and no
    # fallback_date is DROPPED, not stamped with the current time. Injecting
    # pd.Timestamp.now() would be a now-relative leak — non-deterministic and
    # replay-incorrect — mirroring GDELT's drop-on-missing-seendate behaviour.
    fallback_ts = pd.Timestamp(fallback_date, tz="UTC") if fallback_date is not None else None
    rows: list[dict] = []
    for e in entries:
        url = getattr(e, "link", None)
        if not url:
            continue
        ts = _entry_timestamp(e) or fallback_ts
        if ts is None:
            continue
        extra = {
            "feed_name": feed_name,
            "domain": domain,
        }
        rows.append(
            {
                "id": _stable_id(url),
                "source": "rss",
                "timestamp": ts,
                "tickers": [],
                "title": getattr(e, "title", "") or "",
                "body": getattr(e, "summary", "") or "",
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


def fetch_feed(
    *,
    name: str,
    url: str,
    domain: str,
    fallback_date: dt.date | None = None,
) -> pd.DataFrame:
    parsed = _parse_feed(url)
    if getattr(parsed, "bozo", 0):
        logger.warning(
            "RSS feed %s marked malformed by feedparser: %s",
            name,
            getattr(parsed, "bozo_exception", "no exception detail"),
        )
    entries = list(getattr(parsed, "entries", []) or [])
    return transform(entries, feed_name=name, domain=domain, fallback_date=fallback_date)


def fetch_daily_news(
    *,
    date: dt.date,
    feeds: list[dict[str, str]] | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    window_days: int = DEFAULT_WINDOW_DAYS,
    force: bool = False,
) -> pd.DataFrame:
    """Pull all configured RSS feeds, filter to the single UTC day ``[date 00:00, date+1 00:00)``, dedupe.

    ``window_days`` is retained for signature compatibility but no longer widens
    the window — P1a admits only the strict single target UTC day, matching
    polygon_news.py.
    """
    del window_days  # P1a: window is always the single target UTC day
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)

    feeds_list = feeds if feeds is not None else load_feeds()
    frames: list[pd.DataFrame] = []
    for feed in feeds_list:
        try:
            df = fetch_feed(
                name=feed["name"],
                url=feed["url"],
                domain=feed["domain"],
                fallback_date=date,
            )
        except Exception as exc:
            logger.warning("RSS feed %s failed: %s", feed.get("name"), exc, exc_info=True)
            continue
        frames.append(df)

    if not frames:
        merged = empty_news_frame()
    else:
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.drop_duplicates(subset=["id"], keep="first").reset_index(drop=True)

    if len(merged) > 0:
        lo = pd.Timestamp(date, tz="UTC")
        hi = lo + pd.Timedelta(days=1)
        merged = merged[(merged["timestamp"] >= lo) & (merged["timestamp"] < hi)].reset_index(
            drop=True
        )

    merged.to_parquet(cache_path, index=False)
    return merged
