"""Glob-derived completeness check for the job-metrics ExecStopPost hook.

Incident class pinned: "a new cron-driven systemd unit ships without the
``alphalens-emit-job-metrics`` ExecStopPost hook, so its cron-health
metrics (last_success_timestamp / duration / exit_code) never update and
Alertmanager fires a false stale-job alert (or, worse, never fires when
the unit silently dies)." This is the same failure the PR-2 hook
(cron-observability epic) was introduced to prevent — see PR-2 of the
epic and ``deploy/monitoring`` (PR-3).

Why a SEPARATE test from ``test_deploy_systemd_units.py``:
  ``TestJobMetricsHook.test_every_active_service_wires_emit_hook`` already
  asserts the hook shape, but it iterates a HARD-CODED ``ACTIVE_SERVICES``
  tuple. That allowlist is exactly the "no hardcoded allowlist"
  anti-pattern from the integration-e2e test-strategy memo
  (``docs/research/integration_e2e_test_strategy_2026_06_01.md``,
  Phase 1b): a brand-new ``alphalens-foo.service`` added without also
  editing the tuple ships uncovered — the existing test stays green
  because it never looks at the new file.

  This test derives the unit set from a filesystem glob of
  ``deploy/systemd/alphalens-*.service`` instead, so the moment a new
  unit lands it is in scope. The author must either wire the emit hook
  OR add the unit to the DOCUMENTED ``EXEMPT_JOBS`` set below (with a
  reason) — both are a reviewed, explicit decision rather than silent
  drift.

  To avoid duplicating the per-line shape assertions already covered by
  the sibling test, this file pins only the GLOB-DERIVED completeness
  invariant: "every non-exempt alphalens-*.service has an emit-hook line
  naming its own job". The exact ``%h``-rooted path + job-name-matches-
  stem regex is shared so a unit that wires a malformed hook still fails.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

# Test file lives at apps/alphalens-research/tests/<name>.py; the repo
# root is three parents up. deploy/ stays at the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"

# The emitter the cron-observability epic (PR-2) wired into every
# timer-driven service. Same script the sibling test pins; referenced
# here only to build the per-job ExecStopPost regex.
EMIT_HOOK_REL = "deploy/systemd/bin/alphalens-emit-job-metrics"

# Short job name = unit stem minus the ``alphalens-`` prefix. The emit
# hook's argument MUST equal this so the bash helper's ``systemctl show``
# probe targets the right unit (and the Prometheus ``job`` label matches
# the staleness rules in deploy/monitoring/prometheus/rules/alphalens.yaml).
_PREFIX = "alphalens-"


# Services that legitimately do NOT emit per-run job metrics. Each entry
# is the short job name (stem minus ``alphalens-``) and MUST carry a
# one-line reason. Anything not listed here is REQUIRED to wire the hook.
#
# Keep this set in sync with the rationale already documented in
# test_deploy_systemd_units.py (the ACTIVE_SERVICES comment) and
# test_monitoring_alerts.py (the ACTIVE_JOBS comment): both exclude
# form4-backfill for the same reason.
EXEMPT_JOBS: dict[str, str] = {
    "form4-backfill": (
        "Long-running daemon (Type=simple + Restart=on-failure), DONE "
        "2026-05-08. A per-run ExecStopPost hook would emit a single "
        "point at process exit, not the per-fire cadence the textfile "
        "metrics + staleness alerts are designed around. Instrumented "
        "separately if ever needed. Mirrors the exclusion already "
        "documented in test_deploy_systemd_units.py + test_monitoring_alerts.py."
    ),
}


def _job_name(service_path: Path) -> str:
    """``alphalens-edgar-detect.service`` -> ``edgar-detect``."""
    return service_path.stem.removeprefix(_PREFIX)


def _emit_hook_regex(job: str) -> re.Pattern[str]:
    """ExecStopPost directive invoking the emitter with this job name.

    Bound to a directive line (multiline ``^``) so a comment mentioning
    the hook cannot satisfy the check. Trailing ``\\s*$`` rejects extra
    args after the job name.
    """
    return re.compile(
        # Optional leading ``-`` tolerates the failure-tolerant
        # ``ExecStopPost=-...`` form (systemd ignores the exit code); unused
        # today but a unit author could adopt it.
        r"^-?ExecStopPost=%h/AlphaLens/"
        + re.escape(EMIT_HOOK_REL)
        + r"\s+"
        + re.escape(job)
        + r"\s*$",
        re.MULTILINE,
    )


def _discover_services() -> list[Path]:
    return sorted(SYSTEMD_DIR.glob("alphalens-*.service"))


class TestMetricsHookCompleteness(unittest.TestCase):
    """Glob-derived: every non-exempt unit wires the emit hook."""

    def test_glob_finds_service_units(self) -> None:
        # Guard against the scanner silently rotting to a no-op if the
        # directory moves or the glob pattern stops matching. The sibling
        # test references at least edgar-detect + thematic-build, so a
        # zero-length glob means the path resolution broke, not that the
        # repo genuinely has no units.
        services = _discover_services()
        self.assertTrue(
            services,
            f"No alphalens-*.service files found under {SYSTEMD_DIR} — the "
            "glob or REPO_ROOT resolution is broken; the completeness check "
            "would silently pass on an empty set.",
        )

    def test_exempt_jobs_correspond_to_real_units(self) -> None:
        # A stale EXEMPT_JOBS entry (unit renamed / deleted) would silently
        # widen the exclude-set against a unit that no longer exists,
        # masking a future name collision. Pin every exemption to a real
        # file so the set cannot drift away from the filesystem.
        present_jobs = {_job_name(p) for p in _discover_services()}
        unknown = set(EXEMPT_JOBS) - present_jobs
        self.assertEqual(
            unknown,
            set(),
            f"EXEMPT_JOBS lists jobs with no matching unit file: {sorted(unknown)}. "
            "Remove the stale exemption or fix the job name.",
        )

    def test_every_non_exempt_unit_wires_emit_hook(self) -> None:
        # The completeness invariant. A NEW alphalens-*.service that runs
        # a cron-driven job MUST wire ExecStopPost=...alphalens-emit-job-
        # metrics <its-own-job-name> — or be added to EXEMPT_JOBS with a
        # reason. Because this iterates the glob (not a hardcoded tuple),
        # a unit added without touching this file still gets checked.
        missing: list[str] = []
        for service_path in _discover_services():
            job = _job_name(service_path)
            if job in EXEMPT_JOBS:
                continue
            text = service_path.read_text()
            if not _emit_hook_regex(job).search(text):
                missing.append(service_path.name)

        self.assertEqual(
            missing,
            [],
            "These systemd units run a cron-driven job but do NOT wire the "
            "metrics hook (ExecStopPost=%h/AlphaLens/"
            + EMIT_HOOK_REL
            + " <job>): "
            + ", ".join(missing)
            + ". Without it the unit's cron-health metrics never update and "
            "Alertmanager fires a false stale alert (or stays silent when it "
            "dies). Add the ExecStopPost line, or — if this unit legitimately "
            "does not emit per-run metrics (e.g. a long-running daemon) — add "
            "its short job name to EXEMPT_JOBS in this file with a reason.",
        )


class TestMetricsHookCompletenessPositiveControl(unittest.TestCase):
    """Feed deliberately-broken inputs; assert the check FAILS on them.

    Mandatory positive control (project rule): a parity scanner that can
    never fail is worthless. These exercise the same per-job regex the
    real check uses, against synthetic unit bodies, so the assertion can
    never silently rot to a no-op.
    """

    def test_unit_with_no_exec_stop_post_is_flagged(self) -> None:
        # A new oneshot service that forgot the hook entirely — the exact
        # regression this scanner exists to catch.
        body = (
            "[Unit]\n"
            "Description=Alphalens new cron job\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            "WorkingDirectory=%h/AlphaLens\n"
            "ExecStart=%h/AlphaLens/.venv/bin/alphalens newjob run\n"
        )
        self.assertIsNone(
            _emit_hook_regex("newjob").search(body),
            "A unit body with no ExecStopPost MUST NOT match the emit-hook "
            "regex — the completeness check would be a no-op otherwise.",
        )

    def test_unit_with_wrong_job_name_is_flagged(self) -> None:
        # Copy-paste bug: the hook is present but names a DIFFERENT job, so
        # the systemctl-show probe + Prometheus job label point at the
        # wrong unit. The job-name-matches-stem requirement must catch it.
        body = (
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=%h/AlphaLens/.venv/bin/alphalens newjob run\n"
            "ExecStopPost=%h/AlphaLens/" + EMIT_HOOK_REL + " edgar-detect\n"
        )
        self.assertIsNone(
            _emit_hook_regex("newjob").search(body),
            "An emit hook naming the wrong job (edgar-detect) MUST NOT "
            "satisfy the check for the 'newjob' unit.",
        )

    def test_hook_only_in_comment_is_flagged(self) -> None:
        # A commented-out / explanatory mention of the hook must not pass
        # the directive-line-anchored check.
        body = (
            "[Service]\n"
            "Type=oneshot\n"
            "# This job should later wire "
            "ExecStopPost=%h/AlphaLens/" + EMIT_HOOK_REL + " newjob\n"
            "ExecStart=%h/AlphaLens/.venv/bin/alphalens newjob run\n"
        )
        self.assertIsNone(
            _emit_hook_regex("newjob").search(body),
            "A comment mentioning the hook MUST NOT satisfy the directive-line-anchored regex.",
        )

    def test_correctly_wired_unit_passes(self) -> None:
        # The other half of the control: a properly wired body MUST match,
        # so the regex isn't accidentally impossible to satisfy.
        body = (
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=%h/AlphaLens/.venv/bin/alphalens newjob run\n"
            "ExecStopPost=%h/AlphaLens/" + EMIT_HOOK_REL + " newjob\n"
        )
        self.assertIsNotNone(
            _emit_hook_regex("newjob").search(body),
            "A correctly wired ExecStopPost line MUST match — otherwise the "
            "real check would false-fail on every unit.",
        )


if __name__ == "__main__":
    unittest.main()
