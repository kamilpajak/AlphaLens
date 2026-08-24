"""The Grafana provisioning sync: decision core, live-tree ops, and adapters.

The job's contract mirrors the Prometheus rules sync: the live provisioning
tree converges to the ``origin/main`` blobs within one cadence, and every way
the convergence can fail is LOUD — a distinct outcome label in the textfile
metrics plus a non-zero exit. The 2026-08-24 incident shape (the live
datasource yml never declared ``uid: prometheus`` while every dashboard target
referenced that uid, so every panel read "No data") is why the validation gate
parses the datasource yml and REQUIRES that uid, and why it refuses a dashboard
whose targets reference a uid no synced datasource declares.

Nothing here touches git, docker or the network: those are ports, and every
test passes a fake. The live-tree filesystem operations run against a REAL
temporary directory — "check_failed leaves live byte-identical", "prune spares
foreign files" and "no backup or temp name is itself a provisioning file" are
claims about the filesystem, so they are proven on one. The grafana.db reader
likewise runs against a REAL sqlite file carrying the live table shapes,
secret columns included, so "the queries never read a secret" is proven rather
than asserted.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import sync_grafana_provisioning as sync

NOW_TS = 1_756_000_000.0  # 2026-08-24T01:46:40Z

REPO_ROOT = Path(__file__).resolve().parents[3]

DATASOURCE_YML = b"""apiVersion: 1

datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
"""

PROVIDER_YML = b"""apiVersion: 1

providers:
  - name: 'Default'
    orgId: 1
    folder: ''
    type: file
    disableDeletion: false
    editable: true
    options:
      path: /etc/grafana/provisioning/dashboards
    updateIntervalSeconds: 10
"""

DASHBOARD_JSON = json.dumps(
    {
        "uid": "alphalens-cron-health",
        "title": "AlphaLens Cron Health",
        "panels": [
            {
                "title": "Job age",
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "targets": [
                    {
                        "datasource": {"type": "prometheus", "uid": "prometheus"},
                        "expr": "alphalens_job_last_success_timestamp_seconds",
                    }
                ],
            }
        ],
    },
    indent=2,
).encode("utf-8")

STALE = b"apiVersion: 1\n"

# Real neighbours of the managed files in the live dashboards directory
# (2026-08-24 VPS probe). Prune must anchor on the per-file autosync prefix and
# never touch any of these: one is a foreign tenant's provisioned dashboard,
# one is a manual backup in another naming style that sorts BELOW every
# autosync stamp (under a loosened prefix it is the file a keep-newest-10 pass
# deletes first — the mutation kill), one is a manual provider backup.
FOREIGN_FILES = (
    "node-exporter-dashboard.json",
    "alphalens-cron-health.json.bak-20260531-202411",
    "dashboards.yml.bak.20260823T113614Z",
)

DATASOURCE_FILE = next(f for f in sync.MANAGED_FILES if f.kind is sync.FileKind.DATASOURCE)
PROVIDER_FILE = next(f for f in sync.MANAGED_FILES if f.kind is sync.FileKind.DASHBOARD_PROVIDER)
DASHBOARD_FILE = next(f for f in sync.MANAGED_FILES if f.kind is sync.FileKind.DASHBOARD)


def desired_set(**overrides: bytes) -> dict[sync.ManagedFile, bytes]:
    """The three managed blobs, with per-kind overrides for the failure arms."""
    content = {
        DATASOURCE_FILE: DATASOURCE_YML,
        PROVIDER_FILE: PROVIDER_YML,
        DASHBOARD_FILE: DASHBOARD_JSON,
    }
    for kind, blob in overrides.items():
        managed = next(f for f in sync.MANAGED_FILES if f.kind.value == kind)
        content[managed] = blob
    return content


def stale_tree() -> dict[sync.ManagedFile, bytes]:
    """Every managed file present in the live tree but out of date."""
    return dict.fromkeys(sync.MANAGED_FILES, STALE)


class TestManifest(unittest.TestCase):
    """The write set is a closed whitelist, not a directory mirror."""

    def test_the_manifest_is_exactly_the_three_managed_files(self) -> None:
        self.assertEqual(
            [(f.repo_path, f.live_relpath, f.kind) for f in sync.MANAGED_FILES],
            [
                (
                    "deploy/monitoring/grafana/provisioning/datasources/prometheus.yml",
                    "datasources/prometheus.yml",
                    sync.FileKind.DATASOURCE,
                ),
                (
                    "deploy/monitoring/grafana/provisioning/dashboards/dashboards.yml",
                    "dashboards/dashboards.yml",
                    sync.FileKind.DASHBOARD_PROVIDER,
                ),
                (
                    "deploy/monitoring/grafana/dashboards/alphalens-cron-health.json",
                    "dashboards/alphalens-cron-health.json",
                    sync.FileKind.DASHBOARD,
                ),
            ],
        )

    def test_every_managed_repo_path_exists_in_this_checkout(self) -> None:
        for managed in sync.MANAGED_FILES:
            with self.subTest(path=managed.repo_path):
                self.assertTrue((REPO_ROOT / managed.repo_path).is_file())

    def test_backup_names_are_never_themselves_provisioning_files(self) -> None:
        # Grafana's file provider imports every *.json in the dashboards dir as
        # a DASHBOARD and every *.yml as CONFIG. A backup ending in .json would
        # be imported as a duplicate of the dashboard it backs up (same uid).
        for managed in sync.MANAGED_FILES:
            name = f"{sync.backup_prefix(managed.live_name)}20260824T090000Z"
            with self.subTest(name=name):
                self.assertFalse(name.endswith((".json", ".yml", ".yaml")))


class TestShippedProvisioningFiles(unittest.TestCase):
    """The files this repo ships must pass the gate that guards the live tree."""

    def _shipped(self) -> dict[sync.ManagedFile, bytes]:
        return {f: (REPO_ROOT / f.repo_path).read_bytes() for f in sync.MANAGED_FILES}

    def test_the_shipped_set_passes_the_validation_gate(self) -> None:
        self.assertIsNone(sync.validate_desired(self._shipped()))

    def test_the_shipped_datasource_declares_the_uid_the_dashboard_references(self) -> None:
        # The 2026-08-24 root cause, pinned on the parsed document: the
        # dashboard targets reference uid "prometheus" and nothing else may
        # satisfy them.
        shipped = self._shipped()
        declared = sync.parse_datasource_uids(shipped[DATASOURCE_FILE])
        referenced = sync.referenced_datasource_uids(json.loads(shipped[DASHBOARD_FILE]))

        self.assertIn(sync.REQUIRED_DATASOURCE_UID, declared)
        self.assertTrue(referenced)
        self.assertTrue(referenced <= declared)


class TestValidateDesired(unittest.TestCase):
    """The promtool-equivalent: run in-process, BEFORE anything is replaced."""

    def test_the_good_set_is_accepted(self) -> None:
        self.assertIsNone(sync.validate_desired(desired_set()))

    def test_a_datasource_without_the_required_uid_is_refused(self) -> None:
        without_uid = DATASOURCE_YML.replace(b"    uid: prometheus\n", b"")

        problem = sync.validate_desired(desired_set(datasource=without_uid))

        self.assertIsNotNone(problem)
        self.assertIn(sync.REQUIRED_DATASOURCE_UID, str(problem))

    def test_a_consistently_renamed_uid_is_still_refused(self) -> None:
        # Pins the REQUIRED_DATASOURCE_UID clause SPECIFICALLY. The two
        # tests above are also satisfied by the dangling-reference check
        # (their dashboards still point at "prometheus"), so neutering the
        # clause leaves them green. Here the rename is internally
        # consistent - every dashboard target follows the datasource - so
        # only the required-uid clause can refuse it. It must: the live
        # dashboards this repo ships address "prometheus", and a live
        # datasource that stops declaring that uid is the 2026-08-24
        # incident (every panel renders "No data").
        renamed_ds = DATASOURCE_YML.replace(b"uid: prometheus", b"uid: grafana-prom")
        renamed_dash = DASHBOARD_JSON.replace(b'"prometheus"', b'"grafana-prom"')

        problem = sync.validate_desired(desired_set(datasource=renamed_ds, dashboard=renamed_dash))

        self.assertIsNotNone(problem)
        self.assertIn(sync.REQUIRED_DATASOURCE_UID, str(problem))

    def test_a_datasource_with_a_different_uid_is_refused(self) -> None:
        renamed = DATASOURCE_YML.replace(b"uid: prometheus", b"uid: promeetheus")

        self.assertIsNotNone(sync.validate_desired(desired_set(datasource=renamed)))

    def test_unparseable_datasource_yaml_is_refused(self) -> None:
        problem = sync.validate_desired(desired_set(datasource=b"{unclosed"))

        self.assertIsNotNone(problem)
        self.assertIn("datasources/prometheus.yml", str(problem))

    def test_a_datasource_entry_without_any_uid_is_refused(self) -> None:
        anonymous = (
            DATASOURCE_YML
            + b"""  - name: Loki
    type: loki
    url: http://loki:3100
"""
        )

        self.assertIsNotNone(sync.validate_desired(desired_set(datasource=anonymous)))

    def test_a_provider_pointing_at_the_wrong_container_path_is_refused(self) -> None:
        wrong_path = PROVIDER_YML.replace(
            b"path: /etc/grafana/provisioning/dashboards", b"path: /var/lib/grafana/dashboards"
        )

        problem = sync.validate_desired(desired_set(dashboard_provider=wrong_path))

        self.assertIsNotNone(problem)
        self.assertIn("dashboards/dashboards.yml", str(problem))

    def test_a_provider_file_without_providers_is_refused(self) -> None:
        self.assertIsNotNone(
            sync.validate_desired(desired_set(dashboard_provider=b"apiVersion: 1\nproviders: []\n"))
        )

    def test_unparseable_dashboard_json_is_refused(self) -> None:
        problem = sync.validate_desired(desired_set(dashboard=b"{not json"))

        self.assertIsNotNone(problem)
        self.assertIn("alphalens-cron-health.json", str(problem))

    def test_a_dashboard_without_a_uid_is_refused(self) -> None:
        no_uid = json.dumps({"title": "T", "panels": []}).encode("utf-8")

        self.assertIsNotNone(sync.validate_desired(desired_set(dashboard=no_uid)))

    def test_a_dashboard_referencing_an_undeclared_datasource_is_refused(self) -> None:
        # This is the check that would have caught 2026-08-24 at the gate
        # instead of in the UI: the dashboard asks for a uid nothing provides.
        dangling = DASHBOARD_JSON.replace(b'"uid": "prometheus"', b'"uid": "prom-old"')

        problem = sync.validate_desired(desired_set(dashboard=dangling))

        self.assertIsNotNone(problem)
        self.assertIn("prom-old", str(problem))

    def test_template_variable_datasource_references_are_not_cross_checked(self) -> None:
        # "${DS_PROM}" is resolved by Grafana at import time from the dashboard
        # inputs, not by our provisioning tree — refusing it would be wrong.
        templated = DASHBOARD_JSON.replace(b'"uid": "prometheus"', b'"uid": "${DS_PROM}"')

        self.assertIsNone(sync.validate_desired(desired_set(dashboard=templated)))


class TestReferencedDatasourceUids(unittest.TestCase):
    def test_uids_are_collected_from_panels_and_targets(self) -> None:
        doc = {
            "panels": [
                {
                    "datasource": {"type": "prometheus", "uid": "a"},
                    "targets": [{"datasource": {"type": "prometheus", "uid": "b"}}],
                }
            ],
            "templating": {"list": [{"datasource": {"type": "prometheus", "uid": "c"}}]},
        }

        self.assertEqual(sync.referenced_datasource_uids(doc), {"a", "b", "c"})

    def test_a_dashboard_with_no_datasource_references_yields_an_empty_set(self) -> None:
        self.assertEqual(sync.referenced_datasource_uids({"panels": []}), set())


class TestBuildMetrics(unittest.TestCase):
    """Zero-initialised one-hot: absence must mean broken emitter, never a state."""

    def test_every_outcome_label_is_emitted_every_run_zeros_included(self) -> None:
        metrics = sync.build_metrics(sync.Outcome.CHECK_FAILED)

        self.assertEqual(len(metrics), 5)
        for outcome in sync.Outcome:
            self.assertIn(f'{sync.OUTCOME_METRIC}{{outcome="{outcome.value}"}}', metrics)
        self.assertEqual(sum(metrics.values()), 1)
        self.assertEqual(metrics[f'{sync.OUTCOME_METRIC}{{outcome="check_failed"}}'], 1)

    def test_the_outcome_family_is_the_only_emitted_metric(self) -> None:
        # Job-level staleness comes from the unit's ExecStopPost hook; a
        # script-side timestamp would be an unconsumed duplicate. Pinning the
        # EXACT key set kills any silent metric-name drift too.
        expected = {f'{sync.OUTCOME_METRIC}{{outcome="{o.value}"}}' for o in sync.Outcome}
        for outcome in sync.Outcome:
            with self.subTest(outcome=outcome):
                self.assertEqual(set(sync.build_metrics(outcome)), expected)


class TestLiveTree(unittest.TestCase):
    """Real filesystem operations against a real temporary directory."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.live = sync.LiveTree(self.dir)
        self.relpath = DASHBOARD_FILE.live_relpath

    def test_read_live_returns_none_when_the_file_is_missing(self) -> None:
        self.assertIsNone(self.live.read_live(self.relpath))

    def test_read_live_returns_bytes(self) -> None:
        path = self.live.live_path(self.relpath)
        path.parent.mkdir(parents=True)
        path.write_bytes(STALE)

        self.assertEqual(self.live.read_live(self.relpath), STALE)

    def test_write_temp_lands_next_to_the_target_world_readable(self) -> None:
        # Grafana reads the bind-mounted files as its own in-container user; a
        # 0600 file would be unreadable and provisioning would silently skip it.
        name = self.live.write_temp(self.relpath, DASHBOARD_JSON)

        temp_path = self.live.live_path(self.relpath).parent / name
        self.assertTrue(temp_path.exists())
        self.assertEqual(temp_path.read_bytes(), DASHBOARD_JSON)
        self.assertEqual(temp_path.stat().st_mode & 0o777, 0o644)

    def test_the_temp_name_is_never_a_file_grafana_would_provision(self) -> None:
        for managed in sync.MANAGED_FILES:
            name = self.live.write_temp(managed.live_relpath, b"x")
            with self.subTest(name=name):
                self.assertFalse(name.endswith((".json", ".yml", ".yaml")))
                self.assertNotEqual(name, managed.live_name)
                self.assertTrue(name.startswith("."))

    def test_backup_live_copies_the_current_content(self) -> None:
        path = self.live.live_path(self.relpath)
        path.parent.mkdir(parents=True)
        path.write_bytes(STALE)

        self.live.backup_live(self.relpath, "20260824T090000Z")

        backup = path.parent / f"{sync.backup_prefix(DASHBOARD_FILE.live_name)}20260824T090000Z"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), STALE)

    def test_backup_live_is_a_noop_when_live_is_missing(self) -> None:
        self.assertIsNone(self.live.backup_live(self.relpath, "20260824T090000Z"))

    def test_prune_keeps_the_newest_ten_autosync_backups(self) -> None:
        directory = self.live.live_path(self.relpath).parent
        directory.mkdir(parents=True)
        prefix = sync.backup_prefix(DASHBOARD_FILE.live_name)
        for hour in range(12):
            (directory / f"{prefix}20260824T{hour:02d}0000Z").write_bytes(b"old")

        self.live.prune_autosync_backups(self.relpath, sync.BACKUP_KEEP)

        kept = sorted(p.name for p in directory.iterdir())
        self.assertEqual(len(kept), 10)
        self.assertNotIn(f"{prefix}20260824T000000Z", kept)
        self.assertNotIn(f"{prefix}20260824T010000Z", kept)
        self.assertIn(f"{prefix}20260824T110000Z", kept)

    def test_prune_never_touches_foreign_files(self) -> None:
        directory = self.live.live_path(self.relpath).parent
        directory.mkdir(parents=True)
        self.live.live_path(self.relpath).write_bytes(b"live")
        for name in FOREIGN_FILES:
            (directory / name).write_bytes(b"foreign")
        prefix = sync.backup_prefix(DASHBOARD_FILE.live_name)
        for hour in range(12):
            (directory / f"{prefix}20260824T{hour:02d}0000Z").write_bytes(b"old")

        self.live.prune_autosync_backups(self.relpath, sync.BACKUP_KEEP)

        survivors = {p.name for p in directory.iterdir()}
        for name in FOREIGN_FILES:
            self.assertIn(name, survivors)
            self.assertEqual((directory / name).read_bytes(), b"foreign")
        # The managed file itself must survive any prune-prefix loosening.
        self.assertIn(DASHBOARD_FILE.live_name, survivors)
        self.assertEqual(self.live.live_path(self.relpath).read_bytes(), b"live")

    def test_prune_of_one_managed_file_spares_another_managed_files_backups(self) -> None:
        # dashboards.yml and alphalens-cron-health.json share ONE directory.
        directory = self.live.live_path(self.relpath).parent
        directory.mkdir(parents=True)
        sibling = f"{sync.backup_prefix(PROVIDER_FILE.live_name)}20260101T000000Z"
        (directory / sibling).write_bytes(b"sibling")
        prefix = sync.backup_prefix(DASHBOARD_FILE.live_name)
        for hour in range(12):
            (directory / f"{prefix}20260824T{hour:02d}0000Z").write_bytes(b"old")

        self.live.prune_autosync_backups(self.relpath, sync.BACKUP_KEEP)

        self.assertTrue((directory / sibling).exists())

    def test_prune_of_the_provider_spares_a_dashboard_backup(self) -> None:
        # The MIRROR of the test above, and the direction that actually
        # tests the code: "alphalens-cron-health.json..." sorts BELOW
        # "dashboards.yml...", so pruning the dashboard relpath spares its
        # provider sibling by alphabet even when the prefix filter is
        # loosened to the shared infix. Pruning the PROVIDER relpath is the
        # direction where only the per-file prefix can save the sibling.
        provider_relpath = PROVIDER_FILE.live_relpath
        directory = self.live.live_path(provider_relpath).parent
        directory.mkdir(parents=True, exist_ok=True)
        sibling = f"{sync.backup_prefix(DASHBOARD_FILE.live_name)}20260824T110000Z"
        (directory / sibling).write_bytes(b"sibling")
        prefix = sync.backup_prefix(PROVIDER_FILE.live_name)
        for hour in range(12):
            (directory / f"{prefix}20260824T{hour:02d}0000Z").write_bytes(b"old")

        self.live.prune_autosync_backups(provider_relpath, sync.BACKUP_KEEP)

        self.assertTrue((directory / sibling).exists())
        self.assertEqual((directory / sibling).read_bytes(), b"sibling")

    def test_prune_tolerates_a_missing_directory(self) -> None:
        self.live.prune_autosync_backups(self.relpath, sync.BACKUP_KEEP)  # must not raise

    def test_replace_installs_the_new_content_and_records_the_relpath(self) -> None:
        name = self.live.write_temp(self.relpath, DASHBOARD_JSON)

        self.live.replace_live_with_temp(self.relpath, name)

        self.assertEqual(self.live.live_path(self.relpath).read_bytes(), DASHBOARD_JSON)
        self.assertFalse((self.live.live_path(self.relpath).parent / name).exists())
        self.assertEqual(self.live.replaced, [self.relpath])

    def test_remove_temp_tolerates_a_missing_file(self) -> None:
        self.live.remove_temp(self.relpath, "never-created.tmp")  # must not raise


class FakeGit:
    """Injected git port: canned blob content per repo path, optional failures."""

    def __init__(
        self,
        content: dict[sync.ManagedFile, bytes] | None = None,
        *,
        fail_fetch: bool = False,
        fail_show: bool = False,
    ):
        blobs = desired_set() if content is None else content
        self._by_path = {managed.repo_path: blob for managed, blob in blobs.items()}
        self._fail_fetch = fail_fetch
        self._fail_show = fail_show
        self.fetch_calls = 0
        self.shown: list[str] = []

    def fetch(self) -> None:
        self.fetch_calls += 1
        if self._fail_fetch:
            raise sync.GitCommandError("fetch: could not resolve host")

    def show(self, repo_path: str) -> bytes:
        self.shown.append(repo_path)
        if self._fail_show:
            raise sync.GitCommandError("show: path not in origin/main")
        return self._by_path[repo_path]


class RecordingEmit:
    def __init__(self, *, fail: bool = False):
        self._fail = fail
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, job: str, metrics: dict) -> None:
        if self._fail:
            raise OSError("textfile dir unwriteable")
        self.calls.append((job, dict(metrics)))

    def outcome(self) -> str:
        """The one-hot label the single emitted run reported."""
        assert len(self.calls) == 1, self.calls
        _job, metrics = self.calls[0]
        hot = [key for key, value in metrics.items() if value == 1]
        assert len(hot) == 1, metrics
        return hot[0].split('"')[1]


class FakeGrafana:
    """Injected Grafana port. Nothing here raises — a ``False``/``None`` answer
    is already the loud arm (``reload_failed``)."""

    def __init__(
        self,
        *,
        restart_ok: bool = True,
        healthy: bool = True,
        uids: set[str] | None = None,
        checksums: dict[str, str] | None = None,
        uid_sequence: list[set[str] | None] | None = None,
        checksum_sequence: list[dict[str, str] | None] | None = None,
        health_sequence: list[bool] | None = None,
        events: list[str] | None = None,
    ):
        self._restart_ok = restart_ok
        self._healthy = healthy
        self._uids = {sync.REQUIRED_DATASOURCE_UID} if uids is None else set(uids)
        self._checksums = checksums
        self._uid_sequence = uid_sequence
        self._checksum_sequence = checksum_sequence
        self._health_sequence = health_sequence
        self.events = [] if events is None else events
        self.restart_calls = 0
        self.health_calls = 0
        self.uid_calls = 0
        self.checksum_calls = 0

    @staticmethod
    def _from_sequence(sequence: list, index: int):
        return sequence[min(index, len(sequence) - 1)]

    def restart(self) -> bool:
        self.restart_calls += 1
        self.events.append("restart")
        return self._restart_ok

    def health_ok(self) -> bool:
        self.health_calls += 1
        self.events.append("health")
        if self._health_sequence is not None:
            return self._from_sequence(self._health_sequence, self.health_calls - 1)
        return self._healthy

    def datasource_uids(self) -> set[str] | None:
        self.uid_calls += 1
        self.events.append("uids")
        if self._uid_sequence is not None:
            return self._from_sequence(self._uid_sequence, self.uid_calls - 1)
        return set(self._uids)

    def provisioned_checksums(self) -> dict[str, str] | None:
        self.checksum_calls += 1
        self.events.append("checksums")
        if self._checksum_sequence is not None:
            return self._from_sequence(self._checksum_sequence, self.checksum_calls - 1)
        if self._checksums is not None:
            return dict(self._checksums)
        return sync.dashboard_checksums(desired_set())


class RecordingLiveTree(sync.LiveTree):
    """Real LiveTree that also appends each mutation to a shared event list."""

    def __init__(self, path: Path, events: list[str]):
        super().__init__(path)
        self.events = events

    def backup_live(self, relpath: str, stamp: str):
        self.events.append(f"backup:{relpath}")
        return super().backup_live(relpath, stamp)

    def prune_autosync_backups(self, relpath: str, keep: int) -> None:
        self.events.append(f"prune:{relpath}")
        return super().prune_autosync_backups(relpath, keep)

    def write_temp(self, relpath: str, content: bytes) -> str:
        self.events.append(f"write_temp:{relpath}")
        return super().write_temp(relpath, content)

    def replace_live_with_temp(self, relpath: str, name: str) -> None:
        self.events.append(f"replace:{relpath}")
        return super().replace_live_with_temp(relpath, name)


class RunTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.live = sync.LiveTree(self.dir)
        self.emit = RecordingEmit()
        self.grafana = FakeGrafana()
        self.slept: list[float] = []

    def seed_live(self, content: dict[sync.ManagedFile, bytes]) -> None:
        for managed, blob in content.items():
            path = self.live.live_path(managed.live_relpath)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)

    def live_names(self) -> set[str]:
        return {str(p.relative_to(self.dir)) for p in self.dir.rglob("*") if p.is_file()}

    def _run(self, *, git=None, live=None, grafana=None, dry_run=False) -> int:
        return sync.run(
            git=git or FakeGit(),
            live=live or self.live,
            grafana=grafana or self.grafana,
            emit=self.emit,
            now_fn=lambda: NOW_TS,
            sleep_fn=self.slept.append,
            dry_run=dry_run,
        )


class TestRun(RunTestBase):
    """Every outcome arm of the run loop, against a real live tree."""

    def test_in_sync_performs_zero_side_effects_beyond_the_metric(self) -> None:
        self.seed_live(desired_set())

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "in_sync")
        self.assertEqual(
            self.live_names(), {managed.live_relpath for managed in sync.MANAGED_FILES}
        )
        self.assertEqual(self.live.replaced, [])

    def test_in_sync_emits_exactly_the_outcome_family(self) -> None:
        self.seed_live(desired_set())

        self._run()

        job, metrics = self.emit.calls[0]
        self.assertEqual(job, sync.JOB_NAME)
        expected = {f'{sync.OUTCOME_METRIC}{{outcome="{o.value}"}}' for o in sync.Outcome}
        self.assertEqual(set(metrics), expected)

    def test_synced_installs_every_differing_file_and_backs_up_the_old(self) -> None:
        self.seed_live(stale_tree())

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        for managed, blob in desired_set().items():
            self.assertEqual(self.live.read_live(managed.live_relpath), blob)
            backup = (
                self.live.live_path(managed.live_relpath).parent
                / f"{sync.backup_prefix(managed.live_name)}{sync.utc_stamp(NOW_TS)}"
            )
            self.assertEqual(backup.read_bytes(), STALE)

    def test_only_the_differing_files_are_touched(self) -> None:
        # A dashboard-only change must not rewrite (or back up) the datasource
        # yml — on the VPS that file's replacement is what costs a restart.
        content = desired_set()
        content[DASHBOARD_FILE] = STALE
        self.seed_live(content)

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.live.replaced, [DASHBOARD_FILE.live_relpath])
        backups = {name for name in self.live_names() if sync.BACKUP_INFIX in name}
        self.assertEqual(
            backups,
            {f"dashboards/{sync.backup_prefix(DASHBOARD_FILE.live_name)}{sync.utc_stamp(NOW_TS)}"},
        )

    def test_sync_step_order_is_backup_prune_write_replace_per_file(self) -> None:
        events: list[str] = []
        live = RecordingLiveTree(self.dir, events)
        self.live = live
        self.seed_live(stale_tree())

        self._run(live=live)

        relpath = DATASOURCE_FILE.live_relpath
        self.assertEqual(
            events[:4],
            [
                f"backup:{relpath}",
                f"prune:{relpath}",
                f"write_temp:{relpath}",
                f"replace:{relpath}",
            ],
        )
        self.assertEqual(len(events), 4 * len(sync.MANAGED_FILES))

    def test_sync_reports_which_files_changed(self) -> None:
        # The changed set is the seam the restart decision hangs off: only a
        # datasource replacement may cost a Grafana container restart.
        self.seed_live({DASHBOARD_FILE: STALE})

        outcome, changed = sync._sync(
            desired_set(),
            differing=[DASHBOARD_FILE],
            live=self.live,
            grafana=self.grafana,
            now_fn=lambda: NOW_TS,
            sleep_fn=self.slept.append,
        )

        self.assertEqual(outcome, sync.Outcome.SYNCED)
        self.assertEqual(changed, (DASHBOARD_FILE,))

    def test_fetch_failure_reports_fetch_failed_and_touches_nothing(self) -> None:
        self.seed_live(stale_tree())

        rc = self._run(git=FakeGit(fail_fetch=True))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "fetch_failed")
        self.assertEqual(
            self.live_names(), {managed.live_relpath for managed in sync.MANAGED_FILES}
        )

    def test_show_failure_is_fetch_failed_too(self) -> None:
        rc = self._run(git=FakeGit(fail_show=True))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "fetch_failed")

    def test_a_refused_blob_leaves_the_whole_tree_byte_identical(self) -> None:
        # The gate runs over the WHOLE desired set before any replace, so one
        # bad file cannot land a half-applied provisioning tree.
        self.seed_live(stale_tree())
        broken = desired_set(datasource=DATASOURCE_YML.replace(b"    uid: prometheus\n", b""))

        rc = self._run(git=FakeGit(broken))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "check_failed")
        self.assertEqual(self.live.replaced, [])
        for managed in sync.MANAGED_FILES:
            self.assertEqual(self.live.read_live(managed.live_relpath), STALE)
        # Not even a backup: nothing was touched.
        self.assertEqual(
            self.live_names(), {managed.live_relpath for managed in sync.MANAGED_FILES}
        )

    def test_missing_live_files_sync_without_inventing_a_backup(self) -> None:
        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        self.assertEqual(
            self.live_names(), {managed.live_relpath for managed in sync.MANAGED_FILES}
        )

    def test_a_failing_metric_emit_does_not_change_the_exit_code(self) -> None:
        self.seed_live(desired_set())
        self.emit = RecordingEmit(fail=True)

        self.assertEqual(self._run(), 0)

    def test_dry_run_on_identical_content_prints_and_writes_nothing(self) -> None:
        self.seed_live(desired_set())

        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self._run(dry_run=True)

        self.assertEqual(rc, 0)
        self.assertIn("in_sync", out.getvalue())
        self.assertEqual(self.emit.calls, [])  # a metric is a write too

    def test_dry_run_on_differing_content_reports_but_never_writes(self) -> None:
        self.seed_live(stale_tree())

        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self._run(dry_run=True)

        self.assertEqual(rc, 0)
        self.assertIn("would sync", out.getvalue())
        self.assertIn(DATASOURCE_FILE.live_relpath, out.getvalue())
        self.assertEqual(
            self.live_names(), {managed.live_relpath for managed in sync.MANAGED_FILES}
        )
        self.assertEqual(self.emit.calls, [])

    def test_dry_run_still_fails_loudly_when_the_fetch_fails(self) -> None:
        rc = self._run(git=FakeGit(fail_fetch=True), dry_run=True)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.calls, [])


class TestRestartPolicy(RunTestBase):
    """A container restart is a real (if brief) Grafana outage, so it is spent
    only where it BUYS something.

    Measured on the VPS 2026-08-24: the dashboards provider declares
    ``updateIntervalSeconds: 10``, and the alphalens dashboard's DB row was
    created 2026-05-30 — between the 2026-05-19 and 2026-08-01 container
    starts. So a dashboard JSON is picked up by the provisioning watcher with
    NO restart, and paying one for a dashboard edit would be pure downtime.

    Datasources are provisioned once at startup, so their file needs the
    restart. The provider yml is treated the same way: Grafana reads the
    provider CONFIG during the startup provisioning pass and the watcher it
    then starts follows the dashboards DIRECTORY, not the config that
    described it. That one is reasoned, not measured — and it fails safe, an
    unnecessary restart rather than a change that silently never applies.
    """

    def test_a_changed_datasource_restarts_the_container_exactly_once(self) -> None:
        content = desired_set()
        content[DATASOURCE_FILE] = STALE
        self.seed_live(content)

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        self.assertEqual(self.grafana.restart_calls, 1)

    def test_a_changed_dashboard_never_restarts_the_container(self) -> None:
        content = desired_set()
        content[DASHBOARD_FILE] = STALE
        self.seed_live(content)

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        self.assertEqual(self.grafana.restart_calls, 0)
        self.assertEqual(self.grafana.health_calls, 0)

    def test_a_changed_provider_config_restarts_the_container(self) -> None:
        content = desired_set()
        content[PROVIDER_FILE] = STALE
        self.seed_live(content)

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.grafana.restart_calls, 1)

    def test_an_in_sync_run_never_restarts_the_container(self) -> None:
        self.seed_live(desired_set())

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "in_sync")
        self.assertEqual(self.grafana.restart_calls, 0)
        self.assertEqual(self.grafana.uid_calls, 0)
        self.assertEqual(self.grafana.checksum_calls, 0)

    def test_a_refused_blob_never_restarts_the_container(self) -> None:
        self.seed_live(stale_tree())
        broken = desired_set(datasource=DATASOURCE_YML.replace(b"    uid: prometheus\n", b""))

        rc = self._run(git=FakeGit(broken))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "check_failed")
        self.assertEqual(self.grafana.restart_calls, 0)

    def test_a_failed_restart_is_reload_failed_never_a_silent_success(self) -> None:
        self.seed_live(stale_tree())
        grafana = FakeGrafana(restart_ok=False)

        rc = self._run(grafana=grafana)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        # The new content IS on disk; the outcome says exactly that Grafana was
        # never confirmed to be serving it.
        self.assertEqual(self.live.read_live(DATASOURCE_FILE.live_relpath), DATASOURCE_YML)
        # No point polling a container that refused to come back.
        self.assertEqual(grafana.uid_calls, 0)

    def test_the_restart_happens_after_every_file_is_installed(self) -> None:
        # A restart mid-install would boot Grafana against a half-applied tree.
        events: list[str] = []
        live = RecordingLiveTree(self.dir, events)
        self.live = live
        self.grafana = FakeGrafana(events=events)
        self.seed_live(stale_tree())

        self._run(live=live)

        self.assertEqual(events.index("restart"), 4 * len(sync.MANAGED_FILES))
        self.assertLess(events.index("restart"), events.index("uids"))


class TestVerification(RunTestBase):
    """``synced`` means Grafana was OBSERVED serving the new content.

    The admin API cannot say so — the compose file's admin password is a dead
    placeholder and Grafana persists its first-init credentials, so
    ``/api/datasources`` answers 401. The unauthenticated ``/api/health`` and
    the provisioning state Grafana itself writes into ``grafana.db`` are the
    two surfaces that remain, and both are read-only.
    """

    def _restarting_run(self, **kwargs) -> int:
        self.seed_live(stale_tree())
        return self._run(**kwargs)

    def test_the_happy_path_polls_health_uids_and_checksums_once(self) -> None:
        rc = self._restarting_run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        self.assertEqual(
            (self.grafana.health_calls, self.grafana.uid_calls, self.grafana.checksum_calls),
            (1, 1, 1),
        )
        self.assertEqual(self.slept, [])

    def test_a_restart_that_never_becomes_healthy_is_reload_failed(self) -> None:
        grafana = FakeGrafana(healthy=False)

        rc = self._restarting_run(grafana=grafana)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        self.assertEqual(grafana.health_calls, sync.VERIFY_ATTEMPTS)
        self.assertEqual(len(self.slept), sync.VERIFY_ATTEMPTS - 1)
        self.assertEqual(set(self.slept), {sync.VERIFY_DELAY_S})

    def test_a_restart_that_comes_back_without_the_uid_is_reload_failed(self) -> None:
        # The 2026-08-24 shape, caught by the instrument rather than by a human
        # noticing every panel reads "No data".
        grafana = FakeGrafana(uids={"prom-old"})

        rc = self._restarting_run(grafana=grafana)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        self.assertEqual(grafana.uid_calls, sync.VERIFY_ATTEMPTS)

    def test_an_unreadable_datasource_table_fails_closed(self) -> None:
        grafana = FakeGrafana(uid_sequence=[None])

        rc = self._restarting_run(grafana=grafana)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")

    def test_a_dashboard_grafana_never_ingested_is_reload_failed(self) -> None:
        # File on disk, watcher never picked it up: the stored checksum still
        # fingerprints the OLD bytes.
        grafana = FakeGrafana(checksums=sync.dashboard_checksums({DASHBOARD_FILE: STALE}))

        rc = self._restarting_run(grafana=grafana)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        self.assertEqual(grafana.checksum_calls, sync.VERIFY_ATTEMPTS)

    def test_an_unreadable_provisioning_table_fails_closed(self) -> None:
        grafana = FakeGrafana(checksum_sequence=[None])

        rc = self._restarting_run(grafana=grafana)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")

    def test_verification_succeeding_on_a_retry_is_synced(self) -> None:
        # The watcher polls on its own 10s schedule, so the first read races it.
        grafana = FakeGrafana(
            checksum_sequence=[{}, sync.dashboard_checksums(desired_set())],
        )

        rc = self._restarting_run(grafana=grafana)

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        self.assertEqual(self.slept, [sync.VERIFY_DELAY_S])

    def test_the_verify_budget_outlasts_the_provisioning_watcher_interval(self) -> None:
        # updateIntervalSeconds is 10 on the VPS; a budget under that would
        # report reload_failed on every healthy dashboard deploy.
        self.assertGreater(sync.VERIFY_ATTEMPTS * sync.VERIFY_DELAY_S, 10.0)

    def test_a_dashboard_only_change_is_verified_by_checksum_not_by_health(self) -> None:
        # No restart means nothing to probe for liveness, but the run still
        # must not claim synced without evidence Grafana ingested the file.
        content = desired_set()
        content[DASHBOARD_FILE] = STALE
        self.seed_live(content)
        grafana = FakeGrafana(checksums=sync.dashboard_checksums({DASHBOARD_FILE: STALE}))

        rc = self._run(grafana=grafana)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        self.assertEqual(grafana.health_calls, 0)
        self.assertEqual(grafana.uid_calls, 0)

    def test_dashboard_checksums_are_keyed_by_container_path(self) -> None:
        # Grafana stores dashboard_provisioning.external_id as the path it read
        # the file from INSIDE the container, and check_sum as md5 of the bytes
        # (both measured against the live DB on 2026-08-24).
        checksums = sync.dashboard_checksums(desired_set())

        self.assertEqual(
            checksums,
            {
                f"{sync.PROVISIONED_DASHBOARD_DIR}/{DASHBOARD_FILE.live_name}": hashlib.md5(
                    DASHBOARD_JSON, usedforsecurity=False
                ).hexdigest()
            },
        )


class TestRunHardening(RunTestBase):
    """Unexpected write-path errors must still land in a metric-emitting
    outcome, phase-mapped by whether anything was already installed."""

    def test_unexpected_error_before_any_replace_is_check_failed(self) -> None:
        class DiskFullTree(sync.LiveTree):
            def backup_live(self, relpath: str, stamp: str):
                raise OSError("no space left on device")

        live = DiskFullTree(self.dir)
        self.live = live
        self.seed_live(stale_tree())

        rc = self._run(live=live)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "check_failed")
        for managed in sync.MANAGED_FILES:
            self.assertEqual(live.read_live(managed.live_relpath), STALE)

    def test_unexpected_error_after_a_replace_is_reload_failed(self) -> None:
        # Half the tree is installed, so the honest report is "content is on
        # disk and Grafana was never confirmed to have picked it up".
        class FailAfterFirstTree(sync.LiveTree):
            def write_temp(self, relpath: str, content: bytes) -> str:
                if self.replaced:
                    raise OSError("no space left on device")
                return super().write_temp(relpath, content)

        live = FailAfterFirstTree(self.dir)
        self.live = live
        self.seed_live(stale_tree())

        rc = self._run(live=live)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        self.assertEqual(live.read_live(DATASOURCE_FILE.live_relpath), DATASOURCE_YML)

    def test_no_temp_file_is_left_behind_when_the_replace_fails(self) -> None:
        # The live dirs are SHARED with a foreign tenant's dashboard - hidden
        # .tmp files must never accumulate there.
        class RaisingReplaceTree(sync.LiveTree):
            def replace_live_with_temp(self, relpath: str, name: str) -> None:
                raise OSError("read-only file system")

        live = RaisingReplaceTree(self.dir)
        self.live = live
        self.seed_live(stale_tree())

        rc = self._run(live=live)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "check_failed")
        leftovers = [name for name in self.live_names() if Path(name).name.startswith(".")]
        self.assertEqual(leftovers, [])


class FakeRunner:
    """Stands in for ``subprocess.run``. Records argv; returns canned output."""

    def __init__(
        self, stdout: bytes | str = b"", *, returncode: int = 0, stderr: bytes | str = b""
    ):
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            args=argv, returncode=self._returncode, stdout=self._stdout, stderr=self._stderr
        )


class TestGitCliAdapter(unittest.TestCase):
    """The argv shapes: origin/main blob, never the working tree."""

    def test_fetch_argv(self) -> None:
        runner = FakeRunner()

        sync.GitCli("/home/jacoren/AlphaLens", runner=runner).fetch()

        self.assertEqual(
            runner.calls[0],
            ["git", "-C", "/home/jacoren/AlphaLens", "fetch", "origin", "main"],
        )

    def test_show_reads_the_origin_main_blob_not_the_working_tree(self) -> None:
        # The stale-checkout trap: the content source must be the fetched ref,
        # so a stale or dirty checkout on the VPS cannot reach the live tree.
        runner = FakeRunner(DASHBOARD_JSON)
        cli = sync.GitCli("/home/jacoren/AlphaLens", runner=runner)

        for managed in sync.MANAGED_FILES:
            content = cli.show(managed.repo_path)

            self.assertEqual(content, DASHBOARD_JSON)
            self.assertEqual(
                runner.calls[-1],
                [
                    "git",
                    "-C",
                    "/home/jacoren/AlphaLens",
                    "show",
                    f"origin/main:{managed.repo_path}",
                ],
            )

    def test_nonzero_exit_raises_with_gits_own_stderr(self) -> None:
        runner = FakeRunner(returncode=128, stderr=b"fatal: could not resolve host")

        with self.assertRaises(sync.GitCommandError) as caught:
            sync.GitCli("/repo", runner=runner).fetch()

        self.assertIn("could not resolve host", str(caught.exception))

    def test_a_hung_git_raises_git_command_error_not_a_timeout(self) -> None:
        def hung_runner(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, 120)

        with self.assertRaises(sync.GitCommandError):
            sync.GitCli("/repo", runner=hung_runner).fetch()


def write_grafana_db(path: Path) -> None:
    """A grafana.db carrying the live table shapes measured on 2026-08-24.

    ``data_source`` really does hold ``password``, ``basic_auth_password`` and
    ``secure_json_data``, so the fixture carries them: the reader must select
    named non-secret columns, never ``*``.
    """
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE data_source (id INTEGER, uid TEXT, name TEXT, type TEXT, "
            "url TEXT, password TEXT, basic_auth_password TEXT, secure_json_data TEXT)"
        )
        conn.execute(
            "INSERT INTO data_source VALUES (1, 'prometheus', 'Prometheus', 'prometheus', "
            "'http://prometheus:9090', 'TOP-SECRET', 'TOP-SECRET', 'TOP-SECRET')"
        )
        conn.execute(
            "CREATE TABLE dashboard_provisioning (id INTEGER, dashboard_id INTEGER, "
            "name TEXT, external_id TEXT, updated INTEGER, check_sum TEXT)"
        )
        conn.executemany(
            "INSERT INTO dashboard_provisioning VALUES (?, ?, 'Default', ?, ?, ?)",
            [
                (
                    1,
                    1,
                    "/etc/grafana/provisioning/dashboards/node-exporter-dashboard.json",
                    1746639200,
                    "83a94c843a9e558cadeca67b1d3dc4a6",
                ),
                (
                    2,
                    2,
                    "/etc/grafana/provisioning/dashboards/alphalens-cron-health.json",
                    1787575599,
                    "f0100f542a6d3dc27cdb1604933f5f27",
                ),
            ],
        )
        conn.commit()


class TestDockerGrafanaAdapter(unittest.TestCase):
    """The only code that talks to docker and to Grafana."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "fixture.db"
        write_grafana_db(self.db)

    def _adapter(self, **kwargs) -> sync.DockerGrafana:
        def copy_db(target: Path) -> bool:
            shutil.copy(self.db, target)
            return True

        kwargs.setdefault("copy_db", copy_db)
        return sync.DockerGrafana(**kwargs)

    def test_restart_argv_addresses_the_container_by_name(self) -> None:
        runner = FakeRunner()

        self.assertTrue(self._adapter(runner=runner).restart())

        self.assertEqual(runner.calls[0], ["docker", "restart", sync.CONTAINER_NAME])

    def test_a_nonzero_restart_is_false_not_an_exception(self) -> None:
        runner = FakeRunner(returncode=1, stderr=b"No such container: grafana")

        self.assertFalse(self._adapter(runner=runner).restart())

    def test_a_docker_that_never_returns_is_false(self) -> None:
        def hung(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        self.assertFalse(self._adapter(runner=hung).restart())

    def test_health_is_ok_only_when_grafana_reports_its_database_ok(self) -> None:
        healthy = self._adapter(
            fetch_json=lambda url: {"database": "ok", "version": "12.0.0"}
        ).health_ok()
        degraded = self._adapter(fetch_json=lambda url: {"database": "failing"}).health_ok()

        self.assertTrue(healthy)
        self.assertFalse(degraded)

    def test_health_reads_the_unauthenticated_endpoint(self) -> None:
        seen: list[str] = []

        def fetch(url: str) -> dict:
            seen.append(url)
            return {"database": "ok"}

        self._adapter(fetch_json=fetch).health_ok()

        self.assertEqual(seen, [sync.HEALTH_URL])

    def test_an_unreachable_health_endpoint_is_false(self) -> None:
        def boom(_url: str) -> dict:
            raise OSError("connection refused")

        self.assertFalse(self._adapter(fetch_json=boom).health_ok())

    def test_datasource_uids_come_from_the_database_without_any_secret(self) -> None:
        uids = self._adapter().datasource_uids()

        self.assertEqual(uids, {"prometheus"})
        self.assertNotIn("TOP-SECRET", repr(uids))

    def test_provisioned_checksums_are_keyed_by_container_path(self) -> None:
        checksums = self._adapter().provisioned_checksums()

        self.assertEqual(
            checksums,
            {
                "/etc/grafana/provisioning/dashboards/node-exporter-dashboard.json": (
                    "83a94c843a9e558cadeca67b1d3dc4a6"
                ),
                "/etc/grafana/provisioning/dashboards/alphalens-cron-health.json": (
                    "f0100f542a6d3dc27cdb1604933f5f27"
                ),
            },
        )

    def test_the_queries_never_select_a_secret_column(self) -> None:
        # data_source carries password / basic_auth_password / secure_json_data;
        # a SELECT * here would put credentials into a log line one exception
        # away from the journal.
        for statement in (sync.DATASOURCE_UID_SQL, sync.DASHBOARD_PROVISIONING_SQL):
            with self.subTest(sql=statement):
                self.assertNotIn("*", statement)
                self.assertNotIn("password", statement.lower())
                self.assertNotIn("secure", statement.lower())

    def test_a_failed_database_copy_reads_none_rather_than_raising(self) -> None:
        adapter = sync.DockerGrafana(copy_db=lambda _target: False)

        self.assertIsNone(adapter.datasource_uids())
        self.assertIsNone(adapter.provisioned_checksums())

    def test_a_corrupt_database_reads_none_rather_than_raising(self) -> None:
        def copy_garbage(target: Path) -> bool:
            target.write_bytes(b"not a sqlite file")
            return True

        adapter = sync.DockerGrafana(copy_db=copy_garbage)

        self.assertIsNone(adapter.datasource_uids())

    def test_the_default_copy_streams_the_database_out_of_the_container(self) -> None:
        runner = FakeRunner()
        adapter = sync.DockerGrafana(runner=runner)

        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "grafana.db"
            adapter._docker_cp(target)

        self.assertEqual(
            runner.calls[0],
            [
                "docker",
                "cp",
                f"{sync.CONTAINER_NAME}:{sync.CONTAINER_DB_PATH}",
                str(target),
            ],
        )


class TestMainWiring(unittest.TestCase):
    """The wake-timer lesson: an untested main() shipped 10 surviving mutants."""

    def _patch(self, rc: int = 0):
        calls: dict = {}

        def fake_run(**kwargs):
            calls.update(kwargs)
            return rc

        return calls, fake_run

    def test_the_exit_code_of_run_is_returned(self) -> None:
        _calls, fake_run = self._patch(rc=1)

        with mock.patch.object(sync, "run", fake_run):
            self.assertEqual(sync.main(["--dry-run"]), 1)

    def test_dry_run_reaches_run_as_true_and_defaults_to_false(self) -> None:
        calls, fake_run = self._patch()

        with mock.patch.object(sync, "run", fake_run):
            sync.main(["--dry-run"])
        self.assertTrue(calls["dry_run"])

        with mock.patch.object(sync, "run", fake_run):
            sync.main([])
        self.assertFalse(calls["dry_run"])

    def test_repo_and_live_dir_overrides_reach_the_adapters(self) -> None:
        calls, fake_run = self._patch()

        with mock.patch.object(sync, "run", fake_run):
            sync.main(["--repo-dir", "/tmp/repo", "--live-dir", "/tmp/live"])

        self.assertEqual(calls["git"].repo_dir, Path("/tmp/repo"))
        self.assertEqual(calls["live"].path, Path("/tmp/live"))

    def test_defaults_point_at_the_vps_layout(self) -> None:
        calls, fake_run = self._patch()

        with mock.patch.object(sync, "run", fake_run):
            sync.main([])

        self.assertEqual(calls["git"].repo_dir, sync.DEFAULT_REPO_DIR)
        self.assertEqual(calls["live"].path, sync.DEFAULT_LIVE_DIR)

    def test_the_default_metric_emitter_is_wired_in(self) -> None:
        calls, fake_run = self._patch()

        with mock.patch.object(sync, "run", fake_run):
            sync.main([])

        self.assertIs(calls["emit"], sync.default_emit)

    def test_the_grafana_port_is_the_docker_adapter(self) -> None:
        calls, fake_run = self._patch()

        with mock.patch.object(sync, "run", fake_run):
            sync.main([])

        self.assertIsInstance(calls["grafana"], sync.DockerGrafana)


class TestAlertMetricParity(unittest.TestCase):
    """The rule that pages on this job must name the metric this job emits.

    Renaming ``OUTCOME_METRIC`` or changing which outcomes count as success on
    ONE side alone turns this red — otherwise the alert would silently sum an
    absent series and never fire again, which is the 2026-08-20 rename class.
    """

    def test_the_failure_alert_selects_this_scripts_success_labels(self) -> None:
        rules = (
            REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alphalens.yaml"
        ).read_text()
        labels = "|".join(sorted(outcome.value for outcome in sync.SUCCESS_OUTCOMES))

        self.assertIn(f'{sync.OUTCOME_METRIC}{{outcome=~"{labels}"}}', rules)


if __name__ == "__main__":
    unittest.main()
