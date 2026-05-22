"""Generic TTM and instant aggregators over EDGAR companyfacts parquet.

Generalises ``_ttm_net_income`` from :mod:`alphalens_research.data.fundamentals.edgar_companyfacts`
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
existing :func:`alphalens_research.data.fundamentals.edgar_companyfacts._pit_filter`.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from datetime import date, timedelta

import pyarrow as pa

from alphalens_research.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    filter_concept,
)
from alphalens_research.data.fundamentals.edgar_companyfacts import (
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

# Forms whose XBRL facts feed the TTM / instant aggregators. Proxy
# statements (DEF 14A), registration statements (S-1, S-3), prospectuses
# (424B*) and similar were observed in 2026-05 to carry scale-truncated or
# illustrative numbers that have no place in a Compustat-style TTM rollup
# (issue #172 Bug 3a: SOUN DEF 14A NetIncomeLoss val=-14,006 stamped in
# $thousands but XBRL-labelled as plain USD, overrode the canonical 10-K
# entry of -14,006,000 because of the filed-date tiebreaker).
DEFAULT_FORM_WHITELIST: frozenset[str] = frozenset(
    {
        # Annual + interim US domestic.
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        # 8-K earnings recasts: some issuers re-state prior periods with
        # standalone-Q rows here before the next 10-Q lands.
        "8-K",
        "8-K/A",
        # Foreign private issuers.
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
        "6-K/A",
    }
)


def _arrow_table_to_entries(
    table: pa.Table,
    concept: str,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
    form_whitelist: frozenset[str] | set[str] | None = DEFAULT_FORM_WHITELIST,
) -> list[_Entry]:
    """Filter ``table`` rows to (taxonomy, concept, unit) and convert to _Entry list.

    Duplicates :func:`alphalens_research.screeners.event_drift.accruals._arrow_table_to_entries`
    intentionally so the new fundamentals store does not import from the
    event_drift screener (cycle direction: data depends on screeners would
    invert the architecture).

    Rows whose ``form`` is not in ``form_whitelist`` are dropped. Pass
    ``form_whitelist=None`` to disable the gate (tests asserting legacy
    behavior on synthetic non-canonical forms).
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
        if form_whitelist is None or (row["form"] or "") in form_whitelist
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
    # Pick the (start, end) entry for end_date with the latest filed date.
    # When a restatement changes period_start, _latest_per_period (keyed by
    # the full (start, end) tuple) lets both rows survive. Without this
    # tiebreaker, dict insertion order would non-deterministically pick the
    # winner — pre-existing pattern, surfaced by zen on PR #164.
    current: _Entry | None = None
    for (_, end), entry in per_period.items():
        if end != end_date:
            continue
        if current is None or entry.filed > current.filed:
            current = entry
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


DEFAULT_TTM_MAX_STALENESS_DAYS = 270

# 4-quarter sum contiguity window (zen finding #1 on PR #174): 4 quarters
# ≈ 273 days; allow ~±3 weeks for 52/53-week fiscal calendar drift.
_TTM_4Q_MIN_SPAN_DAYS = 250
_TTM_4Q_MAX_SPAN_DAYS = 300


def _try_4quarter_sum(
    reader: CompanyfactsParquetReader,
    cik: str,
    chain: Sequence[str],
    asof: date,
    *,
    taxonomy: str,
    unit: str,
    cutoff: date | None,
) -> float | None:
    """Path 1 of :func:`compute_ttm` — trailing 4 standalone quarter rows
    across the merged concept family. Returns ``None`` when fewer than 4
    are available, when the span is non-contiguous, or when the freshest
    row is older than ``cutoff``.
    """
    series = compute_per_quarter_series(reader, cik, chain, asof, taxonomy=taxonomy, unit=unit)
    if len(series) < 4:
        return None
    first_end = date.fromisoformat(series[-4][0])
    last_end = date.fromisoformat(series[-1][0])
    span_days = (last_end - first_end).days
    if not (_TTM_4Q_MIN_SPAN_DAYS <= span_days <= _TTM_4Q_MAX_SPAN_DAYS):
        return None
    if cutoff is not None and last_end < cutoff:
        return None
    return float(sum(v for _, v in series[-4:]))


def _try_compustat_per_concept(
    table: pa.Table,
    chain: Sequence[str],
    asof: date,
    *,
    taxonomy: str,
    unit: str,
    cutoff: date | None,
) -> float | None:
    """Path 2 of :func:`compute_ttm` — per-concept ``current_YTD + prior_FY
    − prior_YTD`` Compustat identity. Keeps the result tied to the
    chronologically latest end; refuses to fall back to an older concept's
    answer when a fresher one was attempted.
    """
    best_end: str | None = None
    best_val: float | None = None
    for concept in chain:
        # Explicit form_whitelist=DEFAULT_FORM_WHITELIST per zen finding #4
        # (PR #174) — visible at the call site for audit.
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
        if not visible:
            continue
        per_period = _latest_per_period(visible)
        end = _latest_end_visible(per_period)
        if end is None or (best_end is not None and end <= best_end):
            continue
        value = _ttm_at_end(per_period, end)
        if value is None:
            continue
        best_end = end
        best_val = value

    if best_val is None or best_end is None:
        return None
    if cutoff is not None and date.fromisoformat(best_end) < cutoff:
        return None
    return best_val


def compute_ttm(
    reader: CompanyfactsParquetReader,
    cik: str,
    chain: Sequence[str],
    asof: date,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
    max_staleness_days: int | None = DEFAULT_TTM_MAX_STALENESS_DAYS,
) -> float | None:
    """TTM for a semantic concept family with chain-migration protection.

    Two paths, tried in order:

    1. **Trailing 4-quarter sum** (:func:`_try_4quarter_sum`) of
       standalone quarter rows merged across the entire chain (delegated
       to :func:`compute_per_quarter_series`, which handles standalone-Q
       + Q4-derivation + concept families). Issue #172 Bug 2 (AVAV): the
       older per-concept Compustat formula returned ``None`` because a
       post-merger family lacked a prior-FY anchor, and the aggregator
       silently fell back to a different concept's ancient TTM. The
       4-quarter sum treats the chain as one semantic family.

    2. **Compustat ``current_YTD + prior_FY − prior_YTD``**
       (:func:`_try_compustat_per_concept`) as a secondary method when
       fewer than 4 quarters are visible across the family. Scoped
       per-concept so that the formula is only applied within an
       accounting-basis-consistent set of rows.

    A ``max_staleness_days`` gate (default 270 ≈ 9 months) rejects results
    whose freshest input is older than the window — better to emit
    ``None`` than to surface a multi-year-old TTM in a daily brief.
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return None
    cutoff: date | None = (
        asof - timedelta(days=max_staleness_days) if max_staleness_days is not None else None
    )
    out = _try_4quarter_sum(reader, cik, chain, asof, taxonomy=taxonomy, unit=unit, cutoff=cutoff)
    if out is not None:
        return out
    return _try_compustat_per_concept(
        table, chain, asof, taxonomy=taxonomy, unit=unit, cutoff=cutoff
    )


# --- Instant concepts: latest balance-sheet value at asof ------------------


def _latest_instant_for_concept(
    table: pa.Table,
    concept: str,
    asof: date,
    *,
    taxonomy: str,
    unit: str,
    cutoff: date | None,
) -> tuple[str, float] | None:
    """Resolve one concept's latest visible (end, value) pair under the
    PIT filter and the optional ``cutoff`` freshness gate. Returns
    ``None`` when no row survives.
    """
    entries = _arrow_table_to_entries(
        table,
        concept,
        taxonomy=taxonomy,
        unit=unit,
        form_whitelist=DEFAULT_FORM_WHITELIST,
    )
    if not entries:
        return None
    visible = _pit_filter(entries, asof)
    if not visible:
        return None
    if cutoff is not None:
        visible = [e for e in visible if date.fromisoformat(e.end) >= cutoff]
        if not visible:
            return None
    per_end = _latest_per_end(visible)
    if not per_end:
        return None
    latest_end = max(per_end.keys())
    return latest_end, per_end[latest_end].val


def latest_instant(
    reader: CompanyfactsParquetReader,
    cik: str,
    chain: Sequence[str],
    asof: date,
    *,
    taxonomy: str = DEFAULT_TAXONOMY,
    unit: str = DEFAULT_UNIT,
    max_age_days: int | None = None,
) -> float | None:
    """Most-recent point-in-time value for an instant concept visible at asof.

    Scans every concept in ``chain`` and returns the value attached to the
    chronologically latest ``end`` date — see :func:`compute_ttm` for why
    we do not early-return on first hit (stale-tag protection).

    When ``max_age_days`` is set, entries whose ``period_end`` is older than
    ``asof - max_age_days`` are treated as if they were not present. Used by
    the shares-outstanding chain to defend against issuers (e.g. C3.ai,
    issue #172 Bug 1) whose ``us-gaap:CommonStockSharesOutstanding`` tag was
    populated once at IPO and never refreshed. ``None`` (default) preserves
    legacy behavior — equity / debt / cash callers don't currently opt in.
    """
    table = reader.get_cik_table(cik)
    if table is None:
        return None
    cutoff: date | None = asof - timedelta(days=max_age_days) if max_age_days is not None else None
    best_end: str | None = None
    best_val: float | None = None
    for concept in chain:
        hit = _latest_instant_for_concept(
            table, concept, asof, taxonomy=taxonomy, unit=unit, cutoff=cutoff
        )
        if hit is None:
            continue
        latest_end, latest_val = hit
        if best_end is None or latest_end > best_end:
            best_end = latest_end
            best_val = latest_val
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


def _keep_latest_filed(target: dict[str, _Entry], key: str, new_entry: _Entry) -> None:
    """Bucketing tiebreaker: when a 10-Q/A restatement changes
    ``period_start`` between filings, ``_latest_per_period`` (keyed by
    ``(start, end)``) lets both rows survive. Without a filed-date
    tiebreaker here, dict insertion order would non-deterministically
    decide the winner. Keep the entry with the latest ``filed`` date.
    """
    cur = target.get(key)
    if cur is None or new_entry.filed > cur.filed:
        target[key] = new_entry


def _bucket_by_span(
    entries: Iterable[_Entry],
) -> tuple[dict[str, _Entry], dict[str, _Entry], dict[str, _Entry]]:
    """Split a concept's PIT-visible entries into standalone-Q / YTD9M / FY
    buckets with filed-date tiebreaker.
    """
    standalone_q: dict[str, _Entry] = {}
    ytd9m_by_start: dict[str, _Entry] = {}
    fy_by_start: dict[str, _Entry] = {}
    for entry in entries:
        span = _span_days(entry)
        if _STANDALONE_Q_MIN_DAYS <= span <= _STANDALONE_Q_MAX_DAYS:
            _keep_latest_filed(standalone_q, entry.end, entry)
        elif _YTD9M_MIN_DAYS <= span <= _YTD9M_MAX_DAYS and entry.start:
            _keep_latest_filed(ytd9m_by_start, entry.start, entry)
        elif _FY_MIN_DAYS <= span <= _FY_MAX_DAYS and entry.start:
            _keep_latest_filed(fy_by_start, entry.start, entry)
    return standalone_q, ytd9m_by_start, fy_by_start


def _apply_standalone_q(
    per_end: dict[str, float],
    derived_ends: set[str],
    standalone_q: dict[str, _Entry],
) -> None:
    """Standalone Q-row override policy: direct measurement beats both
    'nothing yet' and an earlier concept's derived value, but does NOT
    override an existing standalone value (first concept wins).
    """
    for end, entry in standalone_q.items():
        if end not in per_end or end in derived_ends:
            per_end[end] = entry.val
            derived_ends.discard(end)


def _apply_q4_derivation(
    per_end: dict[str, float],
    derived_ends: set[str],
    fy_by_start: dict[str, _Entry],
    ytd9m_by_start: dict[str, _Entry],
) -> None:
    """Q4 = FY - YTD9M, matched by ``period_start``. Fills only ends that
    no concept (this or earlier) has supplied — derivation is the weakest
    evidence and must not displace a measurement.
    """
    for start, fy_entry in fy_by_start.items():
        ytd9m = ytd9m_by_start.get(start)
        if ytd9m is None:
            continue
        end = fy_entry.end
        if end in per_end:
            continue
        per_end[end] = fy_entry.val - ytd9m.val
        derived_ends.add(end)


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
    # Track which ends were populated by Q4 derivation (vs direct standalone
    # measurement) so a later concept's standalone-Q row can correctly
    # override an earlier concept's derived value. Standalone is the direct
    # measurement; derivation is arithmetic and can absorb audit adjustments.
    derived_ends: set[str] = set()

    for concept in chain:
        # Explicit form_whitelist=DEFAULT_FORM_WHITELIST per zen finding #4
        # (PR #174): the constraint is visible at the call site so future
        # readers can audit the form gating without chasing default values.
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
        if not visible:
            continue
        per_period = _latest_per_period(visible)
        standalone_q, ytd9m_by_start, fy_by_start = _bucket_by_span(per_period.values())
        _apply_standalone_q(per_end, derived_ends, standalone_q)
        _apply_q4_derivation(per_end, derived_ends, fy_by_start, ytd9m_by_start)

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
    from alphalens_research.data.fundamentals import concept_chains as chains

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
        # Explicit form_whitelist=DEFAULT_FORM_WHITELIST per zen finding #4
        # (PR #174): the constraint is visible at the call site so future
        # readers can audit the form gating without chasing default values.
        entries = _arrow_table_to_entries(
            table,
            concept,
            taxonomy=taxonomy,
            unit=unit,
            form_whitelist=DEFAULT_FORM_WHITELIST,
        )
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
