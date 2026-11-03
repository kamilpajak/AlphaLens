"""How much of the companyfacts store can the FCFF path still read?

A population census of ``~/.alphalens/companyfacts_parquet/``, emitted as
Prometheus gauges so that the store ageing out of the TTM staleness gate stops
being invisible.

WHY THIS EXISTS. ``compute_ttm`` refuses any result whose freshest input period
ended more than :data:`~alphalens_pipeline.data.fundamentals.ttm_aggregator.DEFAULT_TTM_MAX_STALENESS_DAYS`
days before ``asof``. Until #1636 the store never refreshed a file it already
held, so 2612 cached tables stopped at the quarter ending 2025-12-31 and all
crossed that gate on 2026-09-28 — together, silently, with no error anywhere.
Every column that calls ``compute_ttm`` went blank with them: the FCFF yield,
the valuation composite, the magic-formula health screen, the Buffett layer and
the O'Neil earnings-growth term. #1636 gave the store a lifetime; its "Known
issues" left this half undone, and named the next wave: 1843 tables stopping at
2026-03-31, due to cross on 2026-12-27.

WHAT IT MEASURES, AND WHAT IT DELIBERATELY DOES NOT.

* **The STORE, never the output.** A null ``fcff_yield_pct`` has six causes and
  only two of them are missing data — the largest bucket is a company that
  genuinely burns cash, which is a real signal about the issuer. A gauge
  counting nulls would move when the universe's sector mix moves and stay flat
  while the store froze. That is why the existing output-side gauge
  ``alphalens_thematic_score_core_numerics_finite_total`` covers only columns
  whose absence is always a defect, and ``fcff_yield_pct`` is kept out of it.
* **The BINDING leg, not the union.** ``compute_fcff`` needs an operating-cash-
  flow TTM *and* a capex TTM, each gated on its own. So the age that decides
  whether this issuer can produce an FCFF yield is the OLDER of the two legs.
  This is NOT the measure ``EdgarFundamentalsStore._data_age_days`` uses: that
  one takes a single max over the UNION of both chains, which is right for
  deciding whom to re-ask (any sign of life means the issuer is worth a fetch)
  and wrong for reporting who has gone dark. Counted over all 4195 readable
  files of the local store on 2026-11-03: the union measure places 1228 inside
  the gate, and of those 293 are past it on the binding leg while another 168
  are missing a leg entirely. The three numbers close exactly against this
  census's own bands (767 + 293 + 168 = 1228), which is the second path that
  makes them a measurement rather than a restatement.
* **An absent leg is its own bucket.** ``band="chain_absent"`` holds the
  issuers that never file one of the two chains at all — financials, royalty
  trusts, miners. That is cause 2 of the #1335 decomposition: a structural
  property of the universe, not a staleness a refetch could cure. Folded into
  ``past_gate`` it would put a permanent floor under the series a reader
  watches for movement.

WHAT IS AND IS NOT ALERTABLE, measured on the production store on 2026-11-03
(all 6076 files, read through this measure):

  1827 files, 30.1% of the store, are past the gate. That share is not a fault
  and cannot be drained. ``preload`` re-asks only the tickers in the current
  run's universe, so a CIK nobody scores is never re-asked: 1219 of the 1697
  chain-bearing past-gate files had not been written for over 120 days. A level
  threshold on that share is lit from the day it ships. A RISE threshold is no
  better — 786 files cross the gate before 2026-12-27, 698 of them on that one
  date, and 143 of the 786 were refetched within the last 30 days and still
  carry 2026-03-31 or older, because SEC has nothing newer to give.

So the age bands and the crossing horizon are DASHBOARD series: they make the
wave visible and graphable, and nothing pages on them. What pages is the
machinery — is the store still there, do its files still open, is anything
still writing it. :data:`WRITER_LIVENESS_DAYS` is the series for that last one,
and it is the only one that moves within days of a regression: a frozen store
keeps serving fresh data for about 150 days before one file reaches the refetch
threshold, so every age band is blind for five months after the freeze starts.
It measures the WRITER, not freshness — refetching a dormant royalty trust
advances an mtime and changes nothing at the gate — and must never be read as
the latter.

KNOWN GAP, inherited from the measure this one refines (#1642). ``compute_ttm``
also filters ``unit == "USD"`` and a form whitelist; neither this census nor
``_data_age_days`` filters either. So a CNY or CAD reporter, and a 10-KT filer,
read here as fresher than the consumer actually sees them, and this census
slightly UNDER-reports how much of the store is dark. Measured over 600 random
CIKs on 2026-09-30, 5 disagreed and none was masked, because all five were past
the threshold anyway. Closing it needs its own measurement: a non-USD reporter
would end up with no age at all, which may be worse than the gap.

Read through projected parquet reads rather than through
``CompanyfactsParquetReader``: that reader loads every column and memoises up
to 2500 whole tables, and a store-wide scan has no use for either. Three of the
twelve columns answer the whole census. Measured 0.66-1.21 ms per file, so
about 4-8 s for a 6076-file store; the whole 4200-file local store censuses in
7.6 s end to end through the CLI.
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

#: The two legs, each a CHAIN rather than one concept, so an issuer that
#: migrated to the continuing-operations variant still reads as filing the leg.
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
#: ``band="refetching"`` under a second name. Verified on the local store —
#: within 60d, within 90d and within 120d all read 1218, the size of the band.
CROSSING_HORIZON_DAYS: Final[int] = 30

#: How far back the writer-liveness counter looks.
#:
#: 7 days, because it must clear the longest gap a HEALTHY store can show. The
#: refetch budget is spent freshest-stale-first, so a converged store writes
#: only the few files that newly crossed the threshold: measured on production
#: over the week to 2026-11-03, the daily stale counts were 82, 12, 31 and 5,
#: and 408 files were written in those 7 days. A 1-day window would read zero
#: on a quiet legitimate day; a 30-day window would take a month to notice a
#: freeze.
WRITER_LIVENESS_DAYS: Final[int] = 7

_BAND_FRESH: Final[str] = "fresh"
_BAND_REFETCHING: Final[str] = "refetching"
_BAND_PAST_GATE: Final[str] = "past_gate"
_BAND_CHAIN_ABSENT: Final[str] = "chain_absent"

_METRIC_CACHED_FILES: Final[str] = "alphalens_companyfacts_cached_files"
_METRIC_AGE_BAND: Final[str] = "alphalens_companyfacts_fcff_age_band"
_METRIC_CROSSING: Final[str] = "alphalens_companyfacts_crossing_gate_files"
_METRIC_REFETCHED: Final[str] = "alphalens_companyfacts_refetched_files"

#: Schema of the JSON rendering, so a consumer can branch on the version.
CENSUS_SCHEMA: Final[str] = "alphalens.companyfacts.census/v1"


@dataclass(frozen=True)
class StoreCensus:
    """One reading of the whole store.

    ``readable + empty + unreadable == total_files``, and
    ``fresh + refetching + past_gate + chain_absent == readable``. Both
    identities are pinned by tests; they are what lets a rule compare a count
    against its own denominator rather than against a remembered one.
    """

    asof: dt.date
    total_files: int
    readable: int
    empty: int
    unreadable: int
    fresh: int
    refetching: int
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
    """
    ref = today or dt.date.today()
    counts = {
        _BAND_FRESH: 0,
        _BAND_REFETCHING: 0,
        _BAND_PAST_GATE: 0,
        _BAND_CHAIN_ABSENT: 0,
    }
    total = empty = unreadable = crossing = refetched = 0

    for path in sorted(store_dir.glob("*.parquet")) if store_dir.is_dir() else []:
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
            counts[_BAND_REFETCHING] += 1
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
        refetching=counts[_BAND_REFETCHING],
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
        f'{_METRIC_AGE_BAND}{{band="{_BAND_REFETCHING}"}}': census.refetching,
        f'{_METRIC_AGE_BAND}{{band="{_BAND_PAST_GATE}"}}': census.past_gate,
        f'{_METRIC_AGE_BAND}{{band="{_BAND_CHAIN_ABSENT}"}}': census.chain_absent,
        f'{_METRIC_CROSSING}{{within_days="{CROSSING_HORIZON_DAYS}"}}': (
            census.crossing_gate_within_horizon
        ),
        f'{_METRIC_REFETCHED}{{within_days="{WRITER_LIVENESS_DAYS}"}}': census.refetched_recently,
    }
