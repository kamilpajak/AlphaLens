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

# The active timer-driven services the metrics hook must be wired into.
# Form-4 backfill is excluded — it is a long-running daemon (DONE
# 2026-05-08, per CLAUDE.md) and would emit a single point at end-of-run.
# The Alpaca/Saxo paper-trading + Saxo-refresh units were decommissioned
# with the broker chain (ADR 0012).

# Nightly broker-free ladder + population backfill (Track A v2 PR-T). The unit
# name is retained for the existing systemd timer (a rename is a deferred
# follow-up).
SHADOW_SERVICE = SYSTEMD_DIR / "alphalens-feedback-shadow-returns.service"
SHADOW_TIMER = SYSTEMD_DIR / "alphalens-feedback-shadow-returns.timer"

ACTIVE_SERVICES = (
    EDGAR_SERVICE,
    LIT_WEEKLY_SERVICE,
    LIT_MONTHLY_SERVICE,
    SYSTEMD_DIR / "alphalens-av-earnings-backfill.service",
    SYSTEMD_DIR / "alphalens-thematic-build.service",
    SHADOW_SERVICE,
    SYSTEMD_DIR / "alphalens-form4-incremental.service",
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

    def test_thematic_build_docker_run_routes_textfile_dir_to_node_exporter(self):
        # The 5 thematic stages + `alphalens cache refresh-vix` all run as
        # subprocesses INSIDE this one container and emit Prometheus textfile
        # gauges (alphalens_thematic_stage_* #373/#374, alphalens_vix_cache_
        # fetched_at_timestamp_seconds #376) via emit_domain_metrics. Without
        # an explicit ALPHALENS_TEXTFILE_DIR the container's _resolve_dir()
        # falls back to Path.home()/.alphalens/metrics (= the bind-mounted
        # host ~/.alphalens/metrics), which node_exporter does NOT scrape
        # (live --collector.textfile.directory=/var/lib/node_exporter/textfile).
        # The series then never reach Prometheus and the dead-man-switch +
        # VIX-staleness alerts have no input (AlphalensVixCacheMetricMissing
        # would false-fire on absent() forever). Pin the explicit-value form
        # (NOT bare `-e KEY`): the path is non-secret infra config, so
        # hardcoding it is correct + robust whether or not /etc/alphalens/env
        # defines the var. NOTE: this regex is a regression-guard against the
        # flag being dropped — the real acceptance gate is the live
        # `curl localhost:9100/metrics | grep alphalens_(thematic|vix)`.
        self.assertRegex(
            self.unit_text,
            re.compile(
                r"^\s+-e ALPHALENS_TEXTFILE_DIR=/var/lib/node_exporter/textfile\s*\\?\s*$",
                re.MULTILINE,
            ),
            "ExecStart docker invocation MUST set "
            "`-e ALPHALENS_TEXTFILE_DIR=/var/lib/node_exporter/textfile` so "
            "container-side emit_domain_metrics writes to the scraped dir.",
        )

    def test_thematic_build_docker_run_mounts_node_exporter_textfile_dir(self):
        # The -e above only picks the dir; the container must also be able to
        # WRITE to the host's real scrape dir. An identity bind mount maps the
        # host /var/lib/node_exporter/textfile (jacoren-writable; host hooks
        # already write there) to the same path inside the --user %U:%G
        # container. Without it the .prom files land in an ephemeral in-
        # container dir that vanishes with --rm.
        self.assertRegex(
            self.unit_text,
            re.compile(
                r"^\s+-v /var/lib/node_exporter/textfile:/var/lib/node_exporter/textfile\s*\\?\s*$",
                re.MULTILINE,
            ),
            "ExecStart docker invocation MUST bind-mount "
            "`/var/lib/node_exporter/textfile` (identity) so container emits "
            "land on the host dir node_exporter scrapes.",
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

    def test_thematic_build_no_longer_rebuilds_ladder_outcomes(self):
        # rebuild-ladder-outcomes MOVED to the shadow-returns ExecStartPost —
        # the only job that (re)writes the population-ladder parquet. Leaving
        # it here too re-synced unchanged data 5× per day AND was the source of
        # the up-to-one-slot (~2h) dashboard lag behind the nightly recompute
        # (the recompute lands after the morning thematic-build slot, so the
        # mirror waited for the next slot). Directive-line match (multiline
        # ``^``) so a leftover explanatory comment does not falsely trip it.
        self.assertNotRegex(
            self.unit_text,
            re.compile(
                r"^ExecStartPost=[^\n]*(?:\\\n[^\n]*)*rebuild-ladder-outcomes\b",
                re.MULTILINE,
            ),
            "rebuild-ladder-outcomes must live on the shadow-returns unit "
            "(where the parquet is written), not thematic-build.",
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
        # Guard MUST gate on the STAGED diff (`git diff --cached --quiet`)
        # AFTER `git add` — so a brand-new untracked file (new ISO-week /
        # new month) is staged and seen, while a byte-for-byte idempotent
        # re-fire stages nothing and still skips with no empty commit.
        self.assertIn("git diff --cached --quiet", text)
        # The plain `git diff --quiet` form (no --cached) must be GONE: it
        # only inspects tracked modifications and silently skipped every
        # new-period file (regression confirmed on VPS: 2026-06.md +
        # 2026-W22.md untracked, never committed).
        self.assertNotRegex(text, re.compile(r"git diff --quiet(?!\S)"))

    def test_wrapper_stages_before_gate_so_untracked_file_is_committed(self) -> None:
        # Regression for the silent new-period drop: the skip-gate MUST run
        # `git add` BEFORE testing the diff, or an untracked new-period file
        # is invisible to the gate and never committed. Pin the order.
        # Anchor on the actual DIRECTIVE lines (not substrings that also
        # appear in the explanatory comment) so the order check is robust.
        lines = LIT_PUBLISH_WRAPPER.read_text().splitlines()
        add_line = next(
            (
                i
                for i, ln in enumerate(lines)
                if ln.strip().startswith("git add docs/research/literature_review/")
            ),
            None,
        )
        gate_line = next(
            (
                i
                for i, ln in enumerate(lines)
                if ln.strip().startswith("if git diff --cached --quiet")
            ),
            None,
        )
        self.assertIsNotNone(
            add_line, "`git add docs/research/literature_review/` directive line missing"
        )
        self.assertIsNotNone(gate_line, "`if git diff --cached --quiet` directive line missing")
        assert add_line is not None and gate_line is not None
        self.assertLess(
            add_line,
            gate_line,
            "`git add` must precede the `git diff --cached --quiet` skip-gate "
            "so a new untracked period file is staged and committed (not "
            "silently skipped).",
        )

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

    def test_run_thematic_day_uses_experts_cli(self) -> None:
        # PR-2 renamed `buffett qual-enrich` / `buffett migrate-qual-cache` to the
        # registry-driven `experts` surface; the deploy script must invoke the new
        # commands (migrate strictly before enrich) and carry NO stale old command.
        script_text = RUN_THEMATIC_SCRIPT.read_text()
        self.assertIn("alphalens experts migrate-qual-cache", script_text)
        self.assertRegex(
            script_text,
            re.compile(r"^alphalens experts enrich .*--all --scuttlebutt", re.MULTILINE),
        )
        self.assertNotIn("buffett qual-enrich", script_text)
        self.assertNotIn("buffett migrate-qual-cache", script_text)
        # Ordering: the migrate COMMAND must run before the enrich COMMAND
        # (short-circuit before recompute). Anchor on the `alphalens ...`
        # invocations, not the comment prose that also names them.
        self.assertLess(
            script_text.index("alphalens experts migrate-qual-cache"),
            script_text.index("alphalens experts enrich"),
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


class TestShadowReturnsUnit(unittest.TestCase):
    """Nightly shadow-return backfill unit (Track A v2 PR-T).

    Pins the directives the cron-health + parity tests can't infer: the
    06:30 UTC daily slot (all of the prior day's +5-session horizons + their
    opening windows are closed — Polygon Basic serves only closed sessions),
    ``Persistent=true`` (a missed nightly sweep should replay; the 14-day
    window makes the catch-up cheap + idempotent), the host-venv backfill
    subcommand (NOT the single-date ``compute-shadow-returns``, which is
    always inert on today's brief), and the ``POLYGON_API_KEY`` env the
    pricing leg needs. The emit-hook + staleness-rule are pinned by the
    glob-derived sibling suites.
    """

    def test_service_is_oneshot_with_working_dir(self) -> None:
        text = SHADOW_SERVICE.read_text()
        self.assertIn("Type=oneshot", text)
        self.assertIn("WorkingDirectory=%h/AlphaLens", text)

    def test_service_invokes_backfill_subcommand_on_host_venv(self) -> None:
        # The sweep subcommand, not single-date compute-shadow-returns (which
        # would always price 0 — today's brief never matured). One token, no
        # doubled `alphalens`.
        self.assertRegex(
            SHADOW_SERVICE.read_text(),
            re.compile(
                r"^ExecStart=%h/AlphaLens/\.venv/bin/alphalens\s+feedback\s+"
                r"backfill-shadow-returns\b",
                re.MULTILINE,
            ),
            "ExecStart must run `feedback backfill-shadow-returns` on the host "
            "venv binary (sweep mode), not the single-date command.",
        )

    def test_service_loads_etc_alphalens_env_fail_loud(self) -> None:
        self.assertRegex(
            SHADOW_SERVICE.read_text(),
            re.compile(r"^EnvironmentFile=/etc/alphalens/env\s*$", re.MULTILINE),
            "Must load /etc/alphalens/env without a leading dash (fail loud on "
            "missing POLYGON_API_KEY rather than silently pricing nothing).",
        )

    def test_service_orders_after_docker_for_compose_post(self) -> None:
        # The rebuild-ladder-outcomes ExecStartPost runs `docker compose`, so
        # the unit must order After=docker.service (matching thematic-build).
        # Without it, a freshly-booted VPS could fire the timer before dockerd
        # is ready, the compose call fails, and the whole unit is marked failed
        # until the next nightly run (~24h). (zen MEDIUM, PR #493.)
        self.assertRegex(
            SHADOW_SERVICE.read_text(),
            re.compile(r"^After=.*\bdocker\.service\b.*$", re.MULTILINE),
            "Unit runs `docker compose` in ExecStartPost — must order "
            "After=docker.service so the daemon is ready.",
        )

    def test_service_rebuilds_ladder_outcomes_post_run(self) -> None:
        # The population-ladder parquet is (re)written ONLY by this nightly
        # recompute, so the edge Postgres mirror (the maintenance
        # ``rebuild-ladder-outcomes`` one-shot from the django-prod compose
        # stack) belongs here as an ExecStartPost. It fires only after a
        # successful ExecStart, so a failed recompute leaves the cache
        # untouched. This makes the edge dashboard fresh right after the
        # recompute rather than waiting for the next thematic-build slot.
        self.assertRegex(
            SHADOW_SERVICE.read_text(),
            re.compile(
                r"^ExecStartPost=[^\n]*(?:\\\n[^\n]*)*rebuild-ladder-outcomes\b",
                re.MULTILINE,
            ),
            "Missing or malformed ExecStartPost — the edge Postgres cache will "
            "not pick up the freshly recomputed population-ladder parquet until "
            "the next thematic-build slot (~2h lag).",
        )

    def test_service_documents_polygon_api_key_requirement(self) -> None:
        # POLYGON_API_KEY is the one secret this job needs (the pricing leg).
        # A missing key surfaces as an all-skipped sweep, not a hard failure,
        # so the requirement must be visible in the unit for the operator.
        self.assertIn("POLYGON_API_KEY", SHADOW_SERVICE.read_text())

    def test_service_wires_emit_hook_with_own_job_name(self) -> None:
        self.assertRegex(
            SHADOW_SERVICE.read_text(),
            re.compile(
                r"^ExecStopPost=%h/AlphaLens/deploy/systemd/bin/"
                r"alphalens-emit-job-metrics\s+feedback-shadow-returns\s*$",
                re.MULTILINE,
            ),
        )

    def test_service_sets_generous_timeout_for_rate_limited_sweep(self) -> None:
        # The 14-day sweep × Polygon ~5 req/min throttle can run long after a
        # VPS-downtime backlog (Persistent replay = the largest run). 45min
        # gives headroom; a timeout-kill is self-healing (next nightly fire
        # re-covers via idempotency).
        self.assertRegex(
            SHADOW_SERVICE.read_text(),
            re.compile(r"^TimeoutStartSec=45min\s*$", re.MULTILINE),
        )

    def test_timer_fires_daily_at_0630_utc_persistent(self) -> None:
        text = SHADOW_TIMER.read_text()
        self.assertRegex(text, re.compile(r"^OnCalendar=\*-\*-\* 06:30:00 UTC\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^Persistent=true\s*$", re.MULTILINE))

    def test_timer_carries_install_section(self) -> None:
        text = SHADOW_TIMER.read_text()
        self.assertRegex(text, re.compile(r"^\[Install\]\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^WantedBy=timers\.target\s*$", re.MULTILINE))


class TestStartLimitInUnitSection(unittest.TestCase):
    """``StartLimitIntervalSec`` / ``StartLimitBurst`` are [Unit]-section keys in
    modern systemd; under [Service] they are silently ignored ("Unknown key
    name ... in section 'Service'"), so the crash-loop cap never applies. Pin
    that no alphalens unit misplaces them (caught live on the VPS when the
    trade-stream daemon was deployed).
    """

    _START_LIMIT_KEYS = ("StartLimitIntervalSec", "StartLimitBurst", "StartLimitInterval")

    def _section_of_directives(self, text: str) -> dict[str, str]:
        """Map each StartLimit* directive line to the section it sits in."""
        placement: dict[str, str] = {}
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line
                continue
            if line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key in self._START_LIMIT_KEYS:
                placement[key] = section
        return placement

    def test_no_alphalens_unit_puts_startlimit_in_service_section(self) -> None:
        offenders: list[str] = []
        checked = 0
        for unit in sorted(SYSTEMD_DIR.glob("alphalens-*.service")):
            placement = self._section_of_directives(unit.read_text())
            for key, section in placement.items():
                checked += 1
                if section != "[Unit]":
                    offenders.append(f"{unit.name}: {key} in {section or '(no section)'}")
        # Positive control: at least one unit actually declares these, so the
        # test cannot pass vacuously if the directives are renamed/removed.
        self.assertGreater(checked, 0, "no StartLimit* directives found to check")
        self.assertEqual(offenders, [], f"StartLimit* must live in [Unit]: {offenders}")


if __name__ == "__main__":
    unittest.main()
