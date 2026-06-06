"""Tests for the subtractive teardown of the removed feedback ``decisions`` table.

The Track-A click ledger was removed (#465); nothing writes or opens the
``decisions`` table any more. ``alphalens_feedback.migrate.drop_decisions_table``
is the operator teardown that idempotently drops the dead table from a populated
legacy ``feedback.db`` while NEVER touching the live population-ladder parquets
(separate files, the sole live edge signal).

These pin the BOTH-cases contract the schema evolution requires:
  * fresh db (no ``decisions`` table) -> no-op, never raises;
  * old populated db (``decisions`` table + rows) -> table dropped;
  * missing file -> no-op (returns False);
  * idempotent -> running twice never raises;
  * the SEPARATE population_ladders parquet is byte-identical before/after.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from alphalens_feedback.migrate import drop_decisions_table

# A trimmed copy of the historical gen-4 ``decisions`` DDL (only the columns the
# teardown must tolerate — names are illustrative; the teardown drops the whole
# table regardless of column set). Kept local so the test does not depend on the
# removed ``store`` module.
_LEGACY_DECISIONS_DDL = (
    "CREATE TABLE decisions ("
    "  id TEXT PRIMARY KEY,"
    "  brief_date TEXT NOT NULL,"
    "  ticker TEXT NOT NULL,"
    "  theme TEXT NOT NULL,"
    "  action TEXT NOT NULL,"
    "  shadow_return REAL,"
    "  realized_return REAL,"
    "  ladder_classification TEXT,"
    "  realized_r REAL,"
    "  UNIQUE(brief_date, ticker, theme)"
    ")"
)
_LEGACY_INDEXES = (
    "CREATE INDEX idx_decisions_brief_date ON decisions(brief_date)",
    "CREATE INDEX idx_decisions_ticker ON decisions(ticker)",
    "CREATE INDEX idx_decisions_action ON decisions(action)",
)


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _write_legacy_populated_db(path: Path) -> None:
    """Write an old-schema feedback.db carrying the dead decisions table + a row."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute(_LEGACY_DECISIONS_DDL)
        for ddl in _LEGACY_INDEXES:
            conn.execute(ddl)
        conn.execute(
            "INSERT INTO decisions (id, brief_date, ticker, theme, action, "
            "shadow_return, realized_return, ladder_classification, realized_r) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("dead-1", "2026-05-01", "NVDA", "ai", "interested", 0.01, 0.02, "TP_FULL", 1.5),
        )
    finally:
        conn.close()


class TestDropDecisionsTable(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.feedback_path = self.root / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_missing_file_is_noop_returns_false(self):
        # Nothing to tear down on a host that never had the click ledger.
        self.assertFalse(drop_decisions_table(self.feedback_path))
        self.assertFalse(self.feedback_path.exists())

    def test_old_populated_db_has_decisions_table_dropped(self):
        _write_legacy_populated_db(self.feedback_path)
        self.assertIn("decisions", _table_names(self.feedback_path))

        self.assertTrue(drop_decisions_table(self.feedback_path))

        self.assertNotIn("decisions", _table_names(self.feedback_path))

    def test_fresh_db_without_decisions_table_is_noop(self):
        # A fresh feedback.db that has some OTHER table but no decisions: the
        # teardown must not raise "no such table".
        conn = sqlite3.connect(str(self.feedback_path), isolation_level=None)
        conn.execute("CREATE TABLE keepme (x INTEGER)")
        conn.close()

        self.assertTrue(drop_decisions_table(self.feedback_path))

        self.assertEqual(_table_names(self.feedback_path), {"keepme"})

    def test_idempotent_second_run_does_not_raise(self):
        _write_legacy_populated_db(self.feedback_path)
        drop_decisions_table(self.feedback_path)
        # Re-running on an already-migrated db is a clean no-op (DROP IF EXISTS).
        self.assertTrue(drop_decisions_table(self.feedback_path))
        self.assertNotIn("decisions", _table_names(self.feedback_path))

    def test_population_ladder_parquet_is_untouched(self):
        # The live edge signal lives in SEPARATE population_ladders parquet files.
        # The teardown only opens feedback.db; the parquet must be byte-identical
        # before and after.
        pop_dir = self.root / "population_ladders"
        pop_dir.mkdir()
        parquet = pop_dir / "2026-05-01.parquet"
        payload = b"PAR1-fake-population-ladder-bytes"
        parquet.write_bytes(payload)
        before = parquet.read_bytes()

        _write_legacy_populated_db(self.feedback_path)
        drop_decisions_table(self.feedback_path)

        self.assertTrue(parquet.exists())
        self.assertEqual(parquet.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
