"""Pin the contract of the cron-observability monitoring stack.

PR-3 of the cron-observability epic ships three YAML/JSON artifacts
under ``deploy/monitoring/`` that the existing VPS Prometheus +
Alertmanager + Grafana containers consume. These tests guard against
silent drift:

1. **Every emitted job has at least one staleness alert.** Adding a
   new cron-driven systemd unit without a paired rule means
   Alertmanager would never fire on its outage — the operator might
   not notice the job died until briefs / alerts stop arriving on
   Telegram organically.

2. **No duplicate (alertname, job) combinations.** Prometheus tolerates
   duplicates by replaying both, but two warning alerts firing on the
   same event pollute Telegram + group_by misroutes.

3. **Alert thresholds are integer seconds.** Human strings like
   ``"30m"`` parse in some contexts and not others; the rules file
   uses raw seconds so promtool's lint and our test see the same
   numeric value.

4. **Alertmanager config has a Telegram receiver + uses bot_token_file.**
   Inlining the bot_token would leak it via ``docker inspect``.

5. **Grafana dashboard JSON is well-formed + every active job appears
   in at least one panel target.**
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MONITORING_DIR = REPO_ROOT / "deploy" / "monitoring"
RULES_PATH = MONITORING_DIR / "prometheus" / "rules" / "alphalens.yaml"
ALERTMANAGER_PATH = MONITORING_DIR / "alertmanager" / "config.yaml"
DASHBOARD_PATH = MONITORING_DIR / "grafana" / "dashboards" / "alphalens-cron-health.json"

# Active jobs that emit alphalens_job_* metrics from PR-2. Form-4 is
# excluded (long-running daemon; instrumented separately if needed).
ACTIVE_JOBS = (
    "edgar-detect",
    "literature-scan-weekly",
    "literature-scan-monthly",
    "av-earnings-backfill",
    "thematic-build",
    "feedback-shadow-returns",
)


def _load_rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text())


def _load_alertmanager() -> dict:
    return yaml.safe_load(ALERTMANAGER_PATH.read_text())


def _load_dashboard() -> dict:
    return json.loads(DASHBOARD_PATH.read_text())


class TestPrometheusRulesYaml(unittest.TestCase):
    def test_yaml_is_well_formed(self) -> None:
        # Surfaces the parser error in the assertion message rather than
        # a generic "YAML parse error" buried in promtool output.
        try:
            _load_rules()
        except yaml.YAMLError as exc:
            self.fail(f"deploy/monitoring/prometheus/rules/alphalens.yaml: {exc}")

    def test_has_single_group_named_alphalens_cron_health(self) -> None:
        rules = _load_rules()
        self.assertIn("groups", rules)
        self.assertEqual(len(rules["groups"]), 1)
        self.assertEqual(rules["groups"][0]["name"], "alphalens-cron-health")

    def test_every_active_job_has_a_staleness_rule(self) -> None:
        # The ``AlphalensJobStale`` alert is per-job; missing one means
        # that unit could be down for days without paging.
        rules = _load_rules()["groups"][0]["rules"]
        stale_jobs = set()
        for rule in rules:
            if rule.get("alert") != "AlphalensJobStale":
                continue
            # Pull the job label out of the expr: text. Avoids
            # depending on Prometheus' label engine just for a unit
            # test.
            m = re.search(r'job="([^"]+)"', rule["expr"])
            if m:
                stale_jobs.add(m.group(1))

        missing = set(ACTIVE_JOBS) - stale_jobs
        self.assertEqual(
            missing,
            set(),
            f"Missing AlphalensJobStale rule for: {sorted(missing)}. "
            f"Add a per-job rule with an appropriate threshold.",
        )

    def test_no_duplicate_alertname_job_combos(self) -> None:
        # Two rules with the same alertname AND the same job-label
        # produce two Telegram messages on the same event after
        # group_by collapses them only on (alertname, job).
        rules = _load_rules()["groups"][0]["rules"]
        seen: list[tuple[str, str]] = []
        for rule in rules:
            alertname = rule.get("alert")
            if not alertname:
                continue
            job_match = re.search(r'job="([^"]+)"', rule.get("expr", ""))
            job = job_match.group(1) if job_match else "*"
            seen.append((alertname, job))

        duplicates = {combo for combo in seen if seen.count(combo) > 1}
        self.assertEqual(
            duplicates,
            set(),
            f"Duplicate alertname/job combinations: {sorted(duplicates)}.",
        )

    def test_staleness_thresholds_are_integer_seconds(self) -> None:
        # Promtool tolerates ``30m`` style but mixing units across
        # rules makes them hard to compare. Pin a single representation.
        rules = _load_rules()["groups"][0]["rules"]
        for rule in rules:
            if rule.get("alert") != "AlphalensJobStale":
                continue
            m = re.search(r"> (\d+)\b", rule["expr"])
            self.assertIsNotNone(
                m,
                f"Staleness expr must end in `> <integer-seconds>`; got: {rule['expr']!r}",
            )

    def test_staleness_thresholds_match_expected_cadence(self) -> None:
        # Pin the exact per-job threshold so a future "tune this to
        # be less noisy" PR can't silently double the staleness
        # window without explicit review. Values are 2× the expected
        # cadence (zen-review acceptance).
        expected = {
            "edgar-detect": 1800,  # 30m = 2× 15-min cadence
            "literature-scan-weekly": 1209600,  # 14d = 2× 7d cadence
            "literature-scan-monthly": 6048000,  # 70d = 2.3× 30d cadence (looser, scan is high-cost)
            "av-earnings-backfill": 172800,  # 48h = 2× 24h cadence
            # 12h = 3× the new 4h cadence (PR-F, epic #295 / issue
            # #300). Was 172800 (48h) at 1× cadence; tightened
            # alongside the 6×/day timer in
            # docs/research/polygon_quota_6x_per_day_2026_05_30.md.
            "thematic-build": 43200,
            # 48h = 2× the nightly cadence (Track A v2 PR-T). The job runs
            # every night at 06:30 UTC and exits 0 even on nights that price
            # 0 dates (per-ticker resilience), so last_success refreshes
            # nightly — staleness cleanly catches "the nightly sweep stopped
            # running", which AlphalensJobFailed (non-zero exit) cannot.
            "feedback-shadow-returns": 172800,
        }
        rules = _load_rules()["groups"][0]["rules"]
        found: dict[str, int] = {}
        for rule in rules:
            if rule.get("alert") != "AlphalensJobStale":
                continue
            job_match = re.search(r'job="([^"]+)"', rule["expr"])
            threshold_match = re.search(r"> (\d+)\b", rule["expr"])
            if job_match and threshold_match:
                found[job_match.group(1)] = int(threshold_match.group(1))

        for job, want in expected.items():
            self.assertEqual(
                found.get(job),
                want,
                f"Staleness threshold drift for {job!r}: want {want}s, got {found.get(job)}s.",
            )

    def test_stale_rules_are_threshold_only(self) -> None:
        # The ``absent()`` guard used to be OR-ed into every
        # ``AlphalensJobStale`` rule. That conflated two distinct
        # failure modes into one alert: when the metric was missing,
        # ``absent()`` returns the literal value ``1`` and the shared
        # ``humanizeDuration`` annotation rendered a self-contradictory
        # "stale > 70 days / last run 1s ago" message (real incident
        # 2026-05-31, monthly job before its first VPS run). The guard
        # now lives in a dedicated ``AlphalensJobMetricMissing`` alert,
        # so ``AlphalensJobStale`` must be threshold-only.
        rules = _load_rules()["groups"][0]["rules"]
        for rule in rules:
            if rule.get("alert") != "AlphalensJobStale":
                continue
            self.assertNotIn(
                "absent(",
                rule["expr"],
                "AlphalensJobStale must be threshold-only; the absent() guard "
                "belongs in the paired AlphalensJobMetricMissing alert.",
            )

    def test_every_active_job_has_a_metric_missing_rule(self) -> None:
        # The ``absent()`` guard (zen-pinned in PR #312) must still
        # cover every job: a missing series (fresh VPS, textfile
        # collector misconfigured, node_exporter scrape failure)
        # silently disabling the staleness alert is the worst class of
        # monitoring blind spot. After the split (2026-05-31) the guard
        # lives in a dedicated per-job AlphalensJobMetricMissing alert.
        rules = _load_rules()["groups"][0]["rules"]
        missing_rule_jobs = set()
        for rule in rules:
            if rule.get("alert") != "AlphalensJobMetricMissing":
                continue
            expr = rule["expr"]
            job_match = re.search(r'job="([^"]+)"', expr)
            self.assertIsNotNone(job_match, f"Missing-metric rule lacks job filter: {expr!r}")
            assert job_match is not None
            job = job_match.group(1)
            self.assertIn(
                f'absent(alphalens_job_last_success_timestamp_seconds{{job="{job}"}})',
                expr,
                f"AlphalensJobMetricMissing rule for {job!r} must wrap absent(...).",
            )
            missing_rule_jobs.add(job)

        gaps = set(ACTIVE_JOBS) - missing_rule_jobs
        self.assertEqual(
            gaps,
            set(),
            f"Missing AlphalensJobMetricMissing rule for: {sorted(gaps)}.",
        )

    def test_metric_missing_rules_carry_unit_label(self) -> None:
        # Same promtool duplicate-rule constraint as the stale rules:
        # all 5 share the alertname + {severity, route} block, so each
        # needs a distinguishing unit: <job> static label matching its
        # expr filter.
        rules = _load_rules()["groups"][0]["rules"]
        for rule in rules:
            if rule.get("alert") != "AlphalensJobMetricMissing":
                continue
            job_match = re.search(r'job="([^"]+)"', rule["expr"])
            self.assertIsNotNone(job_match, f"Missing-metric rule lacks job: {rule['expr']!r}")
            assert job_match is not None
            job = job_match.group(1)
            self.assertEqual(
                rule.get("labels", {}).get("unit"),
                job,
                f"AlphalensJobMetricMissing rule for {job!r} must carry unit: {job}.",
            )

    def test_missing_rules_have_distinct_static_label_sets(self) -> None:
        # promtool rejects two rules sharing an identical alertname AND
        # static-label set. Assert the 6 AlphalensJobMetricMissing label
        # blocks are pairwise distinct (via the unit label).
        rules = _load_rules()["groups"][0]["rules"]
        label_sets = [
            frozenset(rule.get("labels", {}).items())
            for rule in rules
            if rule.get("alert") == "AlphalensJobMetricMissing"
        ]
        self.assertEqual(len(label_sets), 6)
        self.assertEqual(len(set(label_sets)), len(label_sets))

    def test_missing_metric_message_claims_no_duration(self) -> None:
        # The whole point of the split: a missing-metric alert must NOT
        # render a duration. ``absent()`` fires with value 1, so any
        # ``humanizeDuration`` in its annotation would print the
        # misleading "1s ago". Conversely, the stale alert MUST keep
        # ``humanizeDuration`` (the real staleness duration is the
        # actionable signal there).
        rules = _load_rules()["groups"][0]["rules"]
        for rule in rules:
            ann = rule.get("annotations", {})
            if rule.get("alert") == "AlphalensJobMetricMissing":
                # Check each field independently — a duration token
                # smuggled into either summary or description would be
                # wrong, so don't let concatenation mask a single field.
                for field in ("summary", "description"):
                    self.assertNotIn(
                        "humanizeDuration",
                        ann.get(field, ""),
                        f"Missing-metric alert {field} must not render a "
                        "duration (absent() value is 1 -> misleading '1s ago').",
                    )
            elif rule.get("alert") == "AlphalensJobStale":
                self.assertIn(
                    "humanizeDuration",
                    ann.get("description", ""),
                    "Stale alert must report the real staleness duration.",
                )

    def test_no_counter_functions_on_gauge_metrics(self) -> None:
        # Zen pre-merge review of PR #312 caught ``increase()`` on
        # ``alphalens_edgar_events_dispatched_total`` (which is a
        # GAUGE per the textfile emitter design — overwrites the
        # file with the latest run's count each fire, not a
        # cumulative counter). Counter functions on a gauge return
        # nonsense; pin the prohibition statically.
        rules = _load_rules()["groups"][0]["rules"]
        counter_funcs = ("increase(", "rate(", "irate(")
        gauge_prefixes = (
            "alphalens_job_",
            "alphalens_edgar_",
            "alphalens_literature_",
            "alphalens_thematic_",
            "alphalens_av_",
        )
        for rule in rules:
            expr = rule.get("expr", "")
            for func in counter_funcs:
                if func not in expr:
                    continue
                # Find the metric name inside the function call and
                # verify it isn't one of our gauges.
                start = expr.index(func) + len(func)
                end_paren = expr.index(")", start)
                inner = expr[start:end_paren]
                for prefix in gauge_prefixes:
                    self.assertNotIn(
                        prefix,
                        inner,
                        f"{rule.get('alert')!r}: counter function {func.rstrip('(')} "
                        f"applied to a gauge metric ({inner!r}). Gauges are "
                        "overwritten on every emit, not monotonically counted; "
                        "use `max_over_time(...)` or `avg_over_time(...)` instead.",
                    )

    def test_stale_rules_carry_unit_label_matching_job(self) -> None:
        # promtool's duplicate-rule lint compares (alert name + static
        # label set). All 5 AlphalensJobStale rules share the same
        # name and the same {severity: warning, route: telegram} block,
        # so they collide. A distinguishing ``unit: <job>`` static
        # label (matching the job filter in the expr) makes each
        # signature unique and improves Alertmanager grouping. (#333)
        rules = _load_rules()["groups"][0]["rules"]
        for rule in rules:
            if rule.get("alert") != "AlphalensJobStale":
                continue
            job_match = re.search(r'job="([^"]+)"', rule["expr"])
            self.assertIsNotNone(job_match, f"Stale rule missing job filter: {rule['expr']!r}")
            assert job_match is not None
            job = job_match.group(1)
            self.assertEqual(
                rule.get("labels", {}).get("unit"),
                job,
                f"AlphalensJobStale rule for job {job!r} must carry a `unit: {job}` static label.",
            )

    def test_stale_rules_have_distinct_static_label_sets(self) -> None:
        # promtool rejects two rules that share both an identical alert
        # name and an identical static-label set. Assert the 5
        # AlphalensJobStale label blocks are pairwise distinct so the
        # duplicate-rule lint passes. (#333)
        rules = _load_rules()["groups"][0]["rules"]
        label_sets = [
            frozenset(rule.get("labels", {}).items())
            for rule in rules
            if rule.get("alert") == "AlphalensJobStale"
        ]
        self.assertEqual(len(label_sets), 6)
        self.assertEqual(
            len(set(label_sets)),
            len(label_sets),
            "AlphalensJobStale rules must have pairwise-distinct static "
            "label sets so promtool's duplicate-rule lint passes.",
        )

    def test_all_alerts_carry_route_telegram_label(self) -> None:
        # Alertmanager routes by label; an alert missing
        # ``route: telegram`` would land on the default receiver
        # silently.
        rules = _load_rules()["groups"][0]["rules"]
        for rule in rules:
            if rule.get("alert"):
                self.assertEqual(
                    rule.get("labels", {}).get("route"),
                    "telegram",
                    f"Alert {rule['alert']!r} missing `route: telegram` label.",
                )


class TestAlertmanagerConfig(unittest.TestCase):
    def test_yaml_is_well_formed(self) -> None:
        try:
            _load_alertmanager()
        except yaml.YAMLError as exc:
            self.fail(f"deploy/monitoring/alertmanager/config.yaml: {exc}")

    def test_has_telegram_receiver(self) -> None:
        cfg = _load_alertmanager()
        receivers = {r["name"]: r for r in cfg.get("receivers", [])}
        self.assertIn(
            "telegram",
            receivers,
            "telegram receiver missing — alerts have nowhere to land.",
        )
        self.assertIn("telegram_configs", receivers["telegram"])

    def test_bot_token_sourced_from_file_not_inline(self) -> None:
        # Inlining the token leaks it via `docker inspect` + lives in
        # plain text on the disk. ``bot_token_file:`` lets the operator
        # bind-mount a tighter-permission file.
        cfg = _load_alertmanager()
        tg = cfg["receivers"][0]["telegram_configs"][0]
        self.assertIn(
            "bot_token_file",
            tg,
            "telegram_configs must use bot_token_file, not inline bot_token.",
        )
        self.assertNotIn(
            "bot_token",
            tg,
            "Remove the inline bot_token field — bot_token_file is the secret-safe form.",
        )

    def test_default_route_targets_telegram_receiver(self) -> None:
        cfg = _load_alertmanager()
        self.assertEqual(cfg["route"]["receiver"], "telegram")

    def test_message_template_distinguishes_firing_from_resolved(self) -> None:
        # send_resolved: true (Alertmanager default for telegram) sends
        # a Telegram notification both when an alert FIRES and when it
        # RESOLVES. Without a status-aware template, both messages
        # look identical and the operator cannot tell at a glance
        # whether to act or relax. Pin the template to prefix the
        # alert status. Caught during VPS cutover 2026-05-30 smoke
        # test — first resolved notification read identically to the
        # original firing one.
        cfg = _load_alertmanager()
        tg = cfg["receivers"][0]["telegram_configs"][0]
        msg = tg.get("message", "")
        self.assertIn(
            'eq .Status "firing"',
            msg,
            "Telegram message template must branch on .Status so resolved "
            "notifications are visually distinct from firing ones.",
        )
        self.assertIn("[FIRING]", msg)
        self.assertIn("[RESOLVED]", msg)

    def test_group_by_includes_alertname_and_job(self) -> None:
        # Without job in group_by, two stale alerts on different jobs
        # would collapse into one Telegram message and the operator
        # would not know which job failed.
        cfg = _load_alertmanager()
        gb = cfg["route"]["group_by"]
        self.assertIn("alertname", gb)
        self.assertIn("job", gb)


class TestGrafanaDashboard(unittest.TestCase):
    def test_json_is_well_formed(self) -> None:
        try:
            _load_dashboard()
        except json.JSONDecodeError as exc:
            self.fail(f"deploy/monitoring/grafana/dashboards/alphalens-cron-health.json: {exc}")

    def test_uid_is_stable_kebab_case(self) -> None:
        # Stable uid lets the operator reference the dashboard by URL
        # without depending on Grafana's auto-generated numeric ID.
        dash = _load_dashboard()
        self.assertEqual(dash["uid"], "alphalens-cron-health")

    def test_cron_health_panels_reference_alphalens_job_metrics(self) -> None:
        # A panel that lost its expression would render "No data" with
        # no error — easy to miss. Check that every panel targeting a
        # known metric exists.
        dash = _load_dashboard()
        all_exprs = []
        for panel in dash.get("panels", []):
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if expr:
                    all_exprs.append(expr)

        self.assertTrue(
            any("alphalens_job_last_success_timestamp_seconds" in e for e in all_exprs),
            "Dashboard must include a `time since last success` panel.",
        )
        self.assertTrue(
            any("alphalens_job_last_duration_seconds" in e for e in all_exprs),
            "Dashboard must include a `last duration` panel.",
        )
        self.assertTrue(
            any("alphalens_job_last_exit_code" in e for e in all_exprs),
            "Dashboard must include an `exit code` panel.",
        )


class TestTemplateEngineMonitoring(unittest.TestCase):
    """Pin the per-template alert + dashboard panel introduced in epic #321 PR-1.

    The structured-event-template engine (issue #143) emits four metric
    families: ``alphalens_template_match_total``,
    ``alphalens_template_attempt_total``,
    ``alphalens_template_holdout_total``,
    ``alphalens_template_predicate_total``. If a future edit silently
    drops the per-template alert or the panel that surfaces these,
    Operator loses the only signal that says "a template stopped
    matching". This file guards the contract.
    """

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def test_template_match_rate_low_alert_exists(self) -> None:
        rules = self._rules()
        match_rate_alerts = [r for r in rules if r.get("alert") == "AlphalensTemplateMatchRateLow"]
        self.assertEqual(
            len(match_rate_alerts),
            1,
            "Expected exactly one AlphalensTemplateMatchRateLow alert.",
        )
        alert = match_rate_alerts[0]
        # Threshold from design memo §1.1 — per-template, not aggregate.
        self.assertIn("0.20", alert["expr"])
        # Per-template grouping is the load-bearing part of the design.
        self.assertIn("by (template_id)", alert["expr"])
        # Denominator-zero guard so a brand-new template doesn't trip
        # on the first second of life (no attempts yet → no rate series).
        self.assertIn(
            "alphalens_template_attempt_total",
            alert["expr"],
            "Alert must reference attempts so the >0 guard can fire.",
        )
        self.assertEqual(alert.get("for"), "1d")
        self.assertEqual(alert.get("labels", {}).get("route"), "telegram")

    def test_template_match_rate_low_has_min_sample_guard(self) -> None:
        # A single low-volume template (e.g. 1 attempt / 0 match in 7 days)
        # must not page on a 7-day low-match-rate alert. The denominator
        # guard is an absolute min-sample count via increase(), not just
        # rate(...) > 0 (which a single attempt already satisfies).
        rules = self._rules()
        match_rate_alerts = [r for r in rules if r.get("alert") == "AlphalensTemplateMatchRateLow"]
        self.assertEqual(len(match_rate_alerts), 1)
        expr = match_rate_alerts[0]["expr"]
        self.assertIn("increase(alphalens_template_attempt_total[7d])", expr)
        self.assertIn(">= 50", expr)

    def test_dashboard_includes_template_engine_panels(self) -> None:
        dash = _load_dashboard()
        all_exprs = []
        for panel in dash.get("panels", []):
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if expr:
                    all_exprs.append(expr)

        # Per-template match-rate panel (the headline signal).
        self.assertTrue(
            any("alphalens_template_match_total" in e for e in all_exprs),
            "Dashboard must include a per-template match-rate panel.",
        )
        # Holdout reason breakdown (no-black-box-scoring doctrine).
        self.assertTrue(
            any("alphalens_template_holdout_total" in e for e in all_exprs),
            "Dashboard must include a holdout-by-reason panel.",
        )
        # Per-predicate pass/fail rate (catches regex rot).
        self.assertTrue(
            any("alphalens_template_predicate_total" in e for e in all_exprs),
            "Dashboard must include a per-predicate pass/fail panel.",
        )


class TestThematicVolumeRules(unittest.TestCase):
    """Phase 4 output-volume dead-man-switch rules.

    The L3 golden-replay tests catch a pipeline regression in CI; these
    rules catch the failure no hermetic test can — a live LLM model retiring
    NEXT month so a stage produces 0 rows from non-empty input while the run
    still exits 0. Two rules:
      * AlphalensThematicStageZeroOutput — per-stage zero-output-with-
        nonempty-input (needs BOTH the input and output gauges to compare);
      * AlphalensThematicBriefVolumeAnomaly — brief output collapsed vs its
        own multi-day baseline (partial degradation, not just zero).
    """

    ZERO_OUTPUT = "AlphalensThematicStageZeroOutput"
    ANOMALY = "AlphalensThematicBriefVolumeAnomaly"

    def _rule(self, name: str) -> dict:
        rules = _load_rules()["groups"][0]["rules"]
        for rule in rules:
            if rule.get("alert") == name:
                return rule
        self.fail(f"alert {name} not found in alphalens.yaml")

    def test_both_volume_rules_present(self) -> None:
        for name in (self.ZERO_OUTPUT, self.ANOMALY):
            self._rule(name)  # fails if absent

    def test_zero_output_rule_references_both_input_and_output(self) -> None:
        # The "needs both metrics" invariant: a zero-output rule that only
        # looked at output cannot tell a silent failure from a quiet day.
        # Pin both halves so a future edit can't drop the input guard.
        expr = self._rule(self.ZERO_OUTPUT)["expr"]
        self.assertIn("alphalens_thematic_stage_output_rows", expr)
        self.assertIn("alphalens_thematic_stage_input_rows", expr)
        self.assertIn("== 0", expr)
        self.assertIn("> 0", expr)

    def test_zero_output_rule_uses_gauge_correct_aggregation(self) -> None:
        # The stage gauges are overwritten each run; increase()/rate() return
        # nonsense on a gauge (the AlphalensEdgarNoCandidates24h lesson,
        # PR #312). The correct "nothing across the window" operator is
        # max_over_time.
        expr = self._rule(self.ZERO_OUTPUT)["expr"]
        self.assertIn("max_over_time", expr)
        self.assertNotIn("increase(", expr)
        self.assertNotIn("rate(", expr)

    def test_anomaly_rule_targets_brief_stage(self) -> None:
        expr = self._rule(self.ANOMALY)["expr"]
        self.assertIn('alphalens_thematic_stage_output_rows{stage="brief"}', expr)
        # Baseline median over a multi-day window (not a fixed threshold).
        self.assertIn("quantile_over_time", expr)

    def test_volume_rules_route_to_telegram(self) -> None:
        for name in (self.ZERO_OUTPUT, self.ANOMALY):
            labels = self._rule(name).get("labels", {})
            self.assertEqual(
                labels.get("route"),
                "telegram",
                f"{name} must route to telegram like the other domain alerts.",
            )

    def test_volume_rules_have_for_debounce(self) -> None:
        # A single quiet fire must not page; require a debounce window.
        for name in (self.ZERO_OUTPUT, self.ANOMALY):
            self.assertIn("for", self._rule(name), f"{name} needs a `for:` debounce clause.")


if __name__ == "__main__":
    unittest.main()
