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
    "thematic-build",
    "feedback-shadow-returns",
    "form4-incremental",
    "grouped-daily-topup",
    # Saxo auto-manager idle OAuth keep-alive oneshot (ADR 0013 T6/T7); wires
    # the standard ExecStopPost emit-job-metrics hook like the other timers.
    # The broker-manager daemon itself stays OUT of this enum — it is a
    # Type=simple daemon with its own distinct heartbeat alert family
    # (AlphalensBrokerManagerHeartbeatStale/Missing), not the shared
    # alphalens_job_* last_success pattern.
    "saxo-refresh",
    # LIVE market-data OAuth idle keep-alive (ADR 0017 / design memo
    # broker_live_daemon_arm_design_2026_08_10.md §5) — the same
    # timer-driven-oneshot shape as saxo-refresh above, just for the
    # separate saxo_auth_live chain. The LIVE broker-manager daemon itself
    # stays OUT of this enum for the same reason SIM's does (its own
    # AlphalensBrokerManagerLiveHeartbeatStale family, not this pattern).
    "saxo-marketdata-refresh",
    # Market-cap bracket-cost replay (PR #1087). A research measurement rather
    # than live infrastructure, so nobody watches it daily — which is precisely
    # why it carries the same staleness pair as the production timers. A silent
    # stop would surface only in October as "the measurement never matured",
    # and the lost weeks of maturity cannot be recovered afterwards.
    "bracket-cost",
    # waiting:data wake-date notifier. The only job whose HEALTHY output is
    # silence, which makes it the one most able to die unnoticed: a dead timer
    # and a day with nothing due look identical from the Telegram side. Every
    # issue parked on a future date would then sleep through its wake and stay
    # parked indefinitely, with nothing anywhere saying so. The staleness pair
    # is the only thing that can tell the two apart.
    "issue-wake",
    # Hourly live-rules sync (issue #1073). The job that keeps THIS rules file
    # deployed: it converges the live VPS Prometheus copy to the origin/main
    # blob. A dead sync timer re-opens the drift class that bit three times
    # (missing rules 2026-05-31, absent new alerts 2026-08-19, a metric rename
    # 2026-08-20) — and the failure is maximally silent because the thing that
    # would report it is the thing that stopped being deployed. The staleness
    # pair below still fires from the LIVE rules already loaded, which keep
    # working until the next change.
    "prometheus-rules-sync",
    # Hourly Grafana provisioning sync (issue #1110). Same convergence contract
    # as prometheus-rules-sync, one layer up: it keeps the live datasource yml,
    # the dashboards provider yml and the cron-health dashboard JSON equal to
    # the origin/main blobs. A dead timer is silent by construction — Grafana
    # keeps serving the LAST provisioned content, so the dashboards look fine
    # right up until someone needs a merged change that never arrived. That is
    # exactly how the 2026-08-24 incident survived two months: the live
    # datasource declared no `uid: prometheus` while every panel asked for it.
    "grafana-provisioning-sync",
    # Hourly host-side systemd drift check (#1135). Detect-never-auto-apply:
    # a dead check quietly re-opens the #1121 blindness (a hand edit to the
    # broker units running unobserved for weeks), so it carries the standard
    # staleness pair; the drift itself pages separately via
    # AlphalensSystemdUnitDrift on the findings gauge.
    "systemd-drift-check",
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
            # 2h = 2× the hourly cadence (the edgar-detect 2× precedent).
            # Tight on purpose: the job is a cheap read-mostly convergence
            # loop, so a 2h silence already means the timer died — and every
            # hour it stays dead is an hour a merged rule change is not live.
            "prometheus-rules-sync": 7200,
            # 2h = 2× the hourly cadence, same reasoning as the rules sync
            # above: a cheap convergence loop, so 2h of silence already means
            # the timer died, and every hour it stays dead is an hour a merged
            # dashboard or datasource change is not live.
            "grafana-provisioning-sync": 7200,
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
            "alphalens_feedback_",
            "alphalens_rules_sync_",
            "alphalens_grafana_sync_",
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
        # nonsense on a gauge (the AlphalensEdgarPressReleaseDark lesson,
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


class TestThematicBriefLadderQualityRule(unittest.TestCase):
    """Pin the brief-quality (not volume) rule.

    2026-07-25/26 shipped volume-NORMAL days whose briefs all carried an
    empty ladder (``brief_trade_setup`` status NO_STRUCTURE, 0 entry
    tiers). Every volume rule above read healthy, so nothing fired. The
    sibling July regression (#910) is deliberately OUT of scope here — it
    collapsed briefs to zero, which the volume rules already own.
    ``AlphalensThematicBriefLadderUsableLow`` ratios the new
    ``alphalens_thematic_brief_usable_ladder_ratio`` gauge against an
    ABSOLUTE floor rather than a rolling baseline — the failure signature
    is a hard 0.0, and an absolute threshold has no cold-start blind
    window on a brand-new series.

    Threshold justification (measured over all 64 days in
    ``~/.alphalens/thematic_briefs/`` that carry the column, 2026-05-27 ..
    2026-07-29): ratio == 1.0 on 62 days; the only two exceptions are
    0.9524 and 0.8421, both single-name yfinance gaps. ``< 0.5`` therefore
    has ZERO historical false positives.

    The incident days themselves are NOT measurable on disk — both
    2026-07-25 and 2026-07-26 were regenerated after the #917 fix and now
    read 1.0. The must-fire direction rests on #917's commit body ("every
    candidate" degraded to ``NO_STRUCTURE``), i.e. ratio 0.0 at n=9 / n=14.

    The benign per-name gap carries the SAME row signature as the incident
    (NO_STRUCTURE, close=0.0, atr=0.0) and differs only in COUNT, so the
    ratio alone is denominator-sensitive: the observed 3-bad-name day
    replayed at n=3 or n=5 would trip a ratio-only rule. The absolute
    ``unusable >= 4`` clause (one above the worst benign count) is what
    makes the rule denominator-safe, mirroring the third clause of
    ``AlphalensThematicBriefUnavailableHigh``.
    """

    LADDER_LOW = "AlphalensThematicBriefLadderUsableLow"
    LADDER_MISSING = "AlphalensThematicBriefLadderUsableMetricMissing"

    def _rule(self, name: str) -> dict:
        rules = _load_rules()["groups"][0]["rules"]
        matches = [r for r in rules if r.get("alert") == name]
        self.assertEqual(len(matches), 1, f"expected exactly one {name} rule, got {len(matches)}")
        return matches[0]

    def test_rule_present_and_ratios_the_quality_gauge(self) -> None:
        expr = self._rule(self.LADDER_LOW)["expr"]
        self.assertIn("alphalens_thematic_brief_usable_ladder_ratio", expr)
        self.assertIn("< 0.5", expr)

    def test_rule_carries_the_volume_guard(self) -> None:
        # A 1-2 brief day must not trip on one bad name; the guard reads
        # briefs_total from the SAME thematic-build emit so the two series
        # are label-compatible without vector-matching gymnastics.
        expr = self._rule(self.LADDER_LOW)["expr"]
        self.assertIn("alphalens_thematic_briefs_total >= 3", expr)

    def test_rule_carries_the_absolute_unusable_count_guard(self) -> None:
        # The ratio alone is denominator-sensitive: the worst OBSERVED
        # benign day had 3 unusable names, which is 0.842 at n=19 (silent)
        # but 0.4 at n=5 and 0.333 at n=3 (both would page). The absolute
        # clause puts the floor one above that worst benign count so no
        # observed benign pattern fires at any n, while a whole-day
        # collapse (unusable == n) still clears it for every n >= 4.
        expr = self._rule(self.LADDER_LOW)["expr"]
        self.assertIn(
            "(alphalens_thematic_briefs_total - alphalens_thematic_brief_usable_ladder_total) >= 4",
            expr,
        )

    def test_rule_debounces_across_at_least_two_slots(self) -> None:
        # thematic-build fires 6x/day (every 4h); 6h survives one bad slot
        # that the next slot's rerun heals.
        self.assertEqual(self._rule(self.LADDER_LOW).get("for"), "6h")

    def test_rule_labels_match_the_thematic_family(self) -> None:
        labels = self._rule(self.LADDER_LOW).get("labels", {})
        self.assertEqual(labels.get("severity"), "warning")
        self.assertEqual(labels.get("route"), "telegram")
        self.assertEqual(labels.get("unit"), "thematic-build")

    def test_rule_carries_no_job_label_or_matcher(self) -> None:
        # Domain rules stay out of the job-keyed cron enumerations.
        rule = self._rule(self.LADDER_LOW)
        self.assertNotIn("job", rule.get("labels", {}))
        self.assertNotIn('job="', rule["expr"])

    def test_missing_gauge_has_its_own_rule(self) -> None:
        # The threshold rule can only fire on a series that EXISTS. If the
        # brief emit is dropped (broken textfile collector, a refactor that
        # loses the splice), the quality gauge vanishes and the alert goes
        # silently dead — the exact failure mode this whole family exists to
        # prevent. Pair it with an absent() rule, as the form4 / edgar / vix
        # gauges already do.
        rule = self._rule(self.LADDER_MISSING)
        self.assertEqual(
            rule["expr"].strip(), "absent(alphalens_thematic_brief_usable_ladder_ratio)"
        )
        self.assertEqual(rule.get("for"), "1h")
        self.assertEqual(rule.get("labels", {}).get("unit"), "thematic-build")


class TestThematicThemeRollupWriteRules(unittest.TestCase):
    """Pin the alerts that watch the theme-rollup write outcome gauge.

    The rollup is written EXACTLY ONCE per asof (the deciding slot) and the write
    is best-effort, so a failure is swallowed and the day is gone for good: a
    later slot cannot redo it (the propensities need the deciding slot's event
    counts, which the growing events parquet has since overwritten). The one-hot
    ``alphalens_thematic_theme_rollup_write{outcome=...}`` gauge exists to make
    that hole countable — and a gauge nothing alerts on is not watched.

    Two properties of the gauge shape the expression and must not be edited away:

    * The six daily slots OVERWRITE the same textfile, so an instant vector reads
      whichever slot fired last. Both sides must be read over a range.
    * The two sides carry DIFFERENT ``outcome`` label values, so a bare ``and``
      (which matches on the full label set) never matches and the rule would sit
      silently green forever. ``ignoring(outcome)`` is what makes it able to fire.

    The second alert covers the state the runtime degradation introduced: an
    outcome literal the emitter does not recognise is published as
    ``outcome="unknown"`` instead of raising mid-stage. That is a code defect, and
    the failed/written rule above cannot see it (the write may well have
    succeeded), so it needs a rule of its own.
    """

    METRIC = "alphalens_thematic_theme_rollup_write"
    LOST = "AlphalensThematicThemeRollupLost"
    UNKNOWN = "AlphalensThematicThemeRollupOutcomeUnknown"
    WINDOW = "24h"

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def _one(self, alertname: str) -> dict:
        matches = [r for r in self._rules() if r.get("alert") == alertname]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {alertname}, found {len(matches)}."
        )
        return matches[0]

    def test_both_alerts_exist(self) -> None:
        for name in (self.LOST, self.UNKNOWN):
            self._one(name)

    def test_lost_expr_reads_both_sides_over_a_range(self) -> None:
        # An instant vector would read whichever of the six slots wrote the
        # textfile last, which is a coin toss rather than a question about the day.
        expr = self._one(self.LOST)["expr"]
        for outcome in ("failed", "written"):
            self.assertIn(
                f'max_over_time({self.METRIC}{{outcome="{outcome}"}}[{self.WINDOW}])',
                expr,
                f"the {outcome} side must be read over a {self.WINDOW} range",
            )

    def test_lost_expr_joins_ignoring_the_outcome_label(self) -> None:
        # The load-bearing clause. Without it the two sides never match and the
        # rule is silently incapable of firing.
        expr = self._one(self.LOST)["expr"]
        self.assertIn("and ignoring(outcome)", expr)

    def test_lost_expr_requires_a_failure_and_no_write(self) -> None:
        expr = self._one(self.LOST)["expr"]
        self.assertIn("> 0", expr)
        self.assertIn("== 0", expr)

    def test_unknown_expr_reads_the_unknown_series_over_a_range(self) -> None:
        expr = self._one(self.UNKNOWN)["expr"]
        self.assertIn(
            f'max_over_time({self.METRIC}{{outcome="unknown"}}[{self.WINDOW}])',
            expr,
        )
        self.assertIn("> 0", expr)

    def test_neither_alert_uses_counter_functions_on_the_gauge(self) -> None:
        # The gauge is overwritten per run, never accumulated.
        for name in (self.LOST, self.UNKNOWN):
            expr = self._one(name)["expr"]
            for func in ("increase(", "rate(", "irate("):
                self.assertNotIn(func, expr, f"{name} applies {func} to a gauge")

    def test_both_alerts_debounce_across_at_least_two_slots(self) -> None:
        # thematic-build fires every 4h; 6h means the condition survived a slot.
        for name in (self.LOST, self.UNKNOWN):
            self.assertEqual(self._one(name).get("for"), "6h")

    def test_both_alerts_carry_the_thematic_family_labels(self) -> None:
        for name in (self.LOST, self.UNKNOWN):
            labels = self._one(name).get("labels", {})
            self.assertEqual(labels.get("severity"), "warning")
            self.assertEqual(labels.get("route"), "telegram")
            self.assertEqual(labels.get("unit"), "thematic-build")

    def test_neither_alert_carries_a_job_label_or_matcher(self) -> None:
        # A per-domain slice, not a systemd unit's last_success — a job= label
        # would falsely register it in the job-keyed parity tests.
        for name in (self.LOST, self.UNKNOWN):
            rule = self._one(name)
            self.assertNotIn("job", rule.get("labels", {}))
            self.assertIsNone(re.search(r'job="[^"]+"', rule.get("expr", "")))


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


class TestEdgarNoDispatchTradingDays(unittest.TestCase):
    """Pin the calendar-aware no-dispatch alert.

    The old ``AlphalensEdgarNoCandidates5d`` rule was a blind PromQL window
    ``max_over_time(alphalens_edgar_events_dispatched_total[5d]) == 0``. PromQL
    cannot consult a holiday calendar, so a [5d] window only tolerates weekends
    statistically and can still false-fire across the December holiday cluster
    or a genuinely quiet small watchlist.

    The replacement moves the calendar awareness into the EMITTER: the
    edgar-detect cron persists the last dispatch date and emits a calendar-aware
    gauge ``alphalens_edgar_trading_days_since_last_dispatch`` (counted with the
    XNYS session calendar, immune to weekends + holidays). The rule is a simple
    threshold on that gauge. Detector LIVENESS stays covered separately by
    AlphalensJobStale + AlphalensJobMetricMissing, so this rule needs no
    absent()/MetricMissing of its own.
    """

    GAUGE = "alphalens_edgar_trading_days_since_last_dispatch"
    ALERT = "AlphalensEdgarNoDispatchTradingDays"
    # Every old PromQL-window alertname must be gone — the rule changed shape
    # (gauge threshold, not max_over_time window), so the window-encoding names
    # would mislead the operator on Telegram.
    OLD_ALERTS = ("AlphalensEdgarNoCandidates5d", "AlphalensEdgarNoCandidates24h")
    # 5 trading days = a full trading week with zero dispatched candidates.
    # Conservative because the dispatch cadence cannot be tightly calibrated
    # (the source gauge is transient + Prometheus retains only ~10d) and this is
    # an info nudge with liveness covered separately. Single easy-to-tune literal.
    THRESHOLD = 5

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def _one(self, alertname: str) -> dict:
        matches = [r for r in self._rules() if r.get("alert") == alertname]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {alertname}, found {len(matches)}."
        )
        return matches[0]

    def test_alert_exists(self) -> None:
        self._one(self.ALERT)

    def test_old_promql_window_alertnames_are_gone(self) -> None:
        names = {r.get("alert") for r in self._rules()}
        for old in self.OLD_ALERTS:
            self.assertNotIn(
                old,
                names,
                f"{old} must be replaced by AlphalensEdgarNoDispatchTradingDays "
                "(calendar-aware gauge rule).",
            )

    def test_expr_is_gauge_threshold(self) -> None:
        # The new rule reads the calendar-aware gauge and compares to the
        # threshold. No PromQL window function (max_over_time / increase /
        # rate) — the calendar math already happened in the emitter.
        expr = self._one(self.ALERT)["expr"]
        self.assertIn(self.GAUGE, expr)
        self.assertNotIn("max_over_time", expr)
        self.assertNotIn("increase(", expr)
        self.assertNotIn("rate(", expr)
        self.assertNotIn("[5d]", expr)
        self.assertNotIn("[24h]", expr)
        # Pin the exact threshold expression: gauge > 5, nothing more.
        self.assertRegex(
            expr.strip(),
            rf"^{re.escape(self.GAUGE)}\s*>\s*{self.THRESHOLD}$",
        )

    def test_has_for_debounce(self) -> None:
        # 6h debounce: the gauge is refreshed every 15-min run, so a single
        # spurious read cannot fire; 6h matches the sibling dark rules.
        self.assertEqual(self._one(self.ALERT).get("for"), "6h")

    def test_severity_is_info(self) -> None:
        # A quiet / emptied watchlist is a nudge to review portfolio.yaml, not
        # an outage — matches the old rule's info severity (alert-fatigue
        # discipline shared with the dark rules).
        self.assertEqual(self._one(self.ALERT).get("labels", {}).get("severity"), "info")

    def test_routes_to_telegram(self) -> None:
        self.assertEqual(self._one(self.ALERT).get("labels", {}).get("route"), "telegram")

    def test_wording_describes_trading_days(self) -> None:
        # The message must describe the calendar-aware semantics, not a stale
        # PromQL window ("24h" / "5 days" clock-time).
        ann = self._one(self.ALERT).get("annotations", {})
        blob = ann.get("summary", "") + " " + ann.get("description", "")
        self.assertIn("trading day", blob)
        self.assertNotIn("24h", blob)

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
        # fatigue (the AlphalensEdgarNoDispatchTradingDays / brief-anomaly precedent).
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


class TestFeedbackGuardSustainedLookupFailures(unittest.TestCase):
    """Pins for the implausible-guard fail-closed alert (#1090 memo §4).

    A ``lookup_failed`` disposition means the guard could not reach the
    corporate-actions reference and carried the row — i.e. the pre-#1090
    blindness, silently back. Sustained lookup failures must page; a single
    clean nightly run emits 0 and clears it. DISTINCT alertname + NO ``job=``
    label keep it out of the cron-keyed enumerations (the form4
    transient-errors precedent), so it needs its OWN pins here.
    """

    ALERT = "AlphalensFeedbackGuardLookupFailed"
    SERIES = 'alphalens_feedback_guard_total{disposition="lookup_failed"}'

    def _one(self) -> dict:
        matches = [r for r in _load_rules()["groups"][0]["rules"] if r.get("alert") == self.ALERT]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {self.ALERT}, found {len(matches)}."
        )
        return matches[0]

    def test_alert_exists(self) -> None:
        self._one()

    def test_expr_is_sustained_gauge_over_two_nightly_runs(self) -> None:
        expr = self._one()["expr"]
        self.assertIn(f"min_over_time({self.SERIES}", expr)
        self.assertIn("[50h]", expr)  # covers 2 daily 06:30 UTC runs, form4 sizing
        self.assertIn("> 0", expr)
        # Textfile metrics are per-run GAUGES despite the _total suffix — no
        # monotonic-counter functions.
        for func in ("increase(", "rate(", "irate("):
            self.assertNotIn(func, expr)

    def test_has_for_debounce_and_routes_warning_telegram(self) -> None:
        rule = self._one()
        self.assertIn("for", rule)
        self.assertEqual(rule.get("labels", {}).get("severity"), "warning")
        self.assertEqual(rule.get("labels", {}).get("route"), "telegram")
        self.assertEqual(rule.get("labels", {}).get("unit"), "feedback-shadow-returns")

    def test_carries_no_job_label_so_it_stays_out_of_cron_enums(self) -> None:
        rule = self._one()
        self.assertNotIn("job", rule.get("labels", {}))
        self.assertIsNone(re.search(r'job="[^"]+"', rule.get("expr", "")))

    def test_annotation_notes_auto_sync_and_emergency_override(self) -> None:
        # The repo rules file is the SoT; the alphalens-prometheus-rules-sync
        # timer converges the live VPS copy hourly, so the operator reading
        # the page must know a merged change deploys itself — and that the
        # old copy+HUP procedure survives only as an emergency override.
        description = self._one().get("annotations", {}).get("description", "")
        self.assertIn("alphalens-prometheus-rules-sync", description)
        self.assertIn("emergency", description)

    def test_feedback_prefix_is_registered_as_gauge_family(self) -> None:
        # Belt-pin: the no-counter-functions test must cover the new
        # alphalens_feedback_* family so a future rule cannot apply
        # increase()/rate() to the per-run gauge.
        source = Path(__file__).read_text()
        self.assertIn('"alphalens_feedback_",', source)


class TestFeedbackGuardGaugeMissing(unittest.TestCase):
    """Pins for the guard gauge's absence companion.

    A missing series (broken emit / dark scrape) silently DISARMS
    AlphalensFeedbackGuardLookupFailed while the nightly job still exits 0 —
    so absence needs its own page, same discipline as the
    AlphalensJobMetricMissing family.
    """

    ALERT = "AlphalensFeedbackGuardGaugeMissing"

    def _one(self) -> dict:
        matches = [r for r in _load_rules()["groups"][0]["rules"] if r.get("alert") == self.ALERT]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {self.ALERT}, found {len(matches)}."
        )
        return matches[0]

    def test_expr_is_absent_on_the_lookup_failed_series(self) -> None:
        expr = self._one()["expr"]
        self.assertIn("absent(", expr)
        self.assertIn('alphalens_feedback_guard_total{disposition="lookup_failed"}', expr)

    def test_has_for_debounce_and_routes_warning_telegram(self) -> None:
        rule = self._one()
        self.assertIn("for", rule)
        self.assertEqual(rule.get("labels", {}).get("severity"), "warning")
        self.assertEqual(rule.get("labels", {}).get("route"), "telegram")
        self.assertEqual(rule.get("labels", {}).get("unit"), "feedback-shadow-returns")

    def test_carries_no_job_label_so_it_stays_out_of_cron_enums(self) -> None:
        rule = self._one()
        self.assertNotIn("job", rule.get("labels", {}))
        self.assertIsNone(re.search(r'job="[^"]+"', rule.get("expr", "")))

    def test_annotation_notes_auto_sync_and_emergency_override(self) -> None:
        description = self._one().get("annotations", {}).get("description", "")
        self.assertIn("alphalens-prometheus-rules-sync", description)
        self.assertIn("emergency", description)


class TestPrometheusRulesSyncFailed(unittest.TestCase):
    """Pins for the sustained live-rules sync failure alert (issue #1073).

    The sync job emits a zero-initialised one-hot outcome family every run
    (``in_sync``/``synced``/``fetch_failed``/``check_failed``/``reload_failed``).
    A run is healthy exactly when one of the two success labels is 1, so
    "failure sustained across 2+ hourly runs" is "no success label reached 1
    anywhere in a window covering two runs" — which also catches two runs
    failing with DIFFERENT outcomes (fetch then check), where a per-label
    ``min_over_time > 0`` would stay quiet. DISTINCT alertname + NO ``job=``
    label keep it out of the cron-keyed enumerations (the feedback-guard
    precedent), so it needs its OWN pins here.
    """

    ALERT = "AlphalensPrometheusRulesSyncFailed"
    SUCCESS_SELECTOR = 'alphalens_rules_sync_outcome{outcome=~"in_sync|synced"}'

    def _one(self) -> dict:
        matches = [r for r in _load_rules()["groups"][0]["rules"] if r.get("alert") == self.ALERT]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {self.ALERT}, found {len(matches)}."
        )
        return matches[0]

    def test_alert_exists(self) -> None:
        self._one()

    def test_expr_is_no_success_across_two_hourly_runs(self) -> None:
        expr = self._one()["expr"]
        self.assertIn(f"max_over_time({self.SUCCESS_SELECTOR}", expr)
        self.assertIn("[150m]", expr)  # 2 hourly fires + scrape/jitter slack
        self.assertIn("== 0", expr)
        # An absent family must stay quiet here (sum() over an empty vector is
        # empty, not 0) — absence is the MetricMissing family's page.
        self.assertNotIn("absent(", expr)
        # Textfile metrics are per-run GAUGES — no monotonic-counter functions.
        for func in ("increase(", "rate(", "irate("):
            self.assertNotIn(func, expr)

    def test_has_for_debounce_and_routes_warning_telegram(self) -> None:
        rule = self._one()
        self.assertIn("for", rule)
        self.assertEqual(rule.get("labels", {}).get("severity"), "warning")
        self.assertEqual(rule.get("labels", {}).get("route"), "telegram")
        self.assertEqual(rule.get("labels", {}).get("unit"), "prometheus-rules-sync")

    def test_carries_no_job_label_so_it_stays_out_of_cron_enums(self) -> None:
        rule = self._one()
        self.assertNotIn("job", rule.get("labels", {}))
        self.assertIsNone(re.search(r'job="[^"]+"', rule.get("expr", "")))

    def test_annotation_names_journal_and_emergency_override(self) -> None:
        # The actionable half: the journal names WHICH stage failed, and the
        # operator must know the manual copy+HUP path still exists while the
        # sync is broken.
        description = self._one().get("annotations", {}).get("description", "")
        self.assertIn("journalctl --user -u alphalens-prometheus-rules-sync.service", description)
        self.assertIn("emergency", description)

    def test_rules_sync_prefix_is_registered_as_gauge_family(self) -> None:
        # Belt-pin: the no-counter-functions test must cover the new
        # alphalens_rules_sync_* family so a future rule cannot apply
        # increase()/rate() to the per-run gauges.
        source = Path(__file__).read_text()
        self.assertIn('"alphalens_rules_sync_",', source)


class TestGrafanaProvisioningSyncFailed(unittest.TestCase):
    """Pins for the sustained Grafana provisioning sync failure alert (#1110).

    Same shape and same reasoning as ``TestPrometheusRulesSyncFailed``: the job
    emits a zero-initialised one-hot outcome family every run, a run is healthy
    exactly when one of the two success labels is 1, and a DISTINCT alertname
    with NO ``job=`` label keeps it out of the cron-keyed enumerations.

    Why this one matters on its own: the staleness pair only sees a job that
    STOPPED running. A sync that runs hourly and fails hourly — a container
    that never comes back healthy, a datasource uid that never appears — keeps
    the last_success clock frozen but would look identical to a broken exporter
    without this rule naming the failure.
    """

    ALERT = "AlphalensGrafanaProvisioningSyncFailed"
    SUCCESS_SELECTOR = 'alphalens_grafana_sync_outcome{outcome=~"in_sync|synced"}'

    def _one(self) -> dict:
        matches = [r for r in _load_rules()["groups"][0]["rules"] if r.get("alert") == self.ALERT]
        self.assertEqual(
            len(matches), 1, f"Expected exactly one {self.ALERT}, found {len(matches)}."
        )
        return matches[0]

    def test_alert_exists(self) -> None:
        self._one()

    def test_expr_is_no_success_across_consecutive_hourly_runs(self) -> None:
        expr = self._one()["expr"]
        self.assertIn(f"max_over_time({self.SUCCESS_SELECTOR}", expr)
        self.assertIn("[150m]", expr)
        self.assertIn("== 0", expr)
        # An absent family must stay quiet here — absence is MetricMissing's page.
        self.assertNotIn("absent(", expr)
        # Textfile metrics are per-run GAUGES — no monotonic-counter functions.
        for func in ("increase(", "rate(", "irate("):
            self.assertNotIn(func, expr)

    def test_has_for_debounce_and_routes_warning_telegram(self) -> None:
        rule = self._one()
        self.assertIn("for", rule)
        self.assertEqual(rule.get("labels", {}).get("severity"), "warning")
        self.assertEqual(rule.get("labels", {}).get("route"), "telegram")
        self.assertEqual(rule.get("labels", {}).get("unit"), "grafana-provisioning-sync")

    def test_carries_no_job_label_so_it_stays_out_of_cron_enums(self) -> None:
        rule = self._one()
        self.assertNotIn("job", rule.get("labels", {}))
        self.assertIsNone(re.search(r'job="[^"]+"', rule.get("expr", "")))

    def test_annotation_names_journal_and_emergency_override(self) -> None:
        description = self._one().get("annotations", {}).get("description", "")
        self.assertIn(
            "journalctl --user -u alphalens-grafana-provisioning-sync.service", description
        )
        self.assertIn("emergency", description)

    def test_grafana_sync_prefix_is_registered_as_gauge_family(self) -> None:
        # Belt-pin: the no-counter-functions test must cover the new
        # alphalens_grafana_sync_* family.
        source = Path(__file__).read_text()
        self.assertIn('"alphalens_grafana_sync_",', source)


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


class TestBrokerStreamRules(unittest.TestCase):
    """Pins for the SIM order/position-stream breaker rules (rearm design memo
    saxo_stream_breaker_rearm_design_2026_08_22.md §4.6 / §6 INC-5).

    Telegram gets EDGES (the daemon's episode latch pages one OPEN and one
    delivery-confirmed CLOSE); Prometheus owns every LEVEL — these three rules
    are what replaced the 2026-08-22 thirty-minute Telegram metronome, so if
    they rot, a tripped breaker is once again invisible past its two pages.
    """

    ALERTS = (
        "AlphalensBrokerStreamBreakerOpen",
        "AlphalensBrokerStreamStale",
        "AlphalensBrokerStreamFlapping",
    )
    FRESHNESS_GUARD = (
        "(time() - alphalens_broker_manager_last_tick_timestamp_seconds"
        '{job="broker-manager-sim"}) < 300'
    )

    def _rules(self) -> list[dict]:
        return _load_rules()["groups"][0]["rules"]

    def _one(self, name: str) -> dict:
        matches = [r for r in self._rules() if r.get("alert") == name]
        self.assertEqual(len(matches), 1, f"Expected exactly one {name}, found {len(matches)}.")
        return matches[0]

    def test_broker_stream_rules_exist_in_the_cron_health_group_with_route_and_unit(self) -> None:
        for name in self.ALERTS:
            rule = self._one(name)
            self.assertEqual(rule.get("labels", {}).get("severity"), "warning", name)
            self.assertEqual(rule.get("labels", {}).get("route"), "telegram", name)
            self.assertEqual(rule.get("labels", {}).get("unit"), "broker-manager", name)
            self.assertIn("for", rule, name)

    def test_broker_stream_rules_form_unique_alertname_job_pairs(self) -> None:
        # Each alertname appears exactly once and every expr is pinned to the
        # SIM daemon's job label — a LIVE mirror (there is no LIVE stream
        # reader, §4.1) or a copy-paste duplicate would collide here.
        all_names = [r.get("alert") for r in self._rules()]
        for name in self.ALERTS:
            self.assertEqual(all_names.count(name), 1, name)
            expr = self._one(name)["expr"]
            jobs = set(re.findall(r'job="([^"]+)"', expr))
            self.assertEqual(jobs, {"broker-manager-sim"}, name)
            # STATIC labels stay job-free so the rules stay out of the
            # cron-keyed enumerations (ACTIVE_JOBS parity machinery).
            self.assertNotIn("job", self._one(name).get("labels", {}))

    def test_every_broker_stream_rule_carries_the_daemon_freshness_guard(self) -> None:
        # emit_domain_metrics never unlinks: a stopped SIM unit leaves
        # node_exporter re-serving a frozen breaker_open=1 forever. The guard
        # goes false as the heartbeat freezes, so
        # AlphalensBrokerManagerHeartbeatStale fires instead — the correct
        # alert for "the daemon stopped".
        for name in self.ALERTS:
            expr = self._one(name)["expr"]
            self.assertIn(
                self.FRESHNESS_GUARD,
                " ".join(expr.split()),
                f"{name} is missing the daemon-freshness guard.",
            )

    def test_stale_rule_uses_unless_for_the_breaker_guard(self) -> None:
        # `unless` (NOT `and == 0`): the live rules copy is hand-synced and may
        # lead the daemon, so an ABSENT breaker gauge must leave the stale
        # alert behaving exactly as before (the price-stream session_asleep
        # precedent at the LivePriceStreamStale rule).
        expr = " ".join(self._one("AlphalensBrokerStreamStale")["expr"].split())
        self.assertIn(
            'unless alphalens_broker_manager_stream_breaker_open{job="broker-manager-sim"} == 1',
            expr,
        )
        self.assertNotIn('breaker_open{job="broker-manager-sim"} == 0', expr)

    def test_annotations_note_auto_sync_and_emergency_override(self) -> None:
        # The repo rules file is the SoT; the alphalens-prometheus-rules-sync
        # timer converges the live VPS copy hourly, so the operator reading
        # the page must know a merged change deploys itself — and that the
        # old copy+HUP procedure survives only as an emergency override.
        for name in self.ALERTS:
            description = self._one(name).get("annotations", {}).get("description", "")
            self.assertIn("alphalens-prometheus-rules-sync", description, name)
            self.assertIn("emergency", description, name)


if __name__ == "__main__":
    unittest.main()
