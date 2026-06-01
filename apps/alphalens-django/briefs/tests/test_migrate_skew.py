"""Postgres-only reproduction of the #331/#340 migration-skew incident.

Test-strategy Phase 2b. The L2 ``test_migration_guard.py`` covers the durable
fix and both skew directions hermetically; THIS test reproduces the live
failure against the real engine, because SQLite cannot: its ``RenameField``
rebuilds the whole table, so a stale-schema write does not raise the
``UndefinedColumn`` (sqlstate 42703) that Postgres does. The production DB is
Postgres, so fidelity requires Postgres.

Reproduced contract (deploy-env-drift, 2026-05-31): migration 0007 renamed
``gemini_confidence`` -> ``llm_confidence``. On the VPS the long-running
``django`` container migrated the shared DB to 0007 while the one-shot
``rebuild-cache`` ran a stale image whose ``Brief`` model still wrote the old
column -> ``UndefinedColumn`` 6x/day, silently. We reproduce the IDENTICAL
column-not-found contract in the deterministic direction one codebase can hold:
roll the DB back to 0006 (column still ``gemini_confidence``) while the
in-process model is at head (writes ``llm_confidence``). The skew MUST fail
loud and write ZERO rows — never a partial/silent cache.

Runs only where the DB is Postgres (the CI ``django`` job's Postgres step sets
``DATABASE_URL`` at it); skipped on the default SQLite suite + local runs.
``transaction=True`` is required so ``MigrationExecutor`` DDL really commits.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from django.db import ProgrammingError, connection
from django.db.migrations.executor import MigrationExecutor

from briefs.ingest.parquet import rebuild_from_parquet
from briefs.models import Brief

pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="migration skew (UndefinedColumn / sqlstate 42703) is Postgres-specific; "
    "SQLite RenameField rebuilds the table and cannot reproduce it",
)

_HEAD = ("briefs", "0007_rename_gemini_confidence_llm_confidence")
_PRE_RENAME = ("briefs", "0006_alter_brief_brief_template_id")


def _migrate_briefs_to(node: tuple[str, str]) -> None:
    """Move ONLY the briefs app to ``node`` against the live test connection."""
    executor = MigrationExecutor(connection)
    executor.migrate([node])


def _write_two_rows(directory: Path, iso_date: str) -> None:
    pd.DataFrame(
        [
            {"ticker": "NVDA", "theme": "ai-infra"},
            {"ticker": "AVGO", "theme": "ai-infra"},
        ]
    ).to_parquet(directory / f"{iso_date}.parquet", index=False)


@pytest.fixture
def _briefs_rolled_back_to_0006():
    """DB schema at 0006 (pre-rename) for the test; restored to head after.

    The in-process ``Brief`` model stays at head — this IS the skew.
    """
    _migrate_briefs_to(_PRE_RENAME)
    try:
        yield
    finally:
        _migrate_briefs_to(_HEAD)


@pytest.mark.django_db(transaction=True)
class TestMigrateSkew:
    def test_skew_fails_loud_with_undefined_column_and_writes_nothing(
        self, _briefs_rolled_back_to_0006, tmp_path: Path
    ):
        _write_two_rows(tmp_path, "2026-05-22")

        with pytest.raises(ProgrammingError) as excinfo:
            rebuild_from_parquet(briefs_dir=tmp_path, force=True)

        # Pin the specific column-not-found class, not any generic DB error:
        # Postgres names the renamed-away column the stale image referenced.
        assert "llm_confidence" in str(excinfo.value)
        cause = excinfo.value.__cause__
        assert cause is not None and getattr(cause, "sqlstate", None) == "42703"

        # Anti-silent-failure: the atomic per-date block rolled back, so the
        # skew aborted the whole date — no partial / truncated cache.
        assert Brief.objects.count() == 0

    def test_control_at_head_writes_rows(self, tmp_path: Path):
        # Same call, schema at head (no skew) -> succeeds with 2 rows. Proves
        # the failure above is the schema skew, not a broken fixture.
        _write_two_rows(tmp_path, "2026-05-22")

        result = rebuild_from_parquet(briefs_dir=tmp_path, force=True)

        assert result.total_briefs == 2
        assert Brief.objects.count() == 2
