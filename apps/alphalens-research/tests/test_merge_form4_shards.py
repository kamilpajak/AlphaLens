"""Form-4 cross-shard merge — TDD.

The Form-4 backfill is split across N CIK shards (Mac + VPS round-robin).
After each shard completes, we have N hive-partitioned parquet trees that
must be merged into one canonical store before downstream Phase B
experiments can read it.

The merge is intentionally **dumb and strict**:

1. Only ``transaction_year=YYYY`` partitions where ``YYYY`` is exactly
   4 digits are treated as canonical. Source canonicals are copied into
   matching target partitions; filenames are uniquified to avoid
   collisions when both shards already ran a within-tree compact.
2. Any ``transaction_year=*`` directory whose label is not 4 digits is
   considered **malformed** (e.g. ``=22`` from a 2-digit ``transaction_date``
   parse bug, ``=15`` from the same bug on a different shard). Malformed
   partitions are quarantined to an out-of-tree directory if
   ``quarantine_dir`` is given, otherwise dropped. **Canonicalising them
   to a 4-digit name is intentionally not supported**: it preserves the
   directory layout while leaving the underlying ``transaction_date``
   column malformed, which produces silently incorrect results when a
   query engine uses Parquet predicate pushdown on the partition column
   versus the date column. See zen review 2026-05-08.
3. Pre-2003 4-digit partitions (``=1992``, ``=1995``) pass through as
   canonical — they hold legitimate retroactive Form-4 amendments.
   Post-SOX-only filtering belongs in the read/analysis layer, not here.
4. The TARGET tree is also swept for malformed orphans before the copy
   begins (Mac shard discovered an orphan ``=15`` partition from the
   same upstream parser bug). This keeps the post-merge tree strictly
   4-digit.
5. Source tree is left intact; caller controls cleanup of the extracted
   tar contents. Target should be re-compacted after merge via
   ``scripts/compact_form4_parquet.py``.

Sharding is round-robin by CIK (see ``scripts/split_cik_list.py``), so
shards are CIK-disjoint and therefore accession-disjoint — no genuine
cross-shard duplicates exist. Within-shard flush/resume duplicates are
already collapsed by per-shard compaction before archive.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow.dataset as ds
from alphalens_research.data.alt_data.form4_bulk_backfill import write_records_to_parquet
from alphalens_research.data.alt_data.form4_records import Form4Record


def _mk_record(*, transaction_date: date, accession: str, cik: str = "0000000001") -> Form4Record:
    return Form4Record(
        issuer_cik=cik,
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
        footnotes=(),
    )


def _seed_malformed_partition(host_root: Path, partition_name: str, accession: str) -> Path:
    """Create a malformed partition by writing real parquet to a temp tree
    then renaming the resulting canonical partition to ``partition_name``.

    Mirrors the upstream-parser-bug failure mode: the partition directory
    has a non-4-digit name, but the parquet files inside are structurally
    valid (they just have a malformed ``transaction_date`` column).
    """
    seed = host_root.parent / f"_seed_{partition_name}"
    seed.mkdir(exist_ok=True)
    write_records_to_parquet(
        [_mk_record(transaction_date=date(2099, 6, 1), accession=accession)],
        parquet_root=seed,
    )
    src = seed / "transaction_year=2099"
    dst = host_root / partition_name
    src.rename(dst)
    return dst


class TestMergeShards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "src"
        self.target = self.root / "tgt"
        self.source.mkdir()
        self.target.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_merges_disjoint_year_partitions(self):
        from scripts.merge_form4_shards import merge_shards

        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="A-2020")],
            parquet_root=self.source,
        )
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2021, 6, 1), accession="A-2021")],
            parquet_root=self.source,
        )
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 6, 1), accession="A-2022")],
            parquet_root=self.target,
        )

        stats = merge_shards(self.source, self.target)

        self.assertEqual(stats["partitions_merged"], 2)
        self.assertEqual(stats["files_copied"], 2)
        self.assertEqual(stats["source_malformed_partitions"], 0)
        self.assertEqual(stats["target_malformed_partitions"], 0)

        df = (
            ds.dataset(str(self.target), partitioning="hive", format="parquet")
            .to_table()
            .to_pandas()
        )
        self.assertEqual(set(df["accession_number"]), {"A-2020", "A-2021", "A-2022"})

    def test_merges_overlapping_partition_files_coexist(self):
        from scripts.merge_form4_shards import merge_shards

        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="SRC", cik="0000000001")],
            parquet_root=self.source,
        )
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 2), accession="TGT", cik="0000000002")],
            parquet_root=self.target,
        )

        merge_shards(self.source, self.target)

        files = list((self.target / "transaction_year=2020").glob("*.parquet"))
        self.assertEqual(len(files), 2)

        df = (
            ds.dataset(str(self.target / "transaction_year=2020"), format="parquet")
            .to_table()
            .to_pandas()
        )
        self.assertEqual(set(df["accession_number"]), {"SRC", "TGT"})

    def test_source_malformed_partitions_dropped_by_default(self):
        """Source has =22 (2-digit bug) — drop it without quarantine_dir."""
        from scripts.merge_form4_shards import merge_shards

        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="GOOD")],
            parquet_root=self.source,
        )
        _seed_malformed_partition(self.source, "transaction_year=22", "BAD")

        stats = merge_shards(self.source, self.target)

        self.assertEqual(stats["partitions_merged"], 1)
        self.assertEqual(stats["files_copied"], 1)
        self.assertEqual(stats["source_malformed_partitions"], 1)
        self.assertEqual(stats["files_quarantined"], 0)

        # Target has no malformed partition AND no canonical-relabeled =2022.
        self.assertFalse((self.target / "transaction_year=22").exists())
        self.assertFalse((self.target / "transaction_year=2022").exists())
        self.assertTrue((self.target / "transaction_year=2020").is_dir())

    def test_target_malformed_orphans_dropped_before_merge(self):
        """Target tree (Mac shard) has its own =15 orphan from the same bug.
        Merge must clean it before copying source files in."""
        from scripts.merge_form4_shards import merge_shards

        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="GOOD")],
            parquet_root=self.target,
        )
        _seed_malformed_partition(self.target, "transaction_year=15", "ORPHAN")

        stats = merge_shards(self.source, self.target)

        self.assertEqual(stats["target_malformed_partitions"], 1)
        self.assertEqual(stats["source_malformed_partitions"], 0)
        self.assertFalse((self.target / "transaction_year=15").exists())
        self.assertTrue((self.target / "transaction_year=2020").is_dir())

    def test_malformed_partitions_quarantined_when_dir_given(self):
        """With quarantine_dir, malformed partitions move there for forensics
        instead of being deleted. Files preserved with their original
        partition layout."""
        from scripts.merge_form4_shards import merge_shards

        quarantine = self.root / "quarantine"

        # Source: malformed =22; target: malformed =15.
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="GOOD-SRC")],
            parquet_root=self.source,
        )
        _seed_malformed_partition(self.source, "transaction_year=22", "BAD-SRC")
        _seed_malformed_partition(self.target, "transaction_year=15", "BAD-TGT")

        stats = merge_shards(self.source, self.target, quarantine_dir=quarantine)

        self.assertEqual(stats["source_malformed_partitions"], 1)
        self.assertEqual(stats["target_malformed_partitions"], 1)
        self.assertEqual(stats["files_quarantined"], 2)

        # Quarantine preserves partition structure for audit trail.
        self.assertTrue((quarantine / "transaction_year=22").is_dir())
        self.assertTrue((quarantine / "transaction_year=15").is_dir())
        self.assertEqual(len(list((quarantine / "transaction_year=22").glob("*.parquet"))), 1)
        self.assertEqual(len(list((quarantine / "transaction_year=15").glob("*.parquet"))), 1)

        # Target still clean.
        self.assertFalse((self.target / "transaction_year=22").exists())
        self.assertFalse((self.target / "transaction_year=15").exists())

    def test_canonical_pre_2003_partitions_pass_through(self):
        """=1992, =1995 are 4-digit canonical — legitimate retroactive
        Form-4 amendments. Don't filter them at merge time."""
        from scripts.merge_form4_shards import merge_shards

        write_records_to_parquet(
            [_mk_record(transaction_date=date(1992, 11, 13), accession="OLD-1")],
            parquet_root=self.source,
        )
        write_records_to_parquet(
            [_mk_record(transaction_date=date(1995, 7, 14), accession="OLD-2")],
            parquet_root=self.source,
        )

        stats = merge_shards(self.source, self.target)

        self.assertEqual(stats["partitions_merged"], 2)
        self.assertEqual(stats["source_malformed_partitions"], 0)
        self.assertTrue((self.target / "transaction_year=1992").is_dir())
        self.assertTrue((self.target / "transaction_year=1995").is_dir())

    def test_various_malformed_partition_widths_dropped(self):
        """1-digit, 2-digit, 3-digit, 5-digit years all malformed —
        any non-4-digit width."""
        from scripts.merge_form4_shards import merge_shards

        for name, acc in [
            ("transaction_year=5", "DIG1"),
            ("transaction_year=22", "DIG2"),
            ("transaction_year=999", "DIG3"),
            ("transaction_year=20200", "DIG5"),
        ]:
            _seed_malformed_partition(self.source, name, acc)

        # Plus one canonical so the merge has a happy path too.
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="GOOD")],
            parquet_root=self.source,
        )

        stats = merge_shards(self.source, self.target)

        self.assertEqual(stats["partitions_merged"], 1)
        self.assertEqual(stats["source_malformed_partitions"], 4)
        for name in ("=5", "=22", "=999", "=20200"):
            self.assertFalse((self.target / f"transaction_year{name}").exists())

    def test_filenames_uniquified_to_avoid_collision(self):
        from scripts.merge_form4_shards import merge_shards

        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="SRC")],
            parquet_root=self.source,
        )
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 2), accession="TGT")],
            parquet_root=self.target,
        )
        src_part = self.source / "transaction_year=2020"
        tgt_part = self.target / "transaction_year=2020"
        next(src_part.glob("part-*.parquet")).rename(src_part / "compacted.parquet")
        next(tgt_part.glob("part-*.parquet")).rename(tgt_part / "compacted.parquet")

        merge_shards(self.source, self.target)

        files = sorted(f.name for f in tgt_part.glob("*.parquet"))
        self.assertEqual(len(files), 2)
        self.assertIn("compacted.parquet", files)
        self.assertEqual(len([n for n in files if n != "compacted.parquet"]), 1)

    def test_source_tree_left_intact(self):
        from scripts.merge_form4_shards import merge_shards

        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="A")],
            parquet_root=self.source,
        )

        merge_shards(self.source, self.target)

        src_files = list((self.source / "transaction_year=2020").glob("*.parquet"))
        self.assertEqual(len(src_files), 1)

    def test_non_partition_dirs_in_source_ignored(self):
        """Stray non-`transaction_year=*` entries are silently skipped —
        they're not malformed partitions, just unrelated files."""
        from scripts.merge_form4_shards import merge_shards

        write_records_to_parquet(
            [_mk_record(transaction_date=date(2020, 6, 1), accession="A")],
            parquet_root=self.source,
        )
        (self.source / ".ipynb_checkpoints").mkdir()
        (self.source / "_SUCCESS").write_text("")
        (self.source / "manifest.json").write_text("{}")

        stats = merge_shards(self.source, self.target)

        self.assertEqual(stats["partitions_merged"], 1)
        self.assertEqual(stats["source_malformed_partitions"], 0)
        self.assertEqual(
            sorted(p.name for p in self.target.iterdir()),
            ["transaction_year=2020"],
        )

    def test_end_to_end_merge_then_compact_yields_unique_rows(self):
        """Integration: malformed source/target dropped, canonical merged,
        compact dedupes — final tree has only canonical partitions."""
        from scripts.compact_form4_parquet import compact_root
        from scripts.merge_form4_shards import merge_shards

        for year in (2020, 2021, 2022):
            for i in range(2):
                write_records_to_parquet(
                    [_mk_record(transaction_date=date(year, 6, i + 1), accession=f"S-{year}-{i}")],
                    parquet_root=self.source,
                )
        for year in (2020, 2021, 2022):
            for i in range(2):
                write_records_to_parquet(
                    [
                        _mk_record(
                            transaction_date=date(year, 7, i + 1),
                            accession=f"T-{year}-{i}",
                            cik="0000000002",
                        )
                    ],
                    parquet_root=self.target,
                )
        # Add a malformed partition on each side.
        _seed_malformed_partition(self.source, "transaction_year=22", "BAD-SRC")
        _seed_malformed_partition(self.target, "transaction_year=15", "BAD-TGT")

        stats = merge_shards(self.source, self.target)
        compact_root(self.target)

        self.assertEqual(stats["source_malformed_partitions"], 1)
        self.assertEqual(stats["target_malformed_partitions"], 1)

        df = (
            ds.dataset(str(self.target), partitioning="hive", format="parquet")
            .to_table()
            .to_pandas()
        )
        self.assertEqual(len(df), 12)  # only canonical rows
        self.assertEqual(len(set(df["accession_number"])), 12)

        # Only canonical partitions remain.
        partitions = sorted(p.name for p in self.target.iterdir() if p.is_dir())
        self.assertEqual(
            partitions,
            ["transaction_year=2020", "transaction_year=2021", "transaction_year=2022"],
        )
        for year in (2020, 2021, 2022):
            files = list((self.target / f"transaction_year={year}").glob("*.parquet"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "compacted.parquet")


if __name__ == "__main__":
    unittest.main()
