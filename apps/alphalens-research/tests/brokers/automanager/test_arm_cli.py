"""CLI tests for `alphalens broker arm`.

PR-7 (broker-manager extraction memo section 5): arm now parses the brief's
trade_setup into a full TradeIntent CLIENT-SIDE (parse_brief_to_spec +
build_exit_geometry_spec) and persists it via arm_pick — the daemon never
touches a brief. Loading is lazy-imported inside the command body, so patches
target the SOURCE modules.

Pure executor (2026-08-03): arm carries NO selection / filtering logic — it
arms exactly the named ticker after only the structural checks (brief present,
ticker present, trade_setup plannable). The old arm-time earnings-window gate
was removed: selection-policy filters (earnings-window avoidance included)
belong at brief-creation (the selection tier), so a filtered-out candidate
never reaches the brief in the first place. The client invoking arm is
responsible for knowing what it arms; the command never second-guesses it.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
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

    def test_arm_has_no_earnings_or_selection_filtering_logic(self) -> None:
        # arm is a pure executor: after the structural checks it arms the named
        # ticker, full stop. It must consult NO selection-policy filter — the old
        # arm-time earnings-window gate was removed (that policy moves to
        # brief-creation). Walk the AST and collect every identifier / import —
        # docstrings are Constant nodes, never collected, so a doc note about the
        # removed gate is ignored while any real earnings identifier (a param, a
        # lookup call, an import) is caught. Structural refusals (brief/ticker
        # absent, unplannable setup) are fine — those are not selection policy.
        from alphalens_cli.commands import broker

        tree = ast.parse(inspect.getsource(broker.arm_command))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, ast.arg):
                names.add(node.arg.lower())
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.name.lower())
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.lower())
        earnings_refs = sorted(n for n in names if "earnings" in n)
        self.assertEqual(
            earnings_refs,
            [],
            f"arm_command must carry no earnings/selection filter; found {earnings_refs}",
        )

    def test_arm_arms_even_with_imminent_earnings_no_lookup(self) -> None:
        # The behavioural counterpart: a candidate the OLD gate would have
        # refused (earnings imminent) now arms — because arm never looks earnings
        # up. No earnings mock is needed precisely because no lookup happens
        # (hermetic: zero network).
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO")],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 0, result.output)
        arm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
