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
import json
import logging
import re
import textwrap
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.thematic import dedup, text_similarity
from alphalens_pipeline.thematic.extraction.schema import NOISE_EVENT_TYPES
from alphalens_pipeline.thematic.extraction.templates.holdout import (
    HOLDOUT_SUPERSEDED_BY_TEMPLATE,
    TemplateMetrics,
)
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload

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

# PR-2 precedence rule: when both a template event AND a Flash event exist
# for the same (primary_entity_ticker, event_type) within this window, the
# template event wins and the Flash event is dropped to holdout. 24h is the
# design-memo §1.1 value — keeps duplicate-reporting collisions clustered
# while letting separate corporate actions on the same ticker (e.g. a
# Tuesday acquisition + a Friday earnings call) coexist.
SUPERSESSION_WINDOW = dt.timedelta(hours=24)


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


def _primary_ticker(value: Any) -> str | None:
    """Pick the FIRST resolved entity ticker for the supersession key.

    The precedence rule keys on a single ticker per event because the
    typical duplicate-reporting case ("six outlets cover the same M&A")
    always shares the acquirer + the target; pinning on the first
    primary_entity gives a stable key without requiring the templates +
    the LLM to agree on entity ordering. Multi-ticker corporate actions
    where the FIRST entity differs across outlets are rare enough that
    the dedup miss is acceptable for PR-2; PR-4 (multi-source dedup
    via template tuples) handles the full set-overlap case.

    Defensive shape coercion mirrors ``_entity_set`` — pandas-typed
    cells can land as numpy scalars (float NaN), so we narrow to the
    iterable cases explicitly.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip().upper()
        return stripped or None
    if not isinstance(value, list | tuple | set | frozenset) and not hasattr(value, "__iter__"):
        return None
    try:
        for e in value:
            s = str(e).strip().upper()
            if s:
                return s
    except TypeError:
        return None
    return None


def _apply_template_precedence(
    joined: pd.DataFrame,
    time_col: str,
    *,
    metrics: TemplateMetrics | None,
) -> pd.DataFrame:
    """Drop Flash events superseded by a template event in the same 24h slot.

    Design memo §1.1 precedence rule. Operates on the post-join frame so
    each row already has its news ``timestamp``. The pass is a no-op
    when ``extraction_method`` is absent (pre-PR-2 parquet) — those rows
    are treated as flash and pass through unchanged.
    """
    if "extraction_method" not in joined.columns:
        return joined
    if joined.empty:
        return joined

    # Project the supersession key per row. Rows whose ticker can't be
    # determined skip the dedup pass — keeping them is safer than
    # dropping a potentially-real catalyst on a missing key.
    keys = joined.apply(
        lambda r: (_primary_ticker(r.get("primary_entities")), r.get("event_type")),
        axis=1,
    )
    joined = joined.assign(_super_key=keys)

    keep_mask = pd.Series(True, index=joined.index)
    superseded_count = 0
    for key, group in joined.groupby("_super_key", sort=False):
        # pandas types ``key`` as Hashable, but the underlying column was
        # built from a 2-tuple per row above. Explicit unpack with a
        # length check keeps pyright happy and guards against future
        # changes to the key shape.
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        ticker, _event_type = key
        if ticker is None or len(group) < 2:
            continue
        templates = group[group["extraction_method"] == "template"]
        if templates.empty:
            continue
        # Each template row asserts a 24h window — Flash rows whose
        # timestamp falls inside any of those windows are dropped.
        for _, tmpl_row in templates.iterrows():
            tmpl_ts = tmpl_row[time_col]
            lo = tmpl_ts - SUPERSESSION_WINDOW
            hi = tmpl_ts + SUPERSESSION_WINDOW
            in_window = (group[time_col] >= lo) & (group[time_col] <= hi)
            flash_in_window = group[in_window & (group["extraction_method"] == "flash")]
            if flash_in_window.empty:
                continue
            keep_mask.loc[flash_in_window.index] = False
            superseded_count += len(flash_in_window)

    if superseded_count and metrics is not None:
        for _ in range(superseded_count):
            metrics.record_drop(HOLDOUT_SUPERSEDED_BY_TEMPLATE)

    return joined[keep_mask].drop(columns="_super_key")


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


def _coerce_template_facts(raw: Any) -> dict | None:
    """Best-effort deserialisation of the ``template_fields_json`` column.

    Returns the parsed dict when ``raw`` is a non-empty JSON object
    string; ``None`` on missing column / NaN / malformed JSON / non-dict
    payload. The brief generator's absent-block branch fires on None
    instead of crashing — a corrupt row is degenerate but should not
    take down the day's brief.
    """
    if raw is None:
        return None
    try:
        if pd.isna(raw):
            return None
    except (TypeError, ValueError):
        # pd.isna raises on some pandas-typed sequences; treat as not-NaN.
        pass
    if not isinstance(raw, str):
        return None
    raw_stripped = raw.strip()
    if not raw_stripped:
        return None
    try:
        decoded = json.loads(raw_stripped)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("template_fields_json failed to parse: %s", exc)
        return None
    if not isinstance(decoded, dict) or not decoded:
        return None
    return decoded


def _build_catalyst_payload_v2(
    catalyst: pd.Series,
    trigger: pd.Series,
    time_col: str,
    *,
    echo_count: int,
) -> CatalystPayload:
    """Build the resolver's typed return payload from catalyst+trigger pair.

    ``catalyst`` is the root of the story arc (earliest entity-overlapping
    event); ``trigger`` is the latest event that activated the brief.
    When ``echo_count == 1`` the resolver degraded to single-event mode and
    ``catalyst is trigger``; in that case the trigger-* fields equal the
    primary fields and ``is_amplified`` is False.

    PR-3: surfaces ``template_id`` + ``template_facts`` when the catalyst
    row is a template-extracted event (PR-2 ``extraction_method`` column).
    Flash rows surface both as ``None`` so the orchestrator's projection
    has a predictable shape on both paths. The fields are read from the
    ``catalyst`` (not ``trigger``) row because the catalyst is the
    earliest entity-overlapping event — the typed facts authored there
    are what the brief should cite.
    """
    title = str(catalyst.get("title", "") or "")
    if len(title) > _TITLE_MAX_LEN:
        title = textwrap.shorten(title, width=_TITLE_MAX_LEN, placeholder="…")
    template_id_raw = catalyst.get("template_id") if "template_id" in catalyst.index else None
    template_id = (
        str(template_id_raw)
        if template_id_raw is not None
        and not (isinstance(template_id_raw, float) and pd.isna(template_id_raw))
        else None
    )
    template_fields_raw = (
        catalyst.get("template_fields_json") if "template_fields_json" in catalyst.index else None
    )
    return CatalystPayload(
        url=str(catalyst.get("url", "") or ""),
        title=title,
        published_at=catalyst[time_col].date().isoformat(),
        event_type=str(catalyst.get("event_type", "") or "") or None,
        confidence=float(catalyst["confidence"]) if pd.notna(catalyst.get("confidence")) else None,
        second_order_implications=_soi_list(catalyst.get("second_order_implications")),
        echo_count=int(echo_count),
        trigger_url=str(trigger.get("url", "") or ""),
        trigger_published_at=trigger[time_col].date().isoformat(),
        is_amplified=int(echo_count) > 1,
        # PR-3: typed-fact provenance.
        template_id=template_id,
        template_facts=_coerce_template_facts(template_fields_raw),
    )


def _build_catalyst_payload(top: pd.Series, time_col: str) -> CatalystPayload:
    """Backward-compatible single-event payload (no arc): catalyst == trigger."""
    return _build_catalyst_payload_v2(top, top, time_col, echo_count=1)


def find_trigger_event(
    *,
    theme: str,
    asof: dt.date,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    news_dir: Path = DEFAULT_NEWS_DIR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    metrics: TemplateMetrics | None = None,
) -> CatalystPayload | None:
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

    # PR-4 multi-source dedup: collapse multi-outlet echoes of the same
    # template-extracted event ((template_id, entity_set, 24h-window)
    # tuple) BEFORE the precedence pass. Without this, ten outlets
    # reporting the same M&A would each run through supersession-window
    # arithmetic + theme-arc traversal as if they were ten distinct
    # events. Flash rows pass through untouched (Flash dedup is the
    # existing PR #141/#142 ingest-time Jaccard's job).
    joined = dedup.dedup_template_events(joined, time_col=time_col)
    if joined.empty:
        return None

    # PR-2 precedence rule: drop Flash events superseded by a template
    # event in the same (ticker, event_type, 24h) slot. Runs AFTER the
    # time-column coercion so the window arithmetic uses pandas datetime
    # rather than strings.
    joined = _apply_template_precedence(joined, time_col, metrics=metrics)
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


def _normalize_symbol(sym: str) -> str:
    """Fold a ticker to a comparison key: strip, upper-case, unify ``.`` / ``-``.

    Many news feeds emit class shares as ``BRK.B`` while SEC / our universe use
    ``BRK-B``. Subject-match (issue #395) must not silently miss on that
    separator difference, so both the index keys and the lookup key route
    through this. Empty input maps to ``""`` (caller treats as no-match).
    """
    return sym.strip().upper().replace(".", "-")


def _template_catalyst_sort_key(payload: CatalystPayload) -> tuple[int, int, str]:
    """Best-first ordering for >=2 template events on one ticker (#395).

    Mirrors the ``dedup`` survivor rule so a multi-event ticker resolves the
    SAME survivor whether duplicates were collapsed by PR-4 dedup or by this
    index. Ascending sort yields best-first:
      1. richest ``template_facts`` (most non-null values) -- DESC (negated)
      2. most-recent ``published_at`` -- DESC (negated ordinal)
      3. lexical ``url`` -- ASC (deterministic final tiebreak)

    Richness counts the already-coerced ``template_facts`` dict (the payload
    shape), matching ``orchestrator._template_facts_richness``; ``dedup``
    counts the raw JSON string. The two agree for well-formed rows -- they are
    separate implementations because each runs on a different stage's shape.
    """
    facts = payload.template_facts or {}
    richness = sum(1 for v in facts.values() if v is not None)
    try:
        published_ord = dt.date.fromisoformat(payload.published_at or "").toordinal()
    except (ValueError, TypeError):
        published_ord = dt.date.min.toordinal()
    return (-richness, -published_ord, payload.url or "")


def build_template_entity_index(
    *,
    asof: dt.date,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    news_dir: Path = DEFAULT_NEWS_DIR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    metrics: TemplateMetrics | None = None,
) -> dict[str, list[CatalystPayload]]:
    """Index template-extracted events by EACH primary-entity ticker (#395).

    Built ONCE per scoring batch so the per-candidate lookup is O(1). Returns
    ``{NORMALIZED_TICKER: [payload, ...]}`` where each payload is a
    :class:`CatalystPayload` and the per-ticker list is
    pre-sorted best-first by :func:`_template_catalyst_sort_key` (lookup takes
    ``[0]``).

    Subject-match contract (option b): a ticker maps to a payload iff it is a
    member of the event's ``primary_entities`` -- the filing SUBJECT (M&A
    acquirer/target, regulated party, earnings name). Theme is irrelevant;
    ``themes=[]`` template rows still get indexed. ``metrics`` is accepted for
    signature parity with :func:`find_trigger_event` but unused (no Flash rows
    to supersede in a template-only frame).

    Only ``extraction_method == "template"`` rows are indexed -- Flash rows are
    the theme-keyed :func:`find_trigger_event` path's job. The SAME
    noise/blocklist filter, time coercion, and PR-4 dedup as the theme path run
    first, so subject-match cannot surface an event the theme path would drop.

    DELIBERATE divergence from ``_apply_template_precedence`` (which keys
    supersession on the first primary entity only): this index maps a payload to
    EVERY primary entity, so for a multi-entity event (acquirer NVDA + target
    ARM) BOTH legitimately get the typed facts -- the subject-match intent.

    Returns ``{}`` on any missing dir / empty window / absent
    ``extraction_method`` column, so the scorer degrades to the theme-only path.
    """
    del metrics  # signature parity only; template-only frame has nothing to supersede
    events = _load_window(events_dir, asof, lookback_days)
    if events.empty or "extraction_method" not in events.columns:
        return {}
    events = events[events["extraction_method"] == "template"]
    if events.empty:
        return {}

    news = _load_window(news_dir, asof, lookback_days)
    if news.empty:
        return {}

    joined = events.merge(news, left_on="news_id", right_on="id", how="inner")
    if joined.empty:
        return {}

    joined = _apply_noise_and_blocklist_filters(joined)
    if joined.empty:
        return {}

    time_col = _resolve_time_column(joined)
    if time_col is None:
        return {}
    joined[time_col] = pd.to_datetime(joined[time_col], errors="coerce", utc=True)
    joined = joined.dropna(subset=[time_col])
    if joined.empty:
        return {}

    # Collapse multi-outlet echoes of the SAME template event before indexing
    # (one payload per (template_id, entity_set, 24h) cluster).
    joined = dedup.dedup_template_events(joined, time_col=time_col)
    if joined.empty:
        return {}

    joined = joined.reset_index(drop=True)
    index: dict[str, list[CatalystPayload]] = {}
    for _, row in joined.iterrows():
        payload = _build_catalyst_payload_v2(row, row, time_col, echo_count=1)
        if payload.template_id is None:
            # Defensive: the extraction_method filter should guarantee a
            # template_id; a corrupt row without one is not a usable catalyst.
            continue
        for sym in _entity_set(row):
            index.setdefault(_normalize_symbol(sym), []).append(payload)

    for payloads in index.values():
        payloads.sort(key=_template_catalyst_sort_key)
    return index


def find_template_catalyst_for_ticker(
    *,
    ticker: str,
    asof: dt.date,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    news_dir: Path = DEFAULT_NEWS_DIR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    metrics: TemplateMetrics | None = None,
    entity_index: dict[str, list[CatalystPayload]] | None = None,
) -> CatalystPayload | None:
    """Best subject-match template catalyst for ``ticker`` (option b, #395).

    Fires iff ``ticker`` is a ``primary_entities`` member of a
    template-extracted event in the window -- independent of theme. Returns the
    same payload shape as :func:`find_trigger_event` (carries ``template_id`` +
    ``template_facts``) so the scorer consumes it unchanged. ``None`` when no
    such event exists.

    ``entity_index`` is the per-batch index from
    :func:`build_template_entity_index`. When the scorer passes it, this is an
    O(1) lookup. When ``None`` (isolated / test path) the index is built for the
    single window -- the ``_load_window`` LRU cache keeps the disk read cheap.
    """
    if entity_index is None:
        entity_index = build_template_entity_index(
            asof=asof,
            events_dir=events_dir,
            news_dir=news_dir,
            lookback_days=lookback_days,
            metrics=metrics,
        )
    payloads = entity_index.get(_normalize_symbol(ticker))
    if not payloads:
        return None
    return payloads[0]


__all__ = [
    "DEFAULT_EVENTS_DIR",
    "DEFAULT_NEWS_DIR",
    "build_template_entity_index",
    "find_template_catalyst_for_ticker",
    "find_trigger_event",
]
