"""The Prometheus live-rules sync: decision core, live-dir ops, and adapters.

The job's contract: live ``alphalens.rules`` converges to the ``origin/main``
blob within one cadence, and every way the convergence can fail is LOUD — a
distinct outcome label in the textfile metrics plus a non-zero exit. The
2026-08-20 incident shape (a metric RENAME leaving live alerts summing absent
series while every alert NAME still matched) is why the reload verification
demands a fingerprint of the NEW content in ``api/v1/rules``, never just a
"reload succeeded" flag.

Nothing here touches git, docker, or the network: those are ports, and every
test passes a fake. The live-dir filesystem operations run against a REAL
temporary directory — "check_failed leaves live byte-identical" and "prune
spares foreign backups" are claims about the filesystem, so they are proven on
one.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import sync_prometheus_rules as sync

NOW_TS = 1_756_000_000.0  # 2026-08-24T01:46:40Z

DESIRED = b"""groups:
  - name: alphalens-cron-health
    rules:
      - alert: AlphalensJobStale
        expr: vector(1)
      - alert: AlphalensRulesSyncBroken
        expr: vector(1)
"""
DESIRED_ALERTS = {"AlphalensJobStale", "AlphalensRulesSyncBroken"}

STALE_LIVE = b"groups: []\n"

# The exact foreign names observed in the live dir on the VPS (2026-08-24
# probe). Prune logic must anchor on the autosync prefix and never touch any
# of these — two are manual backups in OTHER naming styles, two are foreign
# tenants' config the sync must never modify.
FOREIGN_FILES = (
    "alphalens.rules.bak-stream1093-20260824-003837",
    "alphalens.rules.bak.20260823T113614Z",
    # Sorts BELOW every autosync stamp: under a loosened prune prefix this is
    # the file a keep-newest-10 pass would delete first (mutation kill).
    "alphalens.rules.bak-20260531-202411",
    "prometheus.yml",
    "alert.rules",
)


class TestParseAlertNames(unittest.TestCase):
    """The content fingerprint: every alert name in the new YAML."""

    def test_collects_alert_names_across_groups(self) -> None:
        text = (
            b"groups:\n"
            b"  - name: g1\n"
            b"    rules:\n"
            b"      - alert: A\n"
            b"        expr: vector(1)\n"
            b"  - name: g2\n"
            b"    rules:\n"
            b"      - alert: B\n"
            b"        expr: vector(1)\n"
        )

        self.assertEqual(sync.parse_alert_names(text), {"A", "B"})

    def test_recording_rules_are_not_part_of_the_fingerprint(self) -> None:
        # api/v1/rules reports recording rules under a different type; using
        # them in the fingerprint would fail verification on a healthy reload.
        text = (
            b"groups:\n"
            b"  - name: g\n"
            b"    rules:\n"
            b"      - record: job:thing:rate\n"
            b"        expr: vector(1)\n"
            b"      - alert: OnlyAlert\n"
            b"        expr: vector(1)\n"
        )

        self.assertEqual(sync.parse_alert_names(text), {"OnlyAlert"})

    def test_yaml_without_alerts_yields_empty_set(self) -> None:
        self.assertEqual(sync.parse_alert_names(b"groups: []\n"), set())

    def test_invalid_yaml_yields_empty_set_not_a_crash(self) -> None:
        # By the time this runs promtool has already accepted the content, so
        # invalid YAML here means a promtool/parser disagreement. An empty
        # fingerprint makes verification fail LOUDLY (reload_failed), which is
        # the right answer; an exception would skip the metric emit entirely.
        self.assertEqual(sync.parse_alert_names(b"{unclosed"), set())


class TestBuildMetrics(unittest.TestCase):
    """Zero-initialised one-hot: absence must mean broken emitter, never a state."""

    def _one_hot(self, metrics: dict) -> dict[str, float]:
        return {key: value for key, value in metrics.items() if key.startswith(sync.OUTCOME_METRIC)}

    def test_every_outcome_label_is_emitted_every_run_zeros_included(self) -> None:
        metrics = sync.build_metrics(sync.Outcome.CHECK_FAILED)

        one_hot = self._one_hot(metrics)
        self.assertEqual(len(one_hot), 5)
        for outcome in sync.Outcome:
            key = f'{sync.OUTCOME_METRIC}{{outcome="{outcome.value}"}}'
            self.assertIn(key, one_hot)
        self.assertEqual(sum(one_hot.values()), 1)
        self.assertEqual(one_hot[f'{sync.OUTCOME_METRIC}{{outcome="check_failed"}}'], 1)

    def test_the_outcome_family_is_the_only_emitted_metric(self) -> None:
        # Job-level staleness comes from the unit's ExecStopPost hook; a
        # script-side timestamp would be an unconsumed duplicate. Pinning the
        # EXACT key set kills any silent metric-name drift too.
        for outcome in sync.Outcome:
            with self.subTest(outcome=outcome):
                expected = {f'{sync.OUTCOME_METRIC}{{outcome="{o.value}"}}' for o in sync.Outcome}
                self.assertEqual(set(sync.build_metrics(outcome)), expected)


class TestLiveDir(unittest.TestCase):
    """Real filesystem operations against a real temporary directory."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.live = sync.LiveDir(self.dir)

    def test_read_live_returns_none_when_the_file_is_missing(self) -> None:
        self.assertIsNone(self.live.read_live())

    def test_read_live_returns_bytes(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)

        self.assertEqual(self.live.read_live(), STALE_LIVE)

    def test_write_temp_lands_in_the_live_dir_world_readable(self) -> None:
        # promtool runs inside the container as a different user; a 0600 temp
        # file would make every check fail as "cannot read", which reports
        # check_failed for content that is actually fine.
        name = self.live.write_temp(DESIRED)

        temp_path = self.dir / name
        self.assertTrue(temp_path.exists())
        self.assertEqual(temp_path.read_bytes(), DESIRED)
        self.assertEqual(temp_path.stat().st_mode & 0o777, 0o644)
        self.assertNotEqual(name, sync.LIVE_RULES_FILENAME)
        self.assertFalse(name.startswith(sync.BACKUP_PREFIX))

    def test_backup_live_copies_the_current_content(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)

        self.live.backup_live("20260824T090000Z")

        backup = self.dir / f"{sync.BACKUP_PREFIX}20260824T090000Z"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), STALE_LIVE)

    def test_backup_live_is_a_noop_when_live_is_missing(self) -> None:
        self.live.backup_live("20260824T090000Z")

        self.assertEqual(list(self.dir.iterdir()), [])

    def test_prune_keeps_the_newest_ten_autosync_backups(self) -> None:
        for hour in range(12):
            name = f"{sync.BACKUP_PREFIX}20260824T{hour:02d}0000Z"
            (self.dir / name).write_bytes(b"old")

        self.live.prune_autosync_backups(sync.BACKUP_KEEP)

        kept = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(len(kept), 10)
        # The two OLDEST stamps are the ones gone.
        self.assertNotIn(f"{sync.BACKUP_PREFIX}20260824T000000Z", kept)
        self.assertNotIn(f"{sync.BACKUP_PREFIX}20260824T010000Z", kept)
        self.assertIn(f"{sync.BACKUP_PREFIX}20260824T110000Z", kept)

    def test_prune_never_touches_foreign_files(self) -> None:
        # The live dir is SHARED: prometheus.yml and alert.rules belong to
        # other tenants, and the operator keeps manual backups in two other
        # naming styles. Deleting any of them is the disaster scenario.
        self.live.live_path.write_bytes(b"live")
        for name in FOREIGN_FILES:
            (self.dir / name).write_bytes(b"foreign")
        for hour in range(12):
            (self.dir / f"{sync.BACKUP_PREFIX}20260824T{hour:02d}0000Z").write_bytes(b"old")

        self.live.prune_autosync_backups(sync.BACKUP_KEEP)

        survivors = {p.name for p in self.dir.iterdir()}
        for name in FOREIGN_FILES:
            self.assertIn(name, survivors)
            self.assertEqual((self.dir / name).read_bytes(), b"foreign")
        # The live rules file itself must survive any prune-prefix loosening.
        self.assertIn(sync.LIVE_RULES_FILENAME, survivors)
        self.assertEqual(self.live.live_path.read_bytes(), b"live")

    def test_replace_live_with_temp_installs_the_new_content(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)
        name = self.live.write_temp(DESIRED)

        self.live.replace_live_with_temp(name)

        self.assertEqual(self.live.live_path.read_bytes(), DESIRED)
        self.assertFalse((self.dir / name).exists())

    def test_remove_temp_tolerates_a_missing_file(self) -> None:
        self.live.remove_temp("never-created.tmp")  # must not raise


class FakeGit:
    """Injected git port: canned blob content, optional failures."""

    def __init__(
        self, content: bytes = DESIRED, *, fail_fetch: bool = False, fail_show: bool = False
    ):
        self._content = content
        self._fail_fetch = fail_fetch
        self._fail_show = fail_show
        self.fetch_calls = 0

    def fetch(self) -> None:
        self.fetch_calls += 1
        if self._fail_fetch:
            raise sync.GitCommandError("fetch: could not resolve host")

    def show_rules(self) -> bytes:
        if self._fail_show:
            raise sync.GitCommandError("show: path not in origin/main")
        return self._content


class FakeProm:
    """Injected Prometheus port: promtool verdicts, reload, api/v1/rules names."""

    def __init__(
        self,
        *,
        check_ok: bool = True,
        reload_ok: bool = True,
        active: set[str] | None = None,
        active_sequence: list[set[str] | None] | None = None,
        events: list[str] | None = None,
        reload_config_success: bool = True,
        last_config_time: str = "9999-01-01T00:00:00Z",
        runtime_payload: object = "DEFAULT",
    ):
        self._check_ok = check_ok
        self._reload_ok = reload_ok
        self._active = active if active is not None else set(DESIRED_ALERTS) | {"GunbotDown"}
        self._active_sequence = active_sequence
        self._events = events if events is not None else []
        self._reload_config_success = reload_config_success
        self._last_config_time = last_config_time
        self._runtime_payload = runtime_payload
        self.checked: list[str] = []
        self.reload_calls = 0
        self.verify_calls = 0
        self.runtime_calls = 0

    def promtool_check(self, temp_name: str) -> bool:
        self._events.append("check")
        self.checked.append(temp_name)
        return self._check_ok

    def reload(self) -> bool:
        self._events.append("reload")
        self.reload_calls += 1
        return self._reload_ok

    def active_alert_names(self) -> set[str] | None:
        self._events.append("verify")
        self.verify_calls += 1
        if self._active_sequence:
            return self._active_sequence.pop(0)
        return self._active

    def runtime_info(self):
        self._events.append("runtime")
        self.runtime_calls += 1
        if self._runtime_payload != "DEFAULT":
            return self._runtime_payload
        return {
            "data": {
                "reloadConfigSuccess": self._reload_config_success,
                "lastConfigTime": self._last_config_time,
            }
        }


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
        hot = [
            key
            for key, value in metrics.items()
            if key.startswith(sync.OUTCOME_METRIC) and value == 1
        ]
        assert len(hot) == 1, metrics
        return hot[0].split('"')[1]


class RecordingLiveDir(sync.LiveDir):
    """Real LiveDir that also appends each mutation to a shared event list."""

    def __init__(self, path: Path, events: list[str]):
        super().__init__(path)
        self.events = events

    def backup_live(self, stamp: str):
        self.events.append("backup")
        return super().backup_live(stamp)

    def prune_autosync_backups(self, keep: int):
        self.events.append("prune")
        return super().prune_autosync_backups(keep)

    def write_temp(self, content: bytes) -> str:
        self.events.append("write_temp")
        return super().write_temp(content)

    def replace_live_with_temp(self, name: str) -> None:
        self.events.append("replace")
        return super().replace_live_with_temp(name)


class TestRun(unittest.TestCase):
    """Every outcome arm of the run loop, against a real live dir."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.live = sync.LiveDir(self.dir)
        self.emit = RecordingEmit()
        self.sleeps: list[float] = []

    def _run(self, *, git=None, prom=None, live=None, dry_run=False) -> int:
        return sync.run(
            git=git or FakeGit(),
            live=live or self.live,
            prom=prom if prom is not None else FakeProm(),
            emit=self.emit,
            now_fn=lambda: NOW_TS,
            sleep_fn=self.sleeps.append,
            dry_run=dry_run,
        )

    def test_in_sync_performs_zero_side_effects_beyond_the_metric(self) -> None:
        self.live.live_path.write_bytes(DESIRED)
        prom = FakeProm()

        rc = self._run(prom=prom)

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "in_sync")
        # No backup, no temp file, no HUP, no promtool run — nothing at all.
        self.assertEqual([p.name for p in self.dir.iterdir()], [sync.LIVE_RULES_FILENAME])
        self.assertEqual(prom.checked, [])
        self.assertEqual(prom.reload_calls, 0)
        self.assertEqual(prom.verify_calls, 0)

    def test_in_sync_emits_exactly_the_outcome_family(self) -> None:
        self.live.live_path.write_bytes(DESIRED)

        self._run()

        _job, metrics = self.emit.calls[0]
        expected = {f'{sync.OUTCOME_METRIC}{{outcome="{o.value}"}}' for o in sync.Outcome}
        self.assertEqual(set(metrics), expected)

    def test_synced_happy_path_installs_content_and_backs_up_the_old(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        self.assertEqual(self.live.live_path.read_bytes(), DESIRED)
        backups = [p for p in self.dir.iterdir() if p.name.startswith(sync.BACKUP_PREFIX)]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), STALE_LIVE)
        job, _metrics = self.emit.calls[0]
        self.assertEqual(job, sync.JOB_NAME)

    def test_sync_step_order_is_backup_check_replace_reload_verify(self) -> None:
        # The owner-settled order: backup + prune first, promtool check the
        # temp BEFORE the replace, HUP only after the replace, then verify.
        events: list[str] = []
        live = RecordingLiveDir(self.dir, events)
        live.live_path.write_bytes(STALE_LIVE)
        prom = FakeProm(events=events)

        self._run(live=live, prom=prom)

        self.assertEqual(
            events,
            ["backup", "prune", "write_temp", "check", "replace", "reload", "verify", "runtime"],
        )

    def test_fetch_failure_reports_fetch_failed_and_touches_nothing(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)

        rc = self._run(git=FakeGit(fail_fetch=True))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "fetch_failed")
        self.assertEqual(self.live.live_path.read_bytes(), STALE_LIVE)
        self.assertEqual([p.name for p in self.dir.iterdir()], [sync.LIVE_RULES_FILENAME])

    def test_show_failure_is_fetch_failed_too(self) -> None:
        rc = self._run(git=FakeGit(fail_show=True))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "fetch_failed")

    def test_check_failed_leaves_live_byte_identical_and_no_temp_behind(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)

        rc = self._run(prom=FakeProm(check_ok=False))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "check_failed")
        self.assertEqual(self.live.live_path.read_bytes(), STALE_LIVE)
        leftovers = [
            p.name
            for p in self.dir.iterdir()
            if p.name != sync.LIVE_RULES_FILENAME and not p.name.startswith(sync.BACKUP_PREFIX)
        ]
        self.assertEqual(leftovers, [])

    def test_check_failed_never_reloads(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)
        prom = FakeProm(check_ok=False)

        self._run(prom=prom)

        self.assertEqual(prom.reload_calls, 0)

    def test_hup_failure_is_reload_failed_with_the_new_content_installed(self) -> None:
        # The replace already happened — the honest state is "new file on
        # disk, prometheus may still run the old one", and the outcome says so.
        self.live.live_path.write_bytes(STALE_LIVE)

        rc = self._run(prom=FakeProm(reload_ok=False))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        self.assertEqual(self.live.live_path.read_bytes(), DESIRED)

    def test_missing_fingerprint_alert_is_reload_failed_after_retries(self) -> None:
        # The 2026-05-31 incident shape: the HUP "succeeded" but prometheus
        # still served the old rules. A reload-success flag alone cannot see
        # that; the fingerprint (every new alert name active) can.
        self.live.live_path.write_bytes(STALE_LIVE)
        prom = FakeProm(active={"AlphalensJobStale", "GunbotDown"})  # one name absent

        rc = self._run(prom=prom)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        self.assertEqual(prom.verify_calls, sync.VERIFY_ATTEMPTS)
        self.assertEqual(len(self.sleeps), sync.VERIFY_ATTEMPTS - 1)

    def test_rules_api_unreachable_is_reload_failed(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)

        rc = self._run(prom=FakeProm(active_sequence=[None] * sync.VERIFY_ATTEMPTS))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")

    def test_verification_succeeding_on_a_retry_is_synced(self) -> None:
        # A HUP reload is asynchronous; the first poll can race it.
        self.live.live_path.write_bytes(STALE_LIVE)
        prom = FakeProm(active_sequence=[set(), set(DESIRED_ALERTS)])

        rc = self._run(prom=prom)

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        self.assertEqual(len(self.sleeps), 1)

    def test_content_without_alerts_cannot_be_verified_and_is_reload_failed(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)

        no_alerts = b"groups:\n  - name: only-recording\n    rules: []\n"

        rc = self._run(git=FakeGit(content=no_alerts))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")

    def test_missing_live_file_syncs_without_inventing_a_backup(self) -> None:
        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(self.emit.outcome(), "synced")
        self.assertEqual(self.live.live_path.read_bytes(), DESIRED)
        backups = [p for p in self.dir.iterdir() if p.name.startswith(sync.BACKUP_PREFIX)]
        self.assertEqual(backups, [])

    def test_a_failing_metric_emit_does_not_change_the_exit_code(self) -> None:
        self.live.live_path.write_bytes(DESIRED)
        self.emit = RecordingEmit(fail=True)

        self.assertEqual(self._run(), 0)

    def test_dry_run_on_identical_content_prints_and_writes_nothing(self) -> None:
        self.live.live_path.write_bytes(DESIRED)
        prom = FakeProm()

        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self._run(prom=prom, dry_run=True)

        self.assertEqual(rc, 0)
        self.assertIn("in_sync", out.getvalue())
        self.assertEqual(self.emit.calls, [])  # a metric is a write too
        self.assertEqual(prom.checked, [])

    def test_dry_run_on_differing_content_reports_but_never_writes(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)
        prom = FakeProm()

        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self._run(prom=prom, dry_run=True)

        self.assertEqual(rc, 0)
        self.assertIn("would sync", out.getvalue())
        self.assertEqual(self.live.live_path.read_bytes(), STALE_LIVE)
        self.assertEqual([p.name for p in self.dir.iterdir()], [sync.LIVE_RULES_FILENAME])
        self.assertEqual(self.emit.calls, [])
        self.assertEqual(prom.reload_calls, 0)

    def test_dry_run_still_fails_loudly_when_the_fetch_fails(self) -> None:
        rc = self._run(git=FakeGit(fail_fetch=True), dry_run=True)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.calls, [])


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
        # The stale-checkout trap: a deploy on 2026-08-23 nearly shipped a
        # stale working-tree copy. The content source must be the fetched ref.
        runner = FakeRunner(DESIRED)

        content = sync.GitCli("/home/jacoren/AlphaLens", runner=runner).show_rules()

        self.assertEqual(content, DESIRED)
        self.assertEqual(
            runner.calls[0],
            [
                "git",
                "-C",
                "/home/jacoren/AlphaLens",
                "show",
                f"origin/main:{sync.RULES_REPO_PATH}",
            ],
        )

    def test_nonzero_exit_raises_with_gits_own_stderr(self) -> None:
        runner = FakeRunner(returncode=128, stderr=b"fatal: could not resolve host")

        with self.assertRaises(sync.GitCommandError) as caught:
            sync.GitCli("/repo", runner=runner).fetch()

        self.assertIn("could not resolve host", str(caught.exception))


class TestDockerPrometheusAdapter(unittest.TestCase):
    def test_promtool_check_argv_targets_the_container_mount_path(self) -> None:
        runner = FakeRunner()

        ok = sync.DockerPrometheus(runner=runner).promtool_check(".rules-sync-abc.tmp")

        self.assertTrue(ok)
        self.assertEqual(
            runner.calls[0],
            [
                "docker",
                "exec",
                "prometheus",
                "promtool",
                "check",
                "rules",
                "/etc/prometheus/.rules-sync-abc.tmp",
            ],
        )

    def test_promtool_nonzero_exit_is_a_refusal_not_a_crash(self) -> None:
        runner = FakeRunner(returncode=1, stderr=b"FAILED: yaml: line 3")

        self.assertFalse(sync.DockerPrometheus(runner=runner).promtool_check("x.tmp"))

    def test_reload_argv_is_kill_hup_pid_1(self) -> None:
        runner = FakeRunner()

        ok = sync.DockerPrometheus(runner=runner).reload()

        self.assertTrue(ok)
        self.assertEqual(runner.calls[0], ["docker", "exec", "prometheus", "kill", "-HUP", "1"])

    def test_reload_failure_returns_false(self) -> None:
        runner = FakeRunner(returncode=1, stderr=b"No such container")

        self.assertFalse(sync.DockerPrometheus(runner=runner).reload())

    def test_parse_rules_payload_collects_alerting_names_across_tenants(self) -> None:
        # api/v1/rules includes the foreign gunbot group from alert.rules;
        # the fingerprint check is a subset test, so extra names are fine.
        payload = {
            "status": "success",
            "data": {
                "groups": [
                    {
                        "name": "alphalens-cron-health",
                        "rules": [
                            {"type": "alerting", "name": "AlphalensJobStale"},
                            {"type": "recording", "name": "job:thing:rate"},
                        ],
                    },
                    {
                        "name": "gunbot_alerts",
                        "rules": [{"type": "alerting", "name": "GunbotDown"}],
                    },
                ]
            },
        }

        self.assertEqual(sync.parse_rules_payload(payload), {"AlphalensJobStale", "GunbotDown"})

    def test_active_alert_names_is_none_when_the_api_is_unreachable(self) -> None:
        def boom(url: str) -> dict:
            raise OSError("connection refused")

        prom = sync.DockerPrometheus(runner=FakeRunner(), fetch_json=boom)

        self.assertIsNone(prom.active_alert_names())


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


class TestReloadConfirmed(unittest.TestCase):
    """The runtimeinfo gate: names alone are blind to expression-only changes
    (the 2026-08-20 rename class) — prometheus itself must report a fresh,
    successful reload."""

    _FRESH = {"data": {"reloadConfigSuccess": True, "lastConfigTime": "9999-01-01T00:00:00Z"}}

    def test_fresh_successful_reload_confirms(self) -> None:
        self.assertTrue(sync.reload_confirmed(self._FRESH, NOW_TS))

    def test_stale_last_config_time_fails_closed(self) -> None:
        info = {"data": {"reloadConfigSuccess": True, "lastConfigTime": "2000-01-01T00:00:00Z"}}
        self.assertFalse(sync.reload_confirmed(info, NOW_TS))

    def test_reload_config_success_false_fails_closed(self) -> None:
        info = {"data": {"reloadConfigSuccess": False, "lastConfigTime": "9999-01-01T00:00:00Z"}}
        self.assertFalse(sync.reload_confirmed(info, NOW_TS))

    def test_missing_or_garbled_payload_fails_closed(self) -> None:
        for info in (
            None,
            {},
            {"data": None},
            {"data": {"reloadConfigSuccess": True}},
            {"data": {"reloadConfigSuccess": True, "lastConfigTime": "not-a-time"}},
        ):
            with self.subTest(info=info):
                self.assertFalse(sync.reload_confirmed(info, NOW_TS))

    def test_same_second_reload_is_inside_the_slack(self) -> None:
        stamp = sync.dt.datetime.fromtimestamp(NOW_TS, tz=sync.dt.UTC).isoformat()
        info = {"data": {"reloadConfigSuccess": True, "lastConfigTime": stamp}}
        self.assertTrue(sync.reload_confirmed(info, NOW_TS))


class TestRunHardening(unittest.TestCase):
    """Verifier findings: hung subprocesses and unexpected write-path errors
    must still land in a metric-emitting outcome, and the reload gate must
    veto a stale in-memory config even when every alert name matches."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.live = sync.LiveDir(self.dir)
        self.emit = RecordingEmit()

    def _run(self, *, git=None, prom=None, live=None) -> int:
        return sync.run(
            git=git or FakeGit(),
            live=live or self.live,
            prom=prom if prom is not None else FakeProm(),
            emit=self.emit,
            now_fn=lambda: NOW_TS,
            sleep_fn=lambda _s: None,
        )

    def test_git_timeout_raises_git_command_error(self) -> None:
        def hung_runner(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, 120)

        cli = sync.GitCli("/repo", runner=hung_runner)
        with self.assertRaises(sync.GitCommandError):
            cli.fetch()

    def test_unexpected_error_before_replace_is_check_failed_and_live_untouched(self) -> None:
        class DiskFullLive(sync.LiveDir):
            def backup_live(self, stamp):
                raise OSError("no space left on device")

        live = DiskFullLive(self.dir)
        live.live_path.write_bytes(STALE_LIVE)

        rc = self._run(live=live)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "check_failed")
        self.assertEqual(live.live_path.read_bytes(), STALE_LIVE)

    def test_unexpected_error_after_replace_is_reload_failed(self) -> None:
        class RaisingReloadProm(FakeProm):
            def reload(self):
                raise RuntimeError("port contract violated")

        self.live.live_path.write_bytes(STALE_LIVE)

        rc = self._run(prom=RaisingReloadProm())

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")

    def test_matching_names_with_stale_reload_time_is_reload_failed(self) -> None:
        # The 2026-08-20 shape: expression-only change, every alert name
        # already active in the OLD config, HUP silently failed.
        self.live.live_path.write_bytes(STALE_LIVE)
        prom = FakeProm(last_config_time="2000-01-01T00:00:00Z")

        rc = self._run(prom=prom)

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")
        self.assertGreaterEqual(prom.runtime_calls, 1)

    def test_reload_config_success_false_is_reload_failed(self) -> None:
        self.live.live_path.write_bytes(STALE_LIVE)

        rc = self._run(prom=FakeProm(reload_config_success=False))

        self.assertEqual(rc, 1)
        self.assertEqual(self.emit.outcome(), "reload_failed")


class TestAlertMetricParity(unittest.TestCase):
    """The sustained-failure alert must reference the exact metric family and
    success labels the script emits - renaming either side alone is the exact
    silent-blindness class this whole job exists to prevent."""

    def _rules_text(self) -> str:
        root = Path(__file__).resolve().parents[3]
        return (root / "deploy/monitoring/prometheus/rules/alphalens.yaml").read_text()

    def test_alert_expr_uses_the_script_metric_and_success_labels(self) -> None:
        labels = "|".join(sorted(o.value for o in sync.SUCCESS_OUTCOMES))
        fragment = f'{sync.OUTCOME_METRIC}{{outcome=~"{labels}"}}'
        self.assertIn(fragment, self._rules_text())
