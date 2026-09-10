"""CLI tests for ``--env`` on the one-off broker READ commands (#1377).

``--env live`` composes the process environment from the installed systemd
unit so a LIVE read works in a plain shell (no ``set -a; . /etc/alphalens/env``,
no ``env $(systemctl … -p Environment --value)``). The rules under test:

* no ``--env`` -> today's behaviour, and NOTHING is composed (this is the path
  the 15-minute edgar-detect cron takes through the same binary);
* ``--env sim`` -> sets the instance only, never shells out to systemctl (a
  developer Mac has no user manager);
* ``--env live`` -> composes unit ``Environment=`` + the unit's
  ``EnvironmentFile=``, the composed values WIN over the caller's shell, and
  ``ALPHALENS_BROKER_ENVIRONMENT`` / ``ALPHALENS_BROKER_ALLOW_ORDERS`` are
  forced LAST so neither source can steer the instance or arm the process.

The seams patched here are ``unit_env._systemctl_show`` / ``unit_env._read_text``
— NOT ``compose_live_environment`` itself: patching the whole composition would
let a broken parser or a wrong precedence pass every test below.
"""

from __future__ import annotations

import unittest
from unittest import mock

from typer.testing import CliRunner

from tests.brokers.automanager.test_arm_cli import _isolate_home
from tests.test_broker_cli import _CliFakeBroker

ENV_VAR = "ALPHALENS_BROKER_ENVIRONMENT"
ALLOW_ORDERS = "ALPHALENS_BROKER_ALLOW_ORDERS"
LIVE_FACTORY_SEAM = "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env"
REGISTRY_SEAM = "alphalens_pipeline.brokers.registry.get_default_broker"
SHOW_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._systemctl_show"
READ_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._read_text"

_SECRET = "sentinel-secret-value"
# The production shapes measured on the VPS 2026-09-08: ALLOW_ORDERS=1 comes
# from the 10-allow-orders drop-in, so composing gives an ARMED environment.
_RAILS = {
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
_UNIT_PAYLOAD = " ".join(
    [
        f"{ENV_VAR}=live",
        f"{ALLOW_ORDERS}=1",
        *(f"{key}={value}" for key, value in _RAILS.items()),
        "ALPHALENS_SAXO_LIVE_STANDING=ACCT-1",
        "SAXO_LIVE_ACCOUNT_KEY=ACCT-1",
    ]
)
_ENV_FILE = f"SAXO_LIVE_APP_KEY={_SECRET}\nSAXO_LIVE_APP_SECRET=s\n"

_READ_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("account",),
    ("positions",),
    ("orders",),
    ("reconcile",),
    ("cancel", "O-1"),
)


def _show(payload: str = _UNIT_PAYLOAD, *, needs_reload: str = "no"):
    answers = {
        "LoadState": "loaded",
        "Environment": payload,
        "EnvironmentFiles": "/etc/alphalens/env (ignore_errors=no)",
        "DropInPaths": "/h/10-allow-orders.conf",
        "NeedDaemonReload": needs_reload,
    }
    return lambda unit, prop: answers[prop]


class EnvOptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        # `reconcile` runs the ADR 0016 legacy-layout guard, which reads the
        # REAL home tree unless it is isolated (a developer machine running the
        # SIM daemon genuinely holds a pre-migration flat layout).
        _isolate_home(self)
        patcher = mock.patch.dict("os.environ", {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _invoke(self, argv: list[str], *, env_file: str = _ENV_FILE, payload: str = _UNIT_PAYLOAD):
        """Run a command with a live broker + composition seams in place."""
        from alphalens_cli.commands.broker import broker_app

        self.fake = _CliFakeBroker()
        with (
            mock.patch(SHOW_SEAM, _show(payload)),
            mock.patch(READ_SEAM, lambda _path: env_file),
            mock.patch(LIVE_FACTORY_SEAM, return_value=(self.fake, mock.Mock())),
            mock.patch(REGISTRY_SEAM, return_value=self.fake),
        ):
            return self.runner.invoke(broker_app, argv)

    # --- the option targets the instance -----------------------------------

    def test_env_live_routes_every_read_command_to_the_live_instance(self) -> None:
        for argv in _READ_COMMANDS:
            with self.subTest(command=argv[0]):
                result = self._invoke([*argv, "--env", "live"])
                self.assertEqual(result.exit_code, 0, result.output)
                # `reconcile` over an empty journal reports the instance and
                # resolves no gateway by design, so the instance line is the
                # common evidence; the gateway label is asserted below for the
                # four commands that do resolve a broker.
                self.assertIn("env=live", result.stderr)

    def test_env_live_builds_the_live_gateway_for_the_broker_reads(self) -> None:
        for argv in (("account",), ("positions",), ("orders",), ("cancel", "O-1")):
            with self.subTest(command=argv[0]):
                result = self._invoke([*argv, "--env", "live"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn("env=live gateway=live", result.stderr)

    def test_env_live_makes_cli_broker_see_the_live_instance(self) -> None:
        seen: dict[str, str] = {}

        def factory():
            import os

            seen["env"] = os.environ[ENV_VAR]
            seen["allow"] = os.environ[ALLOW_ORDERS]
            return (_CliFakeBroker(), mock.Mock())

        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(SHOW_SEAM, _show()),
            mock.patch(READ_SEAM, lambda _path: _ENV_FILE),
            mock.patch(LIVE_FACTORY_SEAM, side_effect=factory),
        ):
            result = self.runner.invoke(broker_app, ["positions", "--env", "live"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, {"env": "live", "allow": "0"})

    def test_env_sim_returns_to_the_registry_in_the_same_process(self) -> None:
        result = self._invoke(["orders", "--env", "sim"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("env=sim gateway=sim", result.stderr)

    # --- composition content ------------------------------------------------

    def test_all_nine_rails_land_with_their_exact_composed_values(self) -> None:
        import os

        captured: dict[str, str] = {}

        def factory():
            captured.update({k: os.environ.get(k, "") for k in _RAILS})
            return (_CliFakeBroker(), mock.Mock())

        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(SHOW_SEAM, _show()),
            mock.patch(READ_SEAM, lambda _path: _ENV_FILE),
            mock.patch(LIVE_FACTORY_SEAM, side_effect=factory),
        ):
            self.runner.invoke(broker_app, ["account", "--env", "live"])
        self.assertEqual(captured, _RAILS)

    def test_the_credentials_come_from_the_environment_file(self) -> None:
        import os

        captured: dict[str, str] = {}

        def factory():
            captured["key"] = os.environ.get("SAXO_LIVE_APP_KEY", "")
            return (_CliFakeBroker(), mock.Mock())

        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(SHOW_SEAM, _show()),
            mock.patch(READ_SEAM, lambda _path: _ENV_FILE),
            mock.patch(LIVE_FACTORY_SEAM, side_effect=factory),
        ):
            self.runner.invoke(broker_app, ["account", "--env", "live"])
        self.assertEqual(captured["key"], _SECRET)

    def test_composed_values_win_over_the_callers_shell(self) -> None:
        import os

        captured: dict[str, str] = {}

        def factory():
            captured["max_open"] = os.environ["ALPHALENS_BROKER_MAX_OPEN"]
            return (_CliFakeBroker(), mock.Mock())

        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch.dict("os.environ", {"ALPHALENS_BROKER_MAX_OPEN": "999"}),
            mock.patch(SHOW_SEAM, _show()),
            mock.patch(READ_SEAM, lambda _path: _ENV_FILE),
            mock.patch(LIVE_FACTORY_SEAM, side_effect=factory),
        ):
            self.runner.invoke(broker_app, ["account", "--env", "live"])
        self.assertEqual(captured["max_open"], "10")

    def test_the_option_beats_an_instance_pin_hiding_in_the_environment_file(self) -> None:
        # The catastrophic ordering bug: EnvironmentFile= wins over
        # Environment= in systemd, so a file that pinned the instance would
        # silently turn `--env live` into a SIM read. The option is applied LAST.
        result = self._invoke(["orders", "--env", "live"], env_file=f"{ENV_VAR}=sim\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("env=live gateway=live", result.stderr)

    def test_allow_orders_is_forced_to_zero_even_though_the_unit_arms_it(self) -> None:
        import os

        captured: dict[str, str] = {}

        def factory():
            captured["allow"] = os.environ[ALLOW_ORDERS]
            return (_CliFakeBroker(), mock.Mock())

        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(SHOW_SEAM, _show()),
            mock.patch(READ_SEAM, lambda _path: _ENV_FILE),
            mock.patch(LIVE_FACTORY_SEAM, side_effect=factory),
        ):
            self.runner.invoke(broker_app, ["cancel", "O-1", "--env", "live"])
        self.assertEqual(captured["allow"], "0")

    # --- what the operator is told -----------------------------------------

    def test_stderr_names_the_sources_without_any_value(self) -> None:
        result = self._invoke(["orders", "--env", "live"])
        self.assertIn("composed unit=alphalens-broker-manager-live.service", result.stderr)
        self.assertIn("dropins=1", result.stderr)
        self.assertIn("env-file=/etc/alphalens/env", result.stderr)
        self.assertIn("keys=", result.stderr)

    def test_no_composed_value_ever_reaches_stdout_or_stderr(self) -> None:
        result = self._invoke(["account", "--env", "live"])
        self.assertNotIn(_SECRET, result.stdout)
        self.assertNotIn(_SECRET, result.stderr)

    def test_a_banned_key_in_the_file_warns_on_stderr(self) -> None:
        result = self._invoke(
            ["orders", "--env", "live"],
            env_file=f"{_ENV_FILE}ALPHALENS_BROKER_MAX_OPEN=99\n",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ALPHALENS_BROKER_MAX_OPEN", result.stderr)
        self.assertNotIn("99", result.stderr)

    def test_a_pending_daemon_reload_warns_on_stderr(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(SHOW_SEAM, _show(needs_reload="yes")),
            mock.patch(READ_SEAM, lambda _path: _ENV_FILE),
            mock.patch(LIVE_FACTORY_SEAM, return_value=(_CliFakeBroker(), mock.Mock())),
        ):
            result = self.runner.invoke(broker_app, ["orders", "--env", "live"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("daemon-reload", result.stderr)

    # --- nothing is composed unless it is asked for -------------------------

    def test_env_sim_never_shells_out(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        def explode(*_args, **_kwargs):
            raise AssertionError("systemctl must not be called for --env sim")

        with (
            mock.patch(SHOW_SEAM, explode),
            mock.patch(READ_SEAM, explode),
            mock.patch(REGISTRY_SEAM, return_value=_CliFakeBroker()),
        ):
            result = self.runner.invoke(broker_app, ["orders", "--env", "sim"])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_without_the_option_nothing_is_composed(self) -> None:
        # The cron path: `alphalens` runs every 15 minutes for edgar-detect and
        # must never read the shared credentials file.
        from alphalens_cli.commands.broker import broker_app

        def explode(*_args, **_kwargs):
            raise AssertionError("composition must not run without --env")

        for argv in _READ_COMMANDS:
            with self.subTest(command=argv[0]):
                with (
                    mock.patch(SHOW_SEAM, explode),
                    mock.patch(READ_SEAM, explode),
                    mock.patch(REGISTRY_SEAM, return_value=_CliFakeBroker()),
                ):
                    result = self.runner.invoke(broker_app, list(argv))
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn("env=sim", result.stderr)

    def test_without_the_option_the_environment_variable_still_rules(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch.dict("os.environ", {ENV_VAR: "live"}),
            mock.patch(SHOW_SEAM, lambda *_a: (_ for _ in ()).throw(AssertionError("no compose"))),
            mock.patch(LIVE_FACTORY_SEAM, return_value=(_CliFakeBroker(), mock.Mock())),
        ):
            result = self.runner.invoke(broker_app, ["orders"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("env=live gateway=live", result.stderr)

    # --- refusals ----------------------------------------------------------

    def test_an_unknown_env_refuses_with_an_empty_stdout(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["orders", "--env", "bogus"])
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("bogus", result.stderr)

    def test_a_composition_failure_refuses_and_names_the_manual_recipe(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        def missing(*_args, **_kwargs):
            raise FileNotFoundError("systemctl")

        with mock.patch(SHOW_SEAM, missing):
            result = self.runner.invoke(broker_app, ["positions", "--env", "live"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("/etc/alphalens/env", result.stderr)
        self.assertIn("systemctl", result.stderr)


class MutatingCommandAmbientEnvTest(unittest.TestCase):
    """A write must never be steered by an ambient variable (#1377 review).

    ``picks`` and ``watches`` follow ``ALPHALENS_BROKER_ENVIRONMENT``; the
    arming commands keep defaulting to ``sim``. In a shell that exports
    ``live`` those two rules disagree, and the loser used to be the operator:
    ``arm`` wrote to SIM while ``picks`` showed LIVE, so the pick "vanished".
    The arming commands now refuse instead of choosing.
    """

    def setUp(self) -> None:
        self.runner = CliRunner()
        _isolate_home(self)

    def _invoke(self, argv: list[str], env: dict[str, str]):
        from alphalens_cli.commands.broker import broker_app

        with mock.patch.dict("os.environ", env, clear=True):
            return self.runner.invoke(broker_app, argv)

    def test_disarm_refuses_in_a_live_shell_without_an_explicit_env(self) -> None:
        result = self._invoke(["disarm", "KO", "--date", "2026-09-08"], {ENV_VAR: "live"})
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("--env", result.stderr)
        self.assertIn("live", result.stderr)

    def test_arm_refuses_in_a_live_shell_without_an_explicit_env(self) -> None:
        result = self._invoke(["arm", "KO", "--date", "2026-09-08"], {ENV_VAR: "live"})
        self.assertEqual(result.exit_code, 1)
        self.assertIn("--env", result.stderr)

    def test_arm_manual_refuses_in_a_live_shell_without_an_explicit_env(self) -> None:
        result = self._invoke(
            ["arm-manual", "KO", "--tier", "11", "--stop", "10", "--notional", "100"],
            {ENV_VAR: "live"},
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("--env", result.stderr)

    def test_a_sim_shell_is_no_disagreement(self) -> None:
        # Same instance on both sides: the command proceeds and fails later on
        # its own terms (no pick for that date), never on the env check.
        result = self._invoke(["disarm", "KO", "--date", "2026-09-08"], {ENV_VAR: "sim"})
        self.assertNotIn("--env", result.stderr)


if __name__ == "__main__":
    unittest.main()
