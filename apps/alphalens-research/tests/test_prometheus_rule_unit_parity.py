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

The Alpaca/Saxo paper-trading + Saxo-refresh units that used to populate the
``STALENESS_EXEMPT_JOBS`` set were decommissioned with the broker chain
(ADR 0012), so the set is now empty: every surviving emitting unit fires on a
fixed daily/weekly cadence and carries a staleness rule.

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

from tests.test_edge_mirror_metrics_parity import EDGE_MIRROR_GAUGES

# tests/<name>.py -> repo root is three parents up; deploy/ is at root.
REPO_ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
RULES_PATH = REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alphalens.yaml"

STALENESS_ALERT_NAME = "AlphalensJobStale"

# Hook line shape pinned by test_deploy_systemd_units.py::
# test_every_active_service_wires_emit_hook. The trailing token is the
# short job name that becomes the ``job=`` Prometheus label.
EMIT_HOOK_RE = re.compile(
    r"^-?ExecStopPost=%h/AlphaLens/deploy/systemd/bin/"
    r"alphalens-emit-job-metrics\s+(?P<job>\S+)\s*$",
    re.MULTILINE,
)

# Jobs that emit metrics but are intentionally NOT given an
# AlphalensJobStale rule. The Alpaca/Saxo paper-trading + Saxo-refresh units
# that used to populate this set were decommissioned with the broker chain
# (ADR 0012), so the set is currently empty: every surviving emitting unit
# fires on a fixed daily/weekly cadence and DOES carry a staleness rule. Add
# a job here only if a future trading-day-only unit would false-page on a
# weekend staleness window.
STALENESS_EXEMPT_JOBS: frozenset[str] = frozenset(
    {
        # edge-mirror is covered by the dedicated AlphalensEdgeStale alert rather than
        # the generic AlphalensJobStale per-job rule. Since #1436 that alert watches
        # the ingest watermark the mirror READ (alphalens_edge_mirror_watermark_
        # timestamp_seconds), not the unit's last-success clock: the mirror exits 0
        # on a run that refuses the whole store, so the clock advanced hourly on
        # 2026-09-13 while /edge sat a brief day behind. A generic rule on the
        # clock would be wrong here. See TestEdgeRulesReadTheMirrorGauges below.
        "edge-mirror",
        # broker-capital-reader follows the edge-mirror shape: the unit wires the
        # emit hook like every other timer-driven service, but the generic
        # AlphalensJobStale rule is replaced by the dedicated
        # AlphalensBrokerCapitalReadStale alert, which is STRICTLY better here.
        # It watches the freshness of the READING
        # (alphalens_broker_manager_account_read_timestamp_seconds), so it also
        # catches a run that exits 0 while producing nothing usable — which a
        # last_success timestamp cannot. Adding both would page twice for one
        # condition. See deploy/monitoring/prometheus/rules/alphalens.yaml.
        "broker-capital-reader",
    }
)


# Every exempt job replaces the generic AlphalensJobStale with a DEDICATED
# staleness alert — and a staleness alert on its own is disarmable: an
# expression like ``time() - max(series) > N`` simply returns nothing when
# the series does not exist, so the rule sits ``inactive`` forever and looks
# healthy. The generic family pairs each stale rule with an ``absent()``
# guard (AlphalensJobMetricMissing); the dedicated ones must too. Named
# pairs, not job-label matching: broker-capital-reader deliberately watches
# a series labelled job="broker-manager-live", not its own.
#
# #1366: AlphalensEdgeStale had no guard while the edge-mirror unit wrote its
# metric to an unscraped directory — two months of ``inactive`` with no page.
DEDICATED_STALE_RULES: dict[str, tuple[str, str]] = {
    "edge-mirror": ("AlphalensEdgeStale", "AlphalensEdgeMetricMissing"),
    "broker-capital-reader": (
        "AlphalensBrokerCapitalReadStale",
        "AlphalensBrokerCapitalReadMissing",
    ),
}


def _all_rules() -> list[dict]:
    doc = yaml.safe_load(RULES_PATH.read_text())
    return [rule for group in doc["groups"] for rule in group.get("rules", [])]


def _missing_dedicated_pairs(rules: list[dict], table: dict[str, tuple[str, str]]) -> list[str]:
    """Pure helper: problems with the dedicated stale/absent pairs, as messages."""
    by_name = {rule.get("alert"): rule for rule in rules if rule.get("alert")}
    problems: list[str] = []
    for job, (stale_name, missing_name) in sorted(table.items()):
        stale = by_name.get(stale_name)
        missing = by_name.get(missing_name)
        if stale is None:
            problems.append(f"{job}: dedicated stale rule {stale_name!r} not found")
        elif "absent(" in stale.get("expr", ""):
            problems.append(f"{job}: {stale_name!r} must be threshold-only (no absent())")
        if missing is None:
            problems.append(f"{job}: absent() guard {missing_name!r} not found")
        elif "absent(" not in missing.get("expr", ""):
            problems.append(f"{job}: {missing_name!r} must wrap absent(...)")
    return problems


def _emitting_jobs() -> set[str]:
    """Job names emitted by ANY systemd unit's metrics ExecStopPost hook."""
    jobs: set[str] = set()
    for service in sorted(SYSTEMD_DIR.glob("*.service")):
        for match in EMIT_HOOK_RE.finditer(service.read_text()):
            jobs.add(match.group("job"))
    return jobs


def _staleness_rule_jobs() -> set[str]:
    """``job=`` labels referenced by AlphalensJobStale rules in the YAML.

    Flattens rules across ALL groups, not just ``groups[0]`` — the rules
    file already carries several alert families (AlphalensJobStale,
    AlphalensJobFailed, AlphalensJobMetricMissing), and a future split of
    staleness rules into a second group must not silently escape the parity
    check.
    """
    doc = yaml.safe_load(RULES_PATH.read_text())
    rules = [rule for group in doc["groups"] for rule in group.get("rules", [])]
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
        # PromQL accepts both double- and single-quoted label values.
        match = re.search(r"""job=["']([^"']+)["']""", rule.get("expr", ""))
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


class TestDedicatedStaleRulePairs(unittest.TestCase):
    """Every STALENESS_EXEMPT_JOBS entry names a dedicated stale rule AND an
    absent() guard, and the table is the exemption set — bidirectional."""

    def test_dedicated_table_matches_exempt_set(self) -> None:
        self.assertEqual(set(DEDICATED_STALE_RULES), set(STALENESS_EXEMPT_JOBS))

    def test_exempt_jobs_have_named_dedicated_rule_pairs(self) -> None:
        problems = _missing_dedicated_pairs(_all_rules(), DEDICATED_STALE_RULES)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_pair_check_flags_a_stale_rule_without_its_guard(self) -> None:
        # Positive control: a dedicated stale rule with no absent() sibling
        # (the #1366 shape) must be reported.
        synthetic = [
            {"alert": "SyntheticStale", "expr": 'time() - max(x{job="s"}) > 1'},
        ]
        problems = _missing_dedicated_pairs(
            synthetic, {"synthetic": ("SyntheticStale", "SyntheticMissing")}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("SyntheticMissing", problems[0])

    def test_pair_check_flags_a_guard_that_does_not_use_absent(self) -> None:
        synthetic = [
            {"alert": "SyntheticStale", "expr": 'time() - max(x{job="s"}) > 1'},
            {"alert": "SyntheticMissing", "expr": 'x{job="s"} == 0'},
        ]
        problems = _missing_dedicated_pairs(
            synthetic, {"synthetic": ("SyntheticStale", "SyntheticMissing")}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("absent", problems[0])


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


class TestEdgeRulesReadTheMirrorGauges(unittest.TestCase):
    """The four /edge rules read the gauges the mirror command publishes (#1436).

    Until 2026-09-15 ``AlphalensEdgeStale`` read the mirror unit's last-success
    clock, which advances on an all-refused run (exit 0): on 2026-09-13 the
    nightly was killed before writing the ingest watermark, the mirror refused
    117 dates hourly, /edge sat a brief day behind, and the rule stayed silent.
    The rules now read the DATA gauges; this pins the series each one reads,
    that the guard wraps the same series the stale rule reads, that every name
    is one the Django command declares, and that the old blind spot does not
    return under a new alert name.
    """

    EDGE_RULES: dict[str, str] = {
        "AlphalensEdgeStale": "alphalens_edge_mirror_watermark_timestamp_seconds",
        "AlphalensEdgeMetricMissing": "alphalens_edge_mirror_watermark_timestamp_seconds",
        "AlphalensEdgeMirrorRefusing": "alphalens_edge_mirror_unsettled_dates",
        "AlphalensEdgeNewestBriefDateStale": (
            "alphalens_edge_mirror_newest_brief_date_timestamp_seconds"
        ),
    }
    OLD_SERIES = 'alphalens_job_last_success_timestamp_seconds{job="edge-mirror"}'

    def setUp(self) -> None:
        self.by_name = {r["alert"]: r for r in _all_rules() if r.get("alert")}

    def test_each_edge_rule_reads_its_mirror_gauge(self) -> None:
        for alert, series in self.EDGE_RULES.items():
            with self.subTest(alert=alert):
                self.assertIn(alert, self.by_name, f"{alert} missing from the rules file")
                self.assertIn(series, self.by_name[alert]["expr"])

    def test_the_guard_wraps_the_series_the_stale_rule_reads(self) -> None:
        stale = self.by_name["AlphalensEdgeStale"]["expr"]
        guard = self.by_name["AlphalensEdgeMetricMissing"]["expr"]
        series = self.EDGE_RULES["AlphalensEdgeStale"]
        self.assertIn(series, stale)
        self.assertEqual(guard.strip(), f"absent({series})")

    def test_every_series_read_is_one_the_django_command_declares(self) -> None:
        self.assertLessEqual(set(self.EDGE_RULES.values()), set(EDGE_MIRROR_GAUGES))

    def test_no_rule_reads_the_mirror_last_success_clock_any_more(self) -> None:
        readers = [r["alert"] for r in _all_rules() if self.OLD_SERIES in r.get("expr", "")]
        self.assertEqual(
            readers,
            [],
            "a rule reads the edge-mirror last-success clock again — it advances on an "
            "all-refused run, which is the #1436 blind spot",
        )

    def test_edge_rules_share_the_unit_label_and_route(self) -> None:
        for alert in self.EDGE_RULES:
            with self.subTest(alert=alert):
                self.assertIn(alert, self.by_name, f"{alert} missing from the rules file")
                labels = self.by_name[alert]["labels"]
                self.assertEqual(labels.get("unit"), "edge-mirror")
                self.assertEqual(labels.get("route"), "telegram")


if __name__ == "__main__":
    unittest.main()
