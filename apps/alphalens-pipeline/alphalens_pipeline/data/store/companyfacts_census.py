"""How much of the companyfacts store can the FCFF path still read?

A population census of ``~/.alphalens/companyfacts_parquet/``, emitted as
Prometheus gauges so that the store ageing out of the TTM staleness gate stops
being invisible.

WHY THIS EXISTS. ``compute_ttm`` refuses any result whose freshest input period
ended more than :data:`~alphalens_pipeline.data.fundamentals.ttm_aggregator.DEFAULT_TTM_MAX_STALENESS_DAYS`
days before ``asof``. Until #1636 the store never refreshed a file it already
held, so a large cohort of cached tables stopped at the quarter ending
2025-12-31 and crossed that gate together on 2026-09-28 — silently, with no
error anywhere.

What goes blank with such a cohort, traced rather than assumed: ``compute_ttm``
is reached from exactly one place, ``ev_fcff_features_as_of``
(``edgar_fundamentals.py``, eleven calls), so every consumer of that features
dict loses its TTM columns at once — the thematic scorer, and through it the
FCFF yield, the valuation composite and the magic-formula health screen, plus
``experts/buffett/comparison.py``, which reads the same dict. The O'Neil
earnings-growth term does NOT belong on that list, and the Buffett layer only
half belongs: both also read ``annual_series_as_of``, which carries no
staleness gate at all. An earlier version of this docstring named all five as
going dark together; that was repeating #1636's prose rather than reading the
call graph.

#1636 gave the store a lifetime; its "Known issues" left this half undone.

WHAT IT MEASURES, AND WHAT IT DELIBERATELY DOES NOT.

* **The STORE, never the output.** A null ``fcff_yield_pct`` has six causes and
  only two of them are missing data — the largest bucket is a company that
  genuinely burns cash, which is a real signal about the issuer. A gauge
  counting nulls would move when the universe's sector mix moves and stay flat
  while the store froze. That is why the existing output-side gauge
  ``alphalens_thematic_score_core_numerics_finite_total`` covers only columns
  whose absence is always a defect, and ``fcff_yield_pct`` is kept out of it.
* **The BINDING leg, not the union.** ``compute_fcff`` needs an operating-cash-
  flow TTM *and* a capex TTM, each gated on its own, so an issuer whose capex
  stopped four quarters ago cannot produce an FCFF yield however current its
  cash-flow statement is. This census therefore bins on the OLDER of the two
  legs. That is NOT the measure ``EdgarFundamentalsStore._data_age_days`` uses:
  that one takes a single max over the UNION of both chains, which is right for
  deciding whom to re-ask (any sign of life means the issuer is worth a fetch)
  and wrong for reporting who has gone dark. Measured on the production store
  on 2026-11-03, the two disagree about 1007 files — see the table below.

  The binding leg is NECESSARY but not SUFFICIENT, and the gap is measured, not
  waved at. Of the files this census places inside the gate, 5.7% still cannot
  produce both TTM legs, because ``compute_ttm`` has to BUILD the number: four
  standalone quarters spanning 250-300 days, or a Compustat-style construction
  with a prior-FY anchor. This census asks only when the newest row ended. A
  further group fails one level up: ``compute_fcff_yield_pct`` falls back on
  ``impute_fcff(revenue_ttm, fcf_margin_5y_median)`` when actual FCFF is
  non-positive, and both of those come from the same store, so the REVENUE
  chain's own age can darken an issuer whose two FCFF legs are current. So
  ``fresh + aging`` is an upper bound on what the FCFF path can read, by a
  few percent, and the series is a staleness trend rather than a yield count.
* **An absent leg is its own bucket.** ``band="chain_absent"`` holds the
  issuers that never file one of the two chains at all — financials, royalty
  trusts and miners that do not report
  ``PaymentsToAcquirePropertyPlantAndEquipment``, which is cause 2 of the #1335
  decomposition, TOGETHER WITH the tables that carry no past ``us-gaap`` period
  of any kind, which that decomposition counted separately. Both are
  structurally dead for FCFF, which is why they share a band, but the band is
  therefore wider than cause 2 alone. Folded into ``past_gate`` it would put a
  permanent floor under the series a reader watches for movement.

THE PRODUCTION BASELINE. Taken on 2026-11-03 by running THIS code against the
live store, read-only, inside the pipeline image — not by the store's own
measure, and not on the Mac's mirror, which is a different and staler store:

  =====================  =====  ======  ===================================
  band                   files  share   under the store's union measure
  =====================  =====  ======  ===================================
  fresh (<= 150 days)     2051   33.8%  2910
  aging (150-270)          569    9.4%   859
  past_gate (> 270)       2243   36.9%  1827
  chain_absent            1201   19.8%   610
  empty                     12    0.2%    12
  unreadable                 0    0.0%     0
  TOTAL                   6076  100.0%  6076
  =====================  =====  ======  ===================================

Both identities close exactly: 6064 + 12 + 0 = 6076 and the four bands sum to
6064. The right-hand column is why this table exists in full. An earlier
version of this docstring published the union column as though this census had
produced it, and it does not even close — those four rows sum to 6206 against
6076, because the union measure's "no chain" bucket overlaps its past-gate
bucket by 130 files. Expect the first scraped gauges to read 416 MORE past-gate
and 591 MORE chain_absent files than the union measure suggests. That
difference is the whole point of the binding leg, not a defect.

WHAT IS AND IS NOT ALERTABLE.

36.9% of the store being past the gate is not a fault and cannot be drained.
``preload`` re-asks only the tickers in the current run's universe, never the
store, so a CIK nobody scores is never re-asked at all. A level threshold on
that share is therefore lit from the day it ships.

A RISE threshold is no better, and the shape of the ageing says why. 522 files
cross the gate between 2026-11-03 and 2026-12-27, 451 of them on that single
date — but the real cliff is later and nearly four times larger: 1752 files
cross on 2027-03-28. Those dates are quarter-ends plus 271 days, so they are a
property of the filing calendar, and a rule that fires on them fires every
quarter on something no operator action clears.

So the age bands and the crossing horizon are DASHBOARD series: they make the
wave visible and graphable, and nothing pages on them. What pages is the
machinery — is the store still there, do its files still open, is anything
still writing it.

:data:`WRITER_LIVENESS_DAYS` is the series for that last question, and it is
the FAST one. Not because the bands are slow to move — measured on the
production store, the first fresh file reaches the refetch threshold in 9 days
and 1891 of the 2051 fresh files pass it within 30, so a frozen store's bands
start shifting within weeks. The reason is that a shifting band is AMBIGUOUS:
it moves the same way when the store freezes, when the universe rotates, and
when the filing calendar turns. A liveness count that falls to zero means one
thing only. (An earlier version of this docstring claimed the bands stay blind
for five months after a freeze; that was wrong by a factor of twenty and is
corrected here rather than quietly dropped.)

It measures the WRITER, not freshness — refetching a dormant royalty trust
advances an mtime and changes nothing at the gate — and must never be read as
the latter.

KNOWN GAP, inherited from the measure this one refines (#1642). ``compute_ttm``
also filters ``unit == "USD"`` and a form whitelist; neither this census nor
``_data_age_days`` filters either, so a CNY or CAD reporter and a 10-KT filer
read here as fresher than the consumer sees them. Measured rather than merely
admitted: applying both filters to the age moves 16 of 4195 local files between
bands, 0.4%, and 15 of those were dark either way. It is the SMALL half of the
over-report. The large half is TTM constructability above — 43 files against
this gap's 1, on the same population — so a reader calibrating a dashboard
should take ``fresh + aging`` as about 6% optimistic, not 0.4%.

Read through projected parquet reads rather than through
``CompanyfactsParquetReader``: that reader loads every column and memoises up
to 2500 whole tables, and a store-wide scan has no use for either. Three of the
twelve columns answer the whole census.

COST, measured on the VPS against the real 6076-file store, three consecutive
runs inside the pipeline image: 28.6 s, 34.1 s and 34.4 s, so 4.7-5.7 ms per
file, with a peak RSS of 288-327 MB. An earlier version of this docstring said
4-8 s, extrapolated from the Mac's 0.66-1.21 ms per file; the VPS is four to
five times slower per file and the extrapolation was never run there. Against
the thematic build's 210-minute start timeout and its 4 GB MemoryMax that is
still about 0.3% of the time budget and under a tenth of the memory, which is
what makes a full scan three times a day affordable.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from alphalens_pipeline.data.fundamentals import concept_chains as chains
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import newest_period_end
from alphalens_pipeline.data.fundamentals.ttm_aggregator import DEFAULT_TTM_MAX_STALENESS_DAYS
from alphalens_pipeline.data.store.edgar_fundamentals import REFETCH_DATA_AGE_DAYS

logger = logging.getLogger(__name__)

#: The taxonomy both FCFF legs are read from. ``dei`` is excluded by
#: construction: the cover page advances on any filing at all, which is the tag
#: that made the original freshness measure lie.
_TAXONOMY: Final[str] = "us-gaap"

#: The two legs, each a CHAIN rather than one concept, because an issuer can
#: file either member and still be filing the leg. The reasons differ: the OCF
#: chain's second member is the continuing-operations variant the same issuer
#: migrates to, while the capex chain's is a different tag some issuers use
#: instead (``PaymentsToAcquireProductiveAssets``). Reading one member only
#: would put a live filer in ``chain_absent``.
_OCF_LEG: Final[frozenset[str]] = frozenset(chains.OPERATING_CASH_FLOW)
_CAPEX_LEG: Final[frozenset[str]] = frozenset(chains.CAPEX)

#: Columns the census reads. The other nine in
#: ``companyfacts_parquet.SCHEMA`` answer nothing it asks.
_COLUMNS: Final[list[str]] = ["taxonomy", "concept", "period_end"]

#: How far ahead the crossing counter looks.
#:
#: 30 days, bounded on both sides by measurement. The floor: inside the refetch
#: band the retry interval grows with the age of the data,
#: ``min(90, max(7, age // 10))``, which is 27 days at age 270 — so a shorter
#: horizon can count a CIK that is legitimately not yet due to be re-asked. The
#: ceiling: every CIK in the refetch band crosses the gate within
#: ``270 - 150 = 120`` days by arithmetic, so any horizon from 120 up is just
#: ``band="aging"`` under a second name. Verified on the PRODUCTION store
#: with this census's own measure — within 120 days reads 569, exactly the size
#: of the refetch band, while 30 days reads 39. (An earlier version of this
#: comment quoted 1218 for the same identity; that was the union measure's
#: band, taken on the Mac's mirror, and both the instrument and the store were
#: wrong.)
CROSSING_HORIZON_DAYS: Final[int] = 30

#: How far back the writer-liveness counter looks.
#:
#: 7 days, because it must clear the longest gap a HEALTHY store can show. The
#: refetch budget is spent freshest-stale-first, so a converged store writes
#: only the files that newly crossed the threshold. Measured on production on
#: 2026-11-03 by this census: 414 files carry an mtime inside this window, so
#: the healthy reading is firmly non-zero rather than marginally so. A 1-day
#: window would read zero on a quiet legitimate day; a 30-day window would take
#: a month to notice a freeze.
WRITER_LIVENESS_DAYS: Final[int] = 7

_BAND_FRESH: Final[str] = "fresh"
#: Named ``aging`` and deliberately NOT ``refetching``, although its lower
#: bound is :data:`REFETCH_DATA_AGE_DAYS`. The refetcher decides with the
#: store's UNION measure, this census bins on the binding leg, and the two
#: disagree about a thousand files on the production store — so a file can sit
#: in this band while ``preload`` considers it fresh and never re-asks it. A
#: label promising "this is being refetched" would be read as a queue depth,
#: and it is not one: it is how many files are past the refetch threshold and
#: still serving.
_BAND_AGING: Final[str] = "aging"
_BAND_PAST_GATE: Final[str] = "past_gate"
_BAND_CHAIN_ABSENT: Final[str] = "chain_absent"

_METRIC_CACHED_FILES: Final[str] = "alphalens_companyfacts_cached_files"
_METRIC_AGE_BAND: Final[str] = "alphalens_companyfacts_fcff_age_files"
_METRIC_CROSSING: Final[str] = "alphalens_companyfacts_crossing_gate_files"
_METRIC_REFETCHED: Final[str] = "alphalens_companyfacts_refetched_files"

#: Schema of the JSON rendering, so a consumer can branch on the version.
CENSUS_SCHEMA: Final[str] = "alphalens.companyfacts.census/v1"


@dataclass(frozen=True)
class StoreCensus:
    """One reading of the whole store.

    ``readable + empty + unreadable == total_files``, and
    ``fresh + aging + past_gate + chain_absent == readable``. Both
    identities are pinned by tests; they are what lets a rule compare a count
    against its own denominator rather than against a remembered one.
    """

    asof: dt.date
    total_files: int
    readable: int
    empty: int
    unreadable: int
    fresh: int
    aging: int
    past_gate: int
    chain_absent: int
    crossing_gate_within_horizon: int
    refetched_recently: int


def _leg_age_days(table: pa.Table, leg: frozenset[str], today: dt.date) -> int | None:
    """Age of the newest ENDED period on one leg, or ``None`` if it cannot serve.

    ``None`` covers both "the issuer does not file this chain" and "every row it
    files is dated in the future". Neither can produce a TTM, so both belong in
    the same bucket, and ``newest_period_end`` already drops future rows rather
    than clamping them to age zero.
    """
    newest = newest_period_end(table, taxonomy=_TAXONOMY, on_or_before=today, concepts=leg)
    return None if newest is None else (today - newest).days


def _binding_age_days(table: pa.Table, today: dt.date) -> int | None:
    """Age of the older of the two FCFF legs, or ``None`` if either is absent."""
    ocf = _leg_age_days(table, _OCF_LEG, today)
    capex = _leg_age_days(table, _CAPEX_LEG, today)
    if ocf is None or capex is None:
        return None
    return max(ocf, capex)


def _written_within(path: Path, today: dt.date, days: int) -> bool:
    """Was ``path`` last written no more than ``days`` ago?

    Reads mtime, the same thing ``_is_stale`` reads to decide when a ticker was
    last re-asked. mtime does not survive a copy or an rsync of the store, so
    this answers "did the writer run here", never "how old is the data".
    """
    try:
        written = dt.date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return (today - written).days <= days


def census_companyfacts_store(store_dir: Path, *, today: dt.date | None = None) -> StoreCensus:
    """Census every ``*.parquet`` in ``store_dir``.

    ``*.parquet`` exactly, so the ``.parquet.tmp`` sibling an interrupted
    ``_fetch_and_write`` leaves behind is not counted at all. It is debris no
    reader opens, and calling it unreadable would report corruption that is not
    there.

    A ``store_dir`` that does not exist reads as an empty store rather than
    raising. That is a real condition the gauges must be able to report — a
    ``Path.home()`` resolved inside a container once pointed the whole pipeline
    at an empty directory — and the caller is a best-effort observability step
    that would otherwise lose the entire emit.

    That behaviour needs no guard here, which is worth stating because an
    ``is_dir()`` check reads as load-bearing and is not: ``Path.glob`` yields
    nothing for a missing directory instead of raising. An earlier version of
    this function carried the check, and removing it killed no test — measured,
    not assumed. The test stays, because the CONTRACT is what matters and the
    obvious optimisation of a 6000-file scan is ``os.scandir``, which raises
    ``FileNotFoundError`` for exactly this input.
    """
    ref = today or dt.date.today()
    counts = {
        _BAND_FRESH: 0,
        _BAND_AGING: 0,
        _BAND_PAST_GATE: 0,
        _BAND_CHAIN_ABSENT: 0,
    }
    total = empty = unreadable = crossing = refetched = 0

    for path in sorted(store_dir.glob("*.parquet")):
        total += 1
        if _written_within(path, ref, WRITER_LIVENESS_DAYS):
            refetched += 1

        try:
            table = pq.read_table(path, columns=_COLUMNS)
        except (OSError, pa.ArrowInvalid) as exc:
            logger.warning("companyfacts census: %s is unreadable (%s)", path.name, exc)
            unreadable += 1
            continue

        if table.num_rows == 0:
            empty += 1
            continue

        age = _binding_age_days(table, ref)
        if age is None:
            counts[_BAND_CHAIN_ABSENT] += 1
        elif age <= REFETCH_DATA_AGE_DAYS:
            counts[_BAND_FRESH] += 1
        elif age <= DEFAULT_TTM_MAX_STALENESS_DAYS:
            counts[_BAND_AGING] += 1
        else:
            counts[_BAND_PAST_GATE] += 1

        # Inside the gate today, outside it `CROSSING_HORIZON_DAYS` from now.
        # A file already past the gate is excluded: it crossed, it is not about
        # to, and counting it would make this series rise and never fall.
        if age is not None and (
            DEFAULT_TTM_MAX_STALENESS_DAYS - CROSSING_HORIZON_DAYS
            < age
            <= DEFAULT_TTM_MAX_STALENESS_DAYS
        ):
            crossing += 1

    return StoreCensus(
        asof=ref,
        total_files=total,
        readable=total - empty - unreadable,
        empty=empty,
        unreadable=unreadable,
        fresh=counts[_BAND_FRESH],
        aging=counts[_BAND_AGING],
        past_gate=counts[_BAND_PAST_GATE],
        chain_absent=counts[_BAND_CHAIN_ABSENT],
        crossing_gate_within_horizon=crossing,
        refetched_recently=refetched,
    )


def census_metrics(census: StoreCensus) -> dict[str, int]:
    """The census as Prometheus gauges, every series on every run.

    The gauge names live here rather than at the emit call site so that a band
    cannot be added to the census without its series appearing with it. Counts
    only, never a precomputed share: a ratio cannot tell a numerator rising
    from a denominator collapsing, and PromQL divides for free.

    Every label value is written even when its bucket is zero. A series that
    disappears when it empties makes ``absent()`` unusable, because a drained
    bucket and a dead emitter then look the same.
    """
    return {
        f'{_METRIC_CACHED_FILES}{{state="readable"}}': census.readable,
        f'{_METRIC_CACHED_FILES}{{state="empty"}}': census.empty,
        f'{_METRIC_CACHED_FILES}{{state="unreadable"}}': census.unreadable,
        f'{_METRIC_AGE_BAND}{{band="{_BAND_FRESH}"}}': census.fresh,
        f'{_METRIC_AGE_BAND}{{band="{_BAND_AGING}"}}': census.aging,
        f'{_METRIC_AGE_BAND}{{band="{_BAND_PAST_GATE}"}}': census.past_gate,
        f'{_METRIC_AGE_BAND}{{band="{_BAND_CHAIN_ABSENT}"}}': census.chain_absent,
        f'{_METRIC_CROSSING}{{within_days="{CROSSING_HORIZON_DAYS}"}}': (
            census.crossing_gate_within_horizon
        ),
        f'{_METRIC_REFETCHED}{{within_days="{WRITER_LIVENESS_DAYS}"}}': census.refetched_recently,
    }
