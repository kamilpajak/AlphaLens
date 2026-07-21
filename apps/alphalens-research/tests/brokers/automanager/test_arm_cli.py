"""CLI tests for `alphalens broker arm`.

Validates (ticker, date) against the brief at arm time, then appends one
'armed' line. Loading is lazy-imported inside the command body, so patches
target the SOURCE modules.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.paper.brief_loader import CandidateBrief
from typer.testing import CliRunner

_BRIEF_DATE = dt.date(2026, 7, 20)


def _candidate(ticker: str = "KO") -> CandidateBrief:
    return CandidateBrief(
        brief_date=_BRIEF_DATE,
        ticker=ticker,
        theme="test-theme",
        verified=True,
        suggested_size_pct=3.0,
        trade_setup=None,
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
        arm.assert_called_once_with("KO", _BRIEF_DATE)
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


if __name__ == "__main__":
    unittest.main()
