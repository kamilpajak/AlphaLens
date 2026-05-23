"""Sloan (1996) total-accruals-to-avg-total-assets ratio with PIT first-filed.

Quarterly extension per Hribar-Collins (2002):

    accruals_q = (delta_AssetsCurrent - delta_CashAndCashEquivalentsAtCarryingValue)
                 - (delta_LiabilitiesCurrent - delta_LongTermDebtCurrent
                    - delta_IncomeTaxesPayable)
                 - DepreciationAndAmortization_q

    ratio_q    = accruals_q / avg(Assets_q, Assets_{q-1})

PIT contract (mirrors ``alphalens_pipeline.data.fundamentals.sue.FosterSUEStore``):

  At asof t, only entries with ``filed <= t`` are visible. Per ``period_end``
  the FIRST-FILED entry is retained (earliest filed date) and amendments are
  never substituted in. This implements the same temporal semantics as Foster
  SUE: the ratio is what the market actually had access to when it first saw
  the period's balance sheet.

IncomeTaxesPayable is sparsely tagged across EDGAR and is treated as zero
when absent (Hribar-Collins quarterly-quartile approximation). All other
concepts are required; absence -> ``None`` (no ratio for the firm).
"""

from __future__ import annotations

import logging
from datetime import date

import pyarrow as pa
from alphalens_pipeline.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    filter_concept,
)
from alphalens_pipeline.data.fundamentals.edgar_companyfacts import (
    _Entry,
    _pit_filter,
)
from alphalens_pipeline.data.fundamentals.sue import _first_filed_per_period_end

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# us-gaap concept tags

_ASSETS_CURRENT = "AssetsCurrent"
_CASH = "CashAndCashEquivalentsAtCarryingValue"
_LIABILITIES_CURRENT = "LiabilitiesCurrent"
_LONG_TERM_DEBT_CURRENT = "LongTermDebtCurrent"
_INCOME_TAXES_PAYABLE = "IncomeTaxesPayable"  # optional -> 0 fallback
_DEPRECIATION = "DepreciationAndAmortization"
_TOTAL_ASSETS = "Assets"

_REQUIRED_CONCEPTS = (
    _ASSETS_CURRENT,
    _CASH,
    _LIABILITIES_CURRENT,
    _LONG_TERM_DEBT_CURRENT,
    _DEPRECIATION,
    _TOTAL_ASSETS,
)


# ---------------------------------------------------------------------------
# Helpers


def _arrow_table_to_entries(table: pa.Table, concept: str) -> list[_Entry]:
    """Filter ``table`` to USD rows of ``concept`` and convert to _Entry list."""
    filtered = filter_concept(table, "us-gaap", concept, unit="USD")
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


def _is_quarterly_instant(entry: _Entry) -> bool:
    """Instant (balance-sheet) entries are quarterly when fp is Q1..Q4 or Q4-of-FY."""
    if entry.fp in {"Q1", "Q2", "Q3", "Q4"}:
        return True
    # FY 10-K reports a Q4 instant balance even though fp="FY"; accept it.
    return entry.fp == "FY"


def _is_quarterly_duration(entry: _Entry) -> bool:
    """Duration (P&L / cash-flow) entries are quarterly when start..end span ~3 months."""
    if entry.fp in {"Q1", "Q2", "Q3", "Q4"}:
        if entry.start and entry.end:
            try:
                s = date.fromisoformat(entry.start)
                e = date.fromisoformat(entry.end)
            except ValueError:
                return False
            days = (e - s).days
            return 80 <= days <= 100
        return True
    return False


def _extract_concept_at_periods(
    table: pa.Table, concept: str, asof: date, *, quarterly_filter
) -> dict[str, _Entry]:
    """Return {period_end: first-filed _Entry} for ``concept`` visible at asof.

    Filters to quarterly entries via ``quarterly_filter`` (instant vs duration).
    Returns empty dict when concept absent or no quarterly entries.
    """
    entries = _arrow_table_to_entries(table, concept)
    if not entries:
        return {}
    visible = _pit_filter(entries, asof)
    quarterly = [e for e in visible if quarterly_filter(e)]
    return _first_filed_per_period_end(quarterly)


def _last_two_common_period_ends(*period_maps: dict[str, _Entry]) -> tuple[str, str] | None:
    """Find the two most recent period_ends present in ALL ``period_maps``.

    Returns ``(end_prev, end_curr)`` with end_prev < end_curr, or None when
    fewer than two common period_ends exist.
    """
    if not period_maps:
        return None
    common = set(period_maps[0].keys())
    for m in period_maps[1:]:
        common &= set(m.keys())
    if len(common) < 2:
        return None
    sorted_ends = sorted(common)
    return sorted_ends[-2], sorted_ends[-1]


def _compute_ratio(
    *,
    ca_prev: float,
    cash_prev: float,
    cl_prev: float,
    std_prev: float,
    taxpay_prev: float,
    ta_prev: float,
    ca_curr: float,
    cash_curr: float,
    cl_curr: float,
    std_curr: float,
    taxpay_curr: float,
    dep_curr: float,
    ta_curr: float,
) -> float | None:
    """Apply Sloan formula. Returns None when avg_total_assets <= 0."""
    delta_ca = ca_curr - ca_prev
    delta_cash = cash_curr - cash_prev
    delta_cl = cl_curr - cl_prev
    delta_std = std_curr - std_prev
    delta_taxpay = taxpay_curr - taxpay_prev
    accruals = (delta_ca - delta_cash) - (delta_cl - delta_std - delta_taxpay) - dep_curr
    avg_ta = 0.5 * (ta_prev + ta_curr)
    if avg_ta <= 0.0:
        return None
    return accruals / avg_ta


# ---------------------------------------------------------------------------
# Store


class SloanAccrualsStore:
    """PIT Sloan-accruals store backed by parquet companyfacts via reader injection.

    ``accruals_ratio(ticker, asof)`` returns the most recent quarterly ratio
    visible at asof, or None when any required concept is missing or a
    balance-sheet pair cannot be formed.
    """

    def __init__(
        self,
        reader: CompanyfactsParquetReader,
        ticker_cik_map: TickerCikMap,
    ):
        self._reader = reader
        self._cik_map = ticker_cik_map

    def accruals_ratio(self, ticker: str, asof: date) -> float | None:
        """Sloan total-accruals-to-avg-total-assets ratio for most recent quarter."""
        cik = self._cik_map.lookup(ticker)
        if cik is None:
            return None
        table = self._reader.get_cik_table(cik)
        if table is None:
            return None

        # Extract first-filed quarterly entries per period_end for each concept.
        ca = _extract_concept_at_periods(
            table, _ASSETS_CURRENT, asof, quarterly_filter=_is_quarterly_instant
        )
        cash = _extract_concept_at_periods(
            table, _CASH, asof, quarterly_filter=_is_quarterly_instant
        )
        cl = _extract_concept_at_periods(
            table, _LIABILITIES_CURRENT, asof, quarterly_filter=_is_quarterly_instant
        )
        std = _extract_concept_at_periods(
            table, _LONG_TERM_DEBT_CURRENT, asof, quarterly_filter=_is_quarterly_instant
        )
        dep = _extract_concept_at_periods(
            table, _DEPRECIATION, asof, quarterly_filter=_is_quarterly_duration
        )
        ta = _extract_concept_at_periods(
            table, _TOTAL_ASSETS, asof, quarterly_filter=_is_quarterly_instant
        )
        taxpay = _extract_concept_at_periods(
            table, _INCOME_TAXES_PAYABLE, asof, quarterly_filter=_is_quarterly_instant
        )

        # Required concepts must be present.
        if not ca or not cash or not cl or not std or not dep or not ta:
            return None

        # Pair: two most recent period_ends present in ALL required concepts.
        pair = _last_two_common_period_ends(ca, cash, cl, std, dep, ta)
        if pair is None:
            return None
        end_prev, end_curr = pair

        # taxpay is optional: zero fallback when missing for either period.
        taxpay_prev = taxpay[end_prev].val if end_prev in taxpay else 0.0
        taxpay_curr = taxpay[end_curr].val if end_curr in taxpay else 0.0

        return _compute_ratio(
            ca_prev=ca[end_prev].val,
            cash_prev=cash[end_prev].val,
            cl_prev=cl[end_prev].val,
            std_prev=std[end_prev].val,
            taxpay_prev=taxpay_prev,
            ta_prev=ta[end_prev].val,
            ca_curr=ca[end_curr].val,
            cash_curr=cash[end_curr].val,
            cl_curr=cl[end_curr].val,
            std_curr=std[end_curr].val,
            taxpay_curr=taxpay_curr,
            dep_curr=dep[end_curr].val,
            ta_curr=ta[end_curr].val,
        )
