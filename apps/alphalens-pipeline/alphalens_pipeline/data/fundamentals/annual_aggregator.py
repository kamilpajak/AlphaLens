"""Multi-year annual (FY) statement accessor over EDGAR companyfacts parquet.

Where :mod:`alphalens_pipeline.data.fundamentals.ttm_aggregator` collapses
the companyfacts history down to a single TTM-at-asof value per concept,
this module exposes the **full annual series**: one
:class:`AnnualStatement` per fiscal year visible at ``asof``, newest
fiscal-year end first. It is the data backbone for trend analysis —
operating-margin trend, capex/D&A capital-intensity trend, and the
owner-earnings / DCF history — that single-point TTM cannot express.

Two deliberate differences from the TTM path:

1. **Raw FY values, not TTM.** A fiscal-year row's value passes through
   verbatim (FY is already a 12-month figure). Quarterly and YTD rows are
   excluded — only FY-like duration rows (``_is_fy_like``) feed the series.

2. **No staleness gate.** :func:`compute_ttm` rejects results older than
   ~9 months (a stale TTM in a daily brief is a bug). A multi-year series
   is *meant* to surface old years, so the freshness cutoff is omitted.

Instant concepts (equity, debt, cash, shares) are anchored on each year's
fiscal-year end — the balance-sheet value reported *as of* that FY close,
not the latest snapshot at ``asof``. A small +/- day tolerance absorbs
52/53-week fiscal-calendar drift between the duration FY end and the
instant balance-sheet date.

PIT contract (``filed_date <= asof``), the us-gaap concept fallback chains,
and the form whitelist are all reused from the existing aggregator stack so
behaviour stays consistent with :meth:`EdgarFundamentalsStore.ev_fcff_features_as_of`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pyarrow as pa

from alphalens_pipeline.data.fundamentals import concept_chains as chains
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import CompanyfactsParquetReader
from alphalens_pipeline.data.fundamentals.edgar_companyfacts import (
    _Entry,
    _is_fy_like,
    _pit_filter,
)
from alphalens_pipeline.data.fundamentals.ttm_aggregator import (
    DEFAULT_FORM_WHITELIST,
    DEFAULT_TAXONOMY,
    DEFAULT_UNIT,
    _arrow_table_to_entries,
)

# Tolerance (days) between a duration FY end and the matching instant
# balance-sheet date. 52/53-week filers close on the nearest weekday to a
# calendar quarter-end, so the FY duration end and the equity instant end
# can differ by a handful of days within the same 10-K.
_INSTANT_END_TOLERANCE_DAYS = 7


@dataclass(frozen=True)
class AnnualStatement:
    """One fiscal year's headline statement values, PIT-correct at asof.

    ``fy`` is the calendar year of ``fiscal_year_end`` (a convenience, not
    the issuer's reported FY label, which can differ for off-calendar
    fiscal years). The authoritative key is ``fiscal_year_end``. Any field
    is ``None`` when the issuer did not report that concept for the year;
    the year is still emitted as long as at least one duration concept is
    present. ``capex`` carries the EDGAR positive-sign convention (cash
    outflow magnitude). Maintenance-vs-growth capex is NOT split here.
    """

    fiscal_year_end: date
    fy: int
    filed_date: date
    revenue: float | None
    operating_income: float | None
    net_income: float | None
    ocf: float | None
    capex: float | None
    da: float | None
    total_equity: float | None
    long_term_debt: float | None
    short_term_debt: float | None
    cash_and_equivalents: float | None
    shares_outstanding: float | None


def _latest_fy_entry_per_end(entries: Sequence[_Entry]) -> dict[str, _Entry]:
    """Keep, per fiscal-year end, the FY-like entry with the latest filed date.

    Restatements (a later 10-K/A for the same period) supersede the
    original because callers PIT-filter first; ``filed`` ties are broken by
    keeping the later filing.
    """
    out: dict[str, _Entry] = {}
    for e in entries:
        if not _is_fy_like(e):
            continue
        cur = out.get(e.end)
        if cur is None or e.filed > cur.filed:
            out[e.end] = e
    return out


def _annual_duration_entries(
    table: pa.Table,
    chain: Sequence[str],
    asof: date,
) -> dict[str, _Entry]:
    """Per fiscal-year end, the winning FY-like duration entry for ``chain``.

    Concepts are tried in chain order; the first concept supplying a value
    for a given FY end wins (chain-priority fallback, mirroring
    :func:`compute_per_quarter_series`). Only forms in the whitelist and
    only FY-like spans are considered.
    """
    per_end: dict[str, _Entry] = {}
    for concept in chain:
        entries = _arrow_table_to_entries(
            table,
            concept,
            taxonomy=DEFAULT_TAXONOMY,
            unit=DEFAULT_UNIT,
            form_whitelist=DEFAULT_FORM_WHITELIST,
        )
        if not entries:
            continue
        visible = _pit_filter(entries, asof)
        for end, entry in _latest_fy_entry_per_end(visible).items():
            per_end.setdefault(end, entry)
    return per_end


def _annual_da_entries(table: pa.Table, asof: date) -> dict[str, _Entry]:
    """D&A per FY end with the component-sum fallback.

    Mirrors :meth:`EdgarFundamentalsStore.ev_fcff_features_as_of`: try the
    single-tag chain first; for any FY end it does not cover, sum the
    Depreciation + Amortisation component concepts when present.
    """
    da = _annual_duration_entries(table, chains.DEPRECIATION_AMORTISATION, asof)
    components = [
        _annual_duration_entries(table, (concept,), asof)
        for concept in chains.DEPRECIATION_AMORTISATION_COMPONENTS
    ]
    component_ends: set[str] = set()
    for comp in components:
        component_ends.update(comp)
    for end in component_ends:
        if end in da:
            continue
        present = [comp[end] for comp in components if end in comp]
        if not present:
            continue
        base = max(present, key=lambda e: e.filed)
        da[end] = _Entry(
            end=end,
            val=sum(e.val for e in present),
            filed=base.filed,
            form=base.form,
            fp=base.fp,
            start=base.start,
        )
    return da


def _instant_at_end(
    table: pa.Table,
    chain: Sequence[str],
    asof: date,
    target_end: date,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
) -> float | None:
    """Balance-sheet value reported as of ``target_end`` (a FY close).

    Scans ``chain`` in priority order; the first concept with an instant
    row within ``_INSTANT_END_TOLERANCE_DAYS`` of ``target_end`` wins. Among
    candidate rows the one closest to ``target_end`` is taken, with the
    latest filed date breaking distance ties (restatement supersedes).
    """
    for concept in chain:
        entries = _arrow_table_to_entries(
            table,
            concept,
            taxonomy=taxonomy,
            unit=unit,
            form_whitelist=DEFAULT_FORM_WHITELIST,
        )
        if not entries:
            continue
        visible = _pit_filter(entries, asof)
        best: tuple[int, str, float] | None = None  # (distance, filed, val)
        for entry in visible:
            try:
                end = date.fromisoformat(entry.end)
            except ValueError:
                continue
            distance = abs((end - target_end).days)
            if distance > _INSTANT_END_TOLERANCE_DAYS:
                continue
            # Prefer the row closest to target_end; break distance ties by
            # the later filed date (restatement supersedes the original).
            if (
                best is None
                or distance < best[0]
                or (distance == best[0] and entry.filed > best[1])
            ):
                best = (distance, entry.filed, entry.val)
        if best is not None:
            return best[2]
    return None


def _shares_at_end(table: pa.Table, asof: date, target_end: date) -> float | None:
    """Shares outstanding as of ``target_end``: dei tier, then us-gaap tier.

    No yfinance fallback here (that is a network call owned by the store);
    the aggregator stays pure over the parquet.
    """
    shares = _instant_at_end(
        table,
        chains.SHARES_OUTSTANDING_DEI,
        asof,
        target_end,
        taxonomy="dei",
        unit="shares",
    )
    if shares is None:
        shares = _instant_at_end(
            table,
            chains.SHARES_OUTSTANDING_US_GAAP,
            asof,
            target_end,
            unit="shares",
        )
    return shares


def _val(per_end: dict[str, _Entry], end: str) -> float | None:
    entry = per_end.get(end)
    return entry.val if entry is not None else None


def annual_statements(
    reader: CompanyfactsParquetReader,
    cik: str,
    asof: date,
    *,
    max_years: int = 10,
) -> list[AnnualStatement]:
    """Multi-year annual (FY) statement series, PIT-correct at ``asof``.

    Returns up to ``max_years`` :class:`AnnualStatement` records ordered by
    ``fiscal_year_end`` descending (newest first). Empty list when the CIK
    has no parquet on disk.

    The set of fiscal years is the union of FY ends across the *duration*
    concepts (revenue, operating income, net income, OCF, capex, D&A); a
    year with only instant (balance-sheet) rows and no duration row is not
    emitted. The single ``reader.get_cik_table`` call is shared across every
    concept lookup.
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return []

    revenue = _annual_duration_entries(table, chains.REVENUE, asof)
    operating_income = _annual_duration_entries(table, chains.OPERATING_INCOME, asof)
    net_income = _annual_duration_entries(table, chains.NET_INCOME, asof)
    ocf = _annual_duration_entries(table, chains.OPERATING_CASH_FLOW, asof)
    capex = _annual_duration_entries(table, chains.CAPEX, asof)
    da = _annual_da_entries(table, asof)

    duration_maps = (revenue, operating_income, net_income, ocf, capex, da)
    fy_ends = sorted({end for per_end in duration_maps for end in per_end}, reverse=True)
    fy_ends = fy_ends[:max_years]

    out: list[AnnualStatement] = []
    for end in fy_ends:
        present = [per_end[end] for per_end in duration_maps if end in per_end]
        filed = max(e.filed for e in present)
        fiscal_year_end = date.fromisoformat(end)
        out.append(
            AnnualStatement(
                fiscal_year_end=fiscal_year_end,
                fy=fiscal_year_end.year,
                filed_date=date.fromisoformat(filed),
                revenue=_val(revenue, end),
                operating_income=_val(operating_income, end),
                net_income=_val(net_income, end),
                ocf=_val(ocf, end),
                capex=_val(capex, end),
                da=_val(da, end),
                total_equity=_instant_at_end(table, chains.EQUITY, asof, fiscal_year_end),
                long_term_debt=_instant_at_end(table, chains.LONG_TERM_DEBT, asof, fiscal_year_end),
                short_term_debt=_instant_at_end(
                    table, chains.SHORT_TERM_DEBT, asof, fiscal_year_end
                ),
                cash_and_equivalents=_instant_at_end(table, chains.CASH, asof, fiscal_year_end),
                shares_outstanding=_shares_at_end(table, asof, fiscal_year_end),
            )
        )
    return out


__all__ = ["AnnualStatement", "annual_statements"]
