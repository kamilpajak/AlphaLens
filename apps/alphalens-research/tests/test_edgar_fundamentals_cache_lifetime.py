"""The companyfacts cache has a lifetime — #1335.

``preload`` used to fetch a CIK's companyfacts parquet only when the file was
absent, and never again. Measured on the production store 2026-09-30: 4784 of
5992 cached files were written in 2026-05 and never touched, and for 2612 of
them the newest cached quarter ends 2025-12-31. ``compute_ttm`` refuses a
result whose freshest input ends before ``asof - 270 days``, so on 2026-09-28
all 2612 stopped producing a TTM at once and every EDGAR-derived column went
blank for them. A further 1843 files hold a newest quarter ending 2026-03-31
and cross the same gate on 2026-12-27.

These tests pin the refresh rule, the two floors that bound its cost, and the
relationship between the refresh age and the TTM gate it exists to stay ahead of.
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
from alphalens_pipeline.data.fundamentals.ttm_aggregator import DEFAULT_TTM_MAX_STALENESS_DAYS
from alphalens_pipeline.data.store import edgar_fundamentals as ef
from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

TODAY = dt.date(2026, 9, 30)
CIK_A = "0000000001"
CIK_B = "0000000002"


def _table(newest_period: dt.date, *, val: float = 100.0) -> pa.Table:
    """One-row companyfacts table whose newest reported period is ``newest_period``."""
    return pa.Table.from_pylist(
        [
            {
                "taxonomy": "us-gaap",
                "concept": "NetCashProvidedByUsedInOperatingActivities",
                "unit": "USD",
                "period_start": newest_period - dt.timedelta(days=90),
                "period_end": newest_period,
                "val": val,
                "accn": "x",
                "fy": 2026,
                "fp": "Q1",
                "form": "10-Q",
                "filed_date": newest_period + dt.timedelta(days=40),
                "frame": None,
            }
        ],
        schema=ef.SCHEMA if hasattr(ef, "SCHEMA") else None,
    )


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
        path = self.dir / f"{cik}.parquet"
        pq.write_table(_table(newest), path)
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
