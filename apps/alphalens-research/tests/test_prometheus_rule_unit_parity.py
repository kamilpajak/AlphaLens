"""Pin the prometheus-rule <-> systemd-unit parity for staleness alerts.

Failure class this guards (cron-observability "blind spot" drift):

  1. **Orphan rule** — an ``AlphalensJobStale`` rule references a
     ``job="<name>"`` label that NO systemd unit actually emits via its
     ``ExecStopPost=alphalens-emit-job-metrics <name>`` hook. The
     ``alphalens_job_last_success_timestamp_seconds{job="<name>"}`` series
     never exists, so the alert can never fire and never resolve — a dead
     rule that silently does nothing. A typo in the job label
     (``literature-weekly`` vs ``literature-scan-weekly``) produces exactly
     this.

  2. **Uncovered unit** — a metric-emitting unit that SHOULD page on
     outage has no paired ``AlphalensJobStale`` rule. That unit can be
     down for days without the operator noticing (the worst monitoring
     blind spot). Adding a new cron-driven unit without its rule is the
     concrete regression.

``test_monitoring_alerts.py`` already checks forward-completeness against a
HARDCODED ``ACTIVE_JOBS`` tuple
(``test_every_active_job_has_a_staleness_rule``). This file adds the two
pieces it lacks, both driven from the REAL sources of truth rather than a
hand-maintained constant:

  * the set of jobs that units actually emit is parsed from the
    ``alphalens-emit-job-metrics <job>`` ExecStopPost hooks in
    ``deploy/systemd/*.service`` (the same hook
    ``test_deploy_systemd_units.py::test_every_active_service_wires_emit_hook``
    pins), and
  * the set of staleness-rule job labels is parsed from
    ``deploy/monitoring/prometheus/rules/alphalens.yaml``.

The paper jobs (``paper-plan`` / ``paper-submit`` / ``paper-reconcile``)
emit job metrics BUT are deliberately staleness-rule-exempt: they run only
on US trading days, so a weekend / holiday gap would false-page on a
staleness window. Their failures are caught by the global
``AlphalensJobFailed`` (non-zero exit) alert instead — see
``project_paper_chain_date_contract_2026_06_01`` ("No staleness alert for
paper jobs (weekend false-page); failures covered by global
AlphalensJobFailed"). They are listed in ``STALENESS_EXEMPT_JOBS`` below.

Positive control: synthetic inputs (a rule with ``job="nonexistent"``, and
an emitting unit with no rule) are fed through the same parity logic and
MUST be flagged — so the bidirectional check can never silently rot to a
no-op.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

# tests/<name>.py -> repo root is three parents up; deploy/ is at root.
REPO_ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
RULES_PATH = REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alphalens.yaml"

STALENESS_ALERT_NAME = "AlphalensJobStale"

# Hook line shape pinned by test_deploy_systemd_units.py::
# test_every_active_service_wires_emit_hook. The trailing token is the
# short job name that becomes the ``job=`` Prometheus label.
EMIT_HOOK_RE = re.compile(
    r"^ExecStopPost=%h/AlphaLens/deploy/systemd/bin/"
    r"alphalens-emit-job-metrics\s+(?P<job>\S+)\s*$",
    re.MULTILINE,
)

# Jobs that emit metrics but are intentionally NOT given an
# AlphalensJobStale rule. The paper chain runs only on US trading days;
# a staleness window spanning a weekend / holiday would false-page. Their
# outages surface via the global AlphalensJobFailed (non-zero exit) alert
# instead. Documented in project_paper_chain_date_contract_2026_06_01.
STALENESS_EXEMPT_JOBS: frozenset[str] = frozenset(
    {
        "paper-plan",
        "paper-submit",
        "paper-reconcile",
    }
)


def _emitting_jobs() -> set[str]:
    """Job names emitted by ANY systemd unit's metrics ExecStopPost hook."""
    jobs: set[str] = set()
    for service in sorted(SYSTEMD_DIR.glob("*.service")):
        for match in EMIT_HOOK_RE.finditer(service.read_text()):
            jobs.add(match.group("job"))
    return jobs


def _staleness_rule_jobs() -> set[str]:
    """``job=`` labels referenced by AlphalensJobStale rules in the YAML."""
    rules = yaml.safe_load(RULES_PATH.read_text())["groups"][0]["rules"]
    return _staleness_jobs_from_rules(rules)


def _staleness_jobs_from_rules(rules: list[dict]) -> set[str]:
    """Pure helper: extract job labels from a list of rule dicts.

    Split out from file IO so the positive-control tests can feed
    synthetic rule lists through the identical extraction path.
    """
    jobs: set[str] = set()
    for rule in rules:
        if rule.get("alert") != STALENESS_ALERT_NAME:
            continue
        match = re.search(r'job="([^"]+)"', rule.get("expr", ""))
        if match:
            jobs.add(match.group(1))
    return jobs


class TestPrometheusRuleUnitParity(unittest.TestCase):
    """Bidirectional staleness-rule <-> emitting-unit completeness."""

    def test_sources_are_non_empty(self) -> None:
        # Guard against a parser / glob that silently matched nothing —
        # which would make every parity assertion below vacuously pass.
        self.assertTrue(
            _emitting_jobs(),
            "No emit-job-metrics hooks found in deploy/systemd/*.service — "
            "the ExecStopPost parser likely drifted from the unit-file form.",
        )
        self.assertTrue(
            _staleness_rule_jobs(),
            "No AlphalensJobStale job labels parsed from the rules YAML — "
            "the rule parser likely drifted.",
        )

    def test_every_staleness_rule_job_is_emitted_by_a_unit(self) -> None:
        # FORWARD direction: an AlphalensJobStale rule whose job label is
        # emitted by no unit references a series that never exists, so the
        # alert can never fire (dead rule / typo'd job label).
        rule_jobs = _staleness_rule_jobs()
        emitted = _emitting_jobs()
        orphans = rule_jobs - emitted
        self.assertEqual(
            orphans,
            set(),
            f"AlphalensJobStale rule(s) reference job label(s) that no "
            f"systemd unit emits: {sorted(orphans)}. The "
            f"alphalens_job_last_success_timestamp_seconds series for "
            f"these never exists, so the alert can never fire. Fix the "
            f"job= label to match an emit-job-metrics hook, or add the "
            f"missing unit. Emitting units: {sorted(emitted)}.",
        )

    def test_every_emitting_unit_has_a_staleness_rule(self) -> None:
        # BACKWARD direction: a metric-emitting unit with no paired
        # staleness rule can be down for days without paging. The paper
        # jobs are the documented exception (weekend false-page).
        emitted = _emitting_jobs()
        rule_jobs = _staleness_rule_jobs()
        uncovered = emitted - rule_jobs - STALENESS_EXEMPT_JOBS
        self.assertEqual(
            uncovered,
            set(),
            f"Metric-emitting unit(s) have no AlphalensJobStale rule: "
            f"{sorted(uncovered)}. Each can be down for days without "
            f"paging. Add a per-job rule in "
            f"deploy/monitoring/prometheus/rules/alphalens.yaml, or — if "
            f"the unit is intentionally rule-exempt (e.g. trading-day-only "
            f"like the paper jobs) — add it to STALENESS_EXEMPT_JOBS with "
            f"the rationale.",
        )

    def test_exempt_jobs_are_actually_emitting_and_unruled(self) -> None:
        # Keep STALENESS_EXEMPT_JOBS honest: an entry that no longer emits
        # metrics (unit deleted) is dead weight, and an entry that DID get
        # a staleness rule contradicts the exemption. Either drift means
        # the exclude-set rotted and should be re-examined.
        emitted = _emitting_jobs()
        rule_jobs = _staleness_rule_jobs()
        for job in STALENESS_EXEMPT_JOBS:
            self.assertIn(
                job,
                emitted,
                f"Exempt job {job!r} no longer emits metrics — remove it "
                "from STALENESS_EXEMPT_JOBS (stale exemption).",
            )
            self.assertNotIn(
                job,
                rule_jobs,
                f"Exempt job {job!r} now HAS an AlphalensJobStale rule — "
                "the exemption contradicts reality. Drop it from "
                "STALENESS_EXEMPT_JOBS or remove the rule.",
            )


class TestParityPositiveControls(unittest.TestCase):
    """Feed deliberately-broken inputs through the parity logic.

    Project rule: every parity scanner must prove its check FAILS on a
    broken input so the assertion can never silently degrade to a no-op.
    """

    def test_orphan_rule_job_is_flagged(self) -> None:
        # A staleness rule pointing at a job no unit emits must be caught
        # by the forward (rule -> unit) check.
        synthetic_rules = [
            {
                "alert": STALENESS_ALERT_NAME,
                "expr": "time() - alphalens_job_last_success_timestamp_seconds"
                '{job="nonexistent"} > 1800',
            }
        ]
        rule_jobs = _staleness_jobs_from_rules(synthetic_rules)
        emitted = _emitting_jobs()  # real emitting set — "nonexistent" is not in it
        orphans = rule_jobs - emitted
        self.assertIn(
            "nonexistent",
            orphans,
            'Positive control failed: a rule with job="nonexistent" must '
            "be flagged as an orphan (forward direction is a no-op).",
        )

    def test_uncovered_emitting_unit_is_flagged(self) -> None:
        # An emitting unit with no rule (and not exempt) must be caught by
        # the backward (unit -> rule) check. Simulate by adding a fake
        # emitting job to the real emitting set and an empty rule set.
        emitted = _emitting_jobs() | {"new-cron-unit"}
        rule_jobs: set[str] = set()  # nothing has a rule
        uncovered = emitted - rule_jobs - STALENESS_EXEMPT_JOBS
        self.assertIn(
            "new-cron-unit",
            uncovered,
            "Positive control failed: a new emitting unit with no rule must "
            "be flagged as uncovered (backward direction is a no-op).",
        )

    def test_real_inputs_do_not_match_synthetic_breakage(self) -> None:
        # Sanity: the synthetic broken job names must NOT collide with any
        # real job, otherwise the positive controls would be measuring the
        # wrong thing.
        real = _emitting_jobs() | _staleness_rule_jobs()
        self.assertNotIn("nonexistent", real)
        self.assertNotIn("new-cron-unit", real)


if __name__ == "__main__":
    unittest.main()
