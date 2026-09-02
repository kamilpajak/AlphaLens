"""CLI tests for `alphalens broker arm-manual` (#1235).

A thin shell over ``manual_intent.build_manual_intent``: the operator provides
the levels agreed off-brief (the WhatsApp flow), the command compiles them
into a full manual :class:`TradeIntent`, echoes the compiled result for
verification, and appends it to the selected instance inbox via ``arm_pick``
— the same seam `broker arm` uses. ``--dry-run`` does everything except the
append. Level/sizing rules live in ``test_manual_intent.py``; here we pin the
CLI wiring only: option plumbing, the echo, the env-frame fallback, the
``--env`` inbox seam and the legacy-layout guard.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from typer.testing import CliRunner

_HAPPY_ARGS = [
    "arm-manual",
    "nvo",
    "--tier",
    "72.5:60",
    "--tier",
    "70.0:40",
    "--stop",
    "66.0",
    "--tp",
    "80:50",
    "--tp",
    "2R:50",
    "--notional",
    "10000",
    "--frame",
    "15000",
]


def _isolate_home(case: unittest.TestCase) -> Path:
    tmp = TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()
    case.addCleanup(patcher.stop)
    return home


class ArmManualCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_happy_path_appends_compiled_intent(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, _HAPPY_ARGS)

        self.assertEqual(result.exit_code, 0, result.output)
        arm.assert_called_once()
        (intent,), _kwargs = arm.call_args
        self.assertEqual(intent.instrument.ticker, "NVO")
        self.assertEqual(intent.instrument.mic, "XNYS")
        self.assertEqual(intent.meta.source, "manual")
        self.assertTrue(intent.intent_id.endswith(":manual"))
        self.assertIsNone(intent.exit)
        self.assertEqual(intent.spec.disaster_stop, 66.0)
        # blend = 72.5*0.6 + 70*0.4 = 71.5; 2R above blend with R=5.5 => 82.5
        self.assertAlmostEqual(intent.spec.tp_tranches[1].price, 82.5)
        self.assertAlmostEqual(intent.spec.suggested_size_pct, 100.0 * 10000 / 15000)

    def test_echo_shows_compiled_levels(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick"):
            result = self.runner.invoke(broker_app, _HAPPY_ARGS)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("71.5", result.output)  # planned blend
        self.assertIn("82.5", result.output)  # computed 2R target
        self.assertIn("66.67", result.output)  # resolved size_pct
        self.assertIn("manual", result.output)

    def test_dry_run_compiles_but_never_appends(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, [*_HAPPY_ARGS, "--dry-run"])

        self.assertEqual(result.exit_code, 0, result.output)
        arm.assert_not_called()
        self.assertIn("dry-run", result.output)
        self.assertIn("71.5", result.output)

    def test_invalid_levels_refuse_before_any_append(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        args = list(_HAPPY_ARGS)
        args[args.index("70.0:40")] = "70.0:30"  # tier sum 90 != 100
        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, args)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("sum to 100", result.output)
        arm.assert_not_called()

    def test_default_env_targets_sim_inbox(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager import state_paths

        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, _HAPPY_ARGS)

        self.assertEqual(result.exit_code, 0, result.output)
        _args, kwargs = arm.call_args
        self.assertEqual(kwargs["path"], state_paths.picks_path(env="sim"))

    def test_env_live_targets_live_inbox(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager import state_paths

        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, [*_HAPPY_ARGS, "--env", "live"])

        self.assertEqual(result.exit_code, 0, result.output)
        _args, kwargs = arm.call_args
        self.assertEqual(kwargs["path"], state_paths.picks_path(env="live"))


class ArmManualFrameFallbackTest(unittest.TestCase):
    """``--notional`` without ``--frame`` falls back to the declared-frame env
    (``live_rails.SIZING_EQUITY_ENV``); no env either → loud refusal."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)
        self.args = [a for a in _HAPPY_ARGS if a not in ("--frame", "15000")]

    def test_frame_falls_back_to_sizing_equity_env(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager.live_rails import SIZING_EQUITY_ENV

        with (
            mock.patch.dict(os.environ, {SIZING_EQUITY_ENV: "15000"}),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, self.args)

        self.assertEqual(result.exit_code, 0, result.output)
        (intent,), _kwargs = arm.call_args
        self.assertAlmostEqual(intent.spec.suggested_size_pct, 100.0 * 10000 / 15000)

    def test_no_frame_anywhere_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager.live_rails import SIZING_EQUITY_ENV

        env_without_frame = {k: v for k, v in os.environ.items() if k != SIZING_EQUITY_ENV}
        with (
            mock.patch.dict(os.environ, env_without_frame, clear=True),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, self.args)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("frame", result.output)
        arm.assert_not_called()

    def test_malformed_env_frame_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager.live_rails import SIZING_EQUITY_ENV

        with (
            mock.patch.dict(os.environ, {SIZING_EQUITY_ENV: "not-a-number"}),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, self.args)

        self.assertEqual(result.exit_code, 1)
        self.assertIn(SIZING_EQUITY_ENV, result.output)
        arm.assert_not_called()


class ArmManualLegacyLayoutGuardTest(unittest.TestCase):
    """arm-manual runs the same ADR 0016 D4 legacy-layout guard as arm."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_legacy_layout_refuses_before_persisting(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        legacy_dir = self.home / ".alphalens" / "broker_orders"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "submissions.jsonl").write_text("", encoding="utf-8")

        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, _HAPPY_ARGS)

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("legacy flat broker state", result.output)
        arm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
