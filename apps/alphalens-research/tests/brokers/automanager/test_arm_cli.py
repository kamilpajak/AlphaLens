"""CLI tests for `alphalens broker arm`.

PR-7 (broker-manager extraction memo section 5): arm now parses the brief's
trade_setup into a full TradeIntent CLIENT-SIDE (parse_brief_to_spec +
build_exit_geometry_spec) and persists it via arm_pick — the daemon never
touches a brief. Loading is lazy-imported inside the command body, so patches
target the SOURCE modules.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.paper.brief_loader import CandidateBrief
from typer.testing import CliRunner

_BRIEF_DATE = dt.date(2026, 7, 20)


def _plannable_trade_setup() -> dict:
    return {
        "schema_version": "1.0.0",
        "status": "OK",
        "asof_close": 100.0,
        "atr": 1.5,
        "disaster_stop": 90.0,
        "suggested_size_pct": 3.0,
        "entry_tiers": [
            {"limit": 100.0, "alloc_pct": 60.0, "tag": "T1"},
            {"limit": 98.0, "alloc_pct": 40.0, "tag": "T2"},
        ],
        "tp_tranches": [
            {"target": 110.0, "tranche_pct": 100.0, "r_multiple": 2.0, "tag": "TP1"},
        ],
    }


def _candidate(ticker: str = "KO", *, trade_setup: dict | None = "__default__") -> CandidateBrief:
    if trade_setup == "__default__":
        trade_setup = _plannable_trade_setup()
    return CandidateBrief(
        brief_date=_BRIEF_DATE,
        ticker=ticker,
        theme="test-theme",
        verified=True,
        suggested_size_pct=3.0,
        trade_setup=trade_setup,
        n_gates_passed=3,
        n_gates_failed=0,
        layer4_weighted_score=1.0,
        scorer_config_version="scorer-v1-test",
    )


class ArmCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_arm_valid_pick_appends_and_exits_zero(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO"), _candidate("MU")],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "ko", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 0, result.output)
        arm.assert_called_once()
        (intent,), _kwargs = arm.call_args
        self.assertEqual(intent.instrument.ticker, "KO")
        self.assertEqual(intent.meta.brief_date, "2026-07-20")
        self.assertEqual(intent.spec.disaster_stop, 90.0)
        self.assertEqual(len(intent.spec.entry_tiers), 2)
        self.assertIsNotNone(intent.exit)
        self.assertIn("armed KO", result.output)

    def test_arm_ticker_absent_from_brief_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief", return_value=[_candidate("KO")]
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "ZZZZ", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not in the 2026-07-20 brief", result.output)
        arm.assert_not_called()

    def test_arm_bad_date_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "not-a-date"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("invalid --date", result.output)

    def test_arm_missing_brief_parquet_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with mock.patch(
            "alphalens_pipeline.paper.brief_loader.load_brief",
            side_effect=FileNotFoundError("thematic brief parquet not found: /x.parquet"),
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not found", result.output)

    def test_arm_no_trade_setup_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO", trade_setup=None)],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no plannable trade_setup", result.output)
        arm.assert_not_called()

    def test_arm_unplannable_trade_setup_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        bad_setup = _plannable_trade_setup()
        bad_setup["status"] = "NO_STRUCTURE"
        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO", trade_setup=bad_setup)],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        arm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
