"""Guard against regressions in the deploy/systemd unit files.

The pipeline image declares ``ENTRYPOINT ["/app/.venv/bin/alphalens"]`` so
any ``docker compose run pipeline <script>`` invocation must explicitly
override the entrypoint, otherwise typer interprets the script path as a
command name and dies before the pipeline starts. The systemd unit silently
exits non-zero in that case, so a missing override yields the symptom
"daily timer fires, briefs/ stays empty" — surfaced by zen pre-merge review.

The Mac launchd → VPS systemd migration (PR-1 of the observability epic)
added three new units — edgar-detect, literature-scan-weekly,
literature-scan-monthly — covered by the ``TestMigratedLaunchdUnits`` and
``TestLiteraturePublishWrapper`` suites below.
"""

from __future__ import annotations

import re
import stat
import unittest
from pathlib import Path

# Test file lives at apps/alphalens-research/tests/<name>.py; the repo root
# is three parents up. deploy/ stays at the repo root, not under the app.
REPO_ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
SERVICE_PATH = SYSTEMD_DIR / "alphalens-thematic-build.service"

# Units migrated from macOS launchd in PR-1 of the observability epic.
EDGAR_SERVICE = SYSTEMD_DIR / "alphalens-edgar-detect.service"
EDGAR_TIMER = SYSTEMD_DIR / "alphalens-edgar-detect.timer"
LIT_WEEKLY_SERVICE = SYSTEMD_DIR / "alphalens-literature-scan-weekly.service"
LIT_WEEKLY_TIMER = SYSTEMD_DIR / "alphalens-literature-scan-weekly.timer"
LIT_MONTHLY_SERVICE = SYSTEMD_DIR / "alphalens-literature-scan-monthly.service"
LIT_MONTHLY_TIMER = SYSTEMD_DIR / "alphalens-literature-scan-monthly.timer"

LIT_PUBLISH_WRAPPER = SYSTEMD_DIR / "bin" / "alphalens-literature-scan-publish"

# Job-metrics ExecStopPost hook (PR-2 of the cron-observability epic).
# Every active timer-driven service must emit Prometheus textfile
# metrics so the dashboard + alertmanager can see "did it run, did it
# succeed, how long did it take". The bash helper itself is at:
EMIT_JOB_METRICS_HOOK = SYSTEMD_DIR / "bin" / "alphalens-emit-job-metrics"

# All five active services that the metrics hook must be wired into.
# Form-4 backfill is excluded — it is a long-running daemon (DONE
# 2026-05-08, per CLAUDE.md) and would emit a single point at end-of-run.
ACTIVE_SERVICES = (
    EDGAR_SERVICE,
    LIT_WEEKLY_SERVICE,
    LIT_MONTHLY_SERVICE,
    SYSTEMD_DIR / "alphalens-av-earnings-backfill.service",
    SYSTEMD_DIR / "alphalens-thematic-build.service",
)


class TestSystemdUnits(unittest.TestCase):
    def setUp(self) -> None:
        self.unit_text = SERVICE_PATH.read_text()

    def test_thematic_build_service_overrides_entrypoint(self):
        self.assertIn(
            "--entrypoint /bin/bash",
            self.unit_text,
            "ExecStart must override the pipeline image ENTRYPOINT or typer "
            "will refuse to run the script — see "
            "deploy/docker/Dockerfile.pipeline:ENTRYPOINT and the comment "
            "above ExecStart in alphalens-thematic-build.service.",
        )

    def test_thematic_build_service_invokes_driver_script(self):
        self.assertIn(
            "/app/deploy/docker/run_thematic_day.sh",
            self.unit_text,
        )

    def test_thematic_build_service_keeps_oneshot_type(self):
        self.assertIn("Type=oneshot", self.unit_text)

    def test_thematic_build_verify_cache_does_not_repeat_alphalens_arg(self):
        # The pipeline image's ENTRYPOINT is ``/app/.venv/bin/alphalens``
        # (Dockerfile.pipeline). The verify-cache ExecStartPost runs the
        # CLI directly (no ``--entrypoint`` override), so the args after
        # ``alphalens-pipeline:latest`` are forwarded to Typer verbatim.
        # Passing a literal ``alphalens`` as the first arg makes Typer
        # parse it as a subcommand and abort with
        # ``No such command 'alphalens'`` — observed on VPS 2026-05-29.
        # This regex pins the correct first token (``thematic``) and
        # fails loud if the leading ``alphalens`` ever returns.
        #
        # Multiline-anchored so a comment containing ``alphalens thematic
        # verify-cache`` cannot satisfy the assertion.
        bad_pattern = re.compile(
            r"^ExecStartPost=[^\n]*(?:\\\n[^\n]*)*"
            r"alphalens-pipeline:latest\s*\\\s*\n\s*alphalens\s+thematic\s+verify-cache",
            re.MULTILINE,
        )
        self.assertNotRegex(
            self.unit_text,
            bad_pattern,
            "verify-cache ExecStartPost must NOT repeat the `alphalens` "
            "token after the image name — the pipeline image ENTRYPOINT "
            "already IS alphalens. Use `thematic verify-cache ...` "
            "directly. See Dockerfile.pipeline:ENTRYPOINT.",
        )

        good_pattern = re.compile(
            r"^ExecStartPost=[^\n]*(?:\\\n[^\n]*)*"
            r"alphalens-pipeline:latest\s*\\\s*\n\s*thematic\s+verify-cache",
            re.MULTILINE,
        )
        self.assertRegex(
            self.unit_text,
            good_pattern,
            "Expected an ExecStartPost line invoking "
            "`<image> thematic verify-cache ...` (no leading `alphalens` "
            "arg). Found neither the broken nor the correct form — the "
            "gap-detection hook has been removed entirely.",
        )

    def test_thematic_build_service_rebuilds_briefs_cache_post_run(self):
        # After a successful pipeline run the new parquet output must be
        # synced into the Django Postgres-backed cache. The unit invokes
        # the ``rebuild-cache`` maintenance one-shot from the django-prod
        # compose stack. Regression here surfaces as "daily timer fires,
        # parquet refreshed, but the API still serves yesterday's briefs"
        # — silent until someone notices.
        #
        # Regex bound to a directive line (multiline ``^``) so the
        # assertion cannot pass on a comment mentioning ``rebuild-cache``.
        # Allows for the ``\`` line-continuation between the directive
        # and its args.
        self.assertRegex(
            self.unit_text,
            re.compile(r"^ExecStartPost=[^\n]*(?:\\\n[^\n]*)*rebuild-cache\b", re.MULTILINE),
            "Missing or malformed ExecStartPost — the Django briefs "
            "cache will not pick up the freshly written parquet from "
            "the daily pipeline run.",
        )


class TestMigratedLaunchdUnits(unittest.TestCase):
    """The three units migrated from macOS launchd in PR-1.

    These guard the contract the user relies on:

    - edgar-detect MUST fire every 15 min (Layer 1 SoT cadence).
    - Both literature timers MUST be ``Persistent=true`` so a VPS reboot
      doesn't silently skip the Sun-18:00 / 1st-of-month window.
    - Every service MUST source ``/etc/alphalens/env`` without a leading
      ``-`` (fail loud on missing secrets, do NOT silently degrade).
    - No host-venv unit may double the ``alphalens`` token after
      ``.venv/bin/alphalens`` — same trap class that bit PR-E on the
      pipeline image ENTRYPOINT (caught 2026-05-29).
    """

    # --- edgar-detect ---------------------------------------------------

    def test_edgar_detect_service_uses_host_venv_alphalens(self) -> None:
        text = EDGAR_SERVICE.read_text()
        self.assertRegex(
            text,
            re.compile(
                r"^ExecStart=%h/AlphaLens/\.venv/bin/alphalens\s+edgar\s+detect\s*$",
                re.MULTILINE,
            ),
            "edgar-detect ExecStart must invoke the host venv alphalens "
            "binary with `edgar detect` and nothing else — no doubled "
            "subcommand, no extra args.",
        )

    def test_edgar_detect_service_is_oneshot(self) -> None:
        self.assertIn("Type=oneshot", EDGAR_SERVICE.read_text())

    def test_edgar_detect_timer_fires_every_15min_with_boot_offset(self) -> None:
        text = EDGAR_TIMER.read_text()
        # Mirrors launchd StartInterval=900 + a 2min OnBootSec buffer so
        # a reboot doesn't fire instantly into a half-warm env.
        self.assertRegex(text, re.compile(r"^OnUnitActiveSec=15min\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^OnBootSec=2min\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^Persistent=true\s*$", re.MULTILINE))

    # --- literature scan units -----------------------------------------

    def test_literature_weekly_timer_fires_sunday_local(self) -> None:
        text = LIT_WEEKLY_TIMER.read_text()
        # systemd OnCalendar trailing TZ pins DST sanity — the user's
        # Sunday 18:00 expectation is local Europe/Warsaw time.
        self.assertRegex(
            text,
            re.compile(r"^OnCalendar=Sun \*-\*-\* 18:00:00 Europe/Warsaw\s*$", re.MULTILINE),
        )
        self.assertRegex(text, re.compile(r"^Persistent=true\s*$", re.MULTILINE))

    def test_literature_monthly_timer_fires_first_of_month_local(self) -> None:
        text = LIT_MONTHLY_TIMER.read_text()
        self.assertRegex(
            text,
            re.compile(r"^OnCalendar=\*-\*-01 09:00:00 Europe/Warsaw\s*$", re.MULTILINE),
        )
        self.assertRegex(text, re.compile(r"^Persistent=true\s*$", re.MULTILINE))

    def test_literature_services_invoke_publish_wrapper(self) -> None:
        # Services delegate to the wrapper so commit+push logic stays in
        # bash, not in systemd directive substitution.
        wrapper_path = "%h/AlphaLens/deploy/systemd/bin/alphalens-literature-scan-publish"
        self.assertIn(f"ExecStart={wrapper_path} weekly", LIT_WEEKLY_SERVICE.read_text())
        self.assertIn(f"ExecStart={wrapper_path} monthly", LIT_MONTHLY_SERVICE.read_text())

    # --- shared invariants across all 3 migrated units -----------------

    def test_all_migrated_services_load_etc_alphalens_env_fail_loud(self) -> None:
        for path in (EDGAR_SERVICE, LIT_WEEKLY_SERVICE, LIT_MONTHLY_SERVICE):
            text = path.read_text()
            # Fail loud: no leading dash on EnvironmentFile. A typo / missing
            # file MUST surface as a service failure, not "no Telegram alerts
            # silently for a week" (see deploy/systemd/README.md §Environment
            # file setup for the rationale).
            self.assertRegex(
                text,
                re.compile(r"^EnvironmentFile=/etc/alphalens/env\s*$", re.MULTILINE),
                f"{path.name} must load /etc/alphalens/env without a leading "
                "dash (fail loud on missing secrets).",
            )

    def test_all_migrated_services_are_oneshot_with_working_dir(self) -> None:
        for path in (EDGAR_SERVICE, LIT_WEEKLY_SERVICE, LIT_MONTHLY_SERVICE):
            text = path.read_text()
            self.assertIn("Type=oneshot", text)
            self.assertIn("WorkingDirectory=%h/AlphaLens", text)

    def test_all_migrated_timers_carry_install_section(self) -> None:
        # Without [Install] + WantedBy, ``systemctl --user enable --now``
        # silently no-ops on enable — the operator hits start once and the
        # next reboot never re-arms the timer.
        for path in (EDGAR_TIMER, LIT_WEEKLY_TIMER, LIT_MONTHLY_TIMER):
            text = path.read_text()
            self.assertRegex(text, re.compile(r"^\[Install\]\s*$", re.MULTILINE))
            self.assertRegex(text, re.compile(r"^WantedBy=timers\.target\s*$", re.MULTILINE))

    def test_no_doubled_alphalens_token_in_any_host_venv_unit(self) -> None:
        # Generalises the PR #305 / verify-cache trap to every host-venv
        # ExecStart{,Post} line. The Docker entry point's image-name-then-
        # cli-token shape lives in the thematic unit; that's intentionally
        # exempted (it has a separate, more specific regression test).
        bad = re.compile(
            r"^ExecStart(?:Post)?=[^\n]*\.venv/bin/alphalens\s+alphalens\b",
            re.MULTILINE,
        )
        for path in (EDGAR_SERVICE, LIT_WEEKLY_SERVICE, LIT_MONTHLY_SERVICE):
            self.assertNotRegex(
                path.read_text(),
                bad,
                f"{path.name}: `.venv/bin/alphalens alphalens ...` is a "
                "doubled-token bug (same class as the verify-cache trap).",
            )


class TestLiteraturePublishWrapper(unittest.TestCase):
    """Static checks on the literature scan publish wrapper.

    The wrapper is bash, so we can't unit-test the runtime path — but we
    can pin invariants that, if broken, would cause real damage:

    - Missing ``set -euo pipefail`` would let a failed ``git push`` exit 0,
      hiding "scan ran but never published" failures from monitoring.
    - Missing the ``git diff --quiet`` gate would commit on every run even
      when the scan produced no new file, polluting main with empty
      "docs(literature)" commits.
    - Missing the rebase retry on push race would surface as a hard fail
      whenever the Mac happens to push at the same time as the VPS.
    """

    def test_wrapper_exists_and_is_executable(self) -> None:
        self.assertTrue(
            LIT_PUBLISH_WRAPPER.is_file(),
            f"literature publish wrapper missing at {LIT_PUBLISH_WRAPPER}",
        )
        mode = LIT_PUBLISH_WRAPPER.stat().st_mode
        self.assertTrue(
            mode & stat.S_IXUSR,
            "wrapper must be chmod +x — systemd won't run a non-executable "
            f"ExecStart (mode={oct(mode)}).",
        )

    def test_wrapper_uses_strict_bash_mode(self) -> None:
        text = LIT_PUBLISH_WRAPPER.read_text()
        # Pipefail is the load-bearing flag here: a failed `git push` in
        # a chained command MUST propagate, otherwise systemd sees exit 0
        # and the monitoring "last_success" timestamp updates falsely.
        self.assertRegex(text, re.compile(r"^set -[eu]+o\s+pipefail\b", re.MULTILINE))

    def test_wrapper_sets_strict_mode_on_line_2(self) -> None:
        # Zen pre-merge review of PR #310 flagged that ``set -euo pipefail``
        # placed below a comment block can drift if someone reorders the
        # file. Pin it to the line right after the shebang so any
        # accidental insertion above it fails this test loud.
        lines = LIT_PUBLISH_WRAPPER.read_text().splitlines()
        self.assertGreaterEqual(len(lines), 2, "wrapper too short to inspect")
        self.assertTrue(
            lines[0].startswith("#!"),
            f"line 1 must be the shebang, got: {lines[0]!r}",
        )
        self.assertRegex(
            lines[1],
            re.compile(r"^set -[eu]+o\s+pipefail\b"),
            "line 2 must be `set -euo pipefail` (zen-review pinned).",
        )

    def test_wrapper_skips_commit_when_clean(self) -> None:
        text = LIT_PUBLISH_WRAPPER.read_text()
        # Guard MUST short-circuit before `git add` — otherwise every
        # idempotent re-run commits "no change" markdown noise.
        self.assertIn("git diff --quiet", text)

    def test_wrapper_retries_push_on_race(self) -> None:
        text = LIT_PUBLISH_WRAPPER.read_text()
        # Failure mode: VPS push lands a half-second after the user's
        # local commit. One rebase-retry-then-give-up is the rational
        # ceiling (a second race is already a different bug). Pin the
        # shape so a future "simplify the wrapper" PR can't quietly drop
        # the retry.
        #
        # Form is the explicit ``... && exit 0`` / ``exit 1`` per PR #310
        # zen review — bash ``cmd1 || (cmd2 && cmd3)`` was semantically
        # correct (set -e propagates the subshell's non-zero exit) but
        # easier to misread than the linear flow.
        self.assertIn("git push origin main && exit 0", text)
        self.assertRegex(
            text,
            re.compile(
                r"git pull --rebase origin main\s*&&\s*git push origin main\s*&&\s*exit 0",
            ),
        )
        # Final explicit non-zero exit so the unit ends in ``failed``
        # state when both attempts fail — Alertmanager (PR-3) will
        # latch onto this.
        self.assertRegex(text, re.compile(r"^exit 1\s*$", re.MULTILINE))

    def test_wrapper_scrubs_gh_token_from_scan_subprocess(self) -> None:
        # Zen pre-merge review of PR #310 flagged defense-in-depth: the
        # literature scan CLI does not need GH_TOKEN and a future verbose
        # `print(os.environ)` debug line would leak it to journald. The
        # wrapper invokes the scan under ``env -u GH_TOKEN`` to scrub.
        text = LIT_PUBLISH_WRAPPER.read_text()
        self.assertRegex(
            text,
            re.compile(
                r"env -u GH_TOKEN\s+\"\$HOME/AlphaLens/\.venv/bin/alphalens\"\s+literature\s+scan"
            ),
            "Scan subprocess must run under `env -u GH_TOKEN` so the "
            "push token cannot reach the CLI's environment.",
        )

    def test_wrapper_accepts_window_argument(self) -> None:
        # weekly | monthly arrives as $1; the wrapper must validate (or at
        # least bind it to the `--window` flag) so a malformed ExecStart
        # like ``... weekly monthly`` doesn't silently run the wrong scan.
        text = LIT_PUBLISH_WRAPPER.read_text()
        self.assertRegex(text, re.compile(r'WINDOW="?\$\{?1\}?"?'))
        self.assertIn('--window "$WINDOW"', text)


class TestJobMetricsHook(unittest.TestCase):
    """All active services wire the textfile-metrics ExecStopPost hook.

    A unit without the hook would still run but its cron-health
    metrics (last_success_timestamp, duration, exit code) would never
    update — Alertmanager would then fire a false stale-job alert.
    Pinning the hook here forces the operator to add the line in the
    same commit that adds a new cron-driven unit.
    """

    def test_emit_job_metrics_hook_exists_and_executable(self) -> None:
        self.assertTrue(
            EMIT_JOB_METRICS_HOOK.is_file(),
            f"metrics hook missing at {EMIT_JOB_METRICS_HOOK}",
        )
        mode = EMIT_JOB_METRICS_HOOK.stat().st_mode
        self.assertTrue(
            mode & stat.S_IXUSR,
            f"metrics hook must be chmod +x (mode={oct(mode)}).",
        )

    def test_emit_hook_strict_bash_on_line_2(self) -> None:
        lines = EMIT_JOB_METRICS_HOOK.read_text().splitlines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("#!"))
        self.assertRegex(
            lines[1],
            re.compile(r"^set -[eu]+o\s+pipefail\b"),
            "set -euo pipefail must be the first executable line "
            "(same convention as alphalens-literature-scan-publish).",
        )

    def test_emit_hook_writes_atomically_via_mv(self) -> None:
        # Partial file reads from node_exporter would either skip the
        # metric or report a parse error. ``mv`` of a sibling tempfile
        # is the only POSIX-atomic primitive available in bash; pin it.
        text = EMIT_JOB_METRICS_HOOK.read_text()
        self.assertIn('TMP="${OUT}.tmp"', text)
        self.assertIn('mv "$TMP" "$OUT"', text)

    def test_emit_hook_only_writes_last_success_on_success(self) -> None:
        # ``alphalens_job_last_success_timestamp_seconds`` is the
        # alertmanager input for "job hasn't succeeded in N minutes".
        # If the hook wrote it on every fire regardless of outcome,
        # a failed job would still appear "recently successful" and
        # the staleness alert would never fire.
        text = EMIT_JOB_METRICS_HOOK.read_text()
        self.assertRegex(
            text,
            re.compile(
                r'if\s+\[\s*"?\$RESULT"?\s+=\s+"?success"?\s*\]'
                r".*?alphalens_job_last_success_timestamp_seconds",
                re.DOTALL,
            ),
            "last_success_timestamp must be guarded by RESULT=success — "
            "otherwise failed runs falsely refresh the staleness clock.",
        )

    def test_every_active_service_wires_emit_hook(self) -> None:
        # The hook line shape: an absolute %h-rooted path + the short
        # job name. The job name must match the systemd unit's basename
        # (minus the ``alphalens-`` prefix + ``.service`` suffix) so the
        # bash hook's systemctl-show probe targets the right unit.
        for path in ACTIVE_SERVICES:
            text = path.read_text()
            expected_job = path.stem.removeprefix("alphalens-")
            self.assertRegex(
                text,
                re.compile(
                    r"^ExecStopPost=%h/AlphaLens/deploy/systemd/bin/"
                    r"alphalens-emit-job-metrics\s+" + re.escape(expected_job) + r"\s*$",
                    re.MULTILINE,
                ),
                f"{path.name} must wire ExecStopPost=alphalens-emit-job-metrics "
                f"{expected_job} — without it the cron-health metrics for this "
                "unit never update and Alertmanager fires a false stale alert.",
            )


if __name__ == "__main__":
    unittest.main()
