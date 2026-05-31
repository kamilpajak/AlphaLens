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
TIMER_PATH = SYSTEMD_DIR / "alphalens-thematic-build.timer"
RUN_THEMATIC_SCRIPT = REPO_ROOT / "deploy" / "docker" / "run_thematic_day.sh"

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
PAPER_PLAN_SERVICE = SYSTEMD_DIR / "alphalens-paper-plan.service"
PAPER_PLAN_TIMER = SYSTEMD_DIR / "alphalens-paper-plan.timer"
PAPER_SUBMIT_SERVICE = SYSTEMD_DIR / "alphalens-paper-submit.service"
PAPER_SUBMIT_TIMER = SYSTEMD_DIR / "alphalens-paper-submit.timer"
PAPER_RECONCILE_SERVICE = SYSTEMD_DIR / "alphalens-paper-reconcile.service"
PAPER_RECONCILE_TIMER = SYSTEMD_DIR / "alphalens-paper-reconcile.timer"

ACTIVE_SERVICES = (
    EDGAR_SERVICE,
    LIT_WEEKLY_SERVICE,
    LIT_MONTHLY_SERVICE,
    SYSTEMD_DIR / "alphalens-av-earnings-backfill.service",
    SYSTEMD_DIR / "alphalens-thematic-build.service",
    PAPER_PLAN_SERVICE,
    PAPER_SUBMIT_SERVICE,
    PAPER_RECONCILE_SERVICE,
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

    def test_thematic_build_docker_run_passes_openrouter_api_key(self):
        # PR-G (2026-05-30) swapped the thematic pipeline to DeepSeek v4
        # via OpenRouter. The docker `-e KEY` flag list in ExecStart
        # MUST forward OPENROUTER_API_KEY into the container or
        # extract/map-themes/brief exit 2 with "OPENROUTER_API_KEY
        # missing from environment" (caught manually post-merge:
        # service had GOOGLE_API_KEY but not OPENROUTER_API_KEY).
        #
        # The `-e KEY` (no `=value`) form tells docker to copy from
        # the calling env — which is populated from /etc/alphalens/env
        # via EnvironmentFile. Both halves MUST agree on the key name.
        self.assertRegex(
            self.unit_text,
            re.compile(r"^\s+-e OPENROUTER_API_KEY\s*\\?\s*$", re.MULTILINE),
            "ExecStart docker invocation MUST forward OPENROUTER_API_KEY "
            "via `-e OPENROUTER_API_KEY` or the LLM call stage exits 2. "
            "Add the line to the `-e KEY` block in alphalens-thematic-"
            "build.service.",
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


class TestThematicBuildCadence(unittest.TestCase):
    """PR-F (epic #295 / issue #300) — 6×/day thematic-build cadence.

    Moves from daily 06:30 UTC to 6× UTC (`00,04,08,12,16,20:30`) so the
    weekend SPA read-experience picks up Saturday-afternoon ET news the
    same calendar day, and pre-prepares the harness for multi-exchange
    routing (XWAR / XTKS / XHKG / XSHG) where each session needs its own
    "right-before-open" and "right-after-close" refresh.

    Three coupled changes pinned here:

    1. Timer ``OnCalendar`` lists six HH:30 hours.
    2. ``run_thematic_day.sh`` passes ``--force`` to ``thematic ingest``
       so the read-through cache at ``polygon_news.py:124`` does not
       silently short-circuit every run after the first of the UTC day.
    3. Prometheus staleness threshold for ``thematic-build`` tightens
       from ``> 48h`` to ``> 12h`` (3× the new 4h interval) — at 6×
       cadence, 48h would silence the alert for 12 missed runs.

    See ``docs/research/polygon_quota_6x_per_day_2026_05_30.md`` for the
    empirical quota measurement that justifies 6× over 4×.
    """

    def test_timer_fires_six_times_per_day_at_hh30(self) -> None:
        # OnCalendar comma-list is the canonical systemd form for
        # "multiple times per day". HH:30 chosen so each run lands
        # outside the every-15-min EDGAR-detect window (XX:00, XX:15,
        # XX:30, XX:45 — but EDGAR runs are ~30s, no real contention;
        # HH:30 simply keeps the schedule readable in
        # `systemctl --user list-timers`).
        timer_text = TIMER_PATH.read_text()
        self.assertRegex(
            timer_text,
            re.compile(
                r"^OnCalendar=\*-\*-\* 00,04,08,12,16,20:30:00 UTC\s*$",
                re.MULTILINE,
            ),
            "Expected 6× HH:30 UTC schedule (00/04/08/12/16/20). "
            "See docs/research/polygon_quota_6x_per_day_2026_05_30.md "
            "for the timezone-coverage rationale.",
        )

    def test_timer_keeps_persistent_true(self) -> None:
        # Persistence survives across the 6× window the same way it
        # did for the 1× window — a VPS that booted between two runs
        # fires the missed run once at next boot.
        self.assertIn("Persistent=true", TIMER_PATH.read_text())

    def test_timer_carries_randomized_delay_for_boot_storm(self) -> None:
        # ``Persistent=true`` + 6 missed slots in a 24h window means a
        # boot after a full-day outage can queue up to 6 catch-up runs
        # all firing at boot+0. systemd serializes Type=oneshot fires
        # for the same unit (queued, not parallel) so the pipelines
        # themselves don't collide, but the FIRST catch-up fires
        # simultaneously with the every-15-min EDGAR detector + any
        # 00:05 UTC av-earnings backfill that the same boot rescues —
        # contending for the same SEC/Polygon per-IP rate-limit
        # buckets. A 5-min jitter keeps every fire inside its
        # timezone-rotation window (slots are 4h apart; ±5 min still
        # reads as "pre-XNYS open" / "pre-XTKS open" etc.) but
        # deflects the boot-time thundering herd. Zen pre-merge
        # review of PR-F flagged the boot-storm class.
        self.assertRegex(
            TIMER_PATH.read_text(),
            re.compile(r"^RandomizedDelaySec=5min\s*$", re.MULTILINE),
            "Timer must carry RandomizedDelaySec=5min so the catch-up "
            "storm after a VPS reboot does not collide with the "
            "every-15-min EDGAR detector for the per-IP rate-limit "
            "bucket. 5min is small enough to preserve the per-exchange "
            "timezone alignment.",
        )

    def test_service_has_start_timeout_for_hung_runs(self) -> None:
        # ``Type=oneshot`` defaults TimeoutStartSec to infinity (man
        # systemd.service §"Type=oneshot"). A run that wedges on a
        # Gemini quota loop or a Polygon retry storm would block
        # every subsequent timer fire forever — the systemd job
        # manager queues new fires behind the running one. 45min =
        # ~3× the typical 15-20min wall time; a healthy run never
        # trips this, a wedged one gets SIGTERM (then SIGKILL after
        # TimeoutStopSec) so the next slot can fire. Zen pre-merge
        # review of PR-F flagged the hang-blocks-queue class as the
        # real pipeline-overlap risk (the surface concern of "two
        # runs in parallel" doesn't actually occur on Type=oneshot).
        self.assertRegex(
            SERVICE_PATH.read_text(),
            re.compile(r"^TimeoutStartSec=45min\s*$", re.MULTILINE),
            "Service must carry TimeoutStartSec=45min or a wedged run "
            "blocks every subsequent timer fire indefinitely.",
        )

    def test_run_thematic_day_passes_force_to_ingest(self) -> None:
        # Without --force, polygon_news.py:124 returns the cached
        # parquet on every same-UTC-day re-run, so the 6× cadence
        # silently degrades to 1× — the bug we are explicitly fixing.
        # Polygon Stocks Basic has no daily cap, so the cost of
        # forced re-fetch is zero.
        script_text = RUN_THEMATIC_SCRIPT.read_text()
        self.assertRegex(
            script_text,
            re.compile(r"^alphalens thematic ingest --force\s*$", re.MULTILINE),
            "ingest stage must pass --force or the same-UTC-day cache "
            "short-circuits every run after the first.",
        )

    def test_thematic_build_staleness_alert_threshold_is_12h(self) -> None:
        # 12h = 3× the 4h interval. Loose enough that one transient
        # miss (Gemini RPM blip, Polygon outage) does not page; tight
        # enough that two consecutive misses surface within half a day.
        # 48h was the 1×-cadence threshold (2× daily interval); the
        # 12h threshold preserves the same "2-3× cadence" sensitivity.
        rules_path = REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alphalens.yaml"
        rules_text = rules_path.read_text()
        # 12h = 43200 seconds. Look for the threshold inside the
        # thematic-build block (the only place this number appears).
        self.assertRegex(
            rules_text,
            re.compile(
                r'job="thematic-build"\}\s*>\s*43200\b',
            ),
            "thematic-build staleness threshold must be 43200 (12h) at "
            "6× cadence; was 172800 (48h) at 1× cadence.",
        )

    def test_thematic_build_staleness_alert_summary_mentions_new_threshold(self) -> None:
        # Summary string is the operator-facing description; out-of-sync
        # with the threshold expression is the kind of drift that wastes
        # an incident-response cycle. Pin both halves together.
        rules_path = REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alphalens.yaml"
        rules_text = rules_path.read_text()
        self.assertRegex(
            rules_text,
            re.compile(r'"thematic-build stale > 12h \(expected 4h cadence\)"'),
            "Summary annotation must reflect the new 12h threshold + "
            "4h cadence so operator-facing text matches the expression.",
        )


class TestPaperSubmitUnit(unittest.TestCase):
    """PR-D (epic #295, issue #298) — paper-submit systemd unit.

    Fires once per US trading day at 13:25 UTC (= 09:25 ET, 5 min
    before the XNYS opening cross at 13:30 UTC). The ``OnCalendar=Mon..
    Fri`` filter handles weekends; the ``ExecCondition=`` invocation of
    ``alphalens paper is-trading-day`` catches US public holidays
    falling on weekdays (~10/year). Both layers are required — neither
    alone is sufficient.

    The submit command itself ALREADY carries an internal market-closed
    guard (see ``test_cli_market_closed_guard.py``), so ExecCondition
    is belt-and-suspenders: it makes the systemd job show ``condition
    failed`` instead of ``deactivated (success)`` on holidays. Cleaner
    operator observability — one glance at ``systemctl --user
    list-timers`` distinguishes "skipped cleanly" from "ran and
    self-deferred".
    """

    def test_submit_service_uses_host_venv_alphalens(self) -> None:
        # Same pattern as edgar-detect: the paper subtree depends on
        # alpaca-py + pandas which we keep in the host venv, NOT in the
        # pipeline Docker image (image is purpose-built for the
        # thematic build's google-genai dependency stack).
        #
        # ExecStart is wrapped in ``/bin/sh -c`` (needed for ``$(date)``
        # substitution); the actual alphalens invocation lives inside
        # the quoted argument. Match anywhere on the directive line.
        self.assertRegex(
            PAPER_SUBMIT_SERVICE.read_text(),
            re.compile(
                r"^ExecStart=.*%h/AlphaLens/\.venv/bin/alphalens\s+paper\s+submit\b",
                re.MULTILINE,
            ),
            "ExecStart must invoke the host venv `alphalens paper "
            "submit` binary (wrapped in /bin/sh -c for shell "
            "substitution) so the alpaca-py / pandas deps resolve "
            "locally.",
        )

    def test_submit_service_passes_yesterday_utc_date(self) -> None:
        # The brief the morning thematic-build wrote is dated YESTERDAY
        # UTC: ``alphalens thematic brief`` defaults to (today-1) and
        # run_thematic_day.sh runs it with no --date, so the 12:30 UTC
        # build on day D writes ``(D-1).parquet``. plan reads that
        # parquet by date and submit re-reads the ledger rows keyed on
        # the SAME brief_date, so both MUST pass --date yesterday or
        # plan FileNotFoundErrors / submit finds zero PLANNED rows.
        # Hard-code the systemd-escaped command-substitution form
        # (``%%`` is systemd's literal-percent escape; ExecStart must
        # be wrapped in ``/bin/sh -c`` so the substitution actually
        # runs — systemd execs the literal command otherwise).
        text = PAPER_SUBMIT_SERVICE.read_text()
        self.assertRegex(
            text,
            re.compile(
                r"^ExecStart=/bin/sh\s+-c\s+'.*--date \$\(date -u -d yesterday \+%%Y-%%m-%%d\).*'\s*$",
                re.MULTILINE,
            ),
            "Submit MUST be wrapped in /bin/sh -c '... --date "
            "$(date -u -d yesterday +%%Y-%%m-%%d)' so it consumes the "
            "(D-1)-dated brief the morning build wrote. systemd does "
            "not perform shell substitution natively; a bare ExecStart= "
            "would try to exec a binary named '$(date'.",
        )

    def test_submit_service_routes_to_test_alpaca_account(self) -> None:
        # Testing phase: the VPS paper timers route to the Alpaca TEST
        # paper account (--use-test-account), not the main paper account.
        # Pinned so a future "go live on main" is an explicit, reviewed
        # flag removal — not silent drift. The CLI flag is shared by
        # plan / submit / reconcile (commands/paper.py).
        text = PAPER_SUBMIT_SERVICE.read_text()
        exec_start_line = next(
            (line for line in text.splitlines() if line.startswith("ExecStart=")),
            None,
        )
        self.assertIsNotNone(exec_start_line, "ExecStart= directive missing")
        assert exec_start_line is not None
        self.assertIn(
            "--use-test-account",
            exec_start_line,
            "Submit ExecStart must carry --use-test-account during the "
            "testing phase so the VPS routes to the Alpaca test paper "
            "account, not main.",
        )

    def test_submit_service_gates_on_is_trading_day_via_exec_condition(self) -> None:
        # ``ExecCondition=`` semantics: exit 0 = proceed, exit 1-254 =
        # skip silently (no AlphalensJobFailed alert). The CLI subcommand
        # ``alphalens paper is-trading-day`` is the only thing that
        # knows about US public holidays falling on weekdays.
        text = PAPER_SUBMIT_SERVICE.read_text()
        self.assertRegex(
            text,
            re.compile(
                r"^ExecCondition=%h/AlphaLens/\.venv/bin/alphalens\s+paper\s+is-trading-day\s*$",
                re.MULTILINE,
            ),
            "Service must carry ExecCondition= calling "
            "`alphalens paper is-trading-day` — without it the "
            "OnCalendar=Mon..Fri filter would let US public holidays "
            "(e.g. Memorial Day Monday) through.",
        )

    def test_submit_timer_fires_mon_fri_at_13_25_utc(self) -> None:
        # 13:25 UTC = 09:25 ET = 5 min pre-XNYS open. The 5-min window
        # is the right slot for the GTC opening-cross submission: it
        # lands the order BEFORE the cross but with enough margin for
        # Alpaca's order-acknowledge latency. Holidays slip through
        # ``Mon..Fri`` and are caught by ExecCondition (above).
        self.assertRegex(
            PAPER_SUBMIT_TIMER.read_text(),
            re.compile(
                r"^OnCalendar=Mon\.\.Fri \*-\*-\* 13:25:00 UTC\s*$",
                re.MULTILINE,
            ),
            "Submit timer must fire Mon..Fri at 13:25 UTC (09:25 ET, "
            "5 min pre-XNYS open). 13:25 not 13:30 so the order is "
            "queued before the opening cross.",
        )

    def test_submit_timer_keeps_persistent_false(self) -> None:
        # Unlike the thematic-build timer, paper-submit MUST NOT
        # backfill missed slots. A Saturday-boot catch-up firing
        # "Monday's 13:25 UTC submit" on Saturday at 18:00 UTC would
        # push entry-tier limits against a closed XNYS — the very
        # stale-ladder gap risk PR-A (#294) fixed. The ExecCondition
        # gate would catch it too, but defence-in-depth: timer
        # explicitly does NOT use Persistent=true.
        # Tight directive-line check — fuzzy substring would also fire
        # on the unit-file COMMENT explaining why Persistent=true is
        # rejected. Re-compile MULTILINE so ``^...$`` binds to a
        # single directive line, not the whole file.
        self.assertNotRegex(
            PAPER_SUBMIT_TIMER.read_text(),
            re.compile(r"^Persistent=true\s*$", re.MULTILINE),
            "Paper-submit timer MUST NOT carry Persistent=true — a "
            "backfilled fire on the wrong UTC day would submit "
            "against a closed market. See PR-A (#294) stale-ladder "
            "gap risk.",
        )


class TestPaperReconcileUnit(unittest.TestCase):
    """PR-D — paper-reconcile systemd unit.

    Sweeps Alpaca for open-order status updates every 30 min during
    the XNYS regular session (09:30-16:00 ET = 13:30-20:00 UTC; the
    schedule pads to 14:00-21:00 UTC to also cover the +30min slot
    after market close so the final-minute fills get reconciled).
    Same ExecCondition pattern as submit.
    """

    def test_reconcile_service_uses_host_venv_alphalens(self) -> None:
        self.assertRegex(
            PAPER_RECONCILE_SERVICE.read_text(),
            re.compile(
                r"^ExecStart=%h/AlphaLens/\.venv/bin/alphalens\s+paper\s+reconcile\b",
                re.MULTILINE,
            ),
        )

    def test_reconcile_service_takes_no_date_arg(self) -> None:
        # Reconcile sweeps all OPEN orders across ALL dates — adding
        # ``--date $(date -u +%Y-%m-%d)`` would silently drop orders
        # placed on Friday that fill on Monday. Pin the no-date shape
        # on the ExecStart directive line only — the file-level
        # comment explains "no --date here" and would false-positive
        # a fuzzy substring search.
        text = PAPER_RECONCILE_SERVICE.read_text()
        exec_start_line = next(
            (line for line in text.splitlines() if line.startswith("ExecStart=")),
            None,
        )
        self.assertIsNotNone(exec_start_line, "ExecStart= directive missing")
        self.assertNotIn(
            "--date",
            exec_start_line,
            "Reconcile ExecStart MUST NOT carry --date — it sweeps "
            "all OPEN orders across dates, not a single brief's "
            "ladder.",
        )

    def test_reconcile_service_routes_to_test_alpaca_account(self) -> None:
        # Mirror of the submit test: the reconciler must target the SAME
        # Alpaca account its orders were submitted to. account-scoped
        # fetch_open_orders means a main-account reconcile would never
        # see the test-account orders. Pin --use-test-account here too.
        text = PAPER_RECONCILE_SERVICE.read_text()
        exec_start_line = next(
            (line for line in text.splitlines() if line.startswith("ExecStart=")),
            None,
        )
        self.assertIsNotNone(exec_start_line, "ExecStart= directive missing")
        assert exec_start_line is not None
        self.assertIn(
            "--use-test-account",
            exec_start_line,
            "Reconcile ExecStart must carry --use-test-account so it "
            "reconciles the same test paper account submit routes to.",
        )

    def test_reconcile_service_gates_on_is_trading_day_via_exec_condition(self) -> None:
        text = PAPER_RECONCILE_SERVICE.read_text()
        self.assertRegex(
            text,
            re.compile(
                r"^ExecCondition=%h/AlphaLens/\.venv/bin/alphalens\s+paper\s+is-trading-day\s*$",
                re.MULTILINE,
            ),
        )

    def test_reconcile_timer_fires_every_30_min_during_et_session(self) -> None:
        # Inclusive 14:00..21:00 UTC at every :00 + :30 = 15 fires
        # per session day. Covers 13:30-20:00 UTC regular session
        # plus a +30/+60 min trailing slot to reconcile final fills.
        # systemd ``X..Y/N`` minute syntax not used here — readability
        # of a flat HH:00,30 form beats the terser interval form for
        # operator scans of `systemctl --user list-timers`.
        self.assertRegex(
            PAPER_RECONCILE_TIMER.read_text(),
            re.compile(
                r"^OnCalendar=Mon\.\.Fri \*-\*-\* 14\.\.21:00,30:00 UTC\s*$",
                re.MULTILINE,
            ),
            "Reconcile timer must fire Mon..Fri every 30 min from "
            "14:00 to 21:00 UTC (covers 13:30-20:00 UTC XNYS regular "
            "session + trailing fills).",
        )

    def test_reconcile_timer_keeps_persistent_false(self) -> None:
        # Same rationale as submit — a missed 16:00 UTC slot replayed
        # at Saturday boot would hammer Alpaca for orders that no
        # longer matter. The next session's first slot picks up
        # whatever's open. MULTILINE regex pins the actual directive
        # line (the comment block above explains "why no Persistent"
        # and would false-positive a fuzzy substring match).
        self.assertNotRegex(
            PAPER_RECONCILE_TIMER.read_text(),
            re.compile(r"^Persistent=true\s*$", re.MULTILINE),
        )


class TestPaperPlanUnit(unittest.TestCase):
    """paper-plan systemd unit — the missing first link in the chain.

    Runs ``alphalens paper plan`` once per US trading day at 13:05 UTC,
    AFTER the 12:30 UTC thematic-build wrote that morning's (D-1) brief
    parquet and BEFORE paper-submit fires at 13:25 UTC. Without this
    unit the ledger has no PLANNED rows and submit pushes nothing.

    Date contract (the load-bearing subtlety): ``thematic brief``
    defaults to (today-1) and run_thematic_day.sh passes no --date, so
    the build on day D writes ``(D-1).parquet``. plan reads that file
    by date and submit re-reads the ledger keyed on the same
    brief_date, so plan AND submit MUST pass --date yesterday — see
    test_submit_service_passes_yesterday_utc_date.
    """

    def test_plan_service_uses_host_venv_alphalens(self) -> None:
        self.assertRegex(
            PAPER_PLAN_SERVICE.read_text(),
            re.compile(
                r"^ExecStart=.*%h/AlphaLens/\.venv/bin/alphalens\s+paper\s+plan\b",
                re.MULTILINE,
            ),
            "Plan ExecStart must invoke the host venv `alphalens paper plan` "
            "(wrapped in /bin/sh -c for the $(date) substitution).",
        )

    def test_plan_service_passes_yesterday_utc_date(self) -> None:
        # MUST match the submit unit's date token (both consume the
        # (D-1) brief the morning build wrote); a mismatch means submit
        # reads a brief_date plan never wrote -> zero PLANNED rows.
        self.assertRegex(
            PAPER_PLAN_SERVICE.read_text(),
            re.compile(
                r"^ExecStart=/bin/sh\s+-c\s+'.*--date \$\(date -u -d yesterday \+%%Y-%%m-%%d\).*'\s*$",
                re.MULTILINE,
            ),
            "Plan MUST pass --date $(date -u -d yesterday +%%Y-%%m-%%d), "
            "matching the submit unit, so it reads the (D-1).parquet the "
            "morning thematic-build wrote.",
        )

    def test_plan_service_routes_to_test_alpaca_account(self) -> None:
        exec_start_line = next(
            (
                line
                for line in PAPER_PLAN_SERVICE.read_text().splitlines()
                if line.startswith("ExecStart=")
            ),
            None,
        )
        self.assertIsNotNone(exec_start_line, "ExecStart= directive missing")
        assert exec_start_line is not None
        self.assertIn(
            "--use-test-account",
            exec_start_line,
            "Plan must route to the test account (matching submit/reconcile): "
            "plan tags PLANNED rows with its account and submit re-reads by "
            "(brief_date, account), so the two MUST agree.",
        )

    def test_plan_service_does_not_force(self) -> None:
        # --force deletes existing plans + shadow_log rows for the
        # brief_date. A timer re-fire (or manual + timer overlap) with
        # --force would wipe operator state right before submit reads
        # it. Omitting --force makes a duplicate run crash loud on the
        # UNIQUE(brief_date,ticker,account) constraint instead.
        exec_start_line = next(
            (
                line
                for line in PAPER_PLAN_SERVICE.read_text().splitlines()
                if line.startswith("ExecStart=")
            ),
            None,
        )
        self.assertIsNotNone(exec_start_line, "ExecStart= directive missing")
        assert exec_start_line is not None
        self.assertNotIn(
            "--force",
            exec_start_line,
            "Plan ExecStart MUST NOT carry --force — a re-fire would wipe "
            "the PLANNED rows submit is about to read.",
        )

    def test_plan_service_gates_on_is_trading_day_via_exec_condition(self) -> None:
        # plan has NO internal market-closed guard (only submit/reconcile
        # do), so ExecCondition is the ONLY holiday filter for plan.
        self.assertRegex(
            PAPER_PLAN_SERVICE.read_text(),
            re.compile(
                r"^ExecCondition=%h/AlphaLens/\.venv/bin/alphalens\s+paper\s+is-trading-day\s*$",
                re.MULTILINE,
            ),
            "Plan must carry ExecCondition=`alphalens paper is-trading-day` "
            "— it is the only holiday filter for the plan stage.",
        )

    def test_plan_service_no_doubled_alphalens_token(self) -> None:
        self.assertNotRegex(
            PAPER_PLAN_SERVICE.read_text(),
            re.compile(
                r"^ExecStart(?:Post)?=[^\n]*\.venv/bin/alphalens\s+alphalens\b", re.MULTILINE
            ),
            "`.venv/bin/alphalens alphalens ...` is a doubled-token bug.",
        )

    def test_plan_timer_fires_mon_fri_at_13_05_utc(self) -> None:
        # 13:05 UTC = after the 12:30 UTC build's normal ~12:45-12:58
        # finish, 20 min before submit at 13:25 UTC.
        self.assertRegex(
            PAPER_PLAN_TIMER.read_text(),
            re.compile(r"^OnCalendar=Mon\.\.Fri \*-\*-\* 13:05:00 UTC\s*$", re.MULTILINE),
            "Plan timer must fire Mon..Fri at 13:05 UTC (after the morning "
            "build, before the 13:25 UTC submit).",
        )

    def test_plan_timer_keeps_persistent_false(self) -> None:
        # Same rationale as submit/reconcile: a weekend/holiday catch-up
        # would plan against a stale brief. MULTILINE pins the directive
        # line (a comment may explain the rejection).
        self.assertNotRegex(
            PAPER_PLAN_TIMER.read_text(),
            re.compile(r"^Persistent=true\s*$", re.MULTILINE),
            "Paper-plan timer MUST NOT carry Persistent=true.",
        )

    def test_plan_timer_carries_install_section(self) -> None:
        text = PAPER_PLAN_TIMER.read_text()
        self.assertRegex(text, re.compile(r"^\[Install\]\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^WantedBy=timers\.target\s*$", re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
