"""Cache builder behaviour: incremental rebuild, schema mismatch, NaN handling."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

from alphalens.api.cache import REQUIRED_PARQUET_COLUMNS, rebuild_from_parquet
from alphalens.api.schema import SCHEMA_VERSION
from tests.api._fixtures import (
    seed_min_schema_day,
    seed_two_days,
)


class TestCacheRebuild(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.briefs_dir = root / "briefs"
        self.db_path = root / "briefs.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_rebuild_first_pass(self):
        seed_two_days(self.briefs_dir)
        result = rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        self.assertEqual(result.n_rebuilt, 2)
        self.assertEqual(result.n_skipped, 0)
        self.assertEqual(result.n_deleted, 0)
        self.assertEqual(result.total_briefs, 7)  # 3 + 4

    def test_incremental_skip_on_unchanged_mtime(self):
        seed_two_days(self.briefs_dir)
        rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        result = rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        self.assertEqual(result.n_rebuilt, 0)
        self.assertEqual(result.n_skipped, 2)

    def test_rebuild_picks_up_modified_parquet(self):
        _, path_new = seed_two_days(self.briefs_dir)
        rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        # Bump the newer file's mtime; old one is untouched.
        future = time.time() + 5
        os.utime(path_new, (future, future))
        result = rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        self.assertEqual(result.rebuilt_dates, ("2026-05-18",))
        self.assertEqual(result.skipped_dates, ("2026-05-17",))

    def test_deleted_parquet_drops_from_cache(self):
        path_old, _ = seed_two_days(self.briefs_dir)
        rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        path_old.unlink()
        result = rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        self.assertEqual(result.deleted_dates, ("2026-05-17",))

        conn = sqlite3.connect(str(self.db_path))
        try:
            n = conn.execute("SELECT COUNT(*) FROM briefs WHERE date='2026-05-17'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_force_rebuilds_everything(self):
        seed_two_days(self.briefs_dir)
        rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        result = rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path, force=True)
        self.assertEqual(result.n_rebuilt, 2)
        self.assertEqual(result.n_skipped, 0)

    def test_legacy_subset_schema_accepted(self):
        seed_min_schema_day(self.briefs_dir)
        result = rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        self.assertEqual(result.n_rebuilt, 1)
        self.assertEqual(result.total_briefs, 2)

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT ticker, theme, technical_rsi FROM briefs WHERE date='2023-01-23'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["theme"], "solar")
            self.assertIsNone(r["technical_rsi"])

    def test_missing_required_column_errors(self):
        bad = self.briefs_dir / "2026-05-19.parquet"
        bad.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"company_name": ["X"]}).to_parquet(bad, index=False)
        with self.assertRaises(ValueError) as ctx:
            rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        self.assertIn("required columns", str(ctx.exception))
        self.assertEqual(REQUIRED_PARQUET_COLUMNS, frozenset({"ticker", "theme"}))

    def test_nan_coerced_to_null(self):
        seed_two_days(self.briefs_dir)
        rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT technical_rsi FROM briefs WHERE date='2026-05-18' AND ticker='DDD'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIsNone(row["technical_rsi"])

    def test_list_columns_round_trip_as_json(self):
        seed_two_days(self.briefs_dir)
        rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT gates_passed FROM briefs WHERE date='2026-05-18' AND ticker='AAA'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(json.loads(row["gates_passed"]), ["polygon_news", "etf_holdings"])

    def test_schema_version_stamped(self):
        seed_two_days(self.briefs_dir)
        rebuild_from_parquet(briefs_dir=self.briefs_dir, db_path=self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        finally:
            conn.close()
        self.assertEqual(int(row[0]), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
