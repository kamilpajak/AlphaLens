"""Earnings-announcement dates derived from EDGAR companyfacts EPS filings.

Each first-filed quarterly EPS entry (10-Q or 10-K) becomes one
``EarningsAnnouncement``. Subsequent amendments (10-Q/A, 10-K/A) for the
same ``period_end`` are NOT emitted — PEAD anchors to the ORIGINAL surprise
the market reacted to, mirroring ``FosterSUEStore`` first-filed semantics.

v1 limitation: ``accepted_hour_et`` is always ``None`` because companyfacts
exposes only the filing date, not the SEC acceptance timestamp. Consumers
(``t0_timing``) treat this as "unknown -> conservative after-hours
default" so the T0 entry day shifts forward by one trading day to
eliminate intraday lookahead. A future enhancement could parse 8-K Item
2.02 with exact ``acceptanceDateTime`` from cached SEC submissions JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pyarrow as pa

from alphalens_research.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens_research.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    filter_concept,
)
from alphalens_research.data.fundamentals.edgar_companyfacts import _Entry
from alphalens_research.data.fundamentals.sue import _first_filed_per_period_end, _is_quarterly

logger = logging.getLogger(__name__)


_EPS_BASIC = "EarningsPerShareBasic"
_EPS_DILUTED = "EarningsPerShareDiluted"


def _classify_form(form: str) -> Literal["10-K", "10-Q", "10-K/A", "10-Q/A", "8-K", "other"]:
    """Map raw companyfacts ``form`` field to a normalised label."""
    f = form.strip()
    if f == "10-K":
        return "10-K"
    if f == "10-Q":
        return "10-Q"
    if f == "10-K/A":
        return "10-K/A"
    if f == "10-Q/A":
        return "10-Q/A"
    if f == "8-K":
        return "8-K"
    return "other"


@dataclass(frozen=True)
class EarningsAnnouncement:
    """A single first-filed earnings announcement.

    ``accepted_hour_et`` is ``None`` when only the filing date is available
    (current state with companyfacts-only ingestion). The consumer must
    apply a conservative after-hours default in this case.
    """

    ticker: str
    period_end: date
    filed_date: date
    accepted_hour_et: int | None
    source: Literal["10-K", "10-Q", "10-K/A", "10-Q/A", "8-K", "other"]


def _eps_entries_from_arrow_table(table: pa.Table) -> list[_Entry]:
    """Pick first non-empty EPS unit (USD/shares preferred over USD) and convert to _Entry list."""
    for concept in (_EPS_BASIC, _EPS_DILUTED):
        for unit_key in ("USD/shares", "USD"):
            filtered = filter_concept(table, "us-gaap", concept, unit=unit_key)
            if filtered.num_rows == 0:
                continue
            return [
                _Entry(
                    end=row["period_end"].isoformat(),
                    val=float(row["val"]),
                    filed=row["filed_date"].isoformat(),
                    form=row["form"] or "",
                    fp=row["fp"],
                    start=row["period_start"].isoformat()
                    if row["period_start"] is not None
                    else None,
                )
                for row in filtered.to_pylist()
            ]
    return []


class AnnouncementDateProvider:
    """Emit earnings-announcement events from parquet companyfacts via reader injection.

    ``announcements(ticker)`` returns one event per first-filed quarterly EPS
    period_end. Optional ``after`` / ``before`` filters bound the
    ``filed_date`` range (inclusive lower, exclusive upper).
    """

    def __init__(
        self,
        reader: CompanyfactsParquetReader,
        ticker_cik_map: TickerCikMap,
    ):
        self._reader = reader
        self._cik_map = ticker_cik_map

    def announcements(
        self,
        ticker: str,
        *,
        after: date | None = None,
        before: date | None = None,
    ) -> list[EarningsAnnouncement]:
        """Return chronologically-ordered first-filed announcements for ``ticker``."""
        cik = self._cik_map.lookup(ticker)
        if cik is None:
            return []
        table = self._reader.get_cik_table(cik)
        if table is None:
            return []
        entries = _eps_entries_from_arrow_table(table)
        if not entries:
            return []

        quarterly = [e for e in entries if _is_quarterly(e)]
        first_filed = _first_filed_per_period_end(quarterly)

        events: list[EarningsAnnouncement] = []
        for end_iso in sorted(first_filed.keys()):
            entry = first_filed[end_iso]
            try:
                period_end = date.fromisoformat(entry.end)
                filed_d = date.fromisoformat(entry.filed)
            except ValueError:
                continue
            if after is not None and filed_d < after:
                continue
            if before is not None and filed_d >= before:
                continue
            events.append(
                EarningsAnnouncement(
                    ticker=ticker.upper(),
                    period_end=period_end,
                    filed_date=filed_d,
                    accepted_hour_et=None,  # v1: companyfacts has no hour
                    source=_classify_form(entry.form),
                )
            )
        return events
