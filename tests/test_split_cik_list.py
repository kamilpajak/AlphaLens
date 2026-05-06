"""CIK list shard splitter — TDD.

Splits a CIK universe into N shards round-robin so multiple machines (each
with a distinct IP, each subject to SEC's per-IP 10 req/s rate cap) can
fetch their slice in parallel. Round-robin distribution interleaves small
and large filers across shards so no machine gets stuck on a long tail of
prolific issuers.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.split_cik_list import (
    _load_cik_list,
    shard_cik_list,
    write_shards,
)


class TestShardCikList(unittest.TestCase):
    def test_two_shards_round_robin(self):
        ciks = ["A", "B", "C", "D", "E"]
        shards = shard_cik_list(ciks, num_shards=2)
        self.assertEqual(len(shards), 2)
        self.assertEqual(shards[0], ["A", "C", "E"])
        self.assertEqual(shards[1], ["B", "D"])

    def test_no_cik_lost_or_duplicated(self):
        ciks = [f"{i:04d}" for i in range(1000)]
        shards = shard_cik_list(ciks, num_shards=5)
        flat = sum(shards, [])
        self.assertEqual(sorted(flat), sorted(ciks))
        # Each CIK appears exactly once.
        self.assertEqual(len(flat), len(set(flat)))

    def test_balance_within_one_per_shard(self):
        # 1000 CIKs / 5 shards = 200 each, perfect balance.
        ciks = [f"{i:04d}" for i in range(1000)]
        shards = shard_cik_list(ciks, num_shards=5)
        sizes = [len(s) for s in shards]
        self.assertTrue(max(sizes) - min(sizes) <= 1)

    def test_unbalanced_counts_distribute_remainder(self):
        # 7 CIKs / 3 shards: shard sizes should be 3, 2, 2.
        ciks = ["A", "B", "C", "D", "E", "F", "G"]
        shards = shard_cik_list(ciks, num_shards=3)
        sizes = [len(s) for s in shards]
        self.assertEqual(sorted(sizes, reverse=True), [3, 2, 2])

    def test_single_shard_returns_full_list(self):
        ciks = ["A", "B", "C"]
        shards = shard_cik_list(ciks, num_shards=1)
        self.assertEqual(shards, [["A", "B", "C"]])

    def test_more_shards_than_ciks_yields_some_empty_shards(self):
        ciks = ["A", "B"]
        shards = shard_cik_list(ciks, num_shards=5)
        self.assertEqual(len(shards), 5)
        flat = sum(shards, [])
        self.assertEqual(sorted(flat), ["A", "B"])

    def test_zero_or_negative_shards_rejected(self):
        with self.assertRaises(ValueError):
            shard_cik_list(["A"], num_shards=0)
        with self.assertRaises(ValueError):
            shard_cik_list(["A"], num_shards=-1)

    def test_round_robin_interleaves_small_and_large_filers(self):
        # Even if input is sorted by CIK number (which correlates with filer
        # age and historical filing volume), round-robin ensures shard-N gets
        # a mix of low/high CIKs — no single shard is "all old prolific filers".
        ciks = [f"{i:010d}" for i in range(20)]  # sorted by issuance
        shards = shard_cik_list(ciks, num_shards=4)
        # Every shard should span the full range of input positions.
        for shard in shards:
            positions = [int(c) for c in shard]
            self.assertGreater(max(positions), min(positions))


class TestWriteShards(unittest.TestCase):
    def test_writes_one_file_per_shard_with_predictable_naming(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            shards = [["A", "B"], ["C"], ["D"]]
            paths = write_shards(shards, output_dir=out_dir)
            self.assertEqual(len(paths), 3)
            self.assertEqual(paths[0].name, "ciks_shard_1_of_3.txt")
            self.assertEqual(paths[1].name, "ciks_shard_2_of_3.txt")
            self.assertEqual(paths[2].name, "ciks_shard_3_of_3.txt")

            self.assertEqual(paths[0].read_text().splitlines(), ["A", "B"])
            self.assertEqual(paths[1].read_text().splitlines(), ["C"])
            self.assertEqual(paths[2].read_text().splitlines(), ["D"])

    def test_creates_output_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "new" / "nested" / "dir"
            self.assertFalse(out_dir.exists())
            write_shards([["A"]], output_dir=out_dir)
            self.assertTrue(out_dir.is_dir())


class TestLoadCikList(unittest.TestCase):
    """_load_cik_list parses one CIK per line; mutation testing flagged it
    as untested. Empty lines and ``#``-prefixed comments must be skipped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ciks.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_cik_per_line(self):
        self.path.write_text("0000000001\n0000000002\n0000000003\n")
        self.assertEqual(
            _load_cik_list(self.path),
            ["0000000001", "0000000002", "0000000003"],
        )

    def test_unpadded_ciks_are_zero_padded_to_10_digits(self):
        # SEC submissions URLs require 10-digit zero-padded CIKs. Input
        # files often have unpadded ints (e.g. "320193" from spreadsheets).
        # Both run_form4_backfill._load_cik_list and the split version
        # MUST pad consistently so shard files match what the runner
        # produces from the same source.
        self.path.write_text("320193\n789019\n1\n")
        self.assertEqual(
            _load_cik_list(self.path),
            ["0000320193", "0000789019", "0000000001"],
        )

    def test_non_numeric_lines_skipped_with_warning(self):
        self.path.write_text("0000000001\nNOT_A_CIK\n0000000002\n")
        self.assertEqual(
            _load_cik_list(self.path),
            ["0000000001", "0000000002"],
        )

    def test_empty_lines_skipped(self):
        # Boundary case from mutation testing: `if not line or ...` flipped
        # to `if line or ...` would keep blanks.
        self.path.write_text("0000000001\n\n0000000002\n\n\n0000000003\n")
        self.assertEqual(
            _load_cik_list(self.path),
            ["0000000001", "0000000002", "0000000003"],
        )

    def test_comment_lines_skipped(self):
        self.path.write_text("# header comment\n0000000001\n# inline comment\n0000000002\n")
        self.assertEqual(
            _load_cik_list(self.path),
            ["0000000001", "0000000002"],
        )

    def test_whitespace_stripped_from_lines(self):
        self.path.write_text("  0000000001  \n\t0000000002\t\n")
        self.assertEqual(
            _load_cik_list(self.path),
            ["0000000001", "0000000002"],
        )

    def test_empty_file_returns_empty_list(self):
        self.path.write_text("")
        self.assertEqual(_load_cik_list(self.path), [])

    def test_only_comments_returns_empty_list(self):
        self.path.write_text("# only\n# comments\n#\n")
        self.assertEqual(_load_cik_list(self.path), [])


if __name__ == "__main__":
    unittest.main()
