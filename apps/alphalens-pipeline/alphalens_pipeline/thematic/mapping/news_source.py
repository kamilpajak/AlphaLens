"""Which news store a catalyst is resolved against.

The thematic news ingest runs several times a day with ``--force``, so each run
OVERWRITES ``thematic_news/<date>.parquet`` while appending the same frame to an
immutable lake at ``thematic_news_lake/session_date=<date>/run=<stamp>.parquet``.
Extraction is cumulative, so events accumulate the union of every run's view
while the view keeps only the last run's rows.

Measured on the production stores 2026-11-09: 11,288 of 42,037 event rows
(26.85%) reference a ``news_id`` the view no longer holds, and 10,887 of those
11,088 distinct ids (98.2%) are still in the lake. The resolver joins events to
news with ``how="inner"``, so those events are dropped and a theme can lose its
catalyst to a retention artefact rather than to the news. The cause is feed
churn, not cap competition: an orphan is almost always present in the day's
first ingest run and absent from its last, because GDELT and RSS are rolling
windows. A larger cap does not fix it.

This module is the seam that decides what the resolver reads, kept apart from
``catalyst_resolver`` because "where does news come from" is a different
question from "which event is the catalyst".
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

#: The append-only lake the ingest writes beside the current view. Every run of
#: a day lands here and is never replaced.
DEFAULT_NEWS_LAKE_DIR = Path.home() / ".alphalens" / "thematic_news_lake"

#: Reading the lake fixes the orphan loss AND changes which catalyst is rendered
#: into the proposal prompt. ``theme_mapper.mapper_config_version`` exists to
#: fingerprint exactly that kind of change, and the accrual window in
#: ``docs/research/channel_feature_forward_prereg_2026_08_19.md`` (LOCKED) ends
#: when the rendered prompt changes.
#:
#: So this ships FALSE: the capability lands, the cohort key stays byte-identical
#: and nothing in production moves. Turning it on is a one-line commit that must
#: carry the pre-registration amendment. Nothing else has to be edited with it —
#: see :func:`news_source_token`.
READ_NEWS_LAKE = False

#: Values recorded in ``mapper_config_version(news_source=...)``. The ABSENCE of
#: the key is the historical cohort, so ``view`` is never passed in practice; it
#: exists so the vocabulary is closed and readable rather than implied by a bool.
NEWS_SOURCE_VIEW = "view"
NEWS_SOURCE_VIEW_PLUS_LAKE = "view+lake"

_LAKE_SESSION_PREFIX = "session_date="
_LAKE_RUN_GLOB = "run=*.parquet"
#: Sorts before any real run stamp, so a view row wins a body-length tie and the
#: union is deterministic regardless of filesystem iteration order.
_VIEW_RUN_SENTINEL = ""
_NEWS_ID_COLUMN = "id"
_NEWS_BODY_COLUMN = "body"
_RUN_ORDER_COLUMN = "_news_run"
_BODY_LENGTH_COLUMN = "_news_body_chars"
_TRANSIENT_COLUMNS = (_RUN_ORDER_COLUMN, _BODY_LENGTH_COLUMN)


def news_source_token() -> str | None:
    """What ``mapper_config_version`` records about which news store was read.

    ``None`` while the view alone is read, which keeps the key ABSENT and the
    cohort key byte-identical to every candidate parquet frozen before this
    existed. Not-``None`` the moment the lake is read as well.

    The caller passes this UNCONDITIONALLY. That is the point: a guard asking
    "did someone remember to record the boundary" tests a convention, and the
    first version of this change shipped exactly such a guard — it passed under
    a mutation that flipped the flag, because it fed the token in by hand
    instead of reading what production computes. One constant decides both what
    is read and what is recorded, so the two cannot drift.
    """
    return NEWS_SOURCE_VIEW_PLUS_LAKE if READ_NEWS_LAKE else None


def load_lake_window(lake_dir: Path, asof: dt.date, lookback_days: int) -> pd.DataFrame:
    """Every run of every ``session_date`` inside the window, concatenated.

    The lake's layout is a directory per session date holding one parquet per
    ingest run, so this cannot reuse ``catalyst_resolver._load_window``, which
    globs a flat directory of ``<date>.parquet``. A missing lake is empty, never
    an error: the lake write is best-effort by design and a host that has never
    run the ingest has no lake at all.
    """
    if not lake_dir.exists():
        return pd.DataFrame()
    lo = asof - dt.timedelta(days=lookback_days)
    frames: list[pd.DataFrame] = []
    for session in sorted(lake_dir.glob(f"{_LAKE_SESSION_PREFIX}*")):
        if not session.is_dir():
            continue
        try:
            date = dt.date.fromisoformat(session.name[len(_LAKE_SESSION_PREFIX) :])
        except ValueError:
            continue
        if date < lo or date > asof:
            continue
        for path in sorted(session.glob(_LAKE_RUN_GLOB)):
            try:
                frame = pd.read_parquet(path)
            except Exception as exc:
                logger.warning("failed to read %s: %s", path, exc)
                continue
            frame[_RUN_ORDER_COLUMN] = path.stem
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def merge_news_sources(view: pd.DataFrame, lake: pd.DataFrame) -> pd.DataFrame:
    """One row per news id, keeping the LONGEST body seen for it.

    Longest rather than newest because the point is to recover TEXT, not merely
    rows: measured 2026-11-09, the 7,330 ``edgar_press_release`` ids the view had
    dropped carry a median body of 153,648 characters against 22,889 for the
    6,765 it kept. A newest-wins rule would hand back the row the cap already
    chose and recover nothing.

    Ties resolve to the view, then to the earliest run stamp, so the result does
    not depend on filesystem iteration order. The transient sort columns never
    reach the caller on any path, including the single-frame early return and a
    frame with no ``body`` column at all.
    """
    frames = [f for f in (view, lake) if not f.empty]
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0].drop(columns=list(_TRANSIENT_COLUMNS), errors="ignore").copy()
    merged = pd.concat(frames, ignore_index=True)
    if _RUN_ORDER_COLUMN not in merged.columns:
        merged[_RUN_ORDER_COLUMN] = _VIEW_RUN_SENTINEL
    merged[_RUN_ORDER_COLUMN] = merged[_RUN_ORDER_COLUMN].fillna(_VIEW_RUN_SENTINEL)
    body = merged.get(_NEWS_BODY_COLUMN)
    merged[_BODY_LENGTH_COLUMN] = body.fillna("").astype(str).str.len() if body is not None else 0
    merged = merged.sort_values(
        [_BODY_LENGTH_COLUMN, _RUN_ORDER_COLUMN], ascending=[False, True], kind="stable"
    )
    merged = merged.drop_duplicates(subset=[_NEWS_ID_COLUMN], keep="first")
    return merged.drop(columns=list(_TRANSIENT_COLUMNS)).reset_index(drop=True)


def with_lake(
    view: pd.DataFrame, lake_dir: Path, asof: dt.date, lookback_days: int
) -> pd.DataFrame:
    """The news the resolver joins against: ``view``, plus the lake when enabled.

    A UNION, never a replacement. Measured 2026-11-09, 3,638 view ids have no
    lake row at all because the view starts 2026-05-18 and the lake starts
    2026-06-04; reading the lake alone would silently drop that history.
    """
    if not READ_NEWS_LAKE:
        return view
    return merge_news_sources(view, load_lake_window(lake_dir, asof, lookback_days))


__all__ = [
    "DEFAULT_NEWS_LAKE_DIR",
    "NEWS_SOURCE_VIEW",
    "NEWS_SOURCE_VIEW_PLUS_LAKE",
    "READ_NEWS_LAKE",
    "load_lake_window",
    "merge_news_sources",
    "news_source_token",
    "with_lake",
]
