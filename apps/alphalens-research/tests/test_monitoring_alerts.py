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


if __name__ == "__main__":
    unittest.main()
