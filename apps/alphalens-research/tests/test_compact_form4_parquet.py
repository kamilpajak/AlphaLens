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

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from alphalens_pipeline.data.alt_data.form4_bulk_backfill import write_records_to_parquet
from alphalens_pipeline.data.alt_data.form4_records import Form4Record
from scripts.compact_form4_parquet import compact_partition, compact_root


def _mk_record(*, transaction_date: date, accession: str, is_other: bool = False) -> Form4Record:
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
        is_other=is_other,
        officer_title="VP",
        transaction_date=transaction_date,
        transaction_code="P",
        transaction_shares=Decimal("1000"),
        transaction_price_per_share=Decimal("50"),
        acquired_disposed="A",
        is_amendment=False,
        footnotes=(),
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

    def test_dedups_records_when_partial_flush_replayed(self):
        # Crash semantics: _flush_batch writes parquet files first, then
        # marks CIKs complete in manifest. If the run dies between those
        # two steps, the manifest is unchanged and the same CIKs get
        # re-fetched on resume — producing a SECOND parquet file with
        # duplicate accession_numbers (filenames don't collide thanks to
        # timestamp+hex suffix). compact_partition must dedup so the
        # final compacted.parquet doesn't double-count trades.
        rec = _mk_record(transaction_date=date(2022, 5, 1), accession="DUPE")
        write_records_to_parquet([rec], parquet_root=self.root)
        write_records_to_parquet([rec], parquet_root=self.root)  # replayed

        partition = self.root / "transaction_year=2022"
        self.assertEqual(len(list(partition.glob("*.parquet"))), 2)

        compact_partition(partition)

        files = list(partition.glob("*.parquet"))
        self.assertEqual(len(files), 1)
        df = ds.dataset(str(files[0]), format="parquet").to_table().to_pandas()
        # Single row, not two — the duplicate has been collapsed.
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["accession_number"], "DUPE")

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


def _write_legacy_compacted(partition_dir: Path, records: list[Form4Record]) -> None:
    """Write a pre-#82 ``compacted.parquet``: the is_other column is ABSENT.

    Route through the real writer so every other column is byte-identical to
    what a live write produces, then strip the column to reproduce the legacy
    on-disk shape (the VPS seed + every pre-#82 incremental part).
    """
    with tempfile.TemporaryDirectory() as tmp:
        write_records_to_parquet(records, parquet_root=Path(tmp))
        part = next((Path(tmp) / partition_dir.name).glob("part-*.parquet"))
        table = pq.ParquetFile(part).read()
    if "is_other" in table.column_names:
        table = table.drop_columns(["is_other"])
    table = table.cast(_narrow_string_schema(table.schema))
    partition_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, partition_dir / "compacted.parquet")


def _narrow_string_schema(schema: pa.Schema) -> pa.Schema:
    """Return ``schema`` with every ``large_string`` field narrowed to ``string``.

    Reproduces the live store's legacy width. Measured on the VPS 2026-09-05:
    every ``compacted.parquet`` carries ``issuer_cik: string`` while every
    ``part-*.parquet`` the current writer emits carries ``large_string`` — the
    pre-#82 compactor inferred its schema from the first fragment and pyarrow
    cast the wider parts down, so the difference was silently absorbed on
    every cycle and never reached the on-disk compacted file.
    """
    return pa.schema(
        [pa.field(f.name, pa.string()) if f.type == pa.large_string() else f for f in schema]
    )


class TestCompactMixedSchemas(unittest.TestCase):
    """#82: a legacy compacted.parquet (no is_other) + a new part must merge losslessly.

    The naive dataset schema is inferred from the FIRST fragment only, and
    "compacted.parquet" sorts before "part-*", so without schema unification
    the compactor silently projects the new column away and re-pins the old
    15-column schema — permanent data loss on the live store.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.partition = self.root / "transaction_year=2022"

    def tearDown(self):
        self.tmp.cleanup()

    def test_mixed_schema_compaction_preserves_is_other(self):
        _write_legacy_compacted(
            self.partition,
            [_mk_record(transaction_date=date(2022, 1, 1), accession="LEGACY")],
        )
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 2, 1), accession="NEW", is_other=True)],
            parquet_root=self.root,
        )

        compact_partition(self.partition)

        files = list(self.partition.glob("*.parquet"))
        self.assertEqual([f.name for f in files], ["compacted.parquet"])
        df = pq.ParquetFile(files[0]).read().to_pandas().set_index("accession_number")
        self.assertIn("is_other", df.columns)
        # Legacy truth is unknown -> null, never a fabricated False.
        self.assertTrue(pd.isna(df.loc["LEGACY", "is_other"]))
        self.assertEqual(bool(df.loc["NEW", "is_other"]), True)

    def test_mixed_string_width_partition_compacts(self):
        # Live regression 2026-09-05 (#1329): the legacy compacted file carries
        # `string` where the current writer emits `large_string`, so a strict
        # unify_schemas raises ArrowTypeError and the daily ingest dies at
        # compaction. Width is an arrow artifact of one writer, never semantic
        # drift, so it must merge rather than abort the run.
        legacy = _mk_record(transaction_date=date(2022, 1, 1), accession="LEGACY")
        _write_legacy_compacted(self.partition, [legacy])
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 2, 1), accession="NEW")],
            parquet_root=self.root,
        )
        # Guard the premise: the two fragments really do differ in width, so
        # this test cannot rot into a no-op if the writer's types ever change.
        widths = {
            str(pq.read_schema(f).field("issuer_cik").type)
            for f in self.partition.glob("*.parquet")
        }
        self.assertEqual(widths, {"string", "large_string"})

        compact_partition(self.partition)

        df = pq.ParquetFile(self.partition / "compacted.parquet").read().to_pandas()
        self.assertEqual(sorted(df["accession_number"]), ["LEGACY", "NEW"])

    def test_mixed_schema_dedup_prefers_concrete_is_other(self):
        # Catch-up overlap: the SAME logical row exists once in the legacy
        # compacted file (column absent -> null after unification) and once in
        # a post-#82 part (is_other=False). Full-row dedup would keep BOTH
        # (null != False), permanently double-counting the trade in every
        # scorer — the concrete value must supersede the unknown.
        rec = _mk_record(transaction_date=date(2022, 3, 1), accession="OVERLAP")
        _write_legacy_compacted(self.partition, [rec])
        write_records_to_parquet([rec], parquet_root=self.root)

        compact_partition(self.partition)

        df = pq.ParquetFile(self.partition / "compacted.parquet").read().to_pandas()
        self.assertEqual(len(df), 1)
        self.assertEqual(bool(df.iloc[0]["is_other"]), False)


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
