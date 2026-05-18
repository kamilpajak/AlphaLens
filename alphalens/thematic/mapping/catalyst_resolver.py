"""Catalyst/news provenance for Phase C thematic candidates.

Given a (theme, asof, lookback) tuple, walks the rolling
``thematic_events`` parquets to find events whose ``themes`` list
contains the theme, joins them to the ``thematic_news`` parquets via
``news_id``, and returns the most recent matching event's ``url``,
``title`` (truncated), and ``published_at`` (ISO date).

Phase C calls this per-theme once per batch; the returned dict is
attached to every candidate emitted for that theme so the Phase E brief
can show "**Catalyst:** {title} ({date}) {url}" — answering the operator
question "why was this surfaced?".

Returns ``None`` when no theme-tagged event exists in the window or the
parquet directories are missing (graceful degradation; brief simply
omits the catalyst line).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import textwrap
from functools import lru_cache
from pathlib import Path

import pandas as pd

from alphalens.thematic.extraction.schema import NOISE_EVENT_TYPES

logger = logging.getLogger(__name__)

DEFAULT_NEWS_DIR = Path.home() / ".alphalens" / "thematic_news"
DEFAULT_EVENTS_DIR = Path.home() / ".alphalens" / "thematic_events"
DEFAULT_LOOKBACK_DAYS = 30
_TITLE_MAX_LEN = 200
_NOISE_FILTERS_PATH = Path(__file__).parent.parent / "config" / "catalyst_noise_filters.yaml"


@lru_cache(maxsize=1)
def _load_url_blocklist_patterns() -> tuple[re.Pattern, ...]:
    """Load + compile the L1 URL pattern blocklist (cached for batch reuse).

    Returns empty tuple when the YAML file is missing or malformed —
    callers degrade to "no URL filter" rather than crash. The L2 noise
    event_type filter still fires independently.
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not available; URL blocklist disabled")
        return ()
    if not _NOISE_FILTERS_PATH.exists():
        return ()
    try:
        cfg = yaml.safe_load(_NOISE_FILTERS_PATH.read_text()) or {}
    except yaml.YAMLError as exc:
        logger.warning("catalyst noise YAML parse failed: %s", exc)
        return ()
    patterns = cfg.get("url_blocklist", []) or []
    compiled: list[re.Pattern] = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error as exc:
            logger.warning("catalyst noise URL pattern %r is not valid regex: %s", p, exc)
    return tuple(compiled)


def _url_matches_blocklist(url: str, patterns: tuple[re.Pattern, ...]) -> bool:
    if not url or not patterns:
        return False
    return any(p.search(url) for p in patterns)


def _load_window(parquet_dir: Path, asof: dt.date, lookback_days: int) -> pd.DataFrame:
    """Concat all parquets in ``[asof - lookback, asof]`` from ``parquet_dir``."""
    if not parquet_dir.exists():
        return pd.DataFrame()
    lo = asof - dt.timedelta(days=lookback_days)
    frames: list[pd.DataFrame] = []
    for path in sorted(parquet_dir.glob("*.parquet")):
        try:
            date = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if date < lo or date > asof:
            continue
        try:
            frames.append(pd.read_parquet(path))
        except Exception as exc:
            logger.warning("failed to read %s: %s", path, exc)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def find_trigger_event(
    *,
    theme: str,
    asof: dt.date,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    news_dir: Path = DEFAULT_NEWS_DIR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict | None:
    """Return ``{url, title, published_at}`` of the latest theme-tagged event."""
    events = _load_window(events_dir, asof, lookback_days)
    if events.empty:
        return None

    # Filter to events whose `themes` list contains the target theme.
    def _has_theme(themes_field) -> bool:
        if themes_field is None:
            return False
        try:
            return theme in list(themes_field)
        except TypeError:
            return False

    matches = events[events["themes"].apply(_has_theme)]
    if matches.empty:
        return None

    news = _load_window(news_dir, asof, lookback_days)
    if news.empty:
        return None

    joined = matches.merge(news, left_on="news_id", right_on="id", how="inner")
    if joined.empty:
        return None

    # L2 noise filter — drop rows whose event_type is in NOISE_EVENT_TYPES
    # (promo, listicle, opinion, lifestyle, evergreen, sponsored). Older
    # event parquets may lack the event_type column; treat missing as
    # "unknown" (= not noise) so backward-compat is preserved.
    if "event_type" in joined.columns:
        joined = joined[~joined["event_type"].isin(NOISE_EVENT_TYPES)]
        if joined.empty:
            return None

    # L1 URL pattern blocklist — catches cases where Flash mis-classified
    # promo content as a non-noise type (e.g. product_launch on a coupon
    # page). Slug-scoped regex, not domain-wide, to avoid dropping legit
    # cyber-incident reporting from the same publisher.
    blocklist = _load_url_blocklist_patterns()
    if blocklist and "url" in joined.columns:
        joined = joined[
            ~joined["url"].astype(str).apply(lambda u: _url_matches_blocklist(u, blocklist))
        ]
        if joined.empty:
            return None

    # Pick the newest by event time. Canonical news schema (sources/schema.py)
    # uses ``timestamp`` since the 2026-05 ingest refactor; older parquets
    # carry ``published_at``. Prefer the canonical name; fall back to legacy
    # so historical news files still work.
    if "timestamp" in joined.columns:
        time_col = "timestamp"
    elif "published_at" in joined.columns:
        time_col = "published_at"
    else:
        return None
    joined[time_col] = pd.to_datetime(joined[time_col], errors="coerce", utc=True)
    joined = joined.dropna(subset=[time_col])
    if joined.empty:
        return None

    top = joined.sort_values(time_col, ascending=False).iloc[0]
    title = str(top.get("title", "") or "")
    if len(title) > _TITLE_MAX_LEN:
        title = textwrap.shorten(title, width=_TITLE_MAX_LEN, placeholder="…")
    return {
        "url": str(top.get("url", "") or ""),
        "title": title,
        "published_at": top[time_col].date().isoformat(),
    }


__all__ = ["DEFAULT_EVENTS_DIR", "DEFAULT_NEWS_DIR", "find_trigger_event"]
