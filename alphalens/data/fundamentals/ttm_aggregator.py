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
import math
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
    """Scan every concept in ``chain``; return the value at the freshest end.

    For each candidate concept:
      1. Read all entries for that concept from the CIK's parquet table.
      2. PIT-filter on ``filed_date <= asof``.
      3. Keep the latest-filed entry per (start, end) pair (handles
         restatements).
      4. Apply ``_ttm_at_end`` at that concept's most recent visible end.

    Across concepts we keep the result with the chronologically latest
    ``end`` — early-returning on the first non-None hit would silently
    serve a stale value for issuers that switched XBRL tags (e.g. ASC
    606 migration when ``Revenues`` is in the chain alongside
    ``RevenueFromContractWithCustomerExcludingAssessedTax``).

    Returns None when the entire chain misses.
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return None
    best_end: str | None = None
    best_val: float | None = None
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
        if best_end is not None and end <= best_end:
            continue
        value = _ttm_at_end(per_period, end)
        if value is None:
            continue
        best_end = end
        best_val = value
    return best_val


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

    Scans every concept in ``chain`` and returns the value attached to the
    chronologically latest ``end`` date — see :func:`compute_ttm` for why
    we do not early-return on first hit (stale-tag protection).
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return None
    best_end: str | None = None
    best_val: float | None = None
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
        if best_end is None or latest_end > best_end:
            best_end = latest_end
            best_val = per_end[latest_end].val
    return best_val


# --- Per-quarter series (standalone-Q rows + FY-minus-YTD9M Q4 derivation) ---


_STANDALONE_Q_MIN_DAYS = 60
_STANDALONE_Q_MAX_DAYS = 100
_YTD9M_MIN_DAYS = 240
_YTD9M_MAX_DAYS = 300
_FY_MIN_DAYS = 350
_FY_MAX_DAYS = 380


def _span_days(entry: _Entry) -> int:
    if not entry.start:
        return 0
    try:
        s = date.fromisoformat(entry.start)
        e = date.fromisoformat(entry.end)
    except ValueError:
        return 0
    return (e - s).days


def compute_per_quarter_series(
    reader: CompanyfactsParquetReader,
    cik: str,
    chain: Sequence[str],
    asof: date,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
) -> list[tuple[str, float]]:
    """Build a per-quarter value series for one duration concept chain.

    Returns ``[(end_iso, value), ...]`` sorted ascending by ``end``. Each
    entry represents a single fiscal quarter's value (not YTD-cumulative).

    Two paths feed the output:

    1. **Standalone Q rows** — 10-Q filings (and 8-K recasts) typically
       include both a YTD row and a same-period standalone-quarter row
       (span ~90 days, ``fp ∈ {Q1, Q2, Q3}``). Standalone rows are used
       directly when present. Probed across MANH/CAT/AAPL/MSFT/JPM:
       all 5 file standalone Q-rows for OCF/CapEx/Revenue.

    2. **Q4 derivation** — fiscal year-end rows (~365-day span,
       ``fp == 'FY'``) are paired with the matching YTD9M row (~270-day
       span, ``fp == 'Q3'``, same ``period_start``) and the Q4 value
       computed as ``FY - YTD9M``. The fiscal-year match is by
       ``period_start`` equality so a stray cross-year FY+YTD9M pair
       cannot trigger a bogus derivation.

    Standalone Q4 rows (when 8-K recasts file one) win over the derived
    value; auditors occasionally adjust the FY result, so the derived
    value can absorb 100 % of audit adjustments. A direct standalone Q4
    row is more authoritative.

    PIT contract: only entries with ``filed_date <= asof`` are visible.
    Restatements are handled per-(start, end) by keeping the latest
    filed entry.

    Chain traversal mirrors :func:`compute_ttm`: iterate concepts in
    order, but unlike compute_ttm (where the chronologically-latest end
    wins), here we accumulate per-end values from the first concept that
    supplies them. Subsequent chain entries fill ends not already
    covered.
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return []

    per_end: dict[str, float] = {}
    derived_ends: set[str] = set()  # ends populated by Q4 derivation, not standalone

    for concept in chain:
        entries = _arrow_table_to_entries(table, concept, taxonomy=taxonomy, unit=unit)
        if not entries:
            continue
        visible = _pit_filter(entries, asof)
        if not visible:
            continue
        per_period = _latest_per_period(visible)

        # Bucket entries by span class.
        standalone_q: dict[str, _Entry] = {}
        ytd9m_by_start: dict[str, _Entry] = {}
        fy_by_start: dict[str, _Entry] = {}
        for entry in per_period.values():
            span = _span_days(entry)
            if _STANDALONE_Q_MIN_DAYS <= span <= _STANDALONE_Q_MAX_DAYS:
                standalone_q[entry.end] = entry
            elif _YTD9M_MIN_DAYS <= span <= _YTD9M_MAX_DAYS:
                # YTD9M keyed by period_start so we can pair it with a same-
                # fiscal-year FY row.
                if entry.start:
                    ytd9m_by_start[entry.start] = entry
            elif _FY_MIN_DAYS <= span <= _FY_MAX_DAYS and entry.start:
                fy_by_start[entry.start] = entry

        # Standalone Q-rows (primary path).
        for end, entry in standalone_q.items():
            if end not in per_end:
                per_end[end] = entry.val

        # Q4 derivation (FY - YTD9M, matched by period_start).
        for start, fy_entry in fy_by_start.items():
            ytd9m = ytd9m_by_start.get(start)
            if ytd9m is None:
                continue
            end = fy_entry.end
            if end in per_end and end not in derived_ends:
                # A standalone row already populated this end from this or
                # an earlier concept — prefer the direct measurement.
                continue
            per_end[end] = fy_entry.val - ytd9m.val
            derived_ends.add(end)

    return sorted(per_end.items(), key=lambda kv: kv[0])


def fcf_margin_rolling_median(
    reader: CompanyfactsParquetReader,
    cik: str,
    asof: date,
    *,
    window_quarters: int = 20,
    min_quarters: int = 8,
    tax_rate: float = 0.21,
) -> float | None:
    """5-year (20-quarter) rolling median FCF margin.

    For each quarter where OCF, CapEx, and Revenue all align (same
    ``period_end``), compute ``(ocf - capex - interest*(1 - tax)) / revenue``
    and take the median over the trailing ``window_quarters`` (up to 20).
    Quarters with non-positive revenue are skipped. Interest is treated
    as 0 when the issuer files no InterestExpense concept (debt-free
    SaaS / tech) — matches the ``compute_fcff`` "or 0.0" guard.

    Returns ``None`` when fewer than ``min_quarters`` aligned quarters
    survive — too thin a sample for a stable median.

    Local imports of the chain constants avoid a circular dependency:
    ``concept_chains`` imports from this module would invert the
    dependency arrow.
    """
    from alphalens.data.fundamentals import concept_chains as chains

    ocf_series = dict(compute_per_quarter_series(reader, cik, chains.OPERATING_CASH_FLOW, asof))
    if not ocf_series:
        return None
    capex_series = dict(compute_per_quarter_series(reader, cik, chains.CAPEX, asof))
    revenue_series = dict(compute_per_quarter_series(reader, cik, chains.REVENUE, asof))
    interest_series = dict(compute_per_quarter_series(reader, cik, chains.INTEREST_EXPENSE, asof))

    common_ends = sorted(set(ocf_series) & set(capex_series) & set(revenue_series))
    margins: list[float] = []
    for end in common_ends:
        revenue = revenue_series[end]
        if revenue <= 0:
            continue
        interest = interest_series.get(end, 0.0)
        fcff = ocf_series[end] - capex_series[end] - interest * (1.0 - tax_rate)
        margin = fcff / revenue
        if math.isnan(margin) or math.isinf(margin):
            continue
        margins.append(margin)

    if len(margins) < min_quarters:
        return None
    trimmed = margins[-window_quarters:]
    # statistics.median accepts any odd / even length >= 1.
    import statistics

    return float(statistics.median(trimmed))


def has_any_concept(
    reader: CompanyfactsParquetReader,
    cik: str,
    chain: Iterable[str],
    asof: date,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
    min_distinct_ends: int = 4,
) -> bool:
    """True if any concept in ``chain`` has ≥ ``min_distinct_ends`` distinct ends.

    Marker for the debt-free fallback: callers verify the issuer files
    balance sheets at all before treating missing debt rows as 0.0. We
    require ≥4 distinct period-end dates so a single 8-K mention of
    ``Assets`` doesn't false-positive a partial-filing issuer into the
    "structurally debt-free" bucket.
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return False
    for concept in chain:
        entries = _arrow_table_to_entries(table, concept, taxonomy=taxonomy, unit=unit)
        visible = _pit_filter(entries, asof)
        if len({e.end for e in visible}) >= min_distinct_ends:
            return True
    return False


__all__ = [
    "compute_per_quarter_series",
    "compute_ttm",
    "fcf_margin_rolling_median",
    "has_any_concept",
    "latest_instant",
]
