"""Generic TTM and instant aggregators over EDGAR companyfacts parquet.

Generalises ``_ttm_net_income`` from :mod:`alphalens.data.fundamentals.edgar_companyfacts`
(which was hard-coded to ``NetIncomeLoss`` / ``ProfitLoss``) so the same
peer-reviewed Compustat formula (``current_YTD + prior_FY - prior_YTD``)
works for any duration concept — revenue, operating income, OCF, capex,
D&A, etc. — by parameterising over a concept-fallback chain.

Two public entry points:

- :func:`compute_ttm` — duration concepts (P&L, cash flow). Sums the
  trailing 12 months across the most-recently-reported fiscal calendar
  visible at ``asof``.

- :func:`latest_instant` — instant concepts (balance sheet: cash, debt,
  equity). Returns the most-recent point-in-time value visible at
  ``asof``.

Both honour the project's PIT contract (``filed_date <= asof``) via the
existing :func:`alphalens.data.fundamentals.edgar_companyfacts._pit_filter`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import date

import pyarrow as pa

from alphalens.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    filter_concept,
)
from alphalens.data.fundamentals.edgar_companyfacts import (
    _Entry,
    _find_near_end,
    _is_fy_like,
    _latest_per_end,
    _latest_per_period,
    _pit_filter,
    _shift_year_iso,
)

logger = logging.getLogger(__name__)

DEFAULT_TAXONOMY = "us-gaap"
DEFAULT_UNIT = "USD"


def _arrow_table_to_entries(
    table: pa.Table,
    concept: str,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
) -> list[_Entry]:
    """Filter ``table`` rows to (taxonomy, concept, unit) and convert to _Entry list.

    Duplicates :func:`alphalens.screeners.event_drift.accruals._arrow_table_to_entries`
    intentionally so the new fundamentals store does not import from the
    event_drift screener (cycle direction: data depends on screeners would
    invert the architecture).
    """
    filtered = filter_concept(table, taxonomy, concept, unit=unit)
    if filtered.num_rows == 0:
        return []
    return [
        _Entry(
            end=row["period_end"].isoformat(),
            val=float(row["val"]),
            filed=row["filed_date"].isoformat(),
            form=row["form"] or "",
            fp=row["fp"],
            start=row["period_start"].isoformat() if row["period_start"] is not None else None,
        )
        for row in filtered.to_pylist()
    ]


# --- Duration concepts: TTM Compustat formula ------------------------------


def _ttm_at_end(
    per_period: dict[tuple[str | None, str], _Entry],
    end_date: str,
) -> float | None:
    """Apply the ``current_YTD + prior_FY - prior_YTD`` Compustat formula.

    Lifted verbatim from :func:`edgar_companyfacts._ttm_net_income` so the
    behaviour stays identical (same fiscal-calendar drift tolerance, same
    "never extrapolate" guard).
    """
    current: _Entry | None = None
    for (_, end), entry in per_period.items():
        if end == end_date:
            current = entry
            break
    if current is None:
        return None
    if _is_fy_like(current):
        return current.val
    if not current.start:
        return None
    prior_fy_candidates = [
        e for e in per_period.values() if _is_fy_like(e) and e.end < current.start
    ]
    if not prior_fy_candidates:
        return None
    prior_fy = max(prior_fy_candidates, key=lambda e: e.end)
    target = _shift_year_iso(current.end, -1)
    same_fp_candidates = [
        e
        for e in per_period.values()
        if e.fp == current.fp and not _is_fy_like(e) and e.end != current.end
    ]
    prior_ytd = _find_near_end(same_fp_candidates, target)
    if prior_ytd is None:
        return None
    return current.val + prior_fy.val - prior_ytd.val


def _latest_end_visible(per_period: dict[tuple[str | None, str], _Entry]) -> str | None:
    """The lex-greatest ``end`` ISO date across all (start, end) pairs."""
    if not per_period:
        return None
    return max(entry.end for entry in per_period.values())


def compute_ttm(
    reader: CompanyfactsParquetReader,
    cik: str,
    chain: Sequence[str],
    asof: date,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
) -> float | None:
    """Try each concept in ``chain``; return the first non-None TTM value.

    For each candidate concept:
      1. Read all entries for that concept from the CIK's parquet table.
      2. PIT-filter on ``filed_date <= asof``.
      3. Keep the latest-filed entry per (start, end) pair (handles
         restatements).
      4. Apply ``_ttm_at_end`` at the most recent visible end.

    Returns None when the entire chain misses (concept-mismatch + truly
    missing data are indistinguishable at this layer; callers can
    discriminate via ``latest_instant`` on a marker concept).
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return None
    for concept in chain:
        entries = _arrow_table_to_entries(table, concept, taxonomy=taxonomy, unit=unit)
        if not entries:
            continue
        visible = _pit_filter(entries, asof)
        if not visible:
            continue
        per_period = _latest_per_period(visible)
        end = _latest_end_visible(per_period)
        if end is None:
            continue
        value = _ttm_at_end(per_period, end)
        if value is not None:
            return value
    return None


# --- Instant concepts: latest balance-sheet value at asof ------------------


def latest_instant(
    reader: CompanyfactsParquetReader,
    cik: str,
    chain: Sequence[str],
    asof: date,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
) -> float | None:
    """Most-recent point-in-time value for an instant concept visible at asof.

    Tries each concept in ``chain``; first hit (any entry visible at
    asof) wins. Within a single concept's entries we keep the latest-filed
    per ``end`` (handles balance-sheet restatements) and pick the
    chronologically latest end.
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return None
    for concept in chain:
        entries = _arrow_table_to_entries(table, concept, taxonomy=taxonomy, unit=unit)
        if not entries:
            continue
        visible = _pit_filter(entries, asof)
        if not visible:
            continue
        per_end = _latest_per_end(visible)
        if not per_end:
            continue
        latest_end = max(per_end.keys())
        return per_end[latest_end].val
    return None


def has_any_concept(
    reader: CompanyfactsParquetReader,
    cik: str,
    chain: Iterable[str],
    asof: date,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
) -> bool:
    """True if at least one entry for any concept in ``chain`` is visible at asof.

    Marker for the debt-free fallback: callers verify the issuer files
    balance sheets at all before treating missing debt rows as 0.0.
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return False
    for concept in chain:
        entries = _arrow_table_to_entries(table, concept, taxonomy=taxonomy, unit=unit)
        if entries and _pit_filter(entries, asof):
            return True
    return False


__all__ = [
    "compute_ttm",
    "has_any_concept",
    "latest_instant",
]
