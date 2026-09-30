"""Canonical SEC EDGAR fundamentals store for AlphaLens.

Returns the 16-field features dict that the thematic scorers consume,
backed by SEC XBRL companyfacts parquets at
``~/.alphalens/companyfacts_parquet/{CIK}.parquet``. Missing CIKs are
fetched on demand via :class:`alphalens_pipeline.data.alt_data.sec_edgar_client.SecEdgarClient`
(throttled to SEC's 10 req/s polite limit, retry/backoff included).

The 16-field parity contract was originally defined by the now-deleted
SimFin store; downstream scorers in
``alphalens_pipeline/thematic/screening/{fcff_signal,valuation_signal,magic_formula}.py``
work via single-line import swap. Validation gate evidence:
``docs/research/edgar_fundamentals_validation_2026_05_19.md``.

Implementation notes callers do NOT need to handle:

- EDGAR ``CapEx`` is reported with positive sign (cash outflow magnitude),
  not negated as in SimFin. No client-side sign flip needed — but the
  parity contract preserves SimFin's positive convention, so callers see
  the same sign either way.
- ``tax_rate`` is derived from ``IncomeTaxExpenseBenefit / PreTaxIncome``
  and clamped to ``[0, 0.35]`` (SimFin parity); defaults to 0.21 when the
  components are missing.
- ``long_term_debt`` and ``short_term_debt`` use a debt-free fallback to
  ``0.0`` when the issuer has filed at least one balance sheet but never
  a debt row — fixes the gap that broke EV/EBITDA for MANH-class tickers.
- ``shares_outstanding`` follows a 3-tier chain (issue #172 Bug 1):
  1. ``dei:EntityCommonStockSharesOutstanding`` — modern primary (cover-
     page disclosure, often fresher than the balance-sheet tag).
  2. ``us-gaap:CommonStockSharesOutstanding`` — legacy fallback.
  Both XBRL tiers apply a 180-day staleness gate (issuers like C3.ai
  populated us-gaap once at IPO and never refreshed it).
  3. yfinance ``Ticker.get_shares_full`` / ``fast_info.shares`` —
     external fallback when both XBRL chains are missing or stale.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient
from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client
from alphalens_pipeline.data.fundamentals import concept_chains as chains
from alphalens_pipeline.data.fundamentals.annual_aggregator import (
    AnnualStatement,
    annual_statements,
)
from alphalens_pipeline.data.fundamentals.capital_allocation import (
    CapitalAllocation,
    compute_buyback_proxy,
)
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    companyfacts_json_to_parquet_table,
)
from alphalens_pipeline.data.fundamentals.edgar_companyfacts import _pit_filter
from alphalens_pipeline.data.fundamentals.owner_earnings import (
    OwnerEarnings,
    compute_owner_earnings,
)
from alphalens_pipeline.data.fundamentals.sic_index import get_sic
from alphalens_pipeline.data.fundamentals.ttm_aggregator import (
    _arrow_table_to_entries,
    compute_ttm,
    fcf_margin_rolling_median,
    has_any_concept,
    latest_instant,
)

logger = logging.getLogger(__name__)

DEFAULT_PARQUET_DIR = Path.home() / ".alphalens" / "companyfacts_parquet"
DEFAULT_USER_AGENT = "AlphaLens-fundamentals pajakkamil@gmail.com"
USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"

# SimFin parity: clamp tax_rate to this range and default when missing.
_TAX_RATE_MIN = 0.0
_TAX_RATE_MAX = 0.35
_TAX_RATE_DEFAULT = 0.21

# Shares-outstanding XBRL freshness window. Issuers file 10-Q every ~90
# days; 180 days covers one missed quarter plus filing lag. See
# `docs/research/edgar_fundamentals_data_quality_2026_05_20.md` and
# Perplexity research persisted under `~/.claude/projects/.../tool-results/`.
SHARES_MAX_AGE_DAYS = 180

# Ascending, NON-OVERLAPPING [lo, hi] SIC-4 ranges whose gross-vs-net
# assessed-tax gap (fuel/tobacco/alcohol excise, sales tax, USF/utility
# surcharges) is large enough (10-20% of revenue) that the store must NOT
# serve the gross-of-tax ``...IncludingAssessedTax`` fallback (issue #924).
# Shape mirrors ``sector_etf._SIC_RANGES``; pinned by
# TestTaxHeavySicRangesWellFormed.
_TAX_HEAVY_SIC_RANGES: tuple[tuple[int, int, str], ...] = (
    (2082, 2085, "alcohol excise"),  # malt/wine/distilled — federal + state excise tax
    (2100, 2199, "tobacco excise"),  # cigarettes/cigars — federal + state excise tax
    (2911, 2911, "petroleum refining"),  # motor-fuel excise tax
    (4800, 4899, "telecom"),  # USF + state/local telecom excise surcharges
    (4900, 4999, "utilities"),  # electric/gas/sanitary — state utility gross-receipts tax
    (5171, 5172, "petroleum wholesale"),  # motor-fuel excise tax passthrough
    (5200, 5999, "retail"),  # state/local sales tax
)


@cache
def _is_tax_heavy(ticker: str) -> bool:
    """True when ``ticker``'s SIC falls in a ``_TAX_HEAVY_SIC_RANGES`` band.

    Fail-open: no SIC (unresolved ticker or missing index) -> False. The
    guard is forward-insurance — 0 current candidates are tax-heavy today —
    so failing open costs nothing now and simply protects future
    tax-heavy-sector filers from serving gross-of-assessed-tax revenue via
    the ``RevenueFromContractWithCustomerIncludingAssessedTax`` fallback.
    lru-cached: zero I/O after the first call per ticker (``get_sic``
    itself is backed by a process-wide memoised parquet lookup).
    """
    sic = get_sic(ticker)
    if sic is None:
        return False
    return any(lo <= sic <= hi for lo, hi, _label in _TAX_HEAVY_SIC_RANGES)


#: A cached companyfacts table whose newest period ON THE FCFF PATH ends more
#: than this many days before today is REFETCHED by
#: :meth:`EdgarFundamentalsStore.preload`. Which rows count is
#: :meth:`EdgarFundamentalsStore._data_age_days`.
#:
#: 150, derived from the two facts that bracket it (#1335).
#:
#: The ceiling is ``ttm_aggregator.DEFAULT_TTM_MAX_STALENESS_DAYS`` (270): past
#: that, ``compute_ttm`` refuses the result and every EDGAR-derived column for
#: that ticker goes blank. Refreshing must happen well before it, not at it.
#:
#: The floor is what a HEALTHY quarterly filer looks like. Its newest period end
#: is legitimately old between filings: a quarter ending 2026-03-31 stays the
#: newest one until the following 10-Q lands around 2026-08-09, which is 131
#: days. A threshold under ~135 would refetch every healthy ticker on every run.
#:
#: Why this exists: before #1335 ``preload`` fetched a CIK only when its file was
#: absent and never again. Measured on the production store 2026-09-30, 4784 of
#: 5992 files were written in 2026-05 and never touched; 2612 of them stop at the
#: quarter ending 2025-12-31, so on 2026-09-28 they all crossed the 270-day gate
#: at once. 1843 more stop at 2026-03-31 and cross it on 2026-12-27.
REFETCH_DATA_AGE_DAYS = 150

#: Floor on how often one ticker is re-asked, whatever its data age. Does NOT
#: apply to a missing or unreadable file: that one is refetched on sight,
#: because there is no usable answer to protect.
REFETCH_MIN_INTERVAL_DAYS = 7

#: The retry interval scales with the age of the data: a ticker whose newest
#: period is ``A`` days old is re-asked every ``A / REFETCH_BACKOFF_DIVISOR``
#: days, floored at :data:`REFETCH_MIN_INTERVAL_DAYS`.
#:
#: A flat interval does not work, and the reason is measured rather than
#: supposed. Over the 1198 tables fetched on or after 2026-06-01, the age of the
#: newest reported period ON THE DAY OF THE FETCH was: median 66 days, p75 164,
#: p90 336, p99 1284. **35.3% were already past the 150-day threshold the moment
#: they arrived.** Those CIKs do not file quarterly — royalty trusts, closed-end
#: funds, foreign private issuers, dormant shells that entered the universe as
#: SIC peers. A flat 7-day retry would re-ask roughly 2100 of them every week,
#: which is more than the whole per-call budget, so the tickers a refetch would
#: actually help would never be reached.
#:
#: 10, so a ticker is re-asked about ten times over the life of its staleness. At
#: the 150-day threshold that is a 15-day interval, which spends an eighth of the
#: 120-day margin before the TTM gate — fast enough to catch a new filing well
#: before the ticker goes dark, slow enough that a dormant CIK costs little.
REFETCH_BACKOFF_DIVISOR = 10

#: Ceiling on the scaled retry interval. Scoping the age to the FCFF chains made
#: ages larger and therefore intervals longer, which is correct — the measure it
#: replaced was reading a fresh ``dei`` cover page — but ``age // 10`` on a
#: 5470-day-old table is a 547-day wait, and a dormant issuer that resumed filing
#: would go unnoticed for that long.
#:
#: 90, measured 2026-09-30 across the whole store: the ceiling costs 2 extra
#: refetches a day out of the 600/day the budget allows (233 -> 235) and cuts the
#: worst wait from 547 days to 90. It binds on 249 CIKs.
#:
#: Equal to :data:`REFETCH_EMPTY_INTERVAL_DAYS` on purpose rather than by
#: coincidence: a table with no answer and a table whose answer is five years old
#: pose the same operational question. Pinned by
#: ``test_the_ceiling_and_the_no_answer_interval_agree``.
REFETCH_MAX_INTERVAL_DAYS = 90

#: Interval for a table that is READABLE but has no age to report at all: empty,
#: or holding no ``us-gaap`` row with a past period. Both are valid SEC answers,
#: not broken files, so they must not be retried like one — and neither has a
#: period the scaled rule above could scale. 18 of the 5992 cached tables were
#: empty on 2026-09-30.
REFETCH_EMPTY_INTERVAL_DAYS = 90

#: Stale tickers refreshed per :meth:`EdgarFundamentalsStore.preload` call.
#:
#: Measured 2026-09-30: one companyfacts refetch costs 0.36 s and 4.3 MB (mean
#: over TDOC / ZION / BAH / AAPL), and one thematic run's preload universe held
#: 1630 tickers whose data was already past the age above. Unbounded, the first
#: run after this change would pay about 10 minutes and 7 GB inside a build that
#: had been killed by its own start timeout eight days earlier (#1628).
#:
#: 200 is about 72 s and 0.9 GB, under 1% of the build's 210-minute budget. The
#: store converges over several runs instead of stalling one: 4288 cached CIKs
#: are stale and plausibly refreshable (newest period 150-450 days old), and
#: since those sort FIRST, 200 per call across three runs a day works through
#: them in about 7 days — against a 2026-12-27 deadline.
#:
#: Simulated against the real store 2026-09-30, steady-state refetches per day:
#: a flat 7-day retry needs 661, which is MORE than the 600/day this budget
#: allows, so it would never converge; the scaled retry needs 210. Over one
#: run's actual preload universe (2041 tickers, the largest of the last three)
#: it is 199/day flat against 63/day scaled.
REFETCH_BUDGET_PER_CALL = 200

#: The age measure is scoped to the two chains ``compute_ttm`` reads on the FCFF
#: path. Built once at import: rebuilding the value set on every call costs more
#: than the scan it feeds.
_AGE_TAXONOMY: Final[str] = "us-gaap"
_AGE_CONCEPTS: Final[frozenset[str]] = frozenset(chains.OPERATING_CASH_FLOW) | frozenset(
    chains.CAPEX
)
_AGE_CONCEPT_SET = pa.array(sorted(_AGE_CONCEPTS))


def _newest_age(ends: Any, today: date) -> int | None:
    """Days since the newest period in ``ends`` that has actually ENDED.

    Future-dated rows are DROPPED rather than clamped to 0: an age of 0 still
    reads as maximally fresh, which is the thing being fixed. ``None`` when
    nothing is left, so the caller cannot mistake "cannot be dated" for "fresh".
    """
    past = pc.filter(ends, pc.less_equal(ends, pa.scalar(today, type=pa.date32())))
    if len(past) == 0:
        return None
    return (today - pc.max(past).as_py()).days


class EdgarFundamentalsStore:
    """PIT fundamentals store backed by SEC XBRL companyfacts.

    Canonical fundamentals source for AlphaLens. The 16-field dict
    returned by :meth:`ev_fcff_features_as_of` is the parity contract that
    thematic scorers in
    ``alphalens_pipeline/thematic/screening/{fcff_signal,valuation_signal,magic_formula}.py``
    consume.

    Parameters
    ----------
    cache_dir
        Local parquet cache root. Defaults to ``~/.alphalens/companyfacts_parquet/``.
    with_prices
        Reserved for future price-source integration. EDGAR has no quotes —
        the thematic pipeline pairs EDGAR shares with yfinance closes via
        :mod:`alphalens_pipeline.thematic.verification.mcap_filter`. Today this flag
        only controls whether ``price`` and ``shares_outstanding`` appear in
        the returned dict; when ``False``, ``price`` is ``None`` and
        ``shares_outstanding`` is sourced from EDGAR regardless.
    sec_client
        Injectable :class:`SecEdgarClient`; defaults to one instantiated
        from the ``SEC_EDGAR_USER_AGENT`` env var (or a sensible default).
        Tests pass a stub.
    """

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        with_prices: bool = False,
        sec_client: SecEdgarClient | None = None,
    ) -> None:
        self._dir = Path(cache_dir) if cache_dir is not None else DEFAULT_PARQUET_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._with_prices = with_prices
        self._sec_client = sec_client or self._default_sec_client()
        self._reader = CompanyfactsParquetReader(self._dir)
        # ticker (upper) -> 10-digit CIK; populated by preload() + on-demand.
        self._ticker_to_cik: dict[str, str] = {}
        self._loaded_tickers: bool = False
        # ticker (upper) -> latest close from yfinance batch in preload().
        self._prices: dict[str, float] = {}
        # ticker (upper) -> shares from yfinance 3rd-tier fallback.
        self._shares_cache: dict[str, float | None] = {}

    @staticmethod
    def _default_sec_client() -> SecEdgarClient:
        ua = os.environ.get(USER_AGENT_ENV) or DEFAULT_USER_AGENT
        return SecEdgarClient(user_agent=ua)

    # --- ticker → CIK resolution ------------------------------------------

    def _load_ticker_map(self) -> None:
        if self._loaded_tickers:
            return
        try:
            payload = self._sec_client.fetch_company_tickers()
        except Exception as exc:  # network / parse fail — caller can still hit cached parquets
            logger.warning("SEC company_tickers.json fetch failed: %s", exc)
            payload = {}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            t = entry.get("ticker")
            cik = entry.get("cik_str")
            if t and cik is not None:
                self._ticker_to_cik[str(t).upper()] = str(cik).zfill(10)
        self._loaded_tickers = True

    def _cik_for(self, ticker: str) -> str | None:
        self._load_ticker_map()
        return self._ticker_to_cik.get(ticker.upper())

    # --- universe + preload -----------------------------------------------

    def universe(self) -> list[str]:
        """Tickers whose CIK has a parquet on disk.

        Replaces direct ``._income.index.get_level_values('Ticker')`` access
        in ``scripts/experiment_ev_fcff_yield.py`` — exposes the queryable
        ticker universe without leaking internals.
        """
        self._load_ticker_map()
        cik_to_ticker = {cik: ticker for ticker, cik in self._ticker_to_cik.items()}
        available: list[str] = []
        for path in self._dir.glob("*.parquet"):
            cik = path.stem
            ticker = cik_to_ticker.get(cik)
            if ticker is not None:
                available.append(ticker)
        return sorted(set(available))

    def preload(
        self,
        tickers: list[str],
        *,
        refresh_stale: bool = True,
        refresh_budget: int | None = None,
        today: date | None = None,
    ) -> None:
        """Fetch companyfacts parquets that are missing, and refresh ones gone stale.

        Two separate jobs, with different rules, because they carry different
        costs if skipped:

        * **Missing** — no file at all means no data for that ticker. Always
          fetched, never budgeted.
        * **Stale** — a file whose newest period on the FCFF path is more than
          :data:`REFETCH_DATA_AGE_DAYS` old. Refetched at most
          ``refresh_budget`` per call (default :data:`REFETCH_BUDGET_PER_CALL`)
          and at most once per :data:`REFETCH_MIN_INTERVAL_DAYS` per ticker, so
          a large backlog converges over several runs instead of stalling one.

        Refreshing is PIT-safe. SEC serves only the current vintage, but the
        readers filter on ``filed_date <= asof``
        (``ttm_aggregator._pit_filter``), so a newer filing is simply invisible
        to an older ``asof`` — a refetch cannot introduce lookahead into a
        historical replay.

        ``refresh_stale=False`` is for the internal single-ticker calls that
        only want the missing-file fetch; without it every per-ticker lookup
        could trigger a refresh check of its own.

        Before #1335 this method skipped any ticker whose file existed, for as
        long as the file existed. See :data:`REFETCH_DATA_AGE_DAYS` for what
        that cost.
        """
        self._load_ticker_map()
        ref = today or date.today()
        budget = REFETCH_BUDGET_PER_CALL if refresh_budget is None else refresh_budget

        missing: list[tuple[str, str]] = []
        stale: list[tuple[str, str]] = []
        for ticker in tickers:
            cik = self._cik_for(ticker)
            if cik is None:
                logger.warning("ticker %s unresolved (no CIK from SEC), skipping", ticker)
                continue
            # An unreadable file belongs with the missing ones, not the stale ones.
            # It is not an out-of-date answer, it is no answer, and the stale path
            # both budgets it and sorts it last (its data age is unknown), so on a
            # large universe it could wait indefinitely.
            if (
                not (self._dir / f"{cik}.parquet").exists()
                or self._reader.get_cik_table(cik) is None
            ):
                missing.append((ticker, cik))
            elif refresh_stale and self._is_stale(cik, ref):
                stale.append((ticker, cik))

        if missing:
            logger.info("preload: fetching %d missing companyfacts from SEC", len(missing))
        if stale:
            logger.info(
                "preload: %d cached companyfacts older than %d days; refreshing %d this call",
                len(stale),
                REFETCH_DATA_AGE_DAYS,
                min(len(stale), max(budget, 0)),
            )
        # Freshest-stale first. With more stale tickers than budget, spending it
        # in ticker order would hand it to whoever sorts first, including CIKs
        # whose newest filing is from 2014 and will never advance. A ticker that
        # has only just crossed the threshold is the one a refetch helps.
        stale.sort(key=lambda pair: self._data_age_days(pair[1], ref) or 10**6)
        for ticker, cik in missing + stale[: max(budget, 0)]:
            self._fetch_and_write(ticker, cik)

        # Batch-fetch prices for all tickers in one yfinance round-trip even
        # when no companyfacts are missing — otherwise warm-cache runs would
        # never populate prices and fall through to per-ticker fast_info.
        if self._with_prices and tickers:
            self._batch_fetch_prices(tickers)

    def _data_age_days(self, cik: str, today: date) -> int | None:
        """Age of the newest period the FCFF path can consume, or ``None``.

        Scoped to :data:`_AGE_CONCEPTS`, the two chains ``compute_ttm`` reads on
        that path. Rows outside them move for reasons that say nothing about
        whether an EDGAR column can be produced: the ``dei`` cover page advances
        on any filing at all, and debt-maturity and lease instants are dated in
        the FUTURE. Taking the max over EVERY row let a ticker whose cover page
        advanced while its cash-flow chain stood still read as fresh forever,
        with every EDGAR-derived column for it blank, and let a future-dated
        instant read as a NEGATIVE age. Measured over 600 random cached CIKs on
        2026-09-30, three were masked that way for good (0001816815 read age 0
        against a 365-day-old cash-flow period; 0001140859 read -31 against 273)
        and three more were saved only by being close to the threshold anyway.

        Future-dated rows are DROPPED rather than clamped to 0, because an age of
        0 still reads as maximally fresh. Over the same 600 CIKs no chain row is
        future-dated, so this filter changes nothing today; it exists so that one
        cannot silence the measure if it appears.

        A table holding NO chain row at all falls back to its newest ``us-gaap``
        period. The FCFF path is structurally dead for such an issuer, so there is
        no chain left to mask, but the balance-sheet instants can still serve
        columns: measured 2026-09-30, 5 of the 65 sampled CIKs in that state serve
        at least one of cash, short-term debt, total equity or shares outstanding
        today. Without this tier they dropped from a 15-54 day cadence onto the
        90-day branch, a regression this scoping would otherwise have introduced.
        The ``dei`` cover page stays out of the fallback too — it is the tag that
        caused the original defect.

        KNOWN GAP (#1642): ``compute_ttm`` also filters ``unit == "USD"`` and a
        form whitelist, and this measure filters neither, so it counts chain rows
        the consumer will never read. Measured over the same 600 CIKs, 5 disagree
        — two reporting in CNY, one in CAD, two filing form 10-KT. None is masked
        today only because all five are already past the threshold. Closing it
        needs its own measurement, because a non-USD reporter would end up with
        no age at all and that may be worse than the gap.

        ``None`` means "no answer to age": missing, unreadable, empty, or holding
        no ``us-gaap`` row with a past period at all. All of them route to the long
        :data:`REFETCH_EMPTY_INTERVAL_DAYS` branch in :meth:`_is_stale`.

        Kept in Arrow on purpose. The old line materialised the whole
        ``period_end`` column through ``.to_pylist()``, about 12 000 Python date
        objects per issuer, and ``preload`` evaluates the measure twice per stale
        ticker. Over the same 600 tables that was 6.477 s against 0.320 s here.
        """
        table = self._reader.get_cik_table(cik)
        if table is None or table.num_rows == 0:
            return None
        is_us_gaap = pc.equal(table.column("taxonomy"), _AGE_TAXONOMY)
        in_chain = pc.and_(
            is_us_gaap,
            pc.is_in(table.column("concept"), value_set=_AGE_CONCEPT_SET),
        )
        chain_ends = pc.filter(table.column("period_end"), in_chain)
        if len(chain_ends) > 0:
            # The issuer files a chain, so only the chain may answer. Coming back
            # empty here means every chain row is future-dated, which is not a
            # licence to answer with something else: falling through would let a
            # younger non-chain row speak for a chain that exists, which is the
            # masking bug one level down.
            return _newest_age(chain_ends, today)
        return _newest_age(pc.filter(table.column("period_end"), is_us_gaap), today)

    def _is_stale(self, cik: str, today: date) -> bool:
        """Should ``cik``'s cached table be refetched?

        Three cases, in order:

        1. **Missing or unreadable** — refetched on sight, ignoring every
           interval. There is no usable answer to protect. :meth:`preload`
           routes these to its unbudgeted path before asking, so in practice
           this branch only fires when the predicate is called directly; it
           stays here so the predicate is total.
        2. **Readable, but no age to report** — empty, or holding no row of
           either FCFF chain. Both are valid SEC answers, re-asked on the long
           :data:`REFETCH_EMPTY_INTERVAL_DAYS` interval.
        3. **Readable with data** — stale once the newest chain period passes
           :data:`REFETCH_DATA_AGE_DAYS`, and then re-asked on an interval that
           GROWS with that age (see :data:`REFETCH_BACKOFF_DIVISOR`).
        """
        path = self._dir / f"{cik}.parquet"
        table = self._reader.get_cik_table(cik)
        if table is None:
            return True
        try:
            last_try = date.fromtimestamp(path.stat().st_mtime)
        except OSError:
            return True
        waited = (today - last_try).days

        age = self._data_age_days(cik, today)
        if age is None:  # readable, but no FCFF-path period to age
            return waited >= REFETCH_EMPTY_INTERVAL_DAYS
        if age <= REFETCH_DATA_AGE_DAYS:
            return False
        interval = min(
            REFETCH_MAX_INTERVAL_DAYS,
            max(REFETCH_MIN_INTERVAL_DAYS, age // REFETCH_BACKOFF_DIVISOR),
        )
        return waited >= interval

    def _fetch_and_write(self, ticker: str, cik: str) -> None:
        """Fetch one CIK's companyfacts and replace its parquet, best-effort.

        A failure leaves whatever was already on disk. Turning a stale answer
        into no answer would be strictly worse than the state this method
        exists to improve.
        """
        try:
            facts = self._sec_client.fetch_company_facts(cik)
        except Exception as exc:
            logger.warning("companyfacts fetch failed for %s/%s: %s", ticker, cik, exc)
            return
        table = companyfacts_json_to_parquet_table(facts)
        target = self._dir / f"{cik}.parquet"
        # Write a sibling temp file and rename over the target. This method now
        # REPLACES files that other processes (and this one) read, and Parquet
        # keeps its metadata at the END, so a torn in-place write leaves a file
        # that is unreadable rather than merely short. Same reasoning as
        # ``macro/fred_client.py::_write_cache``.
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, suffix=".parquet.tmp")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            pq.write_table(table, tmp)
            os.replace(tmp, target)
        except Exception as exc:
            logger.warning("companyfacts write failed for %s/%s: %s", ticker, cik, exc)
            tmp.unlink(missing_ok=True)
            return
        self._reader.invalidate(cik)

    # --- the parity contract: 16-field features dict ---------------------

    def ev_fcff_features_as_of(self, ticker: str, asof: date) -> dict[str, Any] | None:
        """Return the 16-field features dict, or None when no CIK / no data.

        Field-by-field parity with the now-deleted SimFin store's
        ``ev_fcff_features_as_of`` so the downstream scorers consume the
        same shape regardless of the migration history.
        """
        cik = self._cik_for(ticker)
        if cik is None:
            return None
        # Trigger on-demand fetch if the parquet is missing.
        if not (self._dir / f"{cik}.parquet").exists():
            self.preload([ticker], refresh_stale=False)
        # Re-check; if still missing the fetch failed.
        if self._reader.get_cik_table(cik) is None:
            return None

        # Duration concepts (TTM, Compustat formula).
        # Revenue chain: tax-heavy sectors (fuel/tobacco/alcohol/retail/
        # telecom/utilities) stay on the net-of-assessed-tax chain — the
        # gross-of-tax ...IncludingAssessedTax fallback would understate
        # PS / EV-REV and break cross-sector comparability there (#924).
        revenue_chain = (
            chains.REVENUE_NET_OF_ASSESSED_TAX if _is_tax_heavy(ticker) else chains.REVENUE
        )
        revenue_ttm = compute_ttm(self._reader, cik, revenue_chain, asof)
        operating_income_ttm = compute_ttm(self._reader, cik, chains.OPERATING_INCOME, asof)
        ocf_ttm = compute_ttm(self._reader, cik, chains.OPERATING_CASH_FLOW, asof)
        capex_ttm = compute_ttm(self._reader, cik, chains.CAPEX, asof)
        net_income_ttm = compute_ttm(self._reader, cik, chains.NET_INCOME, asof)

        # D&A: try the single-tag chain first, fall back to summing
        # components when neither single tag is present.
        da_ttm = compute_ttm(self._reader, cik, chains.DEPRECIATION_AMORTISATION, asof)
        if da_ttm is None:
            components = [
                compute_ttm(self._reader, cik, (c,), asof)
                for c in chains.DEPRECIATION_AMORTISATION_COMPONENTS
            ]
            present = [c for c in components if c is not None]
            if present:
                da_ttm = sum(present)

        # Interest expense: None when issuer doesn't break it out (often
        # debt-free); SimFin parity expects None in that case.
        interest_expense_ttm = compute_ttm(self._reader, cik, chains.INTEREST_EXPENSE, asof)

        # Tax rate: clamp + default per SimFin parity.
        tax_expense = compute_ttm(self._reader, cik, chains.INCOME_TAX_EXPENSE, asof)
        pretax_income = compute_ttm(self._reader, cik, chains.PRETAX_INCOME, asof)
        tax_rate = self._derive_tax_rate(tax_expense, pretax_income)

        # Instant concepts (balance sheet).
        cash_and_equivalents = latest_instant(self._reader, cik, chains.CASH, asof)
        total_equity = latest_instant(self._reader, cik, chains.EQUITY, asof)

        # Debt with debt-free fallback (MANH gap fix).
        long_term_debt = latest_instant(self._reader, cik, chains.LONG_TERM_DEBT, asof)
        short_term_debt = latest_instant(self._reader, cik, chains.SHORT_TERM_DEBT, asof)
        if (
            long_term_debt is None
            and short_term_debt is None
            and has_any_concept(self._reader, cik, chains.BALANCE_SHEET_MARKERS, asof)
        ):
            # Issuer files balance sheets but never reports debt rows.
            # Treat as structurally zero rather than missing.
            long_term_debt = 0.0
            short_term_debt = 0.0

        # Shares outstanding — 3-tier chain per issue #172 Bug 1:
        # (1) dei (modern primary, cover-page disclosure)
        # (2) us-gaap (legacy fallback)
        # Both XBRL tiers apply a 180-day age gate to defend against
        # issuers (e.g. C3.ai) whose tag was populated once at IPO and
        # never updated. (3) yfinance fallback when both XBRL tiers miss.
        shares_outstanding = latest_instant(
            self._reader,
            cik,
            chains.SHARES_OUTSTANDING_DEI,
            asof,
            taxonomy="dei",
            unit="shares",
            max_age_days=SHARES_MAX_AGE_DAYS,
        )
        if shares_outstanding is None:
            shares_outstanding = latest_instant(
                self._reader,
                cik,
                chains.SHARES_OUTSTANDING_US_GAAP,
                asof,
                unit="shares",
                max_age_days=SHARES_MAX_AGE_DAYS,
            )
        if shares_outstanding is None:
            shares_outstanding = self._fetch_shares_yf(ticker, asof)

        # Price: not in EDGAR. When ``with_prices=True`` we pull a current
        # close from yfinance. Snapshot, not PIT — acceptable for live
        # thematic briefs (asof ≈ today); historical replay would need a
        # different price source. SimFin parity reserves the field.
        price = self._fetch_price(ticker, asof) if self._with_prices else None

        # FCF margin 5y median: rolling 20-quarter window. Returns None when
        # fewer than 8 quarter-aligned data points are visible at asof — too
        # thin for a stable median. ~90% of S&P 500-class issuers clear the
        # bar (probe 2026-05-20). When None, downstream impute_fcff /
        # _effective_fcf_margin gracefully fall back to the spot TTM margin.
        # We pass the already-derived TTM tax_rate as the per-quarter proxy:
        # per-quarter tax rates are too noisy and a TTM-stable rate yields a
        # consistent FCFF tax shield across the 20-quarter window.
        fcf_margin_5y_median = fcf_margin_rolling_median(
            self._reader, cik, asof, tax_rate=tax_rate, revenue_chain=revenue_chain
        )

        publish_date_str = self._latest_publish_date(cik, asof)

        return {
            "ocf_ttm": ocf_ttm,
            "capex_ttm": capex_ttm,
            "interest_expense_ttm": interest_expense_ttm,
            "tax_rate": tax_rate,
            "revenue_ttm": revenue_ttm,
            "fcf_margin_5y_median": fcf_margin_5y_median,
            "price": price,
            "shares_outstanding": shares_outstanding,
            "long_term_debt": long_term_debt,
            "short_term_debt": short_term_debt,
            "cash_and_equivalents": cash_and_equivalents,
            "net_income_ttm": net_income_ttm,
            "publish_date_str": publish_date_str,
            "operating_income_ttm": operating_income_ttm,
            "total_equity": total_equity,
            "da_ttm": da_ttm,
        }

    def annual_series_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[AnnualStatement]:
        """Multi-year annual (FY) statement series, PIT-correct at ``asof``.

        Unlike :meth:`ev_fcff_features_as_of` (a single TTM-at-asof
        snapshot), this returns the full per-fiscal-year history — newest
        first, capped to ``max_years`` — for margin / capital-intensity
        trend analysis and DCF history. Empty list when the ticker has no
        CIK or no companyfacts on disk; triggers an on-demand fetch when the
        parquet is missing, mirroring :meth:`ev_fcff_features_as_of`.
        """
        cik = self._cik_for(ticker)
        if cik is None:
            return []
        if not (self._dir / f"{cik}.parquet").exists():
            self.preload([ticker], refresh_stale=False)
        return annual_statements(self._reader, cik, asof, max_years=max_years)

    def owner_earnings_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[OwnerEarnings]:
        """Per-fiscal-year owner earnings + working-capital deltas, PIT at ``asof``.

        Delegates to
        :func:`alphalens_pipeline.data.fundamentals.owner_earnings.compute_owner_earnings`
        over the :meth:`annual_series_as_of` series — newest first, capped to
        ``max_years``. The oldest year has no prior fiscal year, so its
        ``working_capital_change`` (and hence ``owner_earnings``) is ``None``.
        ``maintenance_capex`` is the ``min(capex, D&A)`` approximation; see the
        owner-earnings module docstring. Additive and unwired — not consumed by
        the thematic brief pipeline. Empty list when the ticker has no CIK or no
        companyfacts on disk.
        """
        return compute_owner_earnings(self.annual_series_as_of(ticker, asof, max_years=max_years))

    def capital_allocation_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[CapitalAllocation]:
        """Per-fiscal-year buyback proxy (Δ shares outstanding YoY), PIT at ``asof``.

        Delegates to
        :func:`alphalens_pipeline.data.fundamentals.capital_allocation.compute_buyback_proxy`
        over the :meth:`annual_series_as_of` series — newest first, capped to
        ``max_years``. ``net_buyback`` reports only the SIGN of the share-count
        change (a fall = net buyback, a rise = net issuance / dilution), not the
        dollar amount; see the capital-allocation module docstring. The oldest
        year has no prior fiscal year, so its change fields are ``None``.
        Additive and unwired — not consumed by the thematic brief pipeline.
        Empty list when the ticker has no CIK or no companyfacts on disk.
        """
        return compute_buyback_proxy(self.annual_series_as_of(ticker, asof, max_years=max_years))

    # --- internals --------------------------------------------------------

    @staticmethod
    def _derive_tax_rate(tax_expense: float | None, pretax_income: float | None) -> float:
        if tax_expense is None or pretax_income is None or pretax_income == 0:
            return _TAX_RATE_DEFAULT
        try:
            rate = tax_expense / pretax_income
        except ZeroDivisionError:
            return _TAX_RATE_DEFAULT
        if math.isnan(rate):
            return _TAX_RATE_DEFAULT
        return max(_TAX_RATE_MIN, min(_TAX_RATE_MAX, rate))

    def _batch_fetch_prices(self, tickers: list[str]) -> None:
        """Populate self._prices from one canonical-client batch download.

        Cuts N sequential HTTPS round-trips (one per ticker) down to one.
        Misses (delisted, weird tickers, partial yfinance returns, a 429 that
        survives the client's retries) are simply absent and fall through to the
        per-ticker path in :meth:`_fetch_price`. The shared throttle + bounded
        retry now live in :class:`YFinanceClient` (PR #573).
        """
        self._prices.update(get_default_yfinance_client().batch_last_close(tickers))

    def _fetch_price(self, ticker: str, asof: date) -> float | None:
        """Latest close via the canonical :class:`YFinanceClient`; ``None`` on failure.

        Snapshot (most recent close on or before today), not PIT.
        Acceptable for live thematic briefs where asof ≈ today; the
        future paradigm-13 audit replay (which IS PIT-sensitive) is out
        of scope of this store and routes through a separate price loader.

        A value already prefetched by :meth:`_batch_fetch_prices` short-circuits
        the per-ticker call. Only a real value is memoised — a ``None`` (the
        client exhausted its retries or the name is a genuine miss) is NOT cached
        so a later candidate in the same batch re-attempts.

        Note (not time-aligned): a batch-prefetched price is the most recent
        close in a 5-day window (usually the prior trading day) while this
        per-ticker fallback is the live intraday ``last_price``, so within one
        brief some market caps use yesterday's close and some today's live
        price. Pre-existing in this store; a future PIT-aware price loader would
        remove the mismatch.
        """
        key = ticker.upper()
        cached = self._prices.get(key)
        if cached is not None:
            return cached
        price = get_default_yfinance_client().last_price(ticker)
        if price is None:
            return None
        self._prices[key] = price
        return price

    def _fetch_shares_yf(self, ticker: str, asof: date) -> float | None:
        """Third-tier shares fallback via the canonical :class:`YFinanceClient`.

        Delegates to :meth:`YFinanceClient.shares` (the latest
        ``get_shares_full`` value on or before ``asof``, falling back to the
        ``fast_info.shares`` snapshot) so the live brief cohort and any replay
        share one throttled + retried Yahoo seam. The store keeps only the
        memoisation: a definitive value is cached for the store lifetime; a
        ``None`` (rate-limit blip that survived the client's retries, or a
        genuine miss) is NOT cached, so a later candidate re-attempts (zen
        finding #3, PR #174).

        Trade-off vs the pre-migration store: that code cached a clean-call
        ``None`` (a genuinely share-less / delisted name), whereas the uniform
        don't-cache-``None`` rule here re-fetches such a name once per candidate
        in the run. Accepted: the client distinguishes transient from permanent
        only by exhausting retries, share-less names are rare, and correctness
        (always ``None``) is unchanged — only a small, bounded extra call cost.
        """
        key = ticker.upper()
        if key in self._shares_cache:
            return self._shares_cache[key]
        val = get_default_yfinance_client().shares(ticker, asof=asof)
        if val is not None:
            self._shares_cache[key] = val
        return val

    def _latest_publish_date(self, cik: str, asof: date) -> str | None:
        """ISO date of the most recent visible filing across the core concepts.

        Mirrors SimFin's ``publish_date_str`` semantic — used by
        ``valuation_signal`` to render ``financials_age_days`` in briefs.
        """
        table = self._reader.get_cik_table(cik)
        if table is None:
            return None
        latest: str | None = None
        # Sample a handful of high-cardinality concepts likely to span the
        # filing history; iterating all concepts would be wasteful and the
        # result is the same — they all share the same set of filings.
        for chain in (
            chains.NET_INCOME,
            chains.REVENUE,
            chains.CASH,
            chains.EQUITY,
        ):
            for concept in chain:
                entries = _arrow_table_to_entries(table, concept)
                visible = _pit_filter(entries, asof)
                for e in visible:
                    if latest is None or e.filed > latest:
                        latest = e.filed
        return latest


__all__ = ["EdgarFundamentalsStore"]
