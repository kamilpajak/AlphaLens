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

import json
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

    def test_re_arm_after_disarm_gets_the_next_generation(self) -> None:
        # #1371 — the 2026-09-08 shape: a pick armed in the wrong geometry,
        # disarmed, and armed again the same day must be a NEW pick (generation
        # 2), not a replacement the drain would never place.
        import datetime as dt

        from alphalens_cli.commands.broker import broker_app

        today = dt.datetime.now(dt.UTC).date().isoformat()
        first = self.runner.invoke(broker_app, _HAPPY_ARGS)
        self.assertEqual(first.exit_code, 0, first.output)
        self.assertNotIn("generation", first.output)
        disarm = self.runner.invoke(broker_app, ["disarm", "nvo", "--date", today])
        self.assertEqual(disarm.exit_code, 0, disarm.output)
        second = self.runner.invoke(broker_app, _HAPPY_ARGS)
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertIn("generation: 2", second.output)
        self.assertNotIn("REPLACE", second.output)
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        last = json.loads(picks.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(last["generation"], 2)
        self.assertEqual(last["intent"]["meta"]["generation"], 2)
        self.assertTrue(last["intent"]["intent_id"].endswith(":manual-g2"))

    def test_dry_run_shows_the_next_generation_without_spending_it(self) -> None:
        import datetime as dt

        from alphalens_cli.commands.broker import broker_app

        today = dt.datetime.now(dt.UTC).date().isoformat()
        self.assertEqual(self.runner.invoke(broker_app, _HAPPY_ARGS).exit_code, 0)
        self.assertEqual(
            self.runner.invoke(broker_app, ["disarm", "nvo", "--date", today]).exit_code, 0
        )
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        before = picks.read_text(encoding="utf-8")
        for _ in range(2):
            result = self.runner.invoke(broker_app, [*_HAPPY_ARGS, "--dry-run"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("generation: 2", result.output)
        self.assertEqual(picks.read_text(encoding="utf-8"), before)

    def test_re_arm_beside_a_still_armed_generation_is_refused(self) -> None:
        # Two live generations of one ticker would be two live picks on one
        # instrument (the live-long guard only defers after a FILL); the
        # operator must disarm first. Nothing is appended on the refusal.
        from alphalens_cli.commands.broker import broker_app

        self.assertEqual(self.runner.invoke(broker_app, _HAPPY_ARGS).exit_code, 0)
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        before = picks.read_text(encoding="utf-8")
        result = self.runner.invoke(broker_app, _HAPPY_ARGS)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("still armed", result.output)
        self.assertIn("alphalens broker disarm NVO", result.output)
        self.assertEqual(picks.read_text(encoding="utf-8"), before)

    def test_a_failed_append_reports_a_failure_object_not_a_traceback(self) -> None:
        """#1421, and the human branch matters here more than elsewhere: this is
        the one arming command that printed the compiled intent BEFORE the
        append, so a failure left the operator looking at a pick description
        with no "armed" line and a traceback under it."""
        import json as _j

        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.journal import JournalWriteError

        with mock.patch(
            "alphalens_pipeline.brokers.automanager.picks.arm_pick",
            side_effect=JournalWriteError("No space left on device"),
        ):
            result = self.runner.invoke(broker_app, [*_HAPPY_ARGS, "--format", "json"])

        self.assertEqual(result.exit_code, 7, result.output)
        self.assertEqual(result.stdout, "")
        failure = _j.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(failure["code"], "queue_write_failed")

    def test_the_human_echo_comes_after_the_append(self) -> None:
        """Nothing about the pick is printed when it was not queued."""
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.journal import JournalWriteError

        with mock.patch(
            "alphalens_pipeline.brokers.automanager.picks.arm_pick",
            side_effect=JournalWriteError("No space left on device"),
        ):
            result = self.runner.invoke(broker_app, _HAPPY_ARGS)

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")

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

    def test_now_tier_echo_marks_immediate(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        args = [
            "arm-manual",
            "rhi",
            "--tier",
            "now@43.00:40",
            "--tier",
            "41:60",
            "--stop",
            "39",
            "--tp",
            "2R:100",
            "--notional",
            "10000",
            "--frame",
            "15000",
            "--dry-run",
        ]
        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, args)

        self.assertEqual(result.exit_code, 0, result.output)
        arm.assert_not_called()
        self.assertIn("T1 now@43 (40%)", result.output)
        self.assertIn("T2 41 (60%)", result.output)
        self.assertIn("dry-run", result.output)

    def test_now_tier_arms_and_persists_entry_mode(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        args = [
            "arm-manual",
            "rhi",
            "--tier",
            "now@43.00:40",
            "--tier",
            "41:60",
            "--stop",
            "39",
            "--no-tp",
            "--notional",
            "10000",
            "--frame",
            "15000",
        ]
        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, args)

        self.assertEqual(result.exit_code, 0, result.output)
        (intent,), _kwargs = arm.call_args
        self.assertEqual(intent.spec.entry_tiers[0].entry_mode, "immediate")
        self.assertEqual(intent.spec.entry_tiers[1].entry_mode, "pullback")

    def test_invalid_levels_refuse_before_any_append(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        args = list(_HAPPY_ARGS)
        args[args.index("70.0:40")] = "70.0:30"  # tier sum 90 != 100
        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, args)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("sum to 100", result.output)
        arm.assert_not_called()

    def test_an_unsupported_venue_is_its_own_code_not_a_document_complaint(self) -> None:
        """#1406: the document is well formed — this deployment simply does not
        trade that venue. It used to report `intent_invalid` with EMPTY details,
        so a machine had nothing to branch on, and the raw-intent door would
        have had to answer differently for the identical refusal."""
        from alphalens_cli.commands.broker import broker_app

        args = [*_HAPPY_ARGS, "--mic", "XAMS", "--format", "json"]
        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm:
            result = self.runner.invoke(broker_app, args)

        self.assertNotEqual(result.exit_code, 0)
        failure = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(failure["code"], "venue_unsupported")
        self.assertEqual(failure["details"]["mic"], "XAMS")
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


class ArmManualEchoWarningsTest(unittest.TestCase):
    """Echo-completeness warnings (zen review, PR #1237): the operator must
    see an under-100 TP coverage and an about-to-be-replaced pending pick."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_under_100_tp_coverage_is_called_out(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        args = [
            "arm-manual", "nvo",
            "--tier", "72.5:60", "--tier", "70.0:40",
            "--stop", "66.0",
            "--tp", "80:40",  # 60% of the position keeps no TP target
            "--notional", "10000", "--frame", "15000",
        ]  # fmt: skip
        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick"):
            result = self.runner.invoke(broker_app, args)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("60", result.output)  # the uncovered remainder
        self.assertIn("no TP", result.output)

    def test_full_tp_coverage_emits_no_warning(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick"):
            result = self.runner.invoke(broker_app, _HAPPY_ARGS)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("uncovered", result.output)


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


class TheJsonFormEmitsTheArtefactNotADescriptionOfIt(unittest.TestCase):
    """`--format json` prints the intent a producer can feed back (#1389).

    `--dry-run` promised "compile and echo the intent" and printed a human
    summary, so nothing downstream could consume what it produced. The door in
    #1406 needs the emitted document to decode back into the same intent, so the
    property is proven HERE, where the document is produced.

    The contract is the document, so identity is asserted on the document.
    """

    def setUp(self) -> None:
        self.runner = CliRunner()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch("pathlib.Path.home", return_value=Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _emit(self, *extra: str) -> dict:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(
            broker_app,
            [
                "arm-manual",
                "NVO",
                "--tier",
                "100:60",
                "--tier",
                "98:40",
                "--stop",
                "90",
                "--tp",
                "110:100",
                "--size-pct",
                "3",
                "--format",
                "json",
                *extra,
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        return json.loads(result.stdout)

    def test_the_emitted_intent_decodes_back_into_the_same_document(self) -> None:
        from broker_contract.trade_intent.codec import intent_from_jsonable, intent_to_jsonable

        emitted = self._emit("--dry-run")["intent"]
        again = intent_to_jsonable(intent_from_jsonable(emitted))

        self.assertEqual(emitted, again)

    def test_the_envelope_names_the_pick_a_client_would_have_to_read_from_prose(self) -> None:
        """The generation (#1371) was only ever printed as prose."""
        payload = self._emit("--dry-run")

        self.assertEqual(payload["ticker"], "NVO")
        self.assertEqual(payload["generation"], 1)
        self.assertFalse(payload["armed"])
        self.assertTrue(payload["dry_run"])

    def test_a_dry_run_appends_nothing_even_in_json_mode(self) -> None:
        """The flag's promise, checked against the journal rather than the word."""
        payload = self._emit("--dry-run")

        self.assertFalse(Path(payload["picks_journal"]).exists())

    def test_a_real_arm_says_so_and_writes_the_queue(self) -> None:
        """Positive control for the test above: without it, a broken arm path
        would make the dry-run assertion pass for the wrong reason."""
        payload = self._emit()

        self.assertTrue(payload["armed"])
        self.assertFalse(payload["dry_run"])
        self.assertTrue(Path(payload["picks_journal"]).exists())


if __name__ == "__main__":
    unittest.main()
