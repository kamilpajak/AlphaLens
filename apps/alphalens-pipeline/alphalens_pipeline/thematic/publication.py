"""When is the thematic brief for a date decided, and may a run still change it (#1479)?

The owner reads the brief for date ``D`` on ``D+1`` before the NYSE open and does
not look at it again. ``/edge`` and the ML labels measure the brief as stored, so
the stored brief must be the one that existed at that read. Three rules:

- A brief parquet with at least one row is PUBLISHED. No automatic run changes
  its list, order, prose or trade levels after that, whatever the mapper config.
- A date without a published brief is OPEN until the open of its arrival session
  (``ladder_arrival_session``, the session the population monitor enters on), and
  CLOSED from then on: a brief nobody could read before trading is not created.
- An EMPTY brief (quiet day or degraded run) is not published, so a later run can
  still replace it before the open. An UNREADABLE brief is left alone: a read
  error must never look like "no brief" and trigger a rebuild of a list the owner
  may already have read.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from alphalens_pipeline.feedback.ladder_config import ladder_arrival_session
from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, session_open_utc

logger = logging.getLogger(__name__)

BRIEF_PUBLISHED_AT = "brief_published_at"

# Asof dates whose list was set AFTER the arrival open. From 2026-05-24 the source
# is the build journal, read before it rotated away; for the older dates it is the
# brief's own `brief_generated_at` stamp and the file times on the VPS. Sources and
# method: docs/research/thematic_brief_publication_history_2026_09_16.{md,csv} and
# thematic_brief_publication_history_pre_journal_2026_09_17.csv; a research test
# keeps these two constants equal to the two CSVs.
PUBLISHED_AFTER_OPEN_HISTORY: frozenset[dt.date] = frozenset(
    dt.date.fromisoformat(d)
    for d in (
        "2026-05-19",
        "2026-05-28",
        "2026-05-31",
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
        "2026-06-07",
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
        "2026-06-11",
        "2026-06-14",
        "2026-06-15",
        "2026-08-02",
        "2026-08-18",
        "2026-08-19",
    )
)
HISTORY_RECORD_WINDOW: tuple[dt.date, dt.date] = (dt.date(2026, 5, 19), dt.date(2026, 9, 15))


class PublicationStatus(enum.Enum):
    PUBLISHED = "published"
    OPEN = "open"
    CLOSED = "closed"
    UNREADABLE = "unreadable"


def deadline_utc(asof: dt.date, exchange: str = DEFAULT_EXCHANGE) -> dt.datetime:
    """The open of the first session a reader of the brief for ``asof`` can trade."""
    return session_open_utc(ladder_arrival_session(asof, exchange), exchange)


def brief_path(asof: dt.date, briefs_dir: Path) -> Path:
    return briefs_dir / f"{asof.isoformat()}.parquet"


def publication_status(
    asof: dt.date,
    briefs_dir: Path,
    *,
    now: dt.datetime,
    exchange: str = DEFAULT_EXCHANGE,
) -> PublicationStatus:
    """Classify the brief for ``asof`` at ``now`` (see the module docstring)."""
    path = brief_path(asof, briefs_dir)
    if path.exists():
        try:
            n_rows = pq.read_metadata(path).num_rows
        except Exception as exc:  # corrupt footer, I/O error: never treat as missing
            logger.warning("brief %s is unreadable (%s); leaving it untouched", path, exc)
            return PublicationStatus.UNREADABLE
        if n_rows > 0:
            return PublicationStatus.PUBLISHED
    if now < deadline_utc(asof, exchange):
        return PublicationStatus.OPEN
    return PublicationStatus.CLOSED


def published_before_open(
    brief_date: dt.date,
    published_at: pd.Timestamp | dt.datetime | None,
    exchange: str = DEFAULT_EXCHANGE,
) -> bool | None:
    """Did the stored list exist before the arrival open? ``None`` when unknown.

    A publication stamp decides. Without one, a date inside the history record
    is answered from that record; any other date is unknown.
    """
    if published_at is not None and not pd.isna(published_at):
        return pd.Timestamp(published_at) < pd.Timestamp(deadline_utc(brief_date, exchange))
    first, last = HISTORY_RECORD_WINDOW
    if first <= brief_date <= last:
        return brief_date not in PUBLISHED_AFTER_OPEN_HISTORY
    return None


__all__ = [
    "BRIEF_PUBLISHED_AT",
    "HISTORY_RECORD_WINDOW",
    "PUBLISHED_AFTER_OPEN_HISTORY",
    "PublicationStatus",
    "brief_path",
    "deadline_utc",
    "publication_status",
    "published_before_open",
]
