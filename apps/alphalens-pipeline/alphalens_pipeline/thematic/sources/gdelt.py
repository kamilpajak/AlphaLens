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
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news" / "gdelt"
THEMES_PATH = Path(__file__).parent.parent / "config" / "gdelt_themes.yaml"
DEFAULT_MAXRECORDS = 100
# 10s margin over GDELT's documented 5s/req soft limit. Pre-fix value was 8s,
# but bursts from per-bucket retries on broken queries were tripping HTTP 429
# on downstream buckets; with permanent-vs-transient distinction below the
# extra 2s/bucket buys headroom on the tail.
DEFAULT_INTER_QUERY_SLEEP_SEC = 10.0

# GDELT reconstructs titles from tokenized text, leaving spaces around
# punctuation (~25% of rows): "Alphabet ( Google )", "gas prices . ". The
# downstream pipeline + UI render the title verbatim, so normalise at ingest.
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?)\]}])")
_SPACE_AFTER_OPENER = re.compile(r"([(\[{])\s+")
_WHITESPACE_RUN = re.compile(r"\s{2,}")


def _clean_title(title: str) -> str:
    """Strip GDELT's space-padding around punctuation; collapse runs; trim."""
    title = _SPACE_BEFORE_PUNCT.sub(r"\1", title)
    title = _SPACE_AFTER_OPENER.sub(r"\1", title)
    title = _WHITESPACE_RUN.sub(" ", title)
    return title.strip()


class GdeltQueryError(Exception):
    """Permanent error returned by GDELT — query is malformed, do not retry.

    Triggered when the API responds 200 OK with a non-JSON body, e.g.
    ``"The specified phrase is too short."`` for queries containing
    single-word quoted phrases. Retrying just burns the rate-limit budget
    of subsequent buckets without ever succeeding.
    """


class GdeltMaxRetriesError(Exception):
    """Transient retries exhausted (empty body, HTTPError) — bucket dropped."""


def _http_get_json(
    url: str,
    *,
    timeout: float = 20.0,
    max_attempts: int = 3,
    backoff_sec: float = 10.0,
) -> dict:
    """Fetch JSON from GDELT, distinguishing permanent vs transient errors.

    Transient (retried with exponential backoff): empty body (soft rate-limit
    signal) and ``HTTPError`` (including real 429).

    Permanent (raised immediately as ``GdeltQueryError``): non-empty body that
    does not look like JSON. GDELT signals malformed queries with HTTP 200 and
    a plain-text message body, so retrying is wasted wall time AND triggers
    GDELT's 5s/req soft cap for the bucket that comes next.
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AlphaLens-thematic/0.1"})
            # URL built from gdelt-base constant + querystring; file:// not reachable.
            with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
                body = r.read()
            if not body:
                raise json.JSONDecodeError("empty body (likely rate-limited)", "", 0)
            if body.lstrip()[:1] not in (b"{", b"["):
                snippet = body[:200].decode("utf-8", errors="replace").strip()
                raise GdeltQueryError(f"GDELT permanent error: {snippet}")
            return json.loads(body)
        except GdeltQueryError:
            raise
        except (json.JSONDecodeError, urllib.error.HTTPError) as e:
            last_err = e
            if attempt + 1 < max_attempts:
                time.sleep(backoff_sec * (2**attempt))
    raise GdeltMaxRetriesError(f"GDELT fetch failed after {max_attempts} attempts: {last_err}")


@lru_cache(maxsize=1)
def load_theme_buckets() -> dict[str, str]:
    """Return the ``{theme_name: gdelt_query_string}`` map from YAML config."""
    with THEMES_PATH.open() as f:
        data = yaml.safe_load(f)
    return dict(data.get("themes") or {})


def build_query_url(
    *,
    query: str,
    timespan: str = "1d",
    maxrecords: int = DEFAULT_MAXRECORDS,
    sort: str = "datedesc",
) -> str:
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": maxrecords,
        "timespan": timespan,
        "sort": sort,
    }
    return f"{ENDPOINT}?{urllib.parse.urlencode(params)}"


def _parse_seendate(seendate: str) -> pd.Timestamp:
    """Parse GDELT seendate ``YYYYMMDDTHHMMSSZ`` to a UTC pd.Timestamp."""
    return pd.Timestamp(dt.datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC))


def _stable_id(url: str) -> str:
    # Content-addressing only — sha256 over sha1 to satisfy Sonar S4790.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def transform(raw_articles: Iterable[dict], *, theme: str) -> pd.DataFrame:
    """Normalise GDELT artlist rows to the unified ``NEWS_COLUMNS`` schema."""
    rows: list[dict] = []
    for art in raw_articles:
        url = art.get("url") or ""
        if not url:
            continue
        seendate = art.get("seendate")
        try:
            ts = _parse_seendate(seendate) if seendate else pd.Timestamp.now(tz="UTC")
        except ValueError:
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
                "title": _clean_title(art.get("title") or ""),
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
    timespan: str = "1d",
    maxrecords: int = DEFAULT_MAXRECORDS,
) -> pd.DataFrame:
    """Issue one GDELT DOC API query and return the normalised frame."""
    url = build_query_url(query=query, timespan=timespan, maxrecords=maxrecords)
    data = _http_get_json(url)
    return transform(data.get("articles") or [], theme=theme)


def fetch_daily_news(
    *,
    date: dt.date,
    theme_buckets: dict[str, str] | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    timespan: str = "1d",
    maxrecords: int = DEFAULT_MAXRECORDS,
    inter_query_sleep_sec: float = DEFAULT_INTER_QUERY_SLEEP_SEC,
    force: bool = False,
) -> pd.DataFrame:
    """Run all theme-bucket queries for one day, deduplicate, cache, return frame."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)

    buckets = theme_buckets if theme_buckets is not None else load_theme_buckets()
    frames: list[pd.DataFrame] = []
    for i, (theme, query) in enumerate(buckets.items()):
        if i > 0 and inter_query_sleep_sec > 0:
            time.sleep(inter_query_sleep_sec)
        try:
            df = fetch_theme(theme=theme, query=query, timespan=timespan, maxrecords=maxrecords)
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
