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
from typing import Any

import pandas as pd

from alphalens_pipeline.thematic import text_similarity
from alphalens_pipeline.thematic.extraction.schema import NOISE_EVENT_TYPES

logger = logging.getLogger(__name__)

DEFAULT_NEWS_DIR = Path.home() / ".alphalens" / "thematic_news"
DEFAULT_EVENTS_DIR = Path.home() / ".alphalens" / "thematic_events"
DEFAULT_LOOKBACK_DAYS = 30
_TITLE_MAX_LEN = 200
_NOISE_FILTERS_PATH = Path(__file__).parent.parent / "config" / "catalyst_noise_filters.yaml"

# Tier 2 story-arc parameters. Below MIN_TRIGGER_ENTITIES the resolver
# degrades to legacy "latest event = catalyst" behaviour, because a single
# entity is too sparse a signal to define a story arc — it would pull in
# unrelated newsletters happening to mention the same ticker.
ENTITY_JACCARD_THRESHOLD = text_similarity.ENTITY_JACCARD_THRESHOLD
MIN_TRIGGER_ENTITIES = 2


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


@lru_cache(maxsize=8)
def _load_window(parquet_dir: Path, asof: dt.date, lookback_days: int) -> pd.DataFrame:
    """Concat all parquets in ``[asof - lookback, asof]`` from ``parquet_dir``.

    LRU-cached on (dir, asof, lookback) — scorer.py calls find_trigger_event
    once per unique theme in a scoring batch, and each call reads the same
    7-day events + news windows. Cache eliminates the redundant disk reads.
    Cache size 8 = 2 dirs (events + news) × 4 distinct asofs in flight.
    """
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


def _apply_noise_and_blocklist_filters(joined: pd.DataFrame) -> pd.DataFrame:
    """Drop event_type ∈ NOISE_EVENT_TYPES and URLs matching the blocklist regex."""
    if "event_type" in joined.columns:
        joined = joined[~joined["event_type"].isin(NOISE_EVENT_TYPES)]
        if joined.empty:
            return joined

    blocklist = _load_url_blocklist_patterns()
    if blocklist and "url" in joined.columns:
        joined = joined[
            ~joined["url"].astype(str).apply(lambda u: _url_matches_blocklist(u, blocklist))
        ]
    return joined


def _resolve_time_column(joined: pd.DataFrame) -> str | None:
    """Canonical schema uses ``timestamp``; older parquets carry ``published_at``."""
    if "timestamp" in joined.columns:
        return "timestamp"
    if "published_at" in joined.columns:
        return "published_at"
    return None


def _soi_list(value: Any) -> list[str]:
    if value is None:
        return []
    # Defensive: a bare string would otherwise be shredded into per-character
    # entries by ``for s in value``. Wrap as a single-element list instead so
    # an LLM emitting a string-vs-list mistake doesn't corrupt the brief.
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    try:
        return [str(s) for s in value]
    except TypeError:
        return []


def _entity_set(row: pd.Series) -> set[str]:
    """Coerce a row's ``primary_entities`` field to a Python set of upper-cased strings.

    Mirrors the access pattern used by ``_has_theme`` for the ``themes`` field
    but operates on a full row so it can be applied row-wise via ``.apply``.
    Returns an empty set if the column is missing, ``None``, or non-iterable.
    """
    try:
        val = row.get("primary_entities")
    except (AttributeError, TypeError):
        return set()
    if val is None:
        return set()
    # Defensive: a bare string (an LLM mistake emitting ``"AAPL"`` instead of
    # ``["AAPL"]``) would otherwise be iterated character-by-character into
    # ``{"A", "P", "L"}`` and poison the entity-overlap arc. Treat it as a
    # single-entity input instead.
    if isinstance(val, str):
        stripped = val.strip().upper()
        return {stripped} if stripped else set()
    try:
        return {str(e).strip().upper() for e in val if str(e).strip()}
    except TypeError:
        return set()


def _build_catalyst_payload_v2(
    catalyst: pd.Series,
    trigger: pd.Series,
    time_col: str,
    *,
    echo_count: int,
) -> dict:
    """Build the resolver's return payload from catalyst+trigger pair.

    ``catalyst`` is the root of the story arc (earliest entity-overlapping
    event); ``trigger`` is the latest event that activated the brief.
    When ``echo_count == 1`` the resolver degraded to single-event mode and
    ``catalyst is trigger``; in that case the trigger-* fields equal the
    primary fields and ``is_amplified`` is False.
    """
    title = str(catalyst.get("title", "") or "")
    if len(title) > _TITLE_MAX_LEN:
        title = textwrap.shorten(title, width=_TITLE_MAX_LEN, placeholder="…")
    return {
        "url": str(catalyst.get("url", "") or ""),
        "title": title,
        "published_at": catalyst[time_col].date().isoformat(),
        "event_type": str(catalyst.get("event_type", "") or "") or None,
        "confidence": float(catalyst["confidence"])
        if pd.notna(catalyst.get("confidence"))
        else None,
        "second_order_implications": _soi_list(catalyst.get("second_order_implications")),
        "echo_count": int(echo_count),
        "trigger_url": str(trigger.get("url", "") or ""),
        "trigger_published_at": trigger[time_col].date().isoformat(),
        "is_amplified": int(echo_count) > 1,
    }


def _build_catalyst_payload(top: pd.Series, time_col: str) -> dict:
    """Backward-compatible single-event payload (no arc): catalyst == trigger."""
    return _build_catalyst_payload_v2(top, top, time_col, echo_count=1)


def find_trigger_event(
    *,
    theme: str,
    asof: dt.date,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    news_dir: Path = DEFAULT_NEWS_DIR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict | None:
    """Return the catalyst payload for a theme.

    Walks the rolling events window for events tagged with ``theme``, joins
    to news, and either:
      - returns the **latest** event (degraded mode) when the trigger has
        fewer than ``MIN_TRIGGER_ENTITIES`` primary entities, or
      - returns the **earliest** event in the entity-overlap story arc
        (entity Jaccard ≥ ``ENTITY_JACCARD_THRESHOLD`` against the trigger),
        plus ``echo_count`` / ``trigger_url`` / ``is_amplified`` metadata.
    """
    events = _load_window(events_dir, asof, lookback_days)
    if events.empty:
        return None

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

    joined = _apply_noise_and_blocklist_filters(joined)
    if joined.empty:
        return None

    time_col = _resolve_time_column(joined)
    if time_col is None:
        return None
    joined[time_col] = pd.to_datetime(joined[time_col], errors="coerce", utc=True)
    joined = joined.dropna(subset=[time_col])
    if joined.empty:
        return None

    joined = joined.reset_index(drop=True)
    trigger = joined.sort_values(time_col, ascending=False).iloc[0]
    trigger_entities = _entity_set(trigger)

    if len(trigger_entities) < MIN_TRIGGER_ENTITIES:
        return _build_catalyst_payload_v2(trigger, trigger, time_col, echo_count=1)

    arc_mask = joined.apply(
        lambda row: (
            text_similarity.entity_jaccard(_entity_set(row), trigger_entities)
            >= ENTITY_JACCARD_THRESHOLD
        ),
        axis=1,
    )
    arc = joined[arc_mask]
    if arc.empty:
        # Defensive: trigger itself should always satisfy jaccard(s, s) = 1.0.
        return _build_catalyst_payload_v2(trigger, trigger, time_col, echo_count=1)

    catalyst = arc.sort_values(time_col, ascending=True).iloc[0]
    return _build_catalyst_payload_v2(catalyst, trigger, time_col, echo_count=len(arc))


__all__ = ["DEFAULT_EVENTS_DIR", "DEFAULT_NEWS_DIR", "find_trigger_event"]
