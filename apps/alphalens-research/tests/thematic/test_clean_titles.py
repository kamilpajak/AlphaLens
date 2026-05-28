"""Tests for the legacy-titles backfill — `alphalens thematic clean-titles`.

PR #259 fixed GDELT space-padded titles at the ingest source ("forward-only,
backfill not included"). Rows ingested before that — both in the parquet
source-of-truth (~/.alphalens/thematic_briefs/) and in the Django Brief
table downstream — kept their padded titles ("weekend , citing", "Alphabet
( Google )"). This module pins the in-place parquet cleanup that fixes
them, and is paired operationally with `manage.py rebuild_briefs_cache
--force` to refresh the DB from the cleaned parquets.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
from alphalens_pipeline.thematic.clean_titles import (
    CleanResult,
    clean_titles_in_parquet_dir,
)


def _write_parquet(path: Path, titles: list[str | None]) -> None:
    """Tiny helper: one-row-per-title parquet with a single source_event_title col."""
    df = pd.DataFrame(
        {"ticker": [f"T{i}" for i in range(len(titles))], "source_event_title": titles}
    )
    df.to_parquet(path)


class TestCleanTitlesInParquetDir(unittest.TestCase):
    def test_cleans_dirty_titles_in_place(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            _write_parquet(
                tmpd / "2026-05-18.parquet",
                [
                    "Newsom office warns Californians to avoid Chevron this holiday weekend , citing high gas prices",
                    "Druckenmiller Dumped Alphabet ( Google ) and Bought AI",
                    "Already clean title",
                ],
            )
            result = clean_titles_in_parquet_dir(tmpd)
            cleaned = pd.read_parquet(tmpd / "2026-05-18.parquet")
            self.assertEqual(
                list(cleaned["source_event_title"]),
                [
                    "Newsom office warns Californians to avoid Chevron this holiday weekend, citing high gas prices",
                    "Druckenmiller Dumped Alphabet (Google) and Bought AI",
                    "Already clean title",
                ],
            )
            self.assertEqual(result.total_rows_cleaned, 2)
            self.assertEqual(result.files_touched, 1)

    def test_is_idempotent_no_op_on_already_clean(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            _write_parquet(tmpd / "clean.parquet", ["Already clean", "Also clean"])
            first = clean_titles_in_parquet_dir(tmpd)
            self.assertEqual(first.total_rows_cleaned, 0)
            self.assertEqual(first.files_touched, 0)
            second = clean_titles_in_parquet_dir(tmpd)
            self.assertEqual(second.total_rows_cleaned, 0)

    def test_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            _write_parquet(tmpd / "dirty.parquet", ["weekend , citing"])
            mtime_before = (tmpd / "dirty.parquet").stat().st_mtime_ns
            result = clean_titles_in_parquet_dir(tmpd, dry_run=True)
            self.assertEqual(result.total_rows_cleaned, 1)
            # dry-run mirrors real-run shape — files_touched reflects the
            # set that WOULD be rewritten, so operator output matches one-for-one.
            self.assertEqual(result.files_touched, 1)
            # No actual write happened — mtime + contents unchanged.
            self.assertEqual((tmpd / "dirty.parquet").stat().st_mtime_ns, mtime_before)
            still_dirty = pd.read_parquet(tmpd / "dirty.parquet")
            self.assertEqual(list(still_dirty["source_event_title"]), ["weekend , citing"])
            # No leftover temp files from the atomic-write path.
            self.assertEqual(list(tmpd.glob(".dirty.parquet.*")), [])

    def test_atomic_write_leaves_no_temp_files_on_success(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            _write_parquet(tmpd / "x.parquet", ["weekend , citing"])
            clean_titles_in_parquet_dir(tmpd)
            self.assertEqual(list(tmpd.glob(".*.parquet.*")), [])

    def test_handles_null_titles_without_crashing(self) -> None:
        # Field is blank=True on the Django model and arrives as None for some legacy rows.
        with TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            _write_parquet(tmpd / "nullable.parquet", [None, "weekend , citing", None])
            result = clean_titles_in_parquet_dir(tmpd)
            self.assertEqual(result.total_rows_cleaned, 1)
            out = pd.read_parquet(tmpd / "nullable.parquet")
            values = list(out["source_event_title"])
            # Parquet round-trips Python None → pandas NaN — treat both as "missing".
            self.assertTrue(pd.isna(values[0]))
            self.assertEqual(values[1], "weekend, citing")
            self.assertTrue(pd.isna(values[2]))

    def test_skips_parquets_without_the_title_column(self) -> None:
        # Older snapshots may pre-date the source_event_title column.
        with TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            pd.DataFrame({"ticker": ["AAA"]}).to_parquet(tmpd / "old.parquet")
            result = clean_titles_in_parquet_dir(tmpd)
            self.assertEqual(result.total_rows_cleaned, 0)
            self.assertEqual(result.files_touched, 0)

    def test_returns_per_file_stats(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            _write_parquet(tmpd / "a.parquet", ["dirty , one", "dirty , two"])
            _write_parquet(tmpd / "b.parquet", ["clean", "weekend , citing"])
            _write_parquet(tmpd / "c.parquet", ["clean only"])
            result = clean_titles_in_parquet_dir(tmpd)
            per_file = {p.name: cleaned for p, cleaned in result.per_file}
            self.assertEqual(per_file["a.parquet"], 2)
            self.assertEqual(per_file["b.parquet"], 1)
            self.assertEqual(per_file["c.parquet"], 0)
            self.assertEqual(result.total_rows_cleaned, 3)
            self.assertEqual(result.files_touched, 2)


class TestCleanResultDataclass(unittest.TestCase):
    def test_dataclass_shape(self) -> None:
        result = CleanResult(per_file=[], total_rows_cleaned=0, files_touched=0)
        self.assertEqual(result.total_rows_cleaned, 0)


if __name__ == "__main__":
    unittest.main()
