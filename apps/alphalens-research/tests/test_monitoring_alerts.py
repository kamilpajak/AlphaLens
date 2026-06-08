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
    "form4-incremental",
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
            # 48h = 2× the daily cadence (02:30 UTC). The fixed-lookback window
            # makes a one-night miss self-heal on the next overlapping run, and
            # the job exits 0 even on a transient-403 night, so last_success
            # refreshes nightly — staleness catches "the job stopped running".
            "form4-incremental": 172800,
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
        self.assertEqual(len(label_sets), len(ACTIVE_JOBS))
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
            "alphalens_vix_",
            "alphalens_form4_",
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
        self.assertEqual(len(label_sets), len(ACTIVE_JOBS))
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

    def test_anomaly_rule_pins_offset_guard_and_debounce(self) -> None:
        # Pin the load-bearing fragments so a future edit can't silently
        # drop the offset (baseline would include today), relax the
        # cold-start guard (would false-page before a baseline exists), or
        # shorten the debounce.
        rule = self._rule(self.ANOMALY)
        expr = rule["expr"]
        self.assertIn("offset 1h", expr)
        self.assertIn(">= 3", expr)
        self.assertEqual(rule.get("for"), "6h")

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


class TestVixCacheStaleness(unittest.TestCase):
    """Pin the VIX-cache staleness alert pair (Track A v2 PR-2 follow-up).

    ``market_regime_at_entry`` is stamped on the Django feedback POST path
    from ``alphalens_feedback.regime.get_cached_vix``, which silently returns
    ``None`` (-> regime degrades to ``unknown``) once the cache ages past
    ``_VIX_MAX_AGE_SECONDS`` (96h). The refresh step in run_thematic_day.sh is
    best-effort (``|| echo WARN``), so a persistently dead FRED refresher emits
    NO signal — every new decision quietly loses its regime label and the
    per-regime execution-mode cells (PR-4/PR-5) are poisoned at the source.
    ``alphalens cache refresh-vix`` now emits
    ``alphalens_vix_cache_fetched_at_timestamp_seconds`` so these rules can
    alert when it stops landing fresh.

    The VIX cache is refreshed INLINE inside run_thematic_day.sh (not via a
    systemd unit with an ``ExecStopPost`` emit hook), so it is deliberately
    NOT a cron ``job=`` and must stay out of every cron enumeration
    (ACTIVE_JOBS, the staleness-threshold dict, the emit-hook parity test). A
    DISTINCT alertname is what keeps it invisible to those job-keyed tests, so
    this family carries its OWN regression pins below — the cron-keyed asserts
    will never cover it.
    """

    METRIC = "alphalens_vix_cache_fetched_at_timestamp_seconds"
    STALE = "AlphalensVixCacheStale"
    MISSING = "AlphalensVixCacheMetricMissing"
    # 259200s = 72h = 0.75x the 96h reader ceiling in
    # alphalens_feedback.regime._VIX_MAX_AGE_SECONDS, so the alert fires ~24h
    # BEFORE the reader starts degrading stamps to "unknown" — a day of runway
    # to fix a dead refresher rather than paging after the damage is done.
    THRESHOLD = 259200

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def _one(self, alertname: str) -> dict:
        matches = [r for r in self._rules() if r.get("alert") == alertname]
        self.assertEqual(
            len(matches),
            1,
            f"Expected exactly one {alertname} alert, found {len(matches)}.",
        )
        return matches[0]

    def test_stale_alert_is_threshold_only_on_the_vix_gauge(self) -> None:
        rule = self._one(self.STALE)
        expr = rule["expr"]
        self.assertIn(self.METRIC, expr)
        # Threshold-only — the absent() guard belongs in the paired
        # MetricMissing alert (same split contract as the job alerts).
        self.assertNotIn("absent(", expr)
        # Pin the literal threshold: the cron staleness-threshold dict keys on
        # AlphalensJobStale only, so a differently-named VIX rule escapes it —
        # this is its sole regression pin against a silent threshold widen.
        # Allow an optional {label} selector between the metric and `>` —
        # the rule scopes to {series="VIXCLS"} so it stays correct if a second
        # macro series ever emits the same metric name.
        self.assertRegex(
            expr,
            rf"time\(\)\s*-\s*{re.escape(self.METRIC)}(\{{[^}}]*\}})?\s*>\s*{self.THRESHOLD}\b",
            f"Stale expr must be `time() - {self.METRIC}[{{...}}] > {self.THRESHOLD}`.",
        )

    def test_stale_alert_reports_duration_and_routes_to_telegram(self) -> None:
        rule = self._one(self.STALE)
        self.assertIn(
            "humanizeDuration",
            rule.get("annotations", {}).get("description", ""),
            "Stale alert must report the real staleness duration.",
        )
        self.assertEqual(rule.get("labels", {}).get("route"), "telegram")

    def test_metric_missing_alert_wraps_absent_with_no_duration(self) -> None:
        rule = self._one(self.MISSING)
        expr = rule["expr"]
        self.assertIn(f"absent({self.METRIC}", expr)
        # absent() fires with value 1, so any humanizeDuration renders a
        # misleading "1s ago" — must be absent from BOTH annotation fields.
        ann = rule.get("annotations", {})
        for field in ("summary", "description"):
            self.assertNotIn("humanizeDuration", ann.get(field, ""))
        self.assertEqual(rule.get("labels", {}).get("route"), "telegram")

    def test_vix_rules_carry_no_job_label_so_they_stay_out_of_cron_enums(self) -> None:
        # Cheap belt-pin (not load-bearing — the distinct alertname already
        # isolates it): the VIX cache has no systemd unit / ExecStopPost emit
        # hook, so a job= label would falsely register it as an orphan cron
        # rule in the job-keyed parity tests.
        for alertname in (self.STALE, self.MISSING):
            expr = self._one(alertname)["expr"]
            self.assertIsNone(
                re.search(r'job="[^"]+"', expr),
                f"{alertname} must not carry a job= label (it is not a cron job).",
            )


class TestEdgarNoCandidatesWeekendTolerance(unittest.TestCase):
    """Pin the weekend/holiday-tolerant window on the no-candidates alert.

    ``AlphalensEdgarNoCandidates`` catches a genuinely quiet / emptied
    watchlist (the unique signal detector LIVENESS does not cover — that is
    AlphalensJobStale + AlphalensJobMetricMissing). SEC accepts no filings on
    weekends and the watched portfolio is tiny (~6 tickers), so a [24h] window
    is legitimately 0 every weekend and false-fired the operator. The window is
    widened to [5d] to tolerate a weekend AND the worst holiday cluster (the
    pessimistic Thanksgiving Thu-Sun 4-zero-day run), matching the sibling
    AlphalensEdgarPressReleaseDark / AlphalensForm4IncrementalDark rules.

    The window is encoded in the alertname per project convention, so the live
    alert is ``AlphalensEdgarNoCandidates5d`` and the stale 24h name must be
    gone.
    """

    METRIC = "alphalens_edgar_events_dispatched_total"
    ALERT = "AlphalensEdgarNoCandidates5d"
    OLD_ALERT = "AlphalensEdgarNoCandidates24h"
    # 5d, NOT 24h and NOT 4d: max_over_time(...[Nd]) == 0 fires the instant the
    # window holds only zeros, so tolerating the worst legit 4-zero-day cluster
    # (pessimistic Thanksgiving Thu-Sun) needs a window STRICTLY longer than 4d.
    # A future edit that shrinks this back toward 24h re-introduces the weekend
    # false page; growing it past 5d delays a real emptied-watchlist incident.
    WINDOW = "5d"

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def _one(self, alertname: str) -> dict:
        matches = [r for r in self._rules() if r.get("alert") == alertname]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {alertname}, found {len(matches)}."
        )
        return matches[0]

    def test_alert_exists_under_window_named_alertname(self) -> None:
        self._one(self.ALERT)

    def test_old_24h_alertname_is_gone(self) -> None:
        # The name encodes the window; a stale 24h name after widening to 5d
        # would mislead the operator on Telegram.
        names = {r.get("alert") for r in self._rules()}
        self.assertNotIn(
            self.OLD_ALERT,
            names,
            "AlphalensEdgarNoCandidates24h must be renamed to ...5d after the window widen.",
        )

    def test_expr_is_gauge_correct_max_over_time_zero(self) -> None:
        expr = self._one(self.ALERT)["expr"]
        self.assertIn(self.METRIC, expr)
        self.assertIn("max_over_time", expr)
        self.assertIn("== 0", expr)
        # GAUGE (overwritten each run) -> never counter functions (the PR #312
        # lesson; also pinned generically by test_no_counter_functions_*).
        self.assertNotIn("increase(", expr)
        self.assertNotIn("rate(", expr)

    def test_expr_window_is_five_days(self) -> None:
        # Pin the literal [5d] window so a noise-reduction edit can't silently
        # shrink it below a weekend/holiday cluster (false page) or back to 24h.
        expr = self._one(self.ALERT)["expr"]
        self.assertIn(f"max_over_time({self.METRIC}[{self.WINDOW}])", expr)
        self.assertNotIn("[24h]", expr)
        self.assertNotIn("[1d]", expr)

    def test_has_for_debounce(self) -> None:
        # 6h debounce for parity with the sibling dark rules
        # (AlphalensEdgarPressReleaseDark / the brief-anomaly rule). The [5d]
        # window edge ages out the last nonzero sample exactly 120h after it
        # was emitted; a 6h `for:` gives holiday-morning headroom so the first
        # quiet business day after a long-weekend zero-cluster does not page in
        # the ~1h boundary a shorter debounce would leave open.
        self.assertEqual(self._one(self.ALERT).get("for"), "6h")

    def test_routes_to_telegram(self) -> None:
        self.assertEqual(self._one(self.ALERT).get("labels", {}).get("route"), "telegram")

    def test_wording_reflects_five_day_window_not_24h(self) -> None:
        # The summary/description must describe the 5-day window, not a stale
        # "24h", so the Telegram message matches the actual evaluation window.
        ann = self._one(self.ALERT).get("annotations", {})
        summary = ann.get("summary", "")
        self.assertNotIn("24h", summary)
        self.assertNotIn("24h", ann.get("description", ""))

    def test_carries_no_job_label_so_it_stays_out_of_cron_enums(self) -> None:
        # Like the VIX / dark rules: a per-domain slice, not a systemd unit's
        # last_success. A job= label would falsely register it in the job-keyed
        # parity tests. Detector LIVENESS is covered by the job= rules instead.
        rule = self._one(self.ALERT)
        self.assertNotIn("job", rule.get("labels", {}))
        self.assertIsNone(re.search(r'job="[^"]+"', rule.get("expr", "")))


class TestEdgarPressReleaseDark(unittest.TestCase):
    """#384 per-source dead-man-switch for the EDGAR EX-99.1 ingest.

    Epic #379: the EX-99.1 daily-index ingest was 403'd under concurrent per-IP
    SEC load and produced ZERO edgar_press_release rows for ~6 consecutive days,
    silently (the empty frame was swallowed by _safe_call). No hermetic /
    single-shot / L4-live test can catch a load-dependent failure. This rule is
    the backstop: alert when the RAW edgar_press_release source count
    (alphalens_thematic_source_rows{source="edgar_press_release"}, emitted
    UNCONDITIONALLY pre-dedup by `thematic ingest`) stays 0 for a sustained
    window. Distinct alertname + NO job= label (like the VIX-cache rules) keep
    it out of the cron-keyed enumerations, so it needs its OWN pins here.
    """

    METRIC = 'alphalens_thematic_source_rows{source="edgar_press_release"}'
    DARK = "AlphalensEdgarPressReleaseDark"
    MISSING = "AlphalensEdgarPressReleaseMetricMissing"
    # 5d, NOT 4d: max_over_time(...[Nd]) == 0 fires the instant the window holds
    # only zeros, so tolerating the worst legit 4-zero-day cluster (pessimistic
    # Thanksgiving Thu-Sun) needs a window STRICTLY longer than 4d. A future edit
    # that shrinks this to 4d (or below a 3-day holiday weekend) is a false page;
    # growing it past 5d delays the real incident.
    WINDOW = "5d"

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def _one(self, alertname: str) -> dict:
        matches = [r for r in self._rules() if r.get("alert") == alertname]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {alertname}, found {len(matches)}."
        )
        return matches[0]

    def test_dark_alert_exists(self) -> None:
        self._one(self.DARK)

    def test_dark_expr_is_gauge_correct_max_over_time_zero(self) -> None:
        expr = self._one(self.DARK)["expr"]
        self.assertIn(self.METRIC, expr)
        self.assertIn("max_over_time", expr)
        self.assertIn("== 0", expr)
        self.assertNotIn("increase(", expr)
        self.assertNotIn("rate(", expr)

    def test_dark_expr_window_is_five_days(self) -> None:
        # Pin the literal window so a noise-reduction edit can't silently shrink
        # it below the worst holiday cluster (false page) or grow it so the
        # 6-day 403 starvation slips through.
        expr = self._one(self.DARK)["expr"]
        self.assertIn(f"max_over_time({self.METRIC}[{self.WINDOW}])", expr)

    def test_dark_has_for_debounce(self) -> None:
        self.assertIn("for", self._one(self.DARK))

    def test_dark_routes_to_telegram(self) -> None:
        self.assertEqual(self._one(self.DARK).get("labels", {}).get("route"), "telegram")

    def test_dark_severity_is_warning_not_critical(self) -> None:
        # A degraded data source is not a wake-up outage; critical breeds alert
        # fatigue (the AlphalensEdgarNoCandidates24h / brief-anomaly precedent).
        self.assertEqual(self._one(self.DARK).get("labels", {}).get("severity"), "warning")

    def test_missing_alert_wraps_absent(self) -> None:
        expr = self._one(self.MISSING)["expr"]
        self.assertIn(f"absent({self.METRIC}", expr)
        self.assertEqual(self._one(self.MISSING).get("labels", {}).get("route"), "telegram")

    def test_production_ingest_uses_force_so_the_gauge_is_always_fresh(self) -> None:
        # The Dark rule's whole window analysis assumes the source gauge is
        # OVERWRITTEN with a fresh sample every thematic-build run. On a cache
        # hit ingest_daily skips the fetches and leaves source_row_counts empty,
        # so the CLI emits no source gauge and node_exporter re-serves the last
        # (possibly nonzero) value — silencing the alert. The production
        # invocation passes --force precisely to bypass the per-UTC-day cache, so
        # pin it here: a future edit that drops --force from run_thematic_day.sh
        # would quietly break this backstop.
        script = (REPO_ROOT / "deploy" / "docker" / "run_thematic_day.sh").read_text()
        self.assertRegex(script, r"thematic\s+ingest\s+--force")


class TestForm4IncrementalDark(unittest.TestCase):
    """Output-volume dead-man-switch for the Form-4 daily-incremental ingest.

    Mirrors :class:`TestEdgarPressReleaseDark`. The job's staleness rule catches
    "stopped running", but a job that runs clean and writes 0 rows for days (the
    silent-success-noop class — e.g. a daily-index format drift that degrades to
    0 rows + transient, or a persistent 403 starvation) is invisible to the
    exit-code check. ``AlphalensForm4IncrementalDark`` fires when the rows-written
    gauge stays 0 across the window. DISTINCT alertname + NO ``job=`` label keep
    it out of the cron-keyed enumerations, so it needs its OWN pins here.
    """

    METRIC = "alphalens_form4_rows_written"
    DARK = "AlphalensForm4IncrementalDark"
    MISSING = "AlphalensForm4IncrementalMetricMissing"
    # 5d, same reasoning as the EDGAR-dark rule: max_over_time(...[Nd]) == 0 fires
    # the instant the window holds only zeros, so tolerating the worst legitimate
    # all-zero cluster (a holiday + weekend with no new Form-4 filings) needs a
    # window STRICTLY longer than 4 days.
    WINDOW = "5d"

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def _one(self, alertname: str) -> dict:
        matches = [r for r in self._rules() if r.get("alert") == alertname]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {alertname}, found {len(matches)}."
        )
        return matches[0]

    def test_dark_alert_exists(self) -> None:
        self._one(self.DARK)

    def test_dark_expr_is_gauge_correct_max_over_time_zero(self) -> None:
        expr = self._one(self.DARK)["expr"]
        self.assertIn(self.METRIC, expr)
        self.assertIn("max_over_time", expr)
        self.assertIn("== 0", expr)
        self.assertNotIn("increase(", expr)
        self.assertNotIn("rate(", expr)

    def test_dark_expr_window_is_five_days(self) -> None:
        expr = self._one(self.DARK)["expr"]
        self.assertIn(f"max_over_time({self.METRIC}[{self.WINDOW}])", expr)

    def test_dark_has_for_debounce(self) -> None:
        self.assertIn("for", self._one(self.DARK))

    def test_dark_routes_to_telegram(self) -> None:
        self.assertEqual(self._one(self.DARK).get("labels", {}).get("route"), "telegram")

    def test_dark_severity_is_warning_not_critical(self) -> None:
        self.assertEqual(self._one(self.DARK).get("labels", {}).get("severity"), "warning")

    def test_missing_alert_wraps_absent(self) -> None:
        expr = self._one(self.MISSING)["expr"]
        self.assertIn(f"absent({self.METRIC}", expr)
        self.assertEqual(self._one(self.MISSING).get("labels", {}).get("route"), "telegram")

    def test_dark_carries_no_job_label_so_it_stays_out_of_cron_enums(self) -> None:
        # The Dark/MetricMissing pair is a per-output-volume slice, not a
        # systemd unit's last_success — a job= label would falsely register it
        # in the job-keyed parity tests. (The job's OWN AlphalensJobStale +
        # AlphalensJobMetricMissing rules carry job="form4-incremental".)
        for alertname in (self.DARK, self.MISSING):
            rule = self._one(alertname)
            self.assertNotIn("job", rule.get("labels", {}))
            self.assertIsNone(re.search(r'job="[^"]+"', rule.get("expr", "")))


class TestForm4IncrementalSustainedTransientErrors(unittest.TestCase):
    """Pins for the chronic-SEC-fetch-failure alert (zen #477 HIGH).

    rows_written can stay non-zero while one source keeps 403-ing, so the Dark
    (rows==0) and Stale (last_success) rules miss it; a sustained transient-error
    count is the only signal. DISTINCT alertname + NO ``job=`` label keep it out
    of the cron-keyed enumerations, so it needs its OWN pins here.
    """

    ALERT = "AlphalensForm4IncrementalTransientErrors"
    METRIC = "alphalens_form4_transient_errors"

    def _one(self) -> dict:
        matches = [r for r in _load_rules()["groups"][0]["rules"] if r.get("alert") == self.ALERT]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {self.ALERT}, found {len(matches)}."
        )
        return matches[0]

    def test_alert_exists(self) -> None:
        self._one()

    def test_expr_is_sustained_gauge_over_time_positive(self) -> None:
        expr = self._one()["expr"]
        self.assertIn(f"min_over_time({self.METRIC}", expr)
        self.assertIn("> 0", expr)
        # Gauge, not counter — must not use monotonic-counter functions.
        for func in ("increase(", "rate(", "irate("):
            self.assertNotIn(func, expr)

    def test_has_for_debounce_and_routes_warning_telegram(self) -> None:
        rule = self._one()
        self.assertIn("for", rule)
        self.assertEqual(rule.get("labels", {}).get("severity"), "warning")
        self.assertEqual(rule.get("labels", {}).get("route"), "telegram")

    def test_carries_no_job_label_so_it_stays_out_of_cron_enums(self) -> None:
        rule = self._one()
        self.assertNotIn("job", rule.get("labels", {}))
        self.assertIsNone(re.search(r'job="[^"]+"', rule.get("expr", "")))


class TestEdgarPressReleaseDoesNotCollideWithCronEnums(unittest.TestCase):
    """Regression pin: the #384 alerts stay isolated from the cron-keyed
    AlphalensJobStale / AlphalensJobMetricMissing machinery — same contract the
    VIX-cache rules hold.
    """

    DARK = "AlphalensEdgarPressReleaseDark"
    MISSING = "AlphalensEdgarPressReleaseMetricMissing"

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def test_alertnames_are_distinct_from_cron_alertnames(self) -> None:
        cron = {"AlphalensJobStale", "AlphalensJobMetricMissing", "AlphalensJobFailed"}
        self.assertNotIn(self.DARK, cron)
        self.assertNotIn(self.MISSING, cron)

    def test_rules_carry_no_job_label(self) -> None:
        # A job= label would falsely register these in the job-keyed parity test
        # and demand a phantom systemd unit. The distinct alertname is the real
        # isolation; this is the belt-pin (the metric is a per-source slice of
        # thematic-ingest, not a systemd unit's last_success).
        for name in (self.DARK, self.MISSING):
            matches = [r for r in self._rules() if r.get("alert") == name]
            self.assertEqual(len(matches), 1)
            self.assertNotIn("job", matches[0].get("labels", {}))
            self.assertIsNone(re.search(r'job="[^"]+"', matches[0].get("expr", "")))

    def test_active_jobs_not_inflated_by_this_alert(self) -> None:
        # The switch is per-SOURCE, not a new cron unit — it must not be added to
        # ACTIVE_JOBS (which would demand a staleness rule + emitting unit).
        self.assertNotIn("thematic-ingest", ACTIVE_JOBS)
        self.assertNotIn("edgar-press-release", ACTIVE_JOBS)


if __name__ == "__main__":
    unittest.main()
