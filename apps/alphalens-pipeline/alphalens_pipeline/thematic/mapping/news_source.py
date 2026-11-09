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

#: The lake directory's name, resolved as a SIBLING of whatever news directory
#: the caller passed. Never an absolute default: a caller that isolated its news
#: store to a temporary directory must isolate the lake with it, or the moment
#: the flag flips every golden fixture, recorder and resolver test on a host that
#: has a lake joins the PRODUCTION one. ``news_ingest.ingest_daily`` already
#: documents and uses exactly this convention for the write side, and
#: ``lake_dir_for(DEFAULT_NEWS_DIR)`` equals ``news_ingest.DEFAULT_LAKE_DIR``, so
#: production is unchanged.
LAKE_DIR_NAME = "thematic_news_lake"


def lake_dir_for(news_dir: Path) -> Path:
    """The lake that belongs to ``news_dir``.

    Derived rather than defaulted because an absolute default is early-bound at
    ``def`` time: patching a module constant does NOT redirect a signature that
    already captured it, so an isolated caller cannot opt out even deliberately.
    """
    return Path(news_dir).parent / LAKE_DIR_NAME


#: Reading the lake fixes the orphan loss AND changes which catalyst is rendered
#: into the proposal prompt. ``theme_mapper.mapper_config_version`` exists to
#: fingerprint exactly that kind of change, and the accrual window in
#: ``docs/research/channel_feature_forward_prereg_2026_08_19.md`` (LOCKED) ends
#: when the rendered prompt changes.
#:
#: So this ships FALSE: the capability lands, the cohort key stays byte-identical
#: and nothing in production moves. Flipping it needs no second EDIT — see
#: :func:`news_source_token` — but it is NOT a one-line change, and an earlier
#: version of this comment claimed it was. Measured on the production host
#: 2026-11-09, 30-day window (``DEFAULT_LOOKBACK_DAYS``), asof 2026-11-08:
#:
#:   stage                     time      rows     peak RSS
#:   _load_window (view)       1.31 s    5,321    0.90 GB
#:   load_lake_window          7.25 s   36,255    6.29 GB   (93 files)
#:   pd.concat                 0.05 s   36,255    6.28 GB
#:   _within_window              —         —      killed at 10.01 GB
#:
#: A 7-day window completed at 6.69 GB peak against 0.46 GB for the view alone:
#: 33x the time, 14x the memory. Of the window's 4,658 MB uncompressed, ``body``
#: is 4,623 MB (99.2%) — and ``body`` has NO consumer below the news join, proven
#: by dropping the column right after this module's union and getting a
#: byte-identical resolver result on six real themes. Reading only
#: ``[id, source, timestamp, title, url, extra]`` costs 0.53 s and 0.172 GB.
#:
#: There is also no cache on this path while ``_load_window`` has an
#: ``lru_cache``, so a real run re-read the lake four times for six themes.
#: And the ceiling does not bind: ``alphalens-thematic-build.service`` sets
#: ``MemoryMax=4G`` but launches ``docker run``, and the container cgroup reports
#: ``memory.max = max`` with ``HostConfig.Memory=0``. So >10 GB comes out of the
#: host's 19.5 GB, beside Postgres and both broker daemons.
#:
#: THEREFORE, before this is flipped: prune the read, cache it, and bound it.
#: Pruning interacts with the longest-body rule below, so it is a decision about
#: that rule and not a mechanical change.
READ_NEWS_LAKE = False

#: Values recorded in ``mapper_config_version(news_source=...)``. The ABSENCE of
#: the key is the historical cohort, so ``view`` is never passed in practice; it
#: exists so the vocabulary is closed and readable rather than implied by a bool.
NEWS_SOURCE_VIEW = "view"
NEWS_SOURCE_VIEW_PLUS_LAKE = "view+lake"

_LAKE_SESSION_PREFIX = "session_date="
_LAKE_RUN_GLOB = "run=*.parquet"
_NEWS_ID_COLUMN = "id"
_NEWS_BODY_COLUMN = "body"
_TIMESTAMP_COLUMN = "timestamp"
_BODY_LENGTH_COLUMN = "_news_body_chars"
_TRANSIENT_COLUMNS = (_BODY_LENGTH_COLUMN,)


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


def load_lake_window(
    lake_dir: Path,
    asof: dt.date,
    lookback_days: int,
    *,
    needed_ids: frozenset[str] | None = None,
) -> pd.DataFrame:
    """The runs of every ``session_date`` inside the window, concatenated.

    ``needed_ids`` restricts the read to news ids some event actually points at,
    pushed down to the parquet reader so the rows are never materialised.
    Measured on the production lake, 30-day window, asof 2026-11-08: of the
    12,754 ids the lake holds, 6,579 (51.6%) are referenced by no event at all,
    and of the 7,440 ids only the lake has, just 861 (11.6%) have an event. The
    lake stores the raw per-source union from BEFORE dedup, clustering, the
    single-day filter and the cap, while events come only from VIEW rows, so most
    of an unrestricted read cannot join by construction. That read reached
    10.01 GB RSS on a 30-day window and was killed.

    ``None`` means no restriction, which is the pre-existing behaviour. An EMPTY
    set means no event needs anything, and then no file is opened at all.

    The lake's layout is a directory per session date holding one parquet per
    ingest run, so this cannot reuse ``catalyst_resolver._load_window``, which
    globs a flat directory of ``<date>.parquet``. A missing lake is empty, never
    an error: the lake write is best-effort by design and a host that has never
    run the ingest has no lake at all.
    """
    if not lake_dir.exists():
        return pd.DataFrame()
    if needed_ids is not None and not needed_ids:
        return pd.DataFrame()
    read_kwargs: dict[str, object] = {}
    if needed_ids is not None:
        read_kwargs["filters"] = [(_NEWS_ID_COLUMN, "in", sorted(needed_ids))]
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
                frame = pd.read_parquet(path, **read_kwargs)
            except Exception as exc:
                logger.warning("failed to read %s: %s", path, exc)
                continue
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return _within_window(pd.concat(frames, ignore_index=True), asof, lookback_days)


def _within_window(frame: pd.DataFrame, asof: dt.date, lookback_days: int) -> pd.DataFrame:
    """Filter lake rows on their OWN timestamp, not just the partition name.

    The lake stores the raw per-source union from BEFORE the ingest's strict
    single-UTC-day filter (``news_ingest.py`` writes the lake run beside, not
    after, that filter). A partition-name filter alone therefore re-admits rows
    the ingest deliberately excluded from the day, which the view never contained
    and no downstream stage has ever seen.
    """
    if frame.empty or _TIMESTAMP_COLUMN not in frame.columns:
        return frame
    stamps = pd.to_datetime(frame[_TIMESTAMP_COLUMN], utc=True, errors="coerce")
    lo = pd.Timestamp(asof - dt.timedelta(days=lookback_days), tz="UTC")
    hi = pd.Timestamp(asof, tz="UTC") + pd.Timedelta(days=1)
    return frame[stamps.notna() & (stamps >= lo) & (stamps < hi)]


def merge_news_sources(view: pd.DataFrame, lake: pd.DataFrame) -> pd.DataFrame:
    """One row per news id, keeping the LONGEST body seen for it.

    Longest rather than newest because the point is to recover TEXT, not merely
    rows: measured 2026-11-09, the 7,330 ``edgar_press_release`` ids the view had
    dropped carry a median body of 153,648 characters against 22,889 for the
    6,765 it kept. A newest-wins rule would hand back the row the cap already
    chose and recover nothing.

    Ties resolve to the view, because the view frame is concatenated FIRST and
    the sort is stable. Determinism does not rest on a tie-break column: the two
    ``sorted()`` calls in :func:`load_lake_window` fix the order the lake rows
    arrive in, and ``kind="stable"`` preserves it. An earlier version carried a
    run-stamp tie-break and credited it with the determinism; a mutation deleted
    that column and every test stayed green, because it earned nothing.

    There is NO single-frame shortcut. One existed and it broke this function's
    own contract: a lake-only window returns one row per ingest RUN, so six runs
    of one news id came back as six rows and the resolver's inner join fanned
    out. Both paths now go through the same concat, sort and dedup.
    """
    frames = [f for f in (view, lake) if not f.empty]
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    body = merged.get(_NEWS_BODY_COLUMN)
    merged[_BODY_LENGTH_COLUMN] = body.fillna("").astype(str).str.len() if body is not None else 0
    merged = merged.sort_values(_BODY_LENGTH_COLUMN, ascending=False, kind="stable")
    merged = merged.drop_duplicates(subset=[_NEWS_ID_COLUMN], keep="first")
    return merged.drop(columns=list(_TRANSIENT_COLUMNS)).reset_index(drop=True)


def with_lake(
    view: pd.DataFrame,
    lake_dir: Path,
    asof: dt.date,
    lookback_days: int,
    *,
    needed_ids: frozenset[str] | None = None,
) -> pd.DataFrame:
    """The news the resolver joins against: ``view``, plus the lake when enabled.

    A UNION, never a replacement. Measured 2026-11-09, 3,638 view ids have no
    lake row at all because the view starts 2026-05-18 and the lake starts
    2026-06-04; reading the lake alone would silently drop that history.

    SIDE EFFECT, disclosed rather than discovered later: the dedup in
    :func:`merge_news_sources` is global over the window, so once this is on it
    also dedupes the view against ITSELF across dates. A news id the view
    carries on two days collapses to one row, which changes ``echo_count`` and
    ``is_amplified`` for reasons that have nothing to do with the lake.
    """
    if not READ_NEWS_LAKE:
        return view
    lake = load_lake_window(lake_dir, asof, lookback_days, needed_ids=needed_ids)
    if lake.empty:
        # The cohort key records the INTENT to read the lake. Without this line
        # an absent or unmounted lake makes the resolver fall back to the view
        # silently, while every parquet of that day is stamped `view+lake` - a
        # token asserting a source that was never read.
        logger.warning(
            "news lake enabled but %s contributed no rows for the %d-day window ending %s; "
            "the view alone was read while the cohort key says %s",
            lake_dir,
            lookback_days,
            asof.isoformat(),
            NEWS_SOURCE_VIEW_PLUS_LAKE,
        )
    return merge_news_sources(view, lake)


__all__ = [
    "LAKE_DIR_NAME",
    "NEWS_SOURCE_VIEW",
    "NEWS_SOURCE_VIEW_PLUS_LAKE",
    "READ_NEWS_LAKE",
    "lake_dir_for",
    "load_lake_window",
    "merge_news_sources",
    "news_source_token",
    "with_lake",
]
