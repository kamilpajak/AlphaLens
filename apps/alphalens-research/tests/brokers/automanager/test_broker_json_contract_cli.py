"""The broker group's machine output as ONE contract (#1379).

Three rules, held here for every broker command that can emit JSON:

* **one spelling** — ``--format human|json`` everywhere, with ``--json`` kept
  as a documented alias on the two commands that shipped with it
  (``reconcile``, ``reconcile-fills``; the runbook and the first-fill memo use
  it). A conflicting pair is refused rather than silently resolved;
* **one envelope** — stdout carries exactly ONE JSON value, an object whose
  first two fields are ``schema`` and ``env``. ``env`` is the field that says
  which instance was read, which is the whole SIM/LIVE question;
* **no prose on stdout in JSON mode** — the regression that motivated the
  ticket: ``reconcile --json`` over an empty journal printed
  ``no submission records in … — nothing to reconcile`` to stdout and exited
  0, while the first-fill runbook redirects that stdout into a ``.json`` file.

:class:`JsonEnvelopeContractTest` is deliberately table-driven over EVERY
JSON-emitting command rather than per command: a new broker command that grows
``--format json`` and forgets the envelope fails here, which is the only reason
this file can hold the contract as the group grows.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from broker_contract.contract import (
    AccountSnapshot,
    InstrumentRef,
    OrderState,
    OrderStatus,
)
from typer.testing import CliRunner

REGISTRY_SEAM = "alphalens_pipeline.brokers.registry.get_default_broker"
LIVE_FACTORY_SEAM = "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env"
SHOW_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._systemctl_show"
READ_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._read_text"

# The nine rails the LIVE boot-assert requires (ADR 0017), so an `--env live`
# read composes and resolves a gateway the way it does on the VPS. Values are
# the production shapes measured 2026-09-08; nothing here is armed.
_LIVE_RAILS = {
    "ALPHALENS_BROKER_MAX_OPEN": "10",
    "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC": "1.0",
    "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R": "1.0",
    "ALPHALENS_BROKER_SIZING_EQUITY": "15000",
    "ALPHALENS_BROKER_SIZING_EQUITY_MODE": "declared",
    "ALPHALENS_BROKER_EXIT_POLICY": "breakeven_trail",
    "ALPHALENS_BROKER_MAX_FEE_BPS": "1000",
    "ALPHALENS_BROKER_ENTRY_TRAIL_BPS": "50",
    "ALPHALENS_BROKER_ENTRY_WATCH_MAX_PICKS": "10",
}
_UNIT_PAYLOAD = " ".join(f"{key}={value}" for key, value in _LIVE_RAILS.items())
_UNIT_PROPERTIES = {
    "LoadState": "loaded",
    "Environment": _UNIT_PAYLOAD,
    "EnvironmentFiles": "/etc/alphalens/env (ignore_errors=no)",
    "DropInPaths": "",
    "NeedDaemonReload": "no",
}

# Every command that can render JSON today, with whether it takes `--env`.
# `account`, `positions`, `resolve` and the five mutators are text-only by
# decision (#1389, deferred until a consumer needs them) — adding one here
# without an envelope fails the table. `reconcile-fills` carries no `--env`
# because it WRITES the execution-quality parquet, so #1377 left it out of the
# read-command option; its envelope still names the instance it read.
_JSON_COMMANDS: tuple[tuple[str, list[str], bool], ...] = (
    ("orders", ["orders"], True),
    ("picks", ["picks"], True),
    ("watches", ["watches"], True),
    ("status", ["status", "--offline"], True),
    ("stream-status", ["stream-status"], True),
    ("reconcile", ["reconcile"], True),
    ("reconcile-fills", ["reconcile-fills"], False),
)

_STREAM_GAUGES = {
    "alphalens_broker_manager_stream_reader_up": 1.0,
    "alphalens_broker_manager_stream_breaker_open": 0.0,
}


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        ticker="KO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id="307",
        broker_symbol="ko:xnys",
        currency="USD",
    )


class _ContractFakeBroker:
    """Read-only fake with the audit capability ``reconcile-fills`` requires."""

    name = "fake"

    def __init__(self) -> None:
        self.open_orders = [
            OrderState(
                "E-1",
                OrderStatus.WORKING,
                _instrument(),
                0.0,
                "Working",
                uic=307,
                side="BUY",
                order_type="Limit",
                amount=12.0,
                external_reference="KO-2026-09-09-entry-t0",
                order_relation="StandAlone",
            )
        ]

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="AK-1",
            currency="USD",
            cash=90_000.0,
            total_value=100_000.0,
            margin_available=50_000.0,
            asof=dt.datetime.now(dt.UTC),
        )

    def get_positions(self) -> list[Any]:
        return []

    def list_open_orders(self) -> list[OrderState]:
        return list(self.open_orders)

    def get_order(self, order_id: str) -> OrderState:
        return self.open_orders[0]

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        return self.open_orders[0]


class _BrokerCliCase(unittest.TestCase):
    """Isolated home + textfile dir + a read-only broker on the registry seam."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        home_patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)

        self.textfile_dir = self.home / "textfiles"
        self.textfile_dir.mkdir()
        env_patcher = mock.patch.dict(
            "os.environ", {"ALPHALENS_TEXTFILE_DIR": str(self.textfile_dir)}, clear=True
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        self._seed_stream_gauges("sim")
        self.broker = _ContractFakeBroker()

    def _seed_stream_gauges(self, env: str) -> None:
        path = self.textfile_dir / f"alphalens_domain_broker-manager-{env}-stream.prom"
        lines = [
            f'{name}{{job="broker-manager-{env}"}} {value}'
            for name, value in _STREAM_GAUGES.items()
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def invoke(self, argv: list[str]):
        """Run one broker command with the SIM and LIVE gateways both faked.

        The unit-composition seams are installed for every call, not just the
        LIVE ones: patching them is inert on a SIM read, and it keeps a single
        entry point for the table-driven tests below.
        """
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(SHOW_SEAM, lambda unit, prop: _UNIT_PROPERTIES[prop]),
            mock.patch(READ_SEAM, lambda _path: ""),
            mock.patch(LIVE_FACTORY_SEAM, return_value=(self.broker, mock.Mock())),
            mock.patch(REGISTRY_SEAM, return_value=self.broker),
        ):
            return self.runner.invoke(broker_app, argv)


class JsonEnvelopeContractTest(_BrokerCliCase):
    """Every JSON-emitting broker command answers with the same envelope."""

    def test_stdout_is_exactly_one_json_object_carrying_schema_and_env(self) -> None:
        for name, argv, _ in _JSON_COMMANDS:
            with self.subTest(command=name):
                result = self.invoke([*argv, "--format", "json"])
                self.assertEqual(result.exit_code, 0, result.output)
                payload = json.loads(result.stdout)
                self.assertIsInstance(payload, dict, f"{name} must answer with an object")
                self.assertEqual(
                    list(payload)[:2],
                    ["schema", "env"],
                    f"{name} must lead with schema then env",
                )
                self.assertTrue(
                    payload["schema"].startswith("alphalens.broker.")
                    and payload["schema"].endswith("/v1"),
                    payload["schema"],
                )
                self.assertEqual(payload["env"], "sim")

    def test_the_schema_names_the_command(self) -> None:
        for name, argv, _ in _JSON_COMMANDS:
            with self.subTest(command=name):
                result = self.invoke([*argv, "--format", "json"])
                payload = json.loads(result.stdout)
                self.assertEqual(payload["schema"], f"alphalens.broker.{name}/v1")

    def test_json_is_compact_so_one_value_is_one_line(self) -> None:
        for name, argv, _ in _JSON_COMMANDS:
            with self.subTest(command=name):
                result = self.invoke([*argv, "--format", "json"])
                self.assertEqual(
                    len(result.stdout.strip().splitlines()),
                    1,
                    f"{name} must print one compact JSON line",
                )

    def test_the_env_option_reaches_the_envelope(self) -> None:
        self._seed_stream_gauges("live")
        for name, argv, has_env_option in _JSON_COMMANDS:
            if not has_env_option:
                continue
            with self.subTest(command=name):
                result = self.invoke([*argv, "--env", "live", "--format", "json"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(json.loads(result.stdout)["env"], "live")

    def test_the_instance_variable_reaches_the_envelope(self) -> None:
        """The commands without `--env` still name the instance they read."""
        self._seed_stream_gauges("live")
        with mock.patch.dict("os.environ", {"ALPHALENS_BROKER_ENVIRONMENT": "live"}):
            for name, argv, _ in _JSON_COMMANDS:
                with self.subTest(command=name):
                    result = self.invoke([*argv, "--format", "json"])
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(json.loads(result.stdout)["env"], "live")

    def test_unknown_format_is_refused_everywhere(self) -> None:
        for name, argv, _ in _JSON_COMMANDS:
            with self.subTest(command=name):
                result = self.invoke([*argv, "--format", "xml"])
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("--format", result.stderr)


class ReconcileEmptyJournalTest(_BrokerCliCase):
    """The regression: an empty journal used to print prose on stdout."""

    def test_json_mode_answers_with_an_empty_verdict_envelope(self) -> None:
        result = self.invoke(["reconcile", "--format", "json"])
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["verdicts"], [])

    def test_human_mode_still_says_there_is_nothing_to_reconcile(self) -> None:
        result = self.invoke(["reconcile"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("nothing to reconcile", result.stdout)


class JsonAliasTest(_BrokerCliCase):
    """``--json`` stays a documented alias on the two commands that shipped it."""

    def test_the_alias_produces_the_same_payload_as_the_option(self) -> None:
        for name in ("reconcile", "reconcile-fills"):
            with self.subTest(command=name):
                alias = self.invoke([name, "--json"])
                option = self.invoke([name, "--format", "json"])
                self.assertEqual(alias.exit_code, 0, alias.output)
                self.assertEqual(json.loads(alias.stdout), json.loads(option.stdout))

    def test_a_conflicting_pair_is_refused_rather_than_resolved(self) -> None:
        for name in ("reconcile", "reconcile-fills"):
            with self.subTest(command=name):
                result = self.invoke([name, "--json", "--format", "human"])
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("--json", result.stderr)
                self.assertIn("--format", result.stderr)

    def test_the_alias_is_absent_from_the_commands_that_never_had_it(self) -> None:
        for name in ("orders", "picks", "watches", "status", "stream-status"):
            with self.subTest(command=name):
                result = self.invoke([name, "--json"])
                self.assertEqual(result.exit_code, 2, result.output)


class HelpMarkupModeTest(unittest.TestCase):
    """``--help`` drops the Rich frames when stdout is not a terminal (#1379).

    The frames cost a non-TTY reader roughly three quarters of the group help
    (measured through the installed binary, ``alphalens broker --help`` piped:
    6618 -> 1720 bytes), and Rich pads every line to the terminal width, so a
    wide ``COLUMNS`` makes it worse rather than better.
    """

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_the_mode_follows_the_terminal(self) -> None:
        from alphalens_cli.main import _help_markup_mode

        with mock.patch("sys.stdout.isatty", return_value=True):
            self.assertEqual(_help_markup_mode(), "rich")
        with mock.patch("sys.stdout.isatty", return_value=False):
            self.assertIsNone(_help_markup_mode())

    def test_a_stdout_without_a_terminal_renders_no_frames(self) -> None:
        self.assertNotIn("╭", self._help_with(isatty=False))

    def test_a_terminal_still_gets_the_rich_frames(self) -> None:
        self.assertIn("╭", self._help_with(isatty=True))

    def test_the_sub_group_inherits_rather_than_declaring_its_own_mode(self) -> None:
        """The rendered help above is a SUB-group's, and nothing was set on it.

        `broker_app` still carries Typer's own default, so the plain rendering
        can only have come from the root — which is what lets one assignment
        in `main.py` cover all twelve groups instead of twelve assignments
        drifting apart.
        """
        import typer
        from alphalens_cli.commands.broker import broker_app

        self.assertEqual(broker_app.rich_markup_mode, typer.Typer().rich_markup_mode)
        self.assertNotIn("╭", self._help_with(isatty=False))

    def _help_with(self, *, isatty: bool) -> str:
        from alphalens_cli import main as cli_main

        previous = cli_main.app.rich_markup_mode
        self.addCleanup(setattr, cli_main.app, "rich_markup_mode", previous)
        with mock.patch("sys.stdout.isatty", return_value=isatty):
            cli_main.app.rich_markup_mode = cli_main._help_markup_mode()
        return self.runner.invoke(cli_main.app, ["broker", "--help"]).output


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
