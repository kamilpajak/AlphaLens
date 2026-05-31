"""Multi-source dedup for template-extracted events (epic #321 PR-4).

When the same M&A is reported by 10 outlets within a 24h window, every
outlet's article runs through the template engine, every match emits the
same typed event (`m_and_a_press_release`, `{NVDA, XYZ}`), and the
catalyst resolver + brief generator otherwise see ten duplicates of the
same event. This module collapses those echoes to one survivor row with
audit-friendly aggregate columns.

Why upstream of the brief layer:
  - ``orchestrator._sort_and_dedup_for_brief`` (PR-3) is a tiebreaker per
    ``(ticker, …)`` — it works because the LAST collision-resolution
    layer in the pipeline runs there. It does NOT help the catalyst
    resolver, which sees the un-collapsed ten-row frame and pays
    ten times the cost on supersession-window arithmetic + theme-arc
    traversal.
  - ``news_ingest._cluster_same_day_lexical`` (PRs #141/#142/#185) is a
    Tier-1 lexical clustering at ingest time — it collapses headline
    near-duplicates but does NOT key on the typed entity / template
    tuple. Two outlets that paraphrase the same M&A with disjoint title
    wording survive ingest and reach extraction as separate news rows.
    PR-4 is the semantic-tuple complement.
  - Per design memo §3 PR-4: "Ingest-stage clustering (today: same-day
    lexical Jaccard) extended with semantic-tuple dedup for template-
    extracted events."

Scope (strict):
  - Only ``template_id != null`` rows are subject to dedup. Flash rows
    (``template_id is None``) pass through unchanged. Flash dedup is
    a separate concern (the existing lexical Jaccard handles the
    common case; semantic dedup on a free-text LLM extraction would
    need entity normalisation + event_type-stability assumptions that
    the typed-template path gives us by construction).
  - Cluster key: ``(template_id, frozenset(primary_entities))``. Order
    of ``primary_entities`` is irrelevant — the set-based key handles
    outlet variation (acquirer-first vs target-first ordering).
  - Time window: sliding anchor — a new cluster starts when the next
    row's timestamp is more than ``window`` past the cluster's anchor
    (first row). Default 24h matches the supersession-window in
    ``catalyst_resolver.SUPERSESSION_WINDOW``.

The function is **pure** — no parquet I/O, no caching, no side effects.
Callers (`catalyst_resolver.find_trigger_event`) decide when to invoke
and on which subset.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = dt.timedelta(hours=24)

# New columns this module stamps on the output frame. Kept as module-level
# constants so the legacy-backfill branch in ``catalyst_resolver`` (and
# any future code reading deduped frames) can reference one source of
# truth instead of repeating string literals.
DEDUP_COUNT_COL = "dedup_count"
DEDUP_SOURCE_URLS_COL = "dedup_source_urls_json"
DEDUP_NEWS_IDS_COL = "dedup_news_ids_json"

_NEW_COLUMNS = (DEDUP_COUNT_COL, DEDUP_SOURCE_URLS_COL, DEDUP_NEWS_IDS_COL)


def _entity_key(value: Any) -> frozenset[str]:
    """Build a stable order-insensitive cluster key from ``primary_entities``.

    Mirrors the defensive shape coercion of
    ``catalyst_resolver._primary_ticker`` — pandas-typed cells can land
    as numpy arrays, scalars, or NaN; we narrow to the iterable case
    and uppercase + strip every element for cross-outlet consistency.
    Returns an empty frozenset on un-iterable input so the caller can
    treat "no resolved entities" as its own (degenerate) cluster bucket.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        s = value.strip().upper()
        return frozenset({s}) if s else frozenset()
    try:
        cleaned = {str(e).strip().upper() for e in value if str(e).strip()}
    except TypeError:
        return frozenset()
    return frozenset(cleaned)


def _fields_richness(value: Any) -> int:
    """Count non-null keys in a JSON-serialised template_fields payload.

    Drives the survivor-selection tiebreaker: the row with the most
    extracted fields wins, because that outlet's article carried the
    fullest set of typed facts (amounts, dates, secondary entities).
    The orchestrator uses the same convention at the brief layer
    (PR-3 ``_template_facts_richness``) — we deliberately re-implement
    inline rather than import across pipeline layers (the orchestrator
    consumes from this module, not the other way around).
    """
    if value is None:
        return 0
    if isinstance(value, float) and pd.isna(value):
        return 0
    if not isinstance(value, str):
        return 0
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return 0
    if not isinstance(parsed, dict):
        return 0
    return sum(1 for v in parsed.values() if v is not None)


def _assign_cluster_ids(group: pd.DataFrame, *, time_col: str, window: dt.timedelta):
    """Walk a same-(template_id, entity_key) group and emit cluster ids.

    Sliding-anchor rule: the first row of a cluster is the anchor; the
    cluster extends to include every subsequent row whose timestamp is
    within ``window`` of the anchor. A row outside that horizon starts
    a NEW cluster (and becomes its own anchor). Conservative vs the
    "next-row" sliding variant — keeps a 30-hour spread split into two
    clusters even if every adjacent pair is < 24h apart, which is the
    auditable semantics ("within one day of the source").
    """
    sorted_group = group.sort_values(time_col, kind="mergesort")
    cluster_ids: list[int] = []
    anchor_ts: pd.Timestamp | None = None
    next_id = 0
    for ts in sorted_group[time_col]:
        if anchor_ts is None or (ts - anchor_ts) > window:
            anchor_ts = ts
            cluster_ids.append(next_id)
            next_id += 1
        else:
            cluster_ids.append(next_id - 1)
    return sorted_group.assign(_dedup_cluster=cluster_ids)


def _collapse_cluster(cluster: pd.DataFrame, *, time_col: str) -> pd.Series:
    """Pick the survivor row + stamp the aggregate columns.

    Survivor selection:
      1. Most non-null keys in ``template_fields_json`` (richest extract).
      2. Tiebreak: earliest timestamp (first-outlet-wins matches the
         catalyst resolver's "earliest event in story arc" convention).
      3. Tiebreak-of-tiebreak: smaller ``news_id`` lexicographically —
         pure determinism guard so the same input always yields the same
         survivor regardless of upstream sort stability.

    Aggregate columns:
      - ``dedup_count`` = group size.
      - ``dedup_source_urls_json`` = JSON list of ``url`` values, ordered
        by timestamp ascending. ``None`` for singletons (audit cost vs
        benefit — a singleton's own row already carries the url).
      - ``dedup_news_ids_json`` = same shape for ``news_id`` values.
    """
    # Sort by survivor priority. ``richness`` is computed inline (no
    # per-row materialisation) so the cluster frame stays cheap on the
    # 10-row M&A-echo case + the larger templates-grow-to-30 future case.
    # Defensive: pre-PR-3 frames (template_id present, no
    # template_fields_json column) get 0 richness for every row, so the
    # tiebreaker falls through to timestamp + news_id.
    ordered = cluster.copy()
    if "template_fields_json" in ordered.columns:
        ordered["_richness"] = ordered["template_fields_json"].apply(_fields_richness)
    else:
        ordered["_richness"] = 0
    ordered = ordered.sort_values(
        by=["_richness", time_col, "news_id"],
        ascending=[False, True, True],
        kind="mergesort",
    )
    survivor = ordered.iloc[0].drop(labels=["_richness", "_dedup_cluster"])

    count = len(cluster)
    survivor[DEDUP_COUNT_COL] = count
    if count > 1:
        # Audit columns: URLs + news_ids in timestamp-ascending order
        # (the chronological story of the echo). Drop the temporary
        # _richness column before listing.
        chronological = cluster.sort_values(time_col, kind="mergesort")
        survivor[DEDUP_SOURCE_URLS_COL] = json.dumps(
            [str(u) for u in chronological["url"].tolist()]
        )
        survivor[DEDUP_NEWS_IDS_COL] = json.dumps(
            [str(n) for n in chronological["news_id"].tolist()]
        )
    else:
        survivor[DEDUP_SOURCE_URLS_COL] = None
        survivor[DEDUP_NEWS_IDS_COL] = None
    return survivor


def _empty_with_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Empty-input fast path: return a frame carrying the new columns."""
    out = df.copy()
    for col in _NEW_COLUMNS:
        if col not in out.columns:
            out[col] = pd.Series(dtype="object")
    return out


def _stamp_passthrough(rows: pd.DataFrame) -> pd.DataFrame:
    """Flash rows (or legacy frames) — pass through with count=1, nulls."""
    out = rows.copy()
    out[DEDUP_COUNT_COL] = 1
    out[DEDUP_SOURCE_URLS_COL] = None
    out[DEDUP_NEWS_IDS_COL] = None
    return out


def dedup_template_events(
    df: pd.DataFrame,
    *,
    time_col: str,
    window: dt.timedelta = DEFAULT_WINDOW,
) -> pd.DataFrame:
    """Collapse template-extracted echoes; pass Flash + singletons through.

    Args:
      df: post-join frame from ``catalyst_resolver`` (events ⋈ news). Must
        carry ``news_id``, ``primary_entities``, ``url``, and ``time_col``.
        May or may not carry ``template_id`` / ``template_fields_json``
        (legacy parquets); missing columns are treated as Flash-only.
      time_col: name of the timestamp column to cluster within. Typically
        ``published_at`` (joined from the news frame).
      window: sliding-anchor horizon. Default 24h matches
        ``catalyst_resolver.SUPERSESSION_WINDOW``.

    Returns a frame with the same rows MINUS collapsed echoes, PLUS three
    new columns: ``dedup_count``, ``dedup_source_urls_json``,
    ``dedup_news_ids_json``. Order of output rows is not guaranteed
    (caller should sort if needed).

    Idempotent — re-running on a deduplicated frame is a no-op. The
    presence of ``dedup_count`` in the input is the sentinel: a frame
    that already carries that column has already been collapsed, and
    a second pass would otherwise re-stamp every (now-singleton)
    survivor back to ``dedup_count=1`` — dropping the audit trail
    earned by the first pass. Short-circuit returns the frame as-is.
    """
    if df.empty:
        return _empty_with_columns(df)

    # Idempotence guard: input already deduped → no-op.
    if DEDUP_COUNT_COL in df.columns:
        return df

    # Defensive: legacy parquet may lack the template_id column entirely
    # (pre-PR-2 events). Treat the whole frame as Flash — no dedup work.
    if "template_id" not in df.columns:
        return _stamp_passthrough(df)

    template_mask = df["template_id"].notna() & (df["template_id"] != "")
    template_rows = df[template_mask]
    flash_rows = df[~template_mask]

    if template_rows.empty:
        return _stamp_passthrough(df)

    # Build the cluster key. Stored as a hashable column for groupby.
    template_rows = template_rows.copy()
    template_rows["_dedup_entity_key"] = template_rows["primary_entities"].apply(_entity_key)

    survivors: list[pd.Series] = []
    grouped = template_rows.groupby(
        ["template_id", "_dedup_entity_key"], sort=False, group_keys=False
    )
    for _key, bucket in grouped:
        # Within a (template_id, entity_key) bucket, run the sliding
        # window to split far-apart events into separate clusters.
        with_cluster = _assign_cluster_ids(bucket, time_col=time_col, window=window)
        for _cid, cluster in with_cluster.groupby("_dedup_cluster", sort=False):
            survivors.append(
                _collapse_cluster(cluster.drop(columns=["_dedup_entity_key"]), time_col=time_col)
            )

    template_out = pd.DataFrame(survivors).reset_index(drop=True) if survivors else pd.DataFrame()
    flash_out = _stamp_passthrough(flash_rows) if not flash_rows.empty else pd.DataFrame()

    # Concatenate; pandas aligns columns on union (the new audit columns
    # exist on both halves).
    if template_out.empty:
        return flash_out
    if flash_out.empty:
        return template_out.reset_index(drop=True)
    return pd.concat([template_out, flash_out], ignore_index=True)


__all__ = [
    "DEDUP_COUNT_COL",
    "DEDUP_NEWS_IDS_COL",
    "DEDUP_SOURCE_URLS_COL",
    "DEFAULT_WINDOW",
    "dedup_template_events",
]
