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
import textwrap
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_NEWS_DIR = Path.home() / ".alphalens" / "thematic_news"
DEFAULT_EVENTS_DIR = Path.home() / ".alphalens" / "thematic_events"
DEFAULT_LOOKBACK_DAYS = 30
_TITLE_MAX_LEN = 200


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

    # Pick the newest by published_at.
    joined["published_at"] = pd.to_datetime(joined["published_at"], errors="coerce")
    joined = joined.dropna(subset=["published_at"])
    if joined.empty:
        return None

    top = joined.sort_values("published_at", ascending=False).iloc[0]
    title = str(top.get("title", "") or "")
    if len(title) > _TITLE_MAX_LEN:
        title = textwrap.shorten(title, width=_TITLE_MAX_LEN, placeholder="…")
    return {
        "url": str(top.get("url", "") or ""),
        "title": title,
        "published_at": top["published_at"].date().isoformat(),
    }


__all__ = ["DEFAULT_EVENTS_DIR", "DEFAULT_NEWS_DIR", "find_trigger_event"]
