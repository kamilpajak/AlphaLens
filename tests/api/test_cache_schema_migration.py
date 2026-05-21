"""Schema-version migration rebuilds the table (not just deletes rows)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphalens.api import cache as cache_module
from alphalens.api.cache import rebuild_from_parquet
from tests.api._fixtures import seed_two_days


class SchemaMigrationTests(unittest.TestCase):
    def test_bumped_schema_version_drops_and_recreates_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            briefs_dir = Path(tmp) / "briefs"
            db_path = Path(tmp) / "briefs.db"
            seed_two_days(briefs_dir)

            # Initial build at version 1.
            rebuild_from_parquet(briefs_dir=briefs_dir, db_path=db_path)

            # Simulate a schema bump that adds a column to ``briefs``. Without
            # a DROP+CREATE the new column would never exist, and the next
            # rebuild's INSERT (column count mismatch) would crash.
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '0')"
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(cache_module, "SCHEMA_VERSION", 2):
                # Forge a new column into the on-disk DDL via the patched schema
                # constant by monkey-patching ``_create_schema`` to append a
                # synthetic column. We only need to prove the migration drops
                # the existing table so the next ``CREATE TABLE`` runs.
                original = cache_module._create_schema

                def new_schema(c):
                    original(c)
                    c.execute("ALTER TABLE briefs ADD COLUMN migration_probe TEXT")

                with patch.object(cache_module, "_create_schema", new_schema):
                    rebuild_from_parquet(briefs_dir=briefs_dir, db_path=db_path)

                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                try:
                    cols = {r["name"] for r in conn.execute("PRAGMA table_info(briefs)").fetchall()}
                    n_rows = conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0]
                    version_row = conn.execute(
                        "SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()
                finally:
                    conn.close()

                self.assertIn(
                    "migration_probe",
                    cols,
                    "DROP+CREATE should have applied the new column",
                )
                self.assertEqual(n_rows, 7, "all rows should be re-inserted")
                self.assertEqual(int(version_row["value"]), 2)


if __name__ == "__main__":
    unittest.main()
