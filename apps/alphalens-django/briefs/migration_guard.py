"""Startup migration-skew guard for the ``rebuild_briefs_cache`` one-shot.

The durable fix for the #331/#340 deploy-env-drift incident (2026-05-31):
migration 0007 renamed ``Brief.gemini_confidence`` -> ``llm_confidence``.
On the VPS the long-running ``django`` container had pulled an image that ran
``migrate`` (DB advanced to 0007), but the one-shot ``rebuild-cache`` resolved
a stale ``:latest`` built BEFORE 0007 — so its in-process ``Brief`` model still
referenced the old column and every ``bulk_create`` hit Postgres
``UndefinedColumn``. The 6x/day cache rebuild died silently (exit 0, empty
work) because nothing checked that the running code's migration graph agrees
with the schema the DB actually has.

``assert_schema_current`` makes the one-shot refuse to run on a skew instead of
crashing per-row later, and reports WHICH direction is wrong:

  * **behind** — the code's graph knows migrations the DB has not applied
    (the image is newer than the DB); ``migration_plan`` is non-empty.
  * **ahead**  — the DB has migrations recorded that the code's graph does not
    contain (the image is older than the DB — the literal #331/#340 case);
    ``migration_plan`` is empty here, so a plain "unapplied migrations" check
    would MISS it. This guard checks the recorded-vs-known set explicitly.

The ``django`` service runs ``migrate`` on start and so is self-consistent; the
guard is wired only into the ``rebuild_briefs_cache`` command path, which never
migrates and is the surface the incident actually hit.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from django.core.exceptions import ImproperlyConfigured
from django.db import connection as default_connection
from django.db.migrations.executor import MigrationExecutor


class _MigrationLike(Protocol):
    """The two attributes ``detect_skew`` reads off a plan's ``Migration``."""

    app_label: str
    name: str


# Apps whose schema this guard protects. Deliberately just ``briefs``:
#  * this command writes only Brief / DayMeta (the briefs app), and
#  * briefs is the ONLY first-party app with migrations on the default
#    (Postgres) connection — core / auth_cf / market have no migrations, and
#    feedback lives on a SEPARATE DB behind a router, so guarding it against
#    the default connection would BEHIND-false-positive (its migrations are
#    recorded on a different connection than the one this guard reads).
#  * contrib / third-party apps squash and replace migrations, so their
#    "recorded-but-unknown-to-graph" set is legitimately non-empty and would
#    AHEAD-false-positive.
# The anti-rot positive control in test_migration_guard.py pins that emptying
# this set blinds the guard, so a future narrowing can't pass silently.
_GUARDED_APPS: tuple[str, ...] = ("briefs",)


class SchemaSkewError(ImproperlyConfigured):
    """The DB schema and the running code's migration graph disagree.

    Subclasses ``ImproperlyConfigured`` so a misconfigured deployment reads as
    a configuration fault, not a transient DB error.
    """


def detect_skew(
    *,
    applied: Iterable[tuple[str, str]],
    graph_nodes: Iterable[tuple[str, str]],
    behind_plan: Sequence[tuple[_MigrationLike, bool]],
    app_labels: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Classify skew from three plain inputs — pure, no DB access.

    ``applied``      — (app_label, name) pairs recorded in ``django_migrations``.
    ``graph_nodes``  — (app_label, name) pairs the running code's loader knows.
    ``behind_plan``  — ``MigrationExecutor.migration_plan`` output: (Migration,
                       backward) tuples still to apply to reach head. The
                       reported ``behind`` list is the leaf nodes of that
                       pending chain (Django applies the full path; an
                       intermediate non-leaf migration may not be named).
    ``app_labels``   — apps to guard; everything else is ignored.

    Returns ``(behind, ahead)`` as sorted ``"app.name"`` string lists.
    """
    guarded = set(app_labels)
    known = set(graph_nodes)

    behind = sorted(
        f"{migration.app_label}.{migration.name}"
        for migration, _backward in behind_plan
        if migration.app_label in guarded
    )
    ahead = sorted(
        f"{app}.{name}" for (app, name) in applied if app in guarded and (app, name) not in known
    )
    return behind, ahead


def assert_schema_current(
    app_labels: Sequence[str] = _GUARDED_APPS,
    *,
    conn=None,
) -> None:
    """Raise ``SchemaSkewError`` if code and DB schema disagree; else no-op.

    Checks both skew directions (see module docstring). Call this BEFORE any
    query that touches a guarded model so a deploy mismatch fails loud and
    early instead of as a late per-row ``UndefinedColumn``.
    """
    conn = conn if conn is not None else default_connection
    executor = MigrationExecutor(conn)
    loader = executor.loader

    targets = [node for node in loader.graph.leaf_nodes() if node[0] in set(app_labels)]
    behind, ahead = detect_skew(
        applied=loader.applied_migrations,
        graph_nodes=loader.graph.nodes,
        behind_plan=executor.migration_plan(targets),
        app_labels=app_labels,
    )

    if not behind and not ahead:
        return

    parts: list[str] = []
    if behind:
        parts.append(
            "the running code expects migrations the database has not applied "
            f"(image newer than DB): {behind}"
        )
    if ahead:
        parts.append(
            f"the database has migrations this code does not know (image older than DB): {ahead}"
        )
    raise SchemaSkewError(
        "Migration skew detected for "
        f"{sorted(set(app_labels))} — refusing to run rebuild_briefs_cache. "
        + "; ".join(parts)
        + ". Pull the image whose code matches the deployed schema "
        "(pin ALPHALENS_DJANGO_TAG to a sha) and retry."
    )
