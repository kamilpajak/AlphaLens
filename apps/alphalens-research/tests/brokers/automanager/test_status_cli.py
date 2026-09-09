"""CLI tests for ``alphalens broker status`` (#1378) — the epic's capstone.

One read-only invocation renders account, exposure/slot/cash headroom, the
resting orders, the open entry-trail tiers and the daemon's health. The rules
under test:

* human and JSON carry the SAME numbers (compared field by field, not by
  "the string appears somewhere");
* an unhealthy instance exits 0 — health is content, and an alert rule must
  not be duplicated in a status command; only a failed broker read exits 1;
* ``--offline`` renders the half that needs no broker, which is exactly what a
  broker outage leaves available;
* ``--env live`` composes the rails from the installed unit (#1377) and says so,
  because the composition describes the LOADED unit config.

The broker is faked at the registry seam; the snapshot itself is NOT patched,
so a broken fold or renderer fails here.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from typer.testing import CliRunner

from tests.brokers.automanager.test_status_snapshot import (
    _account,
    _FakeBroker,
    _position,
    _submission,
    _watch_line,
    _working_order,
)

REGISTRY_SEAM = "alphalens_pipeline.brokers.registry.get_default_broker"
LIVE_FACTORY_SEAM = "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env"
SHOW_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._systemctl_show"
READ_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._read_text"

_UNIT_PAYLOAD = (
    "ALPHALENS_BROKER_ENVIRONMENT=live ALPHALENS_BROKER_ALLOW_ORDERS=1 "
    "ALPHALENS_BROKER_MAX_OPEN=10 ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC=1.0 "
    "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R=1.0 ALPHALENS_BROKER_SIZING_EQUITY=15000 "
    "ALPHALENS_BROKER_SIZING_EQUITY_MODE=declared "
    "ALPHALENS_BROKER_EXIT_POLICY=breakeven_trail ALPHALENS_BROKER_MAX_FEE_BPS=1000 "
    "ALPHALENS_BROKER_ENTRY_TRAIL_BPS=50 ALPHALENS_BROKER_ENTRY_WATCH_MAX_PICKS=10 "
    "ALPHALENS_SAXO_LIVE_STANDING=ACCT-1 SAXO_LIVE_ACCOUNT_KEY=ACCT-1"
)


def _show(unit: str, prop: str) -> str:
    return {
        "LoadState": "loaded",
        "Environment": _UNIT_PAYLOAD,
        "EnvironmentFiles": "/etc/alphalens/env (ignore_errors=no)",
        "DropInPaths": "/h/10-allow-orders.conf",
        "NeedDaemonReload": "no",
        "ActiveState": "active",
        "ActiveEnterTimestamp": "Tue 2026-09-09 06:00:00 UTC",
    }[prop]


class StatusCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.textfile_dir = self.home / "textfile"
        self.textfile_dir.mkdir(parents=True)
        env_patch = mock.patch.dict(
            "os.environ",
            {
                "ALPHALENS_TEXTFILE_DIR": str(self.textfile_dir),
                "ALPHALENS_BROKER_MAX_OPEN": "10",
                "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC": "1.0",
            },
            clear=True,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.root = self.home / ".alphalens" / "broker_orders" / "sim"
        self.root.mkdir(parents=True)

    def _seed_book(self) -> _FakeBroker:
        (self.root / "submissions.jsonl").write_text(
            json.dumps(_submission("O-1")) + "\n", encoding="utf-8"
        )
        (self.root / "entry_trails.jsonl").write_text(
            _watch_line("ENPH-2026-09-08-entry-t0", limit=30.0, qty=4.0, pick_key="ENPH:2026-09-08")
            + "\n",
            encoding="utf-8",
        )
        return _FakeBroker(
            account=_account(),
            positions=[_position(market_value=2_000.0)],
            orders=[_working_order("O-1")],
        )

    def _invoke(self, *args: str, broker: _FakeBroker | None = None):
        from alphalens_cli.commands.broker import broker_app

        fake = broker if broker is not None else _FakeBroker()
        with mock.patch(REGISTRY_SEAM, return_value=fake):
            return self.runner.invoke(broker_app, ["status", *args])

    def _json(self, *args: str, broker: _FakeBroker | None = None) -> dict:
        result = self._invoke("--format", "json", *args, broker=broker)
        self.assertEqual(result.exit_code, 0, result.output)
        return json.loads(result.stdout)

    # --- contract -----------------------------------------------------------

    def test_json_is_one_value_with_every_section(self) -> None:
        payload = self._json(broker=self._seed_book())
        self.assertEqual(payload["schema"], "alphalens.broker.status/v1")
        self.assertEqual(payload["env"], "sim")
        for section in (
            "account",
            "exposure",
            "slots",
            "cash_floor",
            "orders",
            "watches",
            "health",
        ):
            with self.subTest(section=section):
                self.assertIn(section, payload)

    def test_the_numbers_are_the_gate_terms(self) -> None:
        payload = self._json(broker=self._seed_book())
        exposure = payload["exposure"]
        self.assertEqual(exposure["committed"], 500.0)
        self.assertEqual(exposure["filled"], 2_000.0)
        self.assertEqual(exposure["watching"], 120.0)
        self.assertEqual(exposure["used"], 2_620.0)
        self.assertEqual(exposure["limit"], 100_000.0)
        slots = payload["slots"]
        self.assertEqual((slots["brackets"], slots["positions"], slots["watch_picks"]), (1, 1, 1))
        self.assertEqual((slots["used"], slots["limit"], slots["free"]), (3, 10, 7))

    def test_human_and_json_agree_number_by_number(self) -> None:
        broker = self._seed_book()
        payload = self._json(broker=broker)
        human = self._invoke(broker=self._seed_book()).stdout
        for value in (
            f"{payload['exposure']['used']:,.2f}",
            f"{payload['exposure']['limit']:,.2f}",
            f"{payload['exposure']['headroom']:,.2f}",
            f"{payload['account']['total_value']:,.2f}",
            f"slots {payload['slots']['used']}/{payload['slots']['limit']}",
        ):
            with self.subTest(value=value):
                self.assertIn(value, human)

    def test_the_rows_are_rendered_not_summarised(self) -> None:
        # The issue asks for the `orders` renderer and the `watches` fold, so
        # the rows themselves must appear, not just counts.
        human = self._invoke(broker=self._seed_book()).stdout
        self.assertIn("O-1", human)
        self.assertIn("KO E1", human)  # the order's human label
        self.assertIn("ENPH:2026-09-08", human)  # the watch's pick key
        self.assertIn("E1", human)

    # --- exit codes ---------------------------------------------------------

    def test_an_unhealthy_instance_still_exits_zero(self) -> None:
        (self.root / "KILL").write_text("", encoding="utf-8")
        result = self._invoke(broker=self._seed_book())
        self.assertEqual(result.exit_code, 0, result.output)
        # The unhealth must be VISIBLE, not merely survivable (#1385 reworded
        # the line, so this asserts the state rather than the word "KILL").
        kill_line = next(line for line in result.stdout.splitlines() if line.startswith("kill"))
        self.assertIn("PRESENT: instance", kill_line)

    def test_a_failed_broker_read_exits_one_with_empty_stdout(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from broker_contract.contract import BrokerError

        broken = _FakeBroker()
        broken.get_account = mock.Mock(side_effect=BrokerError("gateway down"))
        with mock.patch(REGISTRY_SEAM, return_value=broken):
            result = self.runner.invoke(broker_app, ["status"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("gateway down", result.stderr)

    def test_an_unknown_format_refuses(self) -> None:
        result = self._invoke("--format", "xml")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, "")

    # --- offline ------------------------------------------------------------

    def test_offline_skips_the_broker_and_keeps_the_health_half(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        broker = _FakeBroker()

        def explode():
            raise AssertionError("--offline must not call the broker")

        broker.get_account = explode
        with mock.patch(REGISTRY_SEAM, return_value=broker):
            result = self.runner.invoke(broker_app, ["status", "--offline", "--format", "json"])
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["offline"])
        self.assertIsNone(payload["account"])
        self.assertIsNotNone(payload["health"])

    def test_offline_live_still_gets_the_units_textfile_dir(self) -> None:
        # #1387: composition is where ALPHALENS_TEXTFILE_DIR comes from, so
        # skipping it offline blanked heartbeat, prices and the kill gauge —
        # the three lines the offline half exists to show.
        from alphalens_cli.commands.broker import broker_app

        metrics = self.home / "unit-textfiles"
        metrics.mkdir()
        job = "broker-manager-live"
        stamp = dt.datetime.now(dt.UTC).timestamp() - 30
        (metrics / f"alphalens_domain_{job}.prom").write_text(
            f'alphalens_broker_manager_last_tick_timestamp_seconds{{job="{job}"}} {stamp}\n'
            f'alphalens_broker_manager_kill_active{{job="{job}"}} 0\n',
            encoding="utf-8",
        )
        payload = _UNIT_PAYLOAD + f" ALPHALENS_TEXTFILE_DIR={metrics}"

        def show(unit: str, prop: str) -> str:
            return _show(unit, prop) if prop != "Environment" else payload

        with (
            mock.patch(SHOW_SEAM, show),
            mock.patch(READ_SEAM, lambda _path: "SAXO_LIVE_APP_KEY=k\n"),
        ):
            result = self.runner.invoke(
                broker_app, ["status", "--env", "live", "--offline", "--format", "json"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        health = json.loads(result.stdout)["health"]
        self.assertIsNotNone(health["heartbeat_age_s"])
        self.assertAlmostEqual(health["heartbeat_age_s"], 30, delta=5)
        self.assertEqual(health["kill_active_gauge"], 0.0)

    def test_offline_live_survives_a_broken_systemctl(self) -> None:
        # The offline promise covers a broken user manager too: `--offline
        # --env live` must not COMPOSE the unit (it reports no limits anyway),
        # and the best-effort unit probe must degrade rather than abort. A hung
        # systemctl raises SubprocessError, which is not an OSError.
        import subprocess

        from alphalens_cli.commands.broker import broker_app

        def explode(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="systemctl", timeout=30)

        with mock.patch(SHOW_SEAM, explode):
            result = self.runner.invoke(
                broker_app, ["status", "--env", "live", "--offline", "--format", "json"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["env"], "live")
        # Composition is BEST-EFFORT offline: it degrades, it does not refuse.
        self.assertIn("--offline", payload["limits_source"])
        self.assertIn("not composed", payload["limits_source"])
        self.assertEqual(payload["health"]["unit_state"], "unknown")

    # --- env + provenance ---------------------------------------------------

    def test_env_live_composes_the_rails_and_names_the_source(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        fake = _FakeBroker()
        with (
            mock.patch(SHOW_SEAM, _show),
            mock.patch(READ_SEAM, lambda _path: "SAXO_LIVE_APP_KEY=k\n"),
            mock.patch(LIVE_FACTORY_SEAM, return_value=(fake, mock.Mock())),
        ):
            result = self.runner.invoke(broker_app, ["status", "--env", "live"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("limits from unit alphalens-broker-manager-live.service", result.stdout)
        self.assertIn("env=live gateway=live", result.stderr)

    def test_without_the_option_the_limits_come_from_the_process(self) -> None:
        human = self._invoke().stdout
        self.assertIn("limits from process env (not composed)", human)

    # --- health content -----------------------------------------------------

    def test_health_reports_ages_not_verdicts(self) -> None:
        job = "broker-manager-sim"
        stale = dt.datetime.now(dt.UTC).timestamp() - 4_000
        (self.textfile_dir / f"alphalens_domain_{job}.prom").write_text(
            f'alphalens_broker_manager_last_tick_timestamp_seconds{{job="{job}"}} {stale}\n',
            encoding="utf-8",
        )
        payload = self._json(broker=self._seed_book())
        self.assertGreater(payload["health"]["heartbeat_age_s"], 3_000)
        # An age, never a "STALE" verdict: the Prometheus rule owns thresholds.
        self.assertNotIn("STALE", json.dumps(payload["health"]).upper())

    def test_the_kill_line_states_the_absence_not_only_the_gauge(self) -> None:
        # #1385: with the gauge present the line used to read
        # "kill      daemon view 0", leaving "no KILL file" implied. The one
        # line an operator scans during an emergency stop must SAY it.
        job = "broker-manager-sim"
        (self.textfile_dir / f"alphalens_domain_{job}.prom").write_text(
            f'alphalens_broker_manager_kill_active{{job="{job}"}} 0\n', encoding="utf-8"
        )
        human = self._invoke(broker=self._seed_book()).stdout
        kill_line = next(line for line in human.splitlines() if line.startswith("kill"))
        self.assertIn("none", kill_line)
        self.assertIn("daemon view 0 @ last tick", kill_line)

    def test_a_present_kill_file_is_named_before_the_gauge(self) -> None:
        (self.root / "KILL").write_text("", encoding="utf-8")
        job = "broker-manager-sim"
        (self.textfile_dir / f"alphalens_domain_{job}.prom").write_text(
            f'alphalens_broker_manager_kill_active{{job="{job}"}} 0\n', encoding="utf-8"
        )
        human = self._invoke(broker=self._seed_book()).stdout
        kill_line = next(line for line in human.splitlines() if line.startswith("kill"))
        self.assertIn("PRESENT", kill_line)
        self.assertIn("instance", kill_line)
        self.assertNotIn("none", kill_line)

    def test_the_refusal_line_carries_its_age(self) -> None:
        picks = self.root / "picks.jsonl"
        picks.write_text(
            json.dumps(
                {
                    "ticker": "OLN",
                    "date": "2026-08-13",
                    "refused_ts": (dt.datetime.now(dt.UTC) - dt.timedelta(days=27)).isoformat(
                        timespec="seconds"
                    ),
                    "reason": "gross cap",
                    "status": "refused",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        human = self._invoke(broker=self._seed_book()).stdout
        refusal = next(line for line in human.splitlines() if line.startswith("refused"))
        self.assertIn("27d ago", refusal)
        self.assertIn("2026-08-13", refusal)
        # The age must PREFIX the text: appended, it reads as part of the
        # reason and the brief date still leads the line.
        self.assertLess(refusal.index("27d ago"), refusal.index("OLN"))

    def test_a_refusal_without_a_timestamp_renders_no_age_and_no_none(self) -> None:
        picks = self.root / "picks.jsonl"
        picks.write_text(
            json.dumps(
                {"ticker": "OLN", "date": "2026-08-13", "reason": "gross cap", "status": "refused"}
            )
            + "\n",
            encoding="utf-8",
        )
        human = self._invoke(broker=self._seed_book()).stdout
        refusal = next(line for line in human.splitlines() if line.startswith("refused"))
        self.assertIn("OLN 2026-08-13", refusal)
        self.assertNotIn("ago", refusal)
        self.assertNotIn("None", refusal)

    def test_a_timestamp_ahead_of_now_is_not_rendered_as_just_now(self) -> None:
        # Clamping a negative age to "0s ago" would hide a clock disagreement
        # between the writing host and this one behind a plausible number.
        from alphalens_cli.commands.broker import _age_phrase

        self.assertNotIn("0s ago", _age_phrase(-7200.0))
        self.assertIn("ahead", _age_phrase(-7200.0))

    def test_the_age_floors_the_unit_rather_than_rounding_it_up(self) -> None:
        from alphalens_cli.commands.broker import _age_phrase

        self.assertIn("23h ago", _age_phrase(86_399.9))
        self.assertIn("1d ago", _age_phrase(86_400.0))

    def test_no_token_value_ever_reaches_the_output(self) -> None:
        payload = self._json(broker=self._seed_book())
        blob = json.dumps(payload)
        for key in ("access_token", "refresh_token"):
            with self.subTest(key=key):
                self.assertNotIn(key, blob)


if __name__ == "__main__":
    unittest.main()
