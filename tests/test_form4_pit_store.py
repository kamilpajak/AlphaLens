"""Form4PITStore — point-in-time Form-4 record store with delisting exclusion.

TDD harness. Reads hive-partitioned parquet (transaction_year partition) and
exposes two query patterns:
  - records_as_of(ticker, asof, lookback_days): records used by signal scorer
    (filtered by issuer_cik resolved from ticker).
  - records_for_person(person_cik, classification_year): records used by
    Cohen-Malloy classifier (filtered by reporting_owner_cik over [Y-3, Y)).

Both apply 180d fire-sale exclusion via DelistingEvent (per PIT audit F4).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from alphalens.data.store.form4_pit import FORM4_SCHEMA_COLUMNS, Form4PITStore
from alphalens.data.store.survivorship_pit import DelistingEvent


def _records_to_table(records: list[dict]) -> pa.Table:
    """Convert dict records to a pyarrow Table matching FORM4_SCHEMA_COLUMNS."""
    df = pd.DataFrame.from_records(records, columns=FORM4_SCHEMA_COLUMNS)
    # Ensure types — date columns must be pyarrow date32.
    for col in ("filed_date", "transaction_date"):
        df[col] = pd.to_datetime(df[col]).dt.date
    return pa.Table.from_pandas(df, preserve_index=False)


def _write_partition(root: Path, year: int, records: list[dict]) -> None:
    part_dir = root / f"transaction_year={year}"
    part_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(_records_to_table(records), part_dir / "part-0000.parquet")


def _record(
    *,
    issuer_cik: str = "0000000001",
    ticker: str = "TEST",
    accession_number: str = "0000000001-00-000001",
    filed_date: date,
    reporting_owner_cik: str = "0000000100",
    reporting_owner_name: str = "Doe, John",
    transaction_date: date,
    transaction_code: str = "P",
    transaction_shares: float = 1000.0,
    transaction_price_per_share: float | None = 50.0,
    is_director: bool = False,
    is_officer: bool = True,
    is_ten_percent_owner: bool = False,
    acquired_disposed: str = "A",
    is_amendment: bool = False,
) -> dict:
    return {
        "issuer_cik": issuer_cik,
        "ticker": ticker.upper(),
        "accession_number": accession_number,
        "filed_date": filed_date,
        "reporting_owner_cik": reporting_owner_cik,
        "reporting_owner_name": reporting_owner_name,
        "transaction_date": transaction_date,
        "transaction_code": transaction_code,
        "transaction_shares": transaction_shares,
        "transaction_price_per_share": transaction_price_per_share,
        "is_director": is_director,
        "is_officer": is_officer,
        "is_ten_percent_owner": is_ten_percent_owner,
        "acquired_disposed": acquired_disposed,
        "is_amendment": is_amendment,
    }


class _StaticCikResolver:
    def __init__(self, mapping: dict[str, str]):
        self._mapping = {k.upper(): v for k, v in mapping.items()}

    def lookup(self, ticker: str) -> str | None:
        return self._mapping.get(ticker.upper())


class TestForm4PITStoreRecordsAsOf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.resolver = _StaticCikResolver({"AAPL": "0000320193", "MSFT": "0000789019"})

    def tearDown(self):
        self.tmp.cleanup()

    def _make_store(self, **kwargs) -> Form4PITStore:
        return Form4PITStore(parquet_root=self.root, ticker_cik_resolver=self.resolver, **kwargs)

    def test_returns_records_with_filed_date_at_or_before_asof(self):
        records_2022 = [
            _record(
                issuer_cik="0000320193",
                ticker="AAPL",
                accession_number="A1",
                filed_date=date(2022, 6, 1),
                transaction_date=date(2022, 5, 15),
            ),
            _record(
                issuer_cik="0000320193",
                ticker="AAPL",
                accession_number="A2",
                filed_date=date(2022, 12, 1),  # AFTER asof
                transaction_date=date(2022, 11, 1),
            ),
        ]
        _write_partition(self.root, 2022, records_2022)

        store = self._make_store()
        result = store.records_as_of("AAPL", asof=date(2022, 7, 1), lookback_days=180)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["accession_number"], "A1")

    def test_excludes_transactions_older_than_lookback(self):
        records = [
            _record(
                issuer_cik="0000320193",
                ticker="AAPL",
                accession_number="A1",
                filed_date=date(2022, 1, 5),
                transaction_date=date(2021, 12, 15),  # ~6 months before asof=2022-06-01
            ),
            _record(
                issuer_cik="0000320193",
                ticker="AAPL",
                accession_number="A2",
                filed_date=date(2022, 1, 5),
                transaction_date=date(2021, 1, 15),  # 16 months before asof — too old
            ),
        ]
        _write_partition(self.root, 2021, records)

        store = self._make_store()
        result = store.records_as_of("AAPL", asof=date(2022, 6, 1), lookback_days=180)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["accession_number"], "A1")

    def test_unknown_ticker_returns_empty(self):
        records = [_record(filed_date=date(2022, 1, 1), transaction_date=date(2022, 1, 1))]
        _write_partition(self.root, 2022, records)

        store = self._make_store()
        result = store.records_as_of("UNKNOWN", asof=date(2022, 7, 1))
        self.assertEqual(len(result), 0)

    def test_delisting_within_180d_of_asof_returns_empty(self):
        # AAPL "delisted" 2022-09-01. asof=2022-07-01 means delisted within 180d → exclude.
        records = [
            _record(
                issuer_cik="0000320193",
                ticker="AAPL",
                accession_number="A1",
                filed_date=date(2022, 6, 1),
                transaction_date=date(2022, 5, 15),
            ),
        ]
        _write_partition(self.root, 2022, records)

        store = self._make_store(
            delisting_events=[
                DelistingEvent(ticker="AAPL", delisted_date=date(2022, 9, 1), reason="bankruptcy")
            ]
        )
        result = store.records_as_of("AAPL", asof=date(2022, 7, 1), lookback_days=180)
        self.assertEqual(len(result), 0)

    def test_delisting_far_future_does_not_exclude(self):
        records = [
            _record(
                issuer_cik="0000320193",
                ticker="AAPL",
                accession_number="A1",
                filed_date=date(2018, 6, 1),
                transaction_date=date(2018, 5, 15),
            ),
        ]
        _write_partition(self.root, 2018, records)

        store = self._make_store(
            delisting_events=[
                DelistingEvent(ticker="AAPL", delisted_date=date(2022, 9, 1), reason="merger")
            ]
        )
        # asof=2018-07-01, delisted 2022-09 → ~4 years out, well beyond 180d
        result = store.records_as_of("AAPL", asof=date(2018, 7, 1), lookback_days=180)
        self.assertEqual(len(result), 1)

    def test_reads_across_multiple_year_partitions(self):
        # Lookback spans Dec 2021 → Jun 2022. Records in both partitions.
        _write_partition(
            self.root,
            2021,
            [
                _record(
                    issuer_cik="0000320193",
                    ticker="AAPL",
                    accession_number="A1",
                    filed_date=date(2022, 1, 5),
                    transaction_date=date(2021, 12, 15),
                ),
            ],
        )
        _write_partition(
            self.root,
            2022,
            [
                _record(
                    issuer_cik="0000320193",
                    ticker="AAPL",
                    accession_number="A2",
                    filed_date=date(2022, 4, 1),
                    transaction_date=date(2022, 3, 1),
                ),
            ],
        )

        store = self._make_store()
        result = store.records_as_of("AAPL", asof=date(2022, 6, 1), lookback_days=180)
        self.assertEqual(len(result), 2)
        self.assertEqual(set(result["accession_number"]), {"A1", "A2"})


class TestForm4PITStoreRecordsForPerson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_only_records_for_target_person_in_year_window(self):
        records_2020 = [
            _record(
                accession_number="P1-2020",
                reporting_owner_cik="0000001000",
                filed_date=date(2020, 5, 5),
                transaction_date=date(2020, 5, 1),
            ),
            _record(
                accession_number="OTHER-2020",
                reporting_owner_cik="0000002000",  # different person
                filed_date=date(2020, 5, 5),
                transaction_date=date(2020, 5, 1),
            ),
        ]
        records_2021 = [
            _record(
                accession_number="P1-2021",
                reporting_owner_cik="0000001000",
                filed_date=date(2021, 5, 5),
                transaction_date=date(2021, 5, 1),
            ),
        ]
        records_2022 = [
            _record(
                accession_number="P1-2022",
                reporting_owner_cik="0000001000",
                filed_date=date(2022, 5, 5),
                transaction_date=date(2022, 5, 1),
            ),
        ]
        records_2023 = [
            _record(
                accession_number="P1-2023",
                reporting_owner_cik="0000001000",
                filed_date=date(2023, 5, 5),
                transaction_date=date(2023, 5, 1),
            ),
        ]
        _write_partition(self.root, 2020, records_2020)
        _write_partition(self.root, 2021, records_2021)
        _write_partition(self.root, 2022, records_2022)
        _write_partition(self.root, 2023, records_2023)

        store = Form4PITStore(parquet_root=self.root)
        # year_y=2023 → window [2020, 2023). Year 2023 record excluded.
        result = store.records_for_person(person_cik="0000001000", classification_year=2023)
        accessions = set(result["accession_number"])
        self.assertEqual(accessions, {"P1-2020", "P1-2021", "P1-2022"})

    def test_pre_window_records_excluded(self):
        _write_partition(
            self.root,
            2017,
            [
                _record(
                    accession_number="OLD",
                    reporting_owner_cik="0000001000",
                    filed_date=date(2017, 1, 1),
                    transaction_date=date(2017, 1, 1),
                ),
            ],
        )
        _write_partition(
            self.root,
            2020,
            [
                _record(
                    accession_number="P1-2020",
                    reporting_owner_cik="0000001000",
                    filed_date=date(2020, 5, 5),
                    transaction_date=date(2020, 5, 1),
                ),
            ],
        )

        store = Form4PITStore(parquet_root=self.root)
        result = store.records_for_person(person_cik="0000001000", classification_year=2023)
        # Only 2020-2022 records expected; 2017 partition pre-window
        self.assertEqual(set(result["accession_number"]), {"P1-2020"})

    def test_empty_when_no_partitions_exist(self):
        store = Form4PITStore(parquet_root=self.root)
        result = store.records_for_person(person_cik="0000001000", classification_year=2023)
        self.assertEqual(len(result), 0)


class TestForm4PITStoreSchema(unittest.TestCase):
    def test_schema_columns_match_record_writer_contract(self):
        # Lock the schema so the bulk-backfill writer cannot diverge silently.
        expected = {
            "issuer_cik",
            "ticker",
            "accession_number",
            "filed_date",
            "reporting_owner_cik",
            "reporting_owner_name",
            "transaction_date",
            "transaction_code",
            "transaction_shares",
            "transaction_price_per_share",
            "is_director",
            "is_officer",
            "is_ten_percent_owner",
            "acquired_disposed",
            "is_amendment",
        }
        self.assertEqual(set(FORM4_SCHEMA_COLUMNS), expected)


if __name__ == "__main__":
    unittest.main()
