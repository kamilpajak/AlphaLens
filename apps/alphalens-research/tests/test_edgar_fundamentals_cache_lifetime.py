"""The companyfacts cache has a lifetime — #1335.

``preload`` used to fetch a CIK's companyfacts parquet only when the file was
absent, and never again. Measured on the production store 2026-09-30: 4784 of
5992 cached files were written in 2026-05 and never touched, and for 2612 of
them the newest cached quarter ends 2025-12-31. ``compute_ttm`` refuses a
result whose freshest input ends before ``asof - 270 days``, so on 2026-09-28
all 2612 stopped producing a TTM at once and every EDGAR-derived column went
blank for them. A further 1843 files hold a newest quarter ending 2026-03-31
and cross the same gate on 2026-12-27.

The first cut of that rule measured the age over EVERY row in the parquet, which
is a different quantity from the one the consumer reads. A companyfacts table
holds every XBRL concept the issuer ever filed, in two taxonomies: ``us-gaap``
(the statements) and ``dei`` (the cover page, which advances on ANY filing at
all), plus instants dated in the FUTURE (debt maturities, lease terms). So a
ticker whose cover page moved while its cash-flow chain stood still read as
fresh and was never refreshed, with every EDGAR-derived column for it blank,
and a future-dated instant produced a NEGATIVE age, which reads as maximally
fresh forever. Measured over 600 random cached CIKs on 2026-09-30: 3 were
permanently masked that way (0001816815 at age 0 with its newest cash-flow
period 365 days old; 0000788965 at 92 against 273; 0001140859 at -31 against
273), and 3 more were saved only by sitting within 30 days of crossing 150
anyway.

These tests pin the refresh rule, WHICH ROWS its age measure reads, the two
floors that bound its cost, and the relationship between the refresh age and
the TTM gate it exists to stay ahead of.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.data.fundamentals import concept_chains as chains
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import SCHEMA
from alphalens_pipeline.data.fundamentals.ttm_aggregator import DEFAULT_TTM_MAX_STALENESS_DAYS
from alphalens_pipeline.data.store import edgar_fundamentals as ef
from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

TODAY = dt.date(2026, 9, 30)
CIK_A = "0000000001"
CIK_B = "0000000002"

#: The cover-page tag that advances on any filing at all, including one that
#: carries no statements. It is the tag the masked CIKs above moved on.
COVER_PAGE = "EntityCommonStockSharesOutstanding"
#: A real future-dated us-gaap instant: an issuer states in 2026 what it owes
#: after 2031. 0001140859 carried one and read as age -31 under the old rule.
FUTURE_DATED = "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive"


def _row(
    concept: str,
    period_end: dt.date,
    *,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
    instant: bool = False,
) -> dict:
    """One companyfacts row. ``instant`` drops ``period_start`` (balance-sheet
    shape), which is how the future-dated rows in the real store look."""
    return {
        "taxonomy": taxonomy,
        "concept": concept,
        "unit": unit,
        "period_start": None if instant else period_end - dt.timedelta(days=90),
        "period_end": period_end,
        "val": 100.0,
        "accn": "x",
        "fy": 2026,
        "fp": "Q1",
        "form": "10-Q",
        "filed_date": min(period_end + dt.timedelta(days=40), TODAY),
        "frame": None,
    }


def _table(newest_period: dt.date, *, val: float = 100.0) -> pa.Table:
    """One-row companyfacts table whose newest reported period is ``newest_period``."""
    row = _row(chains.OPERATING_CASH_FLOW[0], newest_period)
    row["val"] = val
    return _rows_table([row])


def _rows_table(rows: list[dict]) -> pa.Table:
    """Table on the REAL companyfacts schema, so ``period_start`` stays nullable
    and every column keeps the type the production reader expects."""
    return pa.Table.from_pylist(rows, schema=SCHEMA)


def _facts(newest_period: dt.date) -> dict:
    """Minimal companyfacts JSON the real converter accepts."""
    return {
        "cik": 1,
        "facts": {
            "us-gaap": {
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            {
                                "start": (newest_period - dt.timedelta(days=90)).isoformat(),
                                "end": newest_period.isoformat(),
                                "val": 999.0,
                                "accn": "y",
                                "fy": 2026,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": (newest_period + dt.timedelta(days=40)).isoformat(),
                            }
                        ]
                    }
                }
            }
        },
    }


class CompanyfactsCacheLifetimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.client = MagicMock()
        self.client.fetch_company_tickers.return_value = {
            "0": {"ticker": "AAA", "cik_str": 1},
            "1": {"ticker": "BBB", "cik_str": 2},
        }
        self.client.fetch_company_facts.side_effect = lambda cik: _facts(dt.date(2026, 6, 30))

    def _store(self) -> EdgarFundamentalsStore:
        return EdgarFundamentalsStore(self.dir, sec_client=self.client)

    def _write(
        self,
        cik: str,
        newest: dt.date,
        *,
        written: dt.date = TODAY - dt.timedelta(days=120),
    ) -> Path:
        """Seed a cached table. ``written`` is the file mtime — when we last TRIED,
        which is a different question from how old the DATA is, and the interval
        floor reads it. Defaults to a month ago: the shape of the #1335 store,
        where 4784 files were written in 2026-05 and never touched again."""
        return self._write_table(cik, _table(newest), written=written)

    def _write_rows(
        self,
        cik: str,
        rows: list[dict],
        *,
        written: dt.date = TODAY - dt.timedelta(days=120),
    ) -> Path:
        """Seed a cached table from explicit rows, for the cases where WHICH rows
        the table holds is the point."""
        return self._write_table(cik, _rows_table(rows), written=written)

    def _write_table(self, cik: str, table: pa.Table, *, written: dt.date) -> Path:
        path = self.dir / f"{cik}.parquet"
        pq.write_table(table, path)
        stamp = dt.datetime.combine(written, dt.time(12, 0)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    # --- the rule ---------------------------------------------------------

    def test_a_cache_whose_newest_period_is_recent_is_not_refetched(self) -> None:
        """Control. Without this the other tests could pass by refetching always."""
        self._write(CIK_A, TODAY - dt.timedelta(days=30))
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_not_called()

    def test_a_cache_past_the_refetch_age_is_refetched(self) -> None:
        """The #1335 defect. Fails before the fix: preload skips any file that exists."""
        self._write(CIK_A, TODAY - dt.timedelta(days=ef.REFETCH_DATA_AGE_DAYS + 1))
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_the_refetched_table_replaces_the_one_on_disk(self) -> None:
        """A refetch that does not land on disk buys nothing."""
        path = self._write(CIK_A, TODAY - dt.timedelta(days=400))
        self._store().preload(["AAA"], today=TODAY)
        newest = max(pq.read_table(path).column("period_end").to_pylist())
        self.assertEqual(newest, dt.date(2026, 6, 30))

    def test_the_in_process_reader_cache_is_dropped_after_a_refetch(self) -> None:
        """Without invalidation the run keeps serving the table it just replaced."""
        self._write(CIK_A, TODAY - dt.timedelta(days=400))
        store = self._store()
        store._reader.get_cik_table(CIK_A)  # warm the memo with the stale table
        store.preload(["AAA"], today=TODAY)
        newest = max(store._reader.get_cik_table(CIK_A).column("period_end").to_pylist())
        self.assertEqual(newest, dt.date(2026, 6, 30))

    # --- the age measures the rows the CONSUMER reads ---------------------

    def test_a_fresh_cover_page_does_not_hide_a_cash_flow_chain_that_stood_still(self) -> None:
        """The masking defect. A ``dei`` cover-page row advances on ANY filing at
        all — an 8-K, a prospectus, a shell's annual cover page — while the
        cash-flow chain ``compute_ttm`` reads stands still. Measured over 600
        random cached CIKs on 2026-09-30, three were permanently masked this way
        (0001816815 read age 0 with its newest cash-flow period 365 days old) and
        three more were saved only by being within 30 days of crossing 150 anyway.
        Every EDGAR-derived column for a masked ticker is blank, forever."""
        self._write_rows(
            CIK_A,
            [
                _row(COVER_PAGE, TODAY - dt.timedelta(days=5), taxonomy="dei", unit="shares"),
                _row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=365)),
            ],
        )
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_the_second_tag_in_the_cash_flow_chain_counts_too(self) -> None:
        """``compute_ttm`` walks the whole chain and takes the first tag that hits, so
        an issuer reporting only the continuing-operations variant is served
        normally. Measuring only the chain's first tag would call it chain-less.

        Same fixture shape as the CapEx test above, and for the same reason: the
        newer non-chain row is what makes narrowing the set observable."""
        self._write_rows(
            CIK_A,
            [
                _row("Revenues", TODAY - dt.timedelta(days=5)),
                _row(chains.OPERATING_CASH_FLOW[1], TODAY - dt.timedelta(days=200)),
            ],
        )
        self.assertEqual(self._store()._data_age_days(CIK_A, TODAY), 200)

    def test_a_capex_row_counts_as_well_as_a_cash_flow_row(self) -> None:
        """The FCFF path reads OCF and CapEx, so a CapEx period is a real answer to
        "how old is the data this store can serve".

        The fixture needs a NEWER non-chain us-gaap row beside it, or the test
        cannot see the difference: drop CapEx from the chain set and the us-gaap
        fallback tier finds the very same row and returns the very same age. That
        is what made an earlier version of this test pass with CapEx deleted."""
        self._write_rows(
            CIK_A,
            [
                _row("Revenues", TODAY - dt.timedelta(days=5)),
                _row(chains.CAPEX[0], TODAY - dt.timedelta(days=200)),
            ],
        )
        self.assertEqual(self._store()._data_age_days(CIK_A, TODAY), 200)

    def test_a_future_dated_period_end_does_not_make_a_ticker_look_fresh(self) -> None:
        """An issuer states in 2026 what it owes after 2031, and that instant's
        period ends in the FUTURE. Taking the max over every row then gives a
        NEGATIVE age, which reads as maximally fresh forever: 0001140859 measured
        -31 days while its newest cash-flow period was 273 days old, past the
        270-day gate at which ``compute_ttm`` refuses to answer."""
        self._write_rows(
            CIK_A,
            [
                _row(FUTURE_DATED, TODAY + dt.timedelta(days=1800), instant=True),
                _row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=273)),
            ],
        )
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_a_future_dated_chain_row_is_dropped_rather_than_read_as_age_zero(self) -> None:
        """A future period inside the chain itself is no evidence about freshness
        either, so it is dropped and the newest period that has actually ENDED is
        what the age reports. Clamping the negative age to 0 instead would answer
        "maximally fresh" — the same wrong answer, only harder to spot in a log."""
        store = self._store()
        self._write_rows(
            CIK_A,
            [
                _row(chains.OPERATING_CASH_FLOW[0], TODAY + dt.timedelta(days=92)),
                _row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=92)),
            ],
        )
        self.assertEqual(store._data_age_days(CIK_A, TODAY), 92)

    def test_a_table_whose_only_chain_row_is_future_dated_backs_off(self) -> None:
        """Dropping every chain row leaves no age to report, which is the same
        position as a table holding no chain at all: the long interval, NOT the
        age-0 "maximally fresh" that a clamp would produce and that would suppress
        the refetch entirely."""
        self._write_rows(
            CIK_A,
            [_row(chains.OPERATING_CASH_FLOW[0], TODAY + dt.timedelta(days=92))],
            written=TODAY - dt.timedelta(days=120),
        )
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_a_table_holding_no_chain_row_at_all_backs_off_like_an_empty_one(self) -> None:
        """Rows but no OCF and no CapEx anywhere: there is no age to report and
        nothing to scale a back-off by, so it joins the empty-table branch. 65 of
        the 600 sampled CIKs are in this state — IFRS-only foreign filers and
        fee-filing-only CIKs. Measured 2026-09-30, the store serves them nothing
        today: 0 have a us-gaap revenue row, ``compute_ttm`` answers for 0 of them
        on revenue and 0 on net income, and 0 are live on any other column. Asking
        them six times less often therefore costs nothing today."""
        rows = [_row(COVER_PAGE, TODAY - dt.timedelta(days=5), taxonomy="dei", unit="shares")]

        # Literal days, NOT the constant — see the empty-table test below for why.
        self._write_rows(CIK_A, rows, written=TODAY - dt.timedelta(days=89))
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_not_called()

        self.client.reset_mock()
        self._write_rows(CIK_A, rows, written=TODAY - dt.timedelta(days=91))
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_a_fresh_chain_row_is_not_refetched_beside_an_ancient_cover_page(self) -> None:
        """Control, the other way round. Narrowing the measure must not turn into
        "refetch everything": a ticker whose cash-flow chain is current is left
        alone however old the rest of its table is."""
        self._write_rows(
            CIK_A,
            [
                _row(COVER_PAGE, dt.date(2014, 3, 31), taxonomy="dei", unit="shares"),
                _row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=30)),
            ],
        )
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_not_called()

    # --- the floors that bound the cost -----------------------------------

    def test_a_ticker_refetched_recently_is_not_refetched_again(self) -> None:
        """A delinquent filer never gets fresher. Without a floor it costs one SEC
        request per run forever, three runs a day, for as long as it is listed."""
        self._write(
            CIK_A,
            TODAY - dt.timedelta(days=160),
            written=TODAY - dt.timedelta(days=1),
        )
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_not_called()

    def test_the_retry_interval_grows_with_the_age_of_the_data(self) -> None:
        """Measured 2026-09-30 over the 1198 tables fetched since 2026-06-01: 35.3%
        were ALREADY past the 150-day age on the day they were fetched (median 66,
        p90 336, p99 1284). Those tickers do not file quarterly — trusts, funds,
        foreign issuers, dormant shells — so a flat 7-day retry would re-ask about
        2100 hopeless CIKs every week, consume the whole budget in steady state and
        starve the tickers a refetch would actually help.

        The interval therefore scales with how old the data is: re-ask roughly ten
        times over the life of the staleness. At the 150-day threshold that is 15
        days, which uses an eighth of the 120-day margin before the TTM gate."""
        old = TODAY - dt.timedelta(days=400)  # scaled interval = 40 days
        self._write(CIK_A, old, written=TODAY - dt.timedelta(days=39))
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_not_called()

        self.client.reset_mock()
        self._write(CIK_A, old, written=TODAY - dt.timedelta(days=41))
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_an_empty_table_backs_off_instead_of_being_re_asked_every_week(self) -> None:
        """An empty table is a valid SEC answer ("no XBRL facts for this CIK"), not a
        broken file, so it must not be retried like one. 18 of the 5992 cached tables
        are empty (measured 2026-09-30). It has no period to age, so the scaled rule
        has nothing to scale and it gets its own long interval."""
        path = self.dir / f"{CIK_A}.parquet"

        def seed(waited_days: int) -> None:
            pq.write_table(_table(TODAY).slice(0, 0), path)
            stamp = dt.datetime.combine(
                TODAY - dt.timedelta(days=waited_days), dt.time(12)
            ).timestamp()
            os.utime(path, (stamp, stamp))

        # Literal days, NOT the constant: a test that derives its own fixture from
        # the constant it pins moves with it, and cannot fail when it changes. That
        # is how a mutation setting this interval to 0 first survived.
        seed(89)
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_not_called()

        self.client.reset_mock()
        seed(91)
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)
        self.assertEqual(
            ef.REFETCH_EMPTY_INTERVAL_DAYS,
            90,
            "the two literals above bracket this value; move them together",
        )

    def test_the_budget_goes_to_the_tickers_most_likely_to_have_a_new_filing(self) -> None:
        """With more stale tickers than budget, spending it in ticker order would hand
        it to whoever sorts first — including CIKs whose newest filing is from 2014 and
        will never advance. Freshest-stale first: those are the ones a refetch helps."""
        self._write(CIK_A, TODAY - dt.timedelta(days=4000), written=TODAY - dt.timedelta(days=900))
        self._write(CIK_B, TODAY - dt.timedelta(days=160), written=TODAY - dt.timedelta(days=90))
        self._store().preload(["AAA", "BBB"], today=TODAY, refresh_budget=1)
        self.client.fetch_company_facts.assert_called_once_with(CIK_B)

    def test_the_budget_bounds_how_many_stale_tickers_one_call_refreshes(self) -> None:
        """Measured 2026-09-30: a refetch costs 0.36s and 4.3 MB, and a single run's
        universe held 1630 stale tickers. Unbounded, the first run pays ~10 min and
        ~7 GB inside a build that was killed by its own timeout eight days earlier."""
        self._write(CIK_A, TODAY - dt.timedelta(days=400), written=TODAY - dt.timedelta(days=90))
        self._write(CIK_B, TODAY - dt.timedelta(days=400), written=TODAY - dt.timedelta(days=90))
        store = self._store()
        store.preload(["AAA", "BBB"], today=TODAY, refresh_budget=1)
        self.assertEqual(self.client.fetch_company_facts.call_count, 1)

    def test_a_missing_parquet_is_fetched_even_when_the_budget_is_zero(self) -> None:
        """The budget throttles refreshing a usable cache. It must never starve the
        fetch that is the difference between some data and none."""
        self._store().preload(["AAA"], today=TODAY, refresh_budget=0)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    # --- never leave the store worse than it was --------------------------

    def test_a_failed_refetch_keeps_the_cached_table(self) -> None:
        """Best-effort: a SEC hiccup must not turn a stale answer into no answer."""
        path = self._write(CIK_A, dt.date(2025, 3, 31))
        self.client.fetch_company_facts.side_effect = RuntimeError("SEC 503")
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)
        self.assertEqual(
            sorted(q.name for q in self.dir.iterdir()),
            [f"{CIK_A}.parquet"],
            "a failed write must not leave a temp file behind",
        )
        newest = max(pq.read_table(path).column("period_end").to_pylist())
        self.assertEqual(newest, dt.date(2025, 3, 31))

    def test_a_failed_write_leaves_no_temp_file_and_keeps_the_cached_table(self) -> None:
        """The other half of the previous test, which only exercises a failed FETCH.
        A write that dies midway must not leave a ``.parquet.tmp`` sibling: the
        store directory is scanned by CIK filename, and a growing pile of temp
        files is the kind of litter nobody notices until the disk fills."""
        path = self._write(CIK_A, dt.date(2025, 3, 31))
        with unittest.mock.patch.object(ef.pq, "write_table", side_effect=OSError("disk full")):
            self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)
        self.assertEqual(
            sorted(q.name for q in self.dir.iterdir()),
            [f"{CIK_A}.parquet"],
            "a write that raised must clean up its temp file",
        )
        newest = max(pq.read_table(path).column("period_end").to_pylist())
        self.assertEqual(newest, dt.date(2025, 3, 31))

    def test_an_unreadable_cached_table_is_fetched_even_when_the_budget_is_zero(self) -> None:
        """An unreadable file is not a stale answer, it is NO answer — the same
        position as a missing file, which is never budgeted. Routing it through the
        stale path made it compete for the budget AND sort last (its data age is
        unknown), so on a large universe it could wait indefinitely while the store
        held a file nothing can read. Found by review, confirmed by running it."""
        (self.dir / f"{CIK_A}.parquet").write_bytes(b"not a parquet")
        self._store().preload(["AAA"], today=TODAY, refresh_budget=0)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_an_unreadable_cached_table_is_refetched(self) -> None:
        """A torn file is a state this store can now reach, because preload has
        started rewriting files other readers hold open."""
        path = self.dir / f"{CIK_A}.parquet"
        path.write_bytes(b"not a parquet")
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_a_fresh_non_chain_usgaap_row_does_not_count(self) -> None:
        """The mask is an AND over taxonomy and concept. Loosen it to an OR and
        every us-gaap row counts again, which is candidate B's measured defect:
        6 of 534 sampled CIKs have a cash-flow chain older than their newest
        us-gaap duration row, the worst by 549 days."""
        self._write_rows(
            CIK_A,
            [
                _row("Revenues", TODAY - dt.timedelta(days=5)),
                _row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=365)),
            ],
        )
        self.assertEqual(self._store()._data_age_days(CIK_A, TODAY), 365)

    def test_only_the_usgaap_taxonomy_counts(self) -> None:
        """The taxonomy half of the mask is what keeps the dei cover page out, and
        the cover page is what caused this defect. Nothing else pins it."""
        self._write_rows(
            CIK_A,
            [
                _row(
                    chains.OPERATING_CASH_FLOW[0],
                    TODAY - dt.timedelta(days=5),
                    taxonomy="dei",
                    unit="shares",
                ),
                _row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=400)),
            ],
        )
        self.assertEqual(self._store()._data_age_days(CIK_A, TODAY), 400)

    def test_the_age_reads_the_newest_chain_period_not_the_oldest(self) -> None:
        """Swapping max for min reads the OLDEST period. Every fixture with a single
        past-dated chain row agrees with both, so only two of them can tell."""
        self._write_rows(
            CIK_A,
            [
                _row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=30)),
                _row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=800)),
            ],
        )
        self.assertEqual(self._store()._data_age_days(CIK_A, TODAY), 30)

    def test_a_chain_period_ending_today_is_age_zero_not_no_answer(self) -> None:
        """The future filter is `<=`, not `<`. Tightened to `<`, an issuer that filed
        a period ending today drops onto the 90-day no-answer branch instead of
        reading as the freshest thing in the store."""
        self._write_rows(CIK_A, [_row(chains.OPERATING_CASH_FLOW[0], TODAY)])
        self.assertEqual(self._store()._data_age_days(CIK_A, TODAY), 0)

    def test_a_table_with_no_chain_row_falls_back_to_its_newest_usgaap_period(self) -> None:
        """Measured 2026-09-30: 5 of the 65 sampled CIKs that hold no cash-flow or
        capex row at all still serve a column today (cash, short-term debt, equity,
        shares outstanding). Dropping them straight onto the 90-day branch slowed
        those from a 15-54 day cadence, which is a regression this change introduced
        and this tier removes. The dei cover page stays excluded."""
        self._write_rows(
            CIK_A,
            [
                _row(COVER_PAGE, TODAY - dt.timedelta(days=2), taxonomy="dei", unit="shares"),
                _row(
                    "CashAndCashEquivalentsAtCarryingValue",
                    TODAY - dt.timedelta(days=200),
                    instant=True,
                ),
            ],
        )
        self.assertEqual(self._store()._data_age_days(CIK_A, TODAY), 200)

    def test_a_table_with_neither_a_chain_row_nor_a_usgaap_row_has_no_age(self) -> None:
        """The fallback is us-gaap only. A file holding nothing but a cover page has
        no age at all and belongs on the long branch."""
        self._write_rows(
            CIK_A,
            [_row(COVER_PAGE, TODAY - dt.timedelta(days=2), taxonomy="dei", unit="shares")],
        )
        self.assertIsNone(self._store()._data_age_days(CIK_A, TODAY))

    def test_the_retry_interval_has_a_ceiling_as_well_as_a_floor(self) -> None:
        """Scoping the age to the FCFF chains made ages larger and therefore
        intervals longer — correctly, because the old measure was reading a fresh
        cover page. But `age // 10` on a 5470-day-old table is a 547-day wait, and
        a dormant issuer that resumes filing would go unnoticed for that long.

        Measured 2026-09-30 over the store: a 90-day ceiling costs 2 extra
        refetches a day out of a 600/day budget (233 -> 235) and cuts the worst
        wait from 547 days to 90."""
        self._write_rows(
            CIK_A,
            [_row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=5470))],
            written=TODAY - dt.timedelta(days=91),
        )
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_called_once_with(CIK_A)

    def test_below_the_ceiling_the_interval_still_scales(self) -> None:
        """A control for the test above. Without it, a ceiling of 7 would pass:
        everything would refetch and the scaling would be gone."""
        self._write_rows(
            CIK_A,
            [_row(chains.OPERATING_CASH_FLOW[0], TODAY - dt.timedelta(days=5470))],
            written=TODAY - dt.timedelta(days=89),
        )
        self._store().preload(["AAA"], today=TODAY)
        self.client.fetch_company_facts.assert_not_called()

    def test_the_ceiling_and_the_no_answer_interval_agree(self) -> None:
        """They answer the same operational question — we hold nothing useful for
        this ticker and asking more often does not change that — so they are the
        same number on purpose rather than by accident. If you mean them to differ,
        change this assertion and write down why."""
        self.assertEqual(ef.REFETCH_MAX_INTERVAL_DAYS, ef.REFETCH_EMPTY_INTERVAL_DAYS)

    # --- the two constants must not drift apart ---------------------------

    def test_the_refetch_age_stays_below_the_ttm_staleness_gate(self) -> None:
        """If the refresh age ever reaches the gate, a cache can go dark before it
        refreshes — which is exactly the 2026-09-28 failure this change answers."""
        self.assertLess(
            ef.REFETCH_DATA_AGE_DAYS,
            DEFAULT_TTM_MAX_STALENESS_DAYS,
            "the refetch age must leave room before compute_ttm starts refusing "
            f"({DEFAULT_TTM_MAX_STALENESS_DAYS}d); otherwise a ticker is silenced "
            "before anything tries to refresh it",
        )

    def test_the_refetch_age_clears_a_healthy_quarterly_filer(self) -> None:
        """An issuer's newest period end is legitimately old between filings: a
        quarter ending 2026-03-31 stays the newest until the next 10-Q lands around
        2026-08-09, which is 131 days. A threshold below that would refetch every
        healthy ticker on every run."""
        self.assertGreater(ef.REFETCH_DATA_AGE_DAYS, 135)


if __name__ == "__main__":
    unittest.main()
