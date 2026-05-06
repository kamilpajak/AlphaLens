"""Form-4 parquet compaction — TDD.

After the multi-day backfill, each ``transaction_year=YYYY`` partition can
contain hundreds or thousands of small ``part-*.parquet`` files (one per
flush batch). ``pyarrow.dataset`` open time scales with file count, so
downstream scorer reads pay a 30+ second penalty per query.

Compaction merges all part files in each year-partition into a single file
``compacted.parquet`` and atomically removes the old part files. Idempotent:
a second pass is a no-op.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow.dataset as ds

from alphalens.data.alt_data.form4_bulk_backfill import write_records_to_parquet
from alphalens.data.alt_data.form4_records import Form4Record
from scripts.compact_form4_parquet import compact_partition, compact_root


def _mk_record(*, transaction_date: date, accession: str) -> Form4Record:
    return Form4Record(
        issuer_cik="0000000001",
        ticker="TEST",
        accession_number=accession,
        filing_date=transaction_date,
        reporting_owner_cik="0000000100",
        reporting_owner_name="Doe, John",
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        is_other=False,
        officer_title="VP",
        transaction_date=transaction_date,
        transaction_code="P",
        transaction_shares=Decimal("1000"),
        transaction_price_per_share=Decimal("50"),
        acquired_disposed="A",
        is_amendment=False,
        footnotes=tuple(),
    )


class TestCompactPartition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_merges_multiple_small_files_into_one(self):
        # Simulate 5 separate write_records_to_parquet calls, each landing
        # a small part-*.parquet file in the 2022 partition.
        for i in range(5):
            write_records_to_parquet(
                [
                    _mk_record(
                        transaction_date=date(2022, 1, 1),
                        accession=f"ACC-{i}",
                    )
                ],
                parquet_root=self.root,
            )
        partition = self.root / "transaction_year=2022"
        before_files = list(partition.glob("*.parquet"))
        self.assertEqual(len(before_files), 5)

        compact_partition(partition)

        after_files = list(partition.glob("*.parquet"))
        self.assertEqual(len(after_files), 1)
        self.assertEqual(after_files[0].name, "compacted.parquet")

        # Content preserved: 5 records.
        df = ds.dataset(str(partition), partitioning=None, format="parquet").to_table().to_pandas()
        self.assertEqual(len(df), 5)
        self.assertEqual(
            set(df["accession_number"]),
            {f"ACC-{i}" for i in range(5)},
        )

    def test_idempotent_second_pass_is_noop(self):
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 1, 1), accession="A")],
            parquet_root=self.root,
        )
        partition = self.root / "transaction_year=2022"

        compact_partition(partition)
        first_pass_files = list(partition.glob("*.parquet"))
        first_pass_mtime = first_pass_files[0].stat().st_mtime

        # Second compaction: same single file, untouched.
        compact_partition(partition)
        second_pass_files = list(partition.glob("*.parquet"))
        self.assertEqual(len(second_pass_files), 1)
        self.assertEqual(second_pass_files[0].name, "compacted.parquet")
        self.assertEqual(second_pass_files[0].stat().st_mtime, first_pass_mtime)

    def test_empty_partition_is_skipped(self):
        partition = self.root / "transaction_year=2022"
        partition.mkdir()
        compact_partition(partition)  # no raise
        self.assertEqual(list(partition.iterdir()), [])

    def test_handles_partition_with_compacted_plus_new_parts(self):
        # After an initial compaction, a resumed backfill can land new
        # part-*.parquet files. A re-compaction must merge them with the
        # existing compacted.parquet.
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 1, 1), accession="OLD")],
            parquet_root=self.root,
        )
        partition = self.root / "transaction_year=2022"
        compact_partition(partition)

        # New part files arrive after first compaction.
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 6, 1), accession="NEW-1")],
            parquet_root=self.root,
        )
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 7, 1), accession="NEW-2")],
            parquet_root=self.root,
        )
        files_mid = list(partition.glob("*.parquet"))
        self.assertEqual(len(files_mid), 3)  # compacted + 2 new parts

        compact_partition(partition)

        files_after = list(partition.glob("*.parquet"))
        self.assertEqual(len(files_after), 1)
        df = ds.dataset(str(partition), partitioning=None, format="parquet").to_table().to_pandas()
        self.assertEqual(set(df["accession_number"]), {"OLD", "NEW-1", "NEW-2"})


class TestCompactRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_compacts_every_year_partition(self):
        for year in (2020, 2021, 2022):
            for i in range(3):
                write_records_to_parquet(
                    [
                        _mk_record(
                            transaction_date=date(year, 1, 1),
                            accession=f"ACC-{year}-{i}",
                        )
                    ],
                    parquet_root=self.root,
                )

        for year in (2020, 2021, 2022):
            self.assertEqual(
                len(list((self.root / f"transaction_year={year}").glob("*.parquet"))),
                3,
            )

        compact_root(self.root)

        for year in (2020, 2021, 2022):
            files = list((self.root / f"transaction_year={year}").glob("*.parquet"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "compacted.parquet")

    def test_ignores_non_partition_directories(self):
        # Stray directories (e.g. _SUCCESS marker, .ipynb_checkpoints) must
        # not crash the compactor.
        (self.root / ".ipynb_checkpoints").mkdir()
        (self.root / "_SUCCESS").write_text("")
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 1, 1), accession="A")],
            parquet_root=self.root,
        )
        compact_root(self.root)  # no raise
        self.assertEqual(
            len(list((self.root / "transaction_year=2022").glob("*.parquet"))),
            1,
        )


if __name__ == "__main__":
    unittest.main()
