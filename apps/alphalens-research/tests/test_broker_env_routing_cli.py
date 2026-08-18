"""CLI tests for the env-aware one-off broker resolution (``_cli_broker``).

The one-off ``alphalens broker`` commands (account / positions / resolve /
submit / orders / reconcile / reconcile-fills / cancel) resolve their broker
per ``ALPHALENS_BROKER_ENVIRONMENT`` through the SAME seam the per-env
journal paths use (``state_paths.broker_environment``), instead of
unconditionally calling the SIM-only registry — pre-fix, ``reconcile`` under
``env=live`` read the live journal but asked the SIM gateway. Under ``sim``
(the default) the registry path is byte-identical to before; under ``live``
read commands (cancel included — risk-reducing) build the broker via the
ADR 0017 LIVE factory, and ad-hoc placement (``submit``, whose dry-run
preview still needs a broker) refuses loud BEFORE any broker construction:
the daemon is the only LIVE placement path.

Every helper-resolved command echoes one ``env=<env> gateway=<label>`` line
to STDERR (stdout carries the result only — CLI convention).

Hermetic: the LIVE factory is patched at its import seam
(``alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env``,
which returns a ``(broker, token_provider)`` TUPLE) — no real Saxo session is
ever constructed, no network.
"""

from __future__ import annotations

import unittest
from unittest import mock

from typer.testing import CliRunner

from tests.test_broker_cli import (
    _CLEAN_KINDS,
    _SUBMIT_ARGS,
    _CliFakeBroker,
    _ReconcileHarness,
    _SubmitHarness,
)

ENV_VAR = "ALPHALENS_BROKER_ENVIRONMENT"

# Source-module seams (the CLI lazy-imports inside command bodies, so patches
# at the source modules are picked up at call time — test_broker_cli pattern).
REGISTRY_SEAM = "alphalens_pipeline.brokers.registry.get_default_broker"
LIVE_FACTORY_SEAM = "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env"


class TestSimRouting(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_env_unset_uses_registry_and_never_calls_the_live_factory(self):
        fake = _CliFakeBroker()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch(REGISTRY_SEAM, return_value=fake) as registry_mock,
            mock.patch(LIVE_FACTORY_SEAM) as live_mock,
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["positions"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        registry_mock.assert_called_once()
        live_mock.assert_not_called()

    def test_explicit_sim_env_matches_the_default_path(self):
        fake = _CliFakeBroker()
        with (
            mock.patch.dict("os.environ", {ENV_VAR: "sim"}, clear=True),
            mock.patch(REGISTRY_SEAM, return_value=fake) as registry_mock,
            mock.patch(LIVE_FACTORY_SEAM) as live_mock,
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["positions"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        registry_mock.assert_called_once()
        live_mock.assert_not_called()

    def test_echo_line_goes_to_stderr_and_stdout_stays_clean(self):
        fake = _CliFakeBroker()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch(REGISTRY_SEAM, return_value=fake),
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["positions"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(result.stderr.splitlines()[0], "env=sim gateway=sim")
        self.assertNotIn("env=sim", result.stdout, "the echo line must never pollute stdout")
        self.assertIn("no open positions", result.stdout)

    def test_unknown_environment_value_fails_clean_without_any_broker(self):
        with (
            mock.patch.dict("os.environ", {ENV_VAR: "prod"}, clear=True),
            mock.patch(REGISTRY_SEAM) as registry_mock,
            mock.patch(LIVE_FACTORY_SEAM) as live_mock,
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["positions"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIsInstance(result.exception, SystemExit, "must refuse, never traceback")
        self.assertIn(ENV_VAR, result.stderr)
        registry_mock.assert_not_called()
        live_mock.assert_not_called()


class TestLiveReadRouting(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_live_read_command_uses_the_live_factory_not_the_registry(self):
        fake = _CliFakeBroker()
        with (
            mock.patch.dict("os.environ", {ENV_VAR: "live"}, clear=True),
            mock.patch(REGISTRY_SEAM) as registry_mock,
            mock.patch(LIVE_FACTORY_SEAM, return_value=(fake, mock.Mock())) as live_mock,
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["positions"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("no open positions", result.stdout)
        live_mock.assert_called_once()
        registry_mock.assert_not_called()
        self.assertIn("env=live gateway=live", result.stderr)

    def test_live_cancel_is_deliberately_allowed_and_routes_live(self):
        # cancel mutates broker state but is risk-reducing (same doctrine that
        # keeps it ungated by ALLOW_ORDERS) — the LIVE manual-flatten runbook
        # needs it, so it takes the LIVE factory branch, never a refusal.
        fake = _CliFakeBroker()
        with (
            mock.patch.dict("os.environ", {ENV_VAR: "live"}, clear=True),
            mock.patch(REGISTRY_SEAM) as registry_mock,
            mock.patch(LIVE_FACTORY_SEAM, return_value=(fake, mock.Mock())),
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["cancel", "E-9"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(fake.cancel_calls, ["E-9"])
        registry_mock.assert_not_called()

    def test_live_reconcile_reconciles_against_the_live_gateway(self):
        # THE pre-fix bug: env=live resolved the live journal paths but the
        # broker came from the SIM-only registry — live journal, SIM gateway.
        _ReconcileHarness(self, verdicts=list(_CLEAN_KINDS))
        live_broker = _CliFakeBroker()
        with (
            mock.patch.dict("os.environ", {ENV_VAR: "live"}, clear=True),
            mock.patch(REGISTRY_SEAM) as registry_mock,
            mock.patch(LIVE_FACTORY_SEAM, return_value=(live_broker, mock.Mock())) as live_mock,
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["reconcile"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        live_mock.assert_called_once()
        registry_mock.assert_not_called()
        self.assertIn("env=live gateway=live", result.stderr)

    def test_live_factory_keyerror_renders_a_clean_refusal(self):
        # Missing SAXO_LIVE_ACCOUNT_KEY raises a raw KeyError inside the
        # factory (not a BrokerError) — the helper must convert it to the
        # standard refusal, never a traceback.
        with (
            mock.patch.dict("os.environ", {ENV_VAR: "live"}, clear=True),
            mock.patch(REGISTRY_SEAM),
            mock.patch(LIVE_FACTORY_SEAM, side_effect=KeyError("SAXO_LIVE_ACCOUNT_KEY")),
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["positions"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIsInstance(result.exception, SystemExit, "must refuse, never traceback")
        self.assertIn("SAXO_LIVE_ACCOUNT_KEY", result.stderr)
        # The echo must never claim a gateway that was not built: the success
        # label is emitted only AFTER factory construction succeeds.
        self.assertIn("env=live gateway=refused", result.stderr)
        self.assertNotIn("gateway=live", result.stderr)

    def test_live_factory_blocked_grant_renders_a_clean_refusal(self):
        # SaxoLiveEnvironmentBlockedError subclasses SaxoError(RuntimeError),
        # NOT BrokerError — it would escape the commands' `except BrokerError`
        # unless the helper wraps the factory call.
        from alphalens_pipeline.brokers.saxo.errors import SaxoLiveEnvironmentBlockedError

        with (
            mock.patch.dict("os.environ", {ENV_VAR: "live"}, clear=True),
            mock.patch(REGISTRY_SEAM),
            mock.patch(
                LIVE_FACTORY_SEAM,
                side_effect=SaxoLiveEnvironmentBlockedError("standing grant mismatch"),
            ),
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, ["positions"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIsInstance(result.exception, SystemExit, "must refuse, never traceback")
        self.assertIn("standing grant mismatch", result.stderr)
        self.assertIn("env=live gateway=refused", result.stderr)
        self.assertNotIn("gateway=live", result.stderr)


class TestLiveSubmitRefusal(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _invoke_submit_live(self, extra_args: list[str]) -> tuple:
        harness = _SubmitHarness(self)
        with (
            mock.patch.dict("os.environ", {ENV_VAR: "live"}, clear=True),
            mock.patch(LIVE_FACTORY_SEAM) as live_mock,
        ):
            from alphalens_cli.commands.broker import broker_app

            result = self.runner.invoke(broker_app, [*_SUBMIT_ARGS, *extra_args])
        return harness, live_mock, result

    def test_live_submit_dry_run_refuses_before_any_broker_read(self):
        # There is NO broker-free preview: the dry-run still reads the account
        # and prechecks server-side, so the WHOLE command refuses under
        # env=live — before the account read, not only before placement.
        harness, live_mock, result = self._invoke_submit_live([])

        self.assertNotEqual(result.exit_code, 0)
        live_mock.assert_not_called()
        self.assertEqual(harness.broker.precheck_calls, [], "no broker read may happen")
        self.assertEqual(harness.broker.place_calls, [])
        self.assertIn("alphalens broker manage", result.stderr)
        self.assertIn("ADR 0017", result.stderr)

    def test_live_submit_execute_refuses_before_any_broker_read(self):
        harness, live_mock, result = self._invoke_submit_live(["--execute", "--yes"])

        self.assertNotEqual(result.exit_code, 0)
        live_mock.assert_not_called()
        self.assertEqual(harness.broker.precheck_calls, [], "no broker read may happen")
        self.assertEqual(harness.broker.place_calls, [], "nothing may be placed")
        self.assertEqual(harness.appended, [], "nothing may be journaled")
        self.assertIn("alphalens broker manage", result.stderr)


if __name__ == "__main__":
    unittest.main()
