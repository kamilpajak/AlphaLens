"""CLI surface tests for `alphalens paper-trade`.

Each command body is heavy (iVolatility API calls, Carhart factor
loads, full scorer pipelines). These tests do **not** exercise the
end-to-end pipeline — that is covered by unit tests of the underlying
modules (test_paper_trade_scorer_v9d, test_paper_trade_verdict, etc.).
What we cover here is the **CLI dispatch layer**:

- ``--strategy`` is required and surfaces a clear error if missing.
- Unknown strategies are rejected at the registry resolver layer.
- Path / callable resolution from REGISTRY runs without ImportError.
- The verdict command short-circuits on an empty ledger (the only
  fully self-contained command path that does not need external APIs).
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from typer.testing import CliRunner

# Strip ANSI escape sequences from typer/click output before substring
# assertions. Rich-mode renders option names with color codes interleaved
# (e.g. ``--strategy`` becomes ``\x1b[36m-\x1b[0m\x1b[36m-strategy\x1b[0m``),
# which breaks naive ``assertIn("--strategy", out)``. Local terminals
# may not exhibit this depending on TERM / NO_COLOR; CI does.
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _plain(s: str) -> str:
    return _ANSI.sub("", s)


class TestVerdictCommand(unittest.TestCase):
    """`paper-trade verdict` is the simplest path — no external APIs."""

    def setUp(self):
        self.runner = CliRunner()

    def test_missing_strategy_arg_fails(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app

        result = self.runner.invoke(paper_trade_app, ["verdict"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--strategy", _plain(result.output + (result.stderr or "")))

    def test_unknown_strategy_raises(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app

        result = self.runner.invoke(paper_trade_app, ["verdict", "--strategy", "nonexistent"])
        self.assertNotEqual(result.exit_code, 0)
        # The KeyError from get_strategy bubbles up; runner captures it.
        self.assertIsNotNone(result.exception)

    def test_v9d_empty_ledger_short_circuits(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app

        with tempfile.TemporaryDirectory() as td:
            empty_path = Path(td) / "empty_ledger.parquet"
            # Patch default_ledger_path used inside the command body so
            # the empty parquet is loaded instead of the real one.
            with (
                patch(
                    "alphalens_research.paper_trade.ledger.default_ledger_path",
                    return_value=empty_path,
                ),
                patch(
                    "alphalens_research.paper_trade.verdict.default_verdict_path",
                    return_value=Path(td) / "verdict.md",
                ),
            ):
                result = self.runner.invoke(paper_trade_app, ["verdict", "--strategy", "v9d"])

        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        self.assertIn("Ledger empty", result.stdout)

    def test_v9d_populated_ledger_writes_verdict_markdown(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app
        from alphalens_research.paper_trade.ledger import LEDGER_COLUMNS
        from alphalens_research.paper_trade.verdict import DecisionRuleResult

        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / "ledger.parquet"
            verdict_path = Path(td) / "verdict.md"
            # Single row is enough for the command's branch coverage —
            # we mock evaluate_decision_rule so we do not need a real
            # Carhart-aligned series.
            row = dict.fromkeys(LEDGER_COLUMNS)
            row.update(
                {
                    "asof": "2026-01-02",
                    "rebalance_n": 1,
                    "n_held": 5,
                    "holdings": ["AAA"],
                    "prior_holdings": [],
                    "realized_return_long_gross": 0.01,
                    "realized_return_long_net": 0.009,
                    "benchmark_return_mdy": 0.005,
                    "cost_drag_bps": 30.0,
                    "universe_size": 100,
                }
            )
            pd.DataFrame([row]).to_parquet(ledger_path, index=False)

            fake_result = DecisionRuleResult(
                n_obs=1,
                checkpoint="pre-26w",
                cumulative_alpha_t=float("nan"),
                cumulative_alpha_annualized=float("nan"),
                cumulative_sharpe_net=float("nan"),
                cumulative_max_drawdown=float("nan"),
                sub_period_alpha_ts=[],
                verdict="PENDING",
                rationale="Stub — n=1 below 26w checkpoint",
            )

            with (
                patch(
                    "alphalens_research.paper_trade.ledger.default_ledger_path",
                    return_value=ledger_path,
                ),
                patch(
                    "alphalens_research.paper_trade.verdict.default_verdict_path",
                    return_value=verdict_path,
                ),
                patch(
                    "alphalens_research.paper_trade.verdict.evaluate_decision_rule",
                    return_value=fake_result,
                ),
            ):
                result = self.runner.invoke(paper_trade_app, ["verdict", "--strategy", "v9d"])

            self.assertEqual(result.exit_code, 0, msg=result.stdout)
            self.assertTrue(verdict_path.exists())
            content = verdict_path.read_text()
            self.assertIn("PENDING", content)
            self.assertIn("v9d paper-trade verdict", content)

    def test_v9d_with_sub_period_alpha_ts_renders_branch(self):
        # Covers the optional sub_period_alpha_ts markdown branch which
        # only fires when DecisionRuleResult populates it (52w checkpoint).
        from alphalens_cli.commands.paper_trade import paper_trade_app
        from alphalens_research.paper_trade.ledger import LEDGER_COLUMNS
        from alphalens_research.paper_trade.verdict import DecisionRuleResult

        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / "ledger.parquet"
            verdict_path = Path(td) / "out.md"
            row = dict.fromkeys(LEDGER_COLUMNS)
            row.update(
                {
                    "asof": "2027-05-01",
                    "rebalance_n": 52,
                    "n_held": 5,
                    "holdings": ["AAA"],
                    "prior_holdings": [],
                    "realized_return_long_gross": 0.01,
                    "realized_return_long_net": 0.009,
                    "benchmark_return_mdy": 0.005,
                    "cost_drag_bps": 30.0,
                    "universe_size": 100,
                }
            )
            pd.DataFrame([row]).to_parquet(ledger_path, index=False)

            fake_result = DecisionRuleResult(
                n_obs=52,
                checkpoint="52w",
                cumulative_alpha_t=2.10,
                cumulative_alpha_annualized=0.083,
                cumulative_sharpe_net=0.45,
                cumulative_max_drawdown=-0.12,
                sub_period_alpha_ts=[1.2, 0.8, 1.5, 0.9],
                verdict="PASS_52W",
                rationale="All sub-periods above floor",
            )

            with (
                patch(
                    "alphalens_research.paper_trade.ledger.default_ledger_path",
                    return_value=ledger_path,
                ),
                patch(
                    "alphalens_research.paper_trade.verdict.evaluate_decision_rule",
                    return_value=fake_result,
                ),
            ):
                result = self.runner.invoke(
                    paper_trade_app,
                    ["verdict", "--strategy", "v9d", "--out", str(verdict_path)],
                )

            self.assertEqual(result.exit_code, 0, msg=result.stdout)
            content = verdict_path.read_text()
            self.assertIn("sub-period αts", content)
            self.assertIn("PASS_52W", content)


class TestScoreCommand(unittest.TestCase):
    """``paper-trade score`` happy + dispatch paths.

    The full scoring pipeline (iVolatility SMD load + Carhart factors +
    cross-sectional residual fit) is covered by ``test_paper_trade_*``
    unit tests. These tests cover the **CLI dispatch and orchestration**
    layer with the underlying pipeline mocked.
    """

    def setUp(self):
        self.runner = CliRunner()

    def test_missing_strategy_arg_fails(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app

        result = self.runner.invoke(paper_trade_app, ["score"])
        self.assertEqual(result.exit_code, 2)

    def test_unknown_strategy_raises_at_registry_lookup(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app

        result = self.runner.invoke(paper_trade_app, ["score", "--strategy", "nonexistent"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIsNotNone(result.exception)

    def test_first_ever_score_writes_state_skips_ledger(self):
        # First-ever invocation: prior_state is empty so no ledger entry
        # is appended (no prior week to mark) and only state is saved.
        from datetime import date

        from alphalens_cli.commands.paper_trade import paper_trade_app
        from alphalens_research.paper_trade.scorer_v9d import ScoringResult

        scoring = ScoringResult(
            asof=date(2026, 5, 4),
            universe_size=100,
            n_scored=80,
            coverage_pct=0.80,
            top_decile_tickers=["AAA", "BBB", "CCC"],
            top_decile_scores={"AAA": 0.9, "BBB": 0.8, "CCC": 0.7},
            decile_size=3,
        )

        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / "ledger.parquet"
            state_path = Path(td) / "state.yaml"

            with (
                patch(
                    "alphalens_research.paper_trade.ledger.default_ledger_path",
                    return_value=ledger_path,
                ),
                patch(
                    "alphalens_research.paper_trade.state.default_state_path",
                    return_value=state_path,
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.make_smd_loader",
                    return_value=object(),
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.latest_trading_asof",
                    return_value=date(2026, 5, 4),
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.pit_union",
                    return_value=["AAA", "BBB", "CCC", "DDD"],
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.score_top_decile",
                    return_value=scoring,
                ),
            ):
                result = self.runner.invoke(paper_trade_app, ["score", "--strategy", "v9d"])

            self.assertEqual(result.exit_code, 0, msg=result.stdout)
            self.assertIn("First-ever score", result.stdout)
            self.assertTrue(state_path.exists())
            # No ledger entry on first-ever run.
            self.assertFalse(ledger_path.exists())

    def test_subsequent_score_appends_ledger_and_updates_state(self):
        # Second invocation: prior_state has held + as_of, so the command
        # computes realized P&L, appends a ledger entry, then refreshes
        # state with the new top decile.
        from datetime import date

        from alphalens_cli.commands.paper_trade import paper_trade_app
        from alphalens_research.paper_trade.scorer_v9d import ScoringResult
        from alphalens_research.paper_trade.state import PaperTradeState

        scoring = ScoringResult(
            asof=date(2026, 5, 11),
            universe_size=100,
            n_scored=85,
            coverage_pct=0.85,
            top_decile_tickers=["BBB", "CCC", "DDD"],
            top_decile_scores={"BBB": 0.9, "CCC": 0.8, "DDD": 0.7},
            decile_size=3,
        )

        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / "ledger.parquet"
            state_path = Path(td) / "state.yaml"
            # Seed prior state so the command takes the "have prior" branch.
            PaperTradeState(
                held=["AAA", "BBB"],
                scores={"AAA": 0.95, "BBB": 0.85},
                as_of=date(2026, 5, 4),
                rebalance_n=1,
            ).save(state_path)

            with (
                patch(
                    "alphalens_research.paper_trade.ledger.default_ledger_path",
                    return_value=ledger_path,
                ),
                patch(
                    "alphalens_research.paper_trade.state.default_state_path",
                    return_value=state_path,
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.make_smd_loader",
                    return_value=object(),
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.latest_trading_asof",
                    return_value=date(2026, 5, 11),
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.pit_union",
                    return_value=["AAA", "BBB", "CCC", "DDD", "EEE"],
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.score_top_decile",
                    return_value=scoring,
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.compute_realized_return",
                    return_value=(0.012, 2),
                ),
                patch(
                    "alphalens_research.paper_trade.scorer_v9d.benchmark_return",
                    return_value=0.005,
                ),
            ):
                result = self.runner.invoke(
                    paper_trade_app, ["score", "--strategy", "v9d", "--asof", "2026-05-11"]
                )

            self.assertEqual(result.exit_code, 0, msg=result.stdout)
            self.assertIn("Ledger appended", result.stdout)
            self.assertTrue(ledger_path.exists())
            # State should now reflect the new decile.
            reloaded = PaperTradeState.load(state_path)
            self.assertEqual(reloaded.held, ["BBB", "CCC", "DDD"])
            self.assertEqual(reloaded.rebalance_n, 2)

    def test_unresolvable_asof_exits_with_error(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app

        with patch(
            "alphalens_research.paper_trade.scorer_v9d.latest_trading_asof",
            return_value=None,
        ):
            result = self.runner.invoke(paper_trade_app, ["score", "--strategy", "v9d"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("could not resolve", _plain(result.output + (result.stderr or "")))


class TestRefreshDataCommandDispatchOnly(unittest.TestCase):
    """``paper-trade refresh-data`` calls iVolatility; only test dispatch."""

    def setUp(self):
        self.runner = CliRunner()

    def test_missing_strategy_arg_fails(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app

        result = self.runner.invoke(paper_trade_app, ["refresh-data"])
        self.assertEqual(result.exit_code, 2)

    def test_missing_api_key_exits_with_clear_error(self):
        from alphalens_cli.commands.paper_trade import paper_trade_app

        with patch.dict("os.environ", {"IVOLATILITY_API_KEY": ""}, clear=False):
            result = self.runner.invoke(paper_trade_app, ["refresh-data", "--strategy", "v9d"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("IVOLATILITY_API_KEY", _plain(result.output + (result.stderr or "")))


if __name__ == "__main__":
    unittest.main()
