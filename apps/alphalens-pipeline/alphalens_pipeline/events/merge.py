"""Merge event-lane candidates into the day's thematic candidates (epic #1293, #1296).

Called by ``alphalens thematic score`` ONLY when ``ALPHALENS_EVENT_LANE=1``.
The thematic candidates parquet is never rewritten (``map-themes`` reuses it
across the six daily slots), so the merge is an in-memory step on the frame
``score`` is about to enrich. Flag OFF -> the frame is untouched and the scored
parquet stays byte-identical.

Rules (pre-registration section 2, issue #1296):

- every thematic row is stamped ``source="thematic"``, ``event_overlap=False``;
- eligible event rows (``eligible == True``) whose ticker is NOT on the thematic
  list are appended with ``source="insider_cluster"``;
- a ticker present on BOTH sides yields ONE card: the thematic row is kept (its
  catalyst stays primary), ``event_overlap=True`` and the ``event_*`` facts are
  copied onto it — the episode is counted in both cohorts downstream;
- the event fact columns exist on every row (``None`` where not applicable) so
  the scored schema does not depend on whether a cluster happened that day.

Collision constraint: an event row must never carry a scorer enrichment name
(``selection_score``, ``catalyst_template_*``, ``technical_*``,
``scorer_config_version``...) — ``score_candidates`` left-merges its enrichment
on ``ticker`` and pandas would suffix the collision.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from alphalens_pipeline.events import (
    event_lane_enabled,  # noqa: F401 — re-exported for callers/tests
)
from alphalens_pipeline.events.insider_cluster import SOURCE_INSIDER_CLUSTER

logger = logging.getLogger(__name__)

SOURCE_THEMATIC = "thematic"
EVENT_FACT_COLUMNS: tuple[str, ...] = (
    "event_n_insiders",
    "event_cluster_usd",
    "event_buyers_json",
    "event_first_leg_date",
    "event_completing_accession",
    "event_acceptance_utc",
    "event_arrival_session",
    "event_filing_lag_bdays",
    "event_gate_version",
)
_SHADOW_ONLY_COLUMNS = ("eligible", "exclusion_reason")


def load_event_candidates(path: Path) -> pd.DataFrame:
    """The event-candidates parquet for one date, or a typed empty frame when absent."""
    if not path.exists():
        logger.warning("event lane ON but %s is missing; merging nothing", path)
        from alphalens_pipeline.events.insider_cluster_detect import EVENT_CANDIDATE_COLUMNS

        return pd.DataFrame(columns=list(EVENT_CANDIDATE_COLUMNS))
    return pd.read_parquet(path)


def merge_event_candidates(candidates: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Stamp ``source``/``event_overlap`` and append the eligible event rows (see module doc)."""
    out = candidates.copy()
    out["source"] = SOURCE_THEMATIC
    out["event_overlap"] = False
    for col in EVENT_FACT_COLUMNS:
        if col not in out.columns:
            out[col] = None
    if events is None or events.empty or "eligible" not in events.columns:
        return out

    # fillna(False): a NaN in a hand-authored parquet must never read as eligible.
    eligible = events[events["eligible"].fillna(False).astype(bool)].copy()
    if eligible.empty:
        return out
    eligible["ticker"] = eligible["ticker"].astype(str).str.upper()
    eligible = eligible.drop_duplicates("ticker", keep="first")

    thematic_tickers = out["ticker"].astype(str).str.upper() if len(out) else pd.Series(dtype=str)
    is_overlap = eligible["ticker"].isin(set(thematic_tickers))
    for ev in eligible[is_overlap].to_dict("records"):
        mask = (thematic_tickers == ev["ticker"]).to_numpy()
        out.loc[mask, "event_overlap"] = True
        for col in EVENT_FACT_COLUMNS:
            out.loc[mask, col] = ev.get(col)

    new = eligible[~is_overlap].drop(columns=list(_SHADOW_ONLY_COLUMNS), errors="ignore")
    if new.empty:
        return out
    new = new.assign(source=SOURCE_INSIDER_CLUSTER, event_overlap=False)
    # An all-null event column (e.g. llm_confidence) is object-typed; align it to
    # the thematic dtype so the concat does not demote a float64 column to object.
    for col in out.columns.intersection(new.columns):
        if new[col].isna().all() and pd.api.types.is_float_dtype(out[col]):
            new[col] = new[col].astype(out[col].dtype)
    merged = pd.concat([out, new], ignore_index=True)
    merged["verified"] = merged["verified"].astype(bool)
    merged["event_overlap"] = merged["event_overlap"].astype(bool)
    return merged
