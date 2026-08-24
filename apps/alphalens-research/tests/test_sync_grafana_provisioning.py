"""The Grafana provisioning sync: decision core, live-tree ops, and adapters.

The job's contract mirrors the Prometheus rules sync: the live provisioning
tree converges to the ``origin/main`` blobs within one cadence, and every way
the convergence can fail is LOUD — a distinct outcome label in the textfile
metrics plus a non-zero exit. The 2026-08-24 incident shape (the live
datasource yml never declared ``uid: prometheus`` while every dashboard target
referenced that uid, so every panel read "No data") is why the validation gate
parses the datasource yml and REQUIRES that uid, and why it refuses a dashboard
whose targets reference a uid no synced datasource declares.

Nothing here touches git or the network: those are ports, and every test passes
a fake. The live-tree filesystem operations run against a REAL temporary
directory — "check_failed leaves live byte-identical", "prune spares foreign
files" and "no backup or temp name is itself a provisioning file" are claims
about the filesystem, so they are proven on one.
"""

from __future__ import annotations

import contextlib
import io
import json
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

    def seed_live(self, content: dict[sync.ManagedFile, bytes]) -> None:
        for managed, blob in content.items():
            path = self.live.live_path(managed.live_relpath)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)

    def live_names(self) -> set[str]:
        return {str(p.relative_to(self.dir)) for p in self.dir.rglob("*") if p.is_file()}

    def _run(self, *, git=None, live=None, dry_run=False) -> int:
        return sync.run(
            git=git or FakeGit(),
            live=live or self.live,
            emit=self.emit,
            now_fn=lambda: NOW_TS,
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
            now_fn=lambda: NOW_TS,
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


if __name__ == "__main__":
    unittest.main()
