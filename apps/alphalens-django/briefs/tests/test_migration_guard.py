"""Unit tests for the startup migration-skew guard (test-strategy Phase 2b).

Pins the durable fix for the #331/#340 deploy-env-drift incident
(2026-05-31): migration 0007 renamed ``gemini_confidence`` ->
``llm_confidence``. The long-running ``django`` container pulled an image
that ran ``migrate`` (DB now at 0007), while the one-shot ``rebuild-cache``
resolved a stale ``:latest`` whose code was still at 0006 — so its ``Brief``
model wrote the renamed-away column and Postgres raised ``UndefinedColumn``
6x/day, silently (the run exited 0). Failure class: deploy-env-drift.

``assert_schema_current`` converts that late, per-row, query-time crash into
an early, explicit "this container's code and the DB schema disagree, refusing
to run" — covering BOTH skew directions:

  * **behind**  — the code knows migrations the DB has not applied
    (DB is older than the image), and
  * **ahead**   — the DB has applied migrations the code's graph does not
    contain (the image is older than the DB — the EXACT #331/#340 direction).

These tests are SQLite-runnable: ``detect_skew`` is a pure function over three
plain inputs, and the integration no-op runs against the synced test DB. The
Postgres-only reproduction of the live ``UndefinedColumn`` lives in
``test_migrate_skew.py`` (the engine cannot be faithfully reproduced on SQLite).
"""

from __future__ import annotations

import pytest

from briefs.migration_guard import (
    SchemaSkewError,
    assert_schema_current,
    detect_skew,
)


class _FakeMigration:
    def __init__(self, app_label: str, name: str) -> None:
        self.app_label = app_label
        self.name = name


class TestDetectSkew:
    """Pure-function classifier — no DB, no Django machinery."""

    def test_in_sync_returns_no_skew(self):
        applied = {("briefs", "0006"), ("briefs", "0007")}
        known = {("briefs", "0006"), ("briefs", "0007")}
        behind, ahead = detect_skew(
            applied=applied,
            graph_nodes=known,
            behind_plan=[],  # nothing left to apply
            app_labels=("briefs",),
        )
        assert behind == []
        assert ahead == []

    def test_db_behind_code_is_flagged_behind(self):
        # Code knows 0007 but the DB has only 0006 applied -> migration_plan
        # returns the forward step still to run.
        behind_plan = [(_FakeMigration("briefs", "0007"), False)]
        behind, ahead = detect_skew(
            applied={("briefs", "0006")},
            graph_nodes={("briefs", "0006"), ("briefs", "0007")},
            behind_plan=behind_plan,
            app_labels=("briefs",),
        )
        assert behind == ["briefs.0007"]
        assert ahead == []

    def test_db_ahead_of_code_is_flagged_ahead(self):
        # The #331/#340 direction: the DB has 0007 recorded but the running
        # code's graph stops at 0006 (stale image). migration_plan is empty
        # (the code thinks it is at head), so ONLY the ahead check catches it.
        behind, ahead = detect_skew(
            applied={("briefs", "0006"), ("briefs", "0007")},
            graph_nodes={("briefs", "0006")},
            behind_plan=[],
            app_labels=("briefs",),
        )
        assert behind == []
        assert ahead == ["briefs.0007"]

    def test_ahead_check_ignores_other_apps(self):
        # An applied migration from an app we do not guard must not trip the
        # ahead detector (third-party / contrib apps squash + replace freely).
        behind, ahead = detect_skew(
            applied={("admin", "0099_unknown"), ("briefs", "0006")},
            graph_nodes={("briefs", "0006")},
            behind_plan=[],
            app_labels=("briefs",),
        )
        assert behind == []
        assert ahead == []


@pytest.mark.django_db
class TestAssertSchemaCurrent:
    def test_noop_on_synced_test_db(self):
        # pytest-django migrates the test DB to head, so code and schema agree
        # -> the guard must be a silent no-op (no raise, returns None).
        assert assert_schema_current() is None

    def test_raises_when_behind(self, monkeypatch):
        # Force the executor to report an unapplied migration -> guard raises.
        import briefs.migration_guard as guard

        class _FakeLoader:
            applied_migrations = {("briefs", "0006")}

            class graph:  # noqa: N801 - mimics MigrationLoader.graph attr
                @staticmethod
                def leaf_nodes():
                    return [("briefs", "0007")]

                nodes = {("briefs", "0006"), ("briefs", "0007")}

        class _FakeExecutor:
            def __init__(self, _conn):
                self.loader = _FakeLoader()

            def migration_plan(self, _targets):
                return [(_FakeMigration("briefs", "0007"), False)]

        monkeypatch.setattr(guard, "MigrationExecutor", _FakeExecutor)
        with pytest.raises(SchemaSkewError, match="briefs.0007"):
            guard.assert_schema_current()

    def test_raises_when_ahead(self, monkeypatch):
        # The #331/#340 direction end-to-end: DB records 0007, code graph stops
        # at 0006, plan empty -> only the ahead branch fires.
        import briefs.migration_guard as guard

        class _FakeLoader:
            applied_migrations = {("briefs", "0006"), ("briefs", "0007")}

            class graph:  # noqa: N801
                @staticmethod
                def leaf_nodes():
                    return [("briefs", "0006")]

                nodes = {("briefs", "0006")}

        class _FakeExecutor:
            def __init__(self, _conn):
                self.loader = _FakeLoader()

            def migration_plan(self, _targets):
                return []

        monkeypatch.setattr(guard, "MigrationExecutor", _FakeExecutor)
        with pytest.raises(SchemaSkewError, match=r"image older than DB.*briefs\.0007"):
            guard.assert_schema_current()


@pytest.mark.django_db
class TestCommandWiring:
    """The one-shot command must run the guard FIRST and surface a skew as a
    clean ``CommandError`` (non-zero exit, no traceback) — not let the late
    per-row ``UndefinedColumn`` through. The skew is forced by monkeypatching
    the guard so this stays SQLite-runnable; the real Postgres reproduction is
    ``test_migrate_skew.py``.
    """

    def test_command_aborts_on_skew_before_touching_db(self, monkeypatch, tmp_path):
        import pandas as pd
        from django.core.management import call_command
        from django.core.management.base import CommandError

        import briefs.management.commands.rebuild_briefs_cache as cmd
        from briefs.migration_guard import SchemaSkewError
        from briefs.models import Brief

        # A perfectly valid parquet — the guard, not the data, must stop the run.
        pd.DataFrame([{"ticker": "NVDA", "theme": "ai"}]).to_parquet(
            tmp_path / "2026-05-22.parquet", index=False
        )

        def _boom(*_a, **_k):
            raise SchemaSkewError("image older than DB: ['briefs.0007']")

        monkeypatch.setattr(cmd, "assert_schema_current", _boom)

        with pytest.raises(CommandError, match=r"briefs\.0007"):
            call_command("rebuild_briefs_cache", "--briefs-dir", str(tmp_path))

        # Fail-loud, not silent: nothing was written despite valid input.
        assert Brief.objects.count() == 0
