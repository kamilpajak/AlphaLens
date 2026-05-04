"""Tests for ``alphalens.paper_trade.verdict.evaluate_decision_rule``.

Decision rule under test:
- 26-week checkpoint: cumulative αt ≥ 1.96 AND Sharpe net ≥ 0.30 → PASS_26W
- 52-week checkpoint: same + sub-period αt floor 0.5 → PASS_52W
- Else PENDING / FAIL_*

Tests use synthetic ledger frames + a mocked ``compute_running_stats``
function so we don't depend on Carhart factor data files at unit-test time.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from alphalens.paper_trade.verdict import (
    DecisionRuleResult,
    _classify_checkpoint,
    evaluate_decision_rule,
)


def _make_ledger(n_weeks: int) -> pd.DataFrame:
    """Synthetic ledger of length ``n_weeks`` — values are placeholders;
    the verdict tests mock ``compute_running_stats`` so the actual numbers
    don't matter for gate evaluation."""
    if n_weeks == 0:
        return pd.DataFrame(
            columns=[
                "asof",
                "rebalance_n",
                "n_held",
                "holdings",
                "prior_holdings",
                "realized_return_long_gross",
                "realized_return_long_net",
                "benchmark_return_mdy",
                "cost_drag_bps",
                "universe_size",
            ]
        )
    base = date(2026, 5, 4)
    rows = []
    for i in range(n_weeks):
        rows.append(
            {
                "asof": (base + timedelta(weeks=i)).isoformat(),
                "rebalance_n": i + 1,
                "n_held": 10,
                "holdings": ["A", "B"],
                "prior_holdings": ["A", "C"],
                "realized_return_long_gross": 0.012,
                "realized_return_long_net": 0.011,
                "benchmark_return_mdy": 0.005,
                "cost_drag_bps": 30.0,
                "universe_size": 1500,
            }
        )
    return pd.DataFrame(rows)


class CheckpointClassificationTests(unittest.TestCase):
    def test_classifies_n_obs(self):
        self.assertEqual(_classify_checkpoint(0), "pre-26w")
        self.assertEqual(_classify_checkpoint(25), "pre-26w")
        self.assertEqual(_classify_checkpoint(26), "26w")
        self.assertEqual(_classify_checkpoint(27), "between")
        self.assertEqual(_classify_checkpoint(51), "between")
        self.assertEqual(_classify_checkpoint(52), "52w")
        self.assertEqual(_classify_checkpoint(60), "post-52w")


class PendingTests(unittest.TestCase):
    @patch("alphalens.paper_trade.verdict.compute_running_stats")
    def test_pre_26w_returns_pending(self, mock_stats):
        mock_stats.return_value = {
            "n_obs": 10,
            "alpha_t": 1.5,
            "alpha_annualized": 0.10,
            "sharpe_net": 0.25,
            "max_drawdown": -0.05,
        }
        result = evaluate_decision_rule(_make_ledger(10))
        self.assertEqual(result.checkpoint, "pre-26w")
        self.assertEqual(result.verdict, "PENDING")
        self.assertEqual(result.n_obs, 10)


class Pass26WTests(unittest.TestCase):
    @patch("alphalens.paper_trade.verdict._per_sub_period_alpha_ts", return_value=[])
    @patch("alphalens.paper_trade.verdict.compute_running_stats")
    def test_clean_pass_at_26w(self, mock_stats, mock_subs):
        mock_stats.return_value = {
            "n_obs": 26,
            "alpha_t": 2.50,
            "alpha_annualized": 0.18,
            "sharpe_net": 0.45,
            "max_drawdown": -0.07,
        }
        result = evaluate_decision_rule(_make_ledger(26))
        self.assertEqual(result.verdict, "PASS_26W")
        self.assertEqual(result.checkpoint, "26w")
        self.assertEqual(result.n_obs, 26)
        self.assertAlmostEqual(result.cumulative_alpha_t, 2.50)


class Fail26WTests(unittest.TestCase):
    @patch("alphalens.paper_trade.verdict._per_sub_period_alpha_ts", return_value=[])
    @patch("alphalens.paper_trade.verdict.compute_running_stats")
    def test_alpha_t_below_threshold(self, mock_stats, mock_subs):
        mock_stats.return_value = {
            "n_obs": 26,
            "alpha_t": 1.5,
            "alpha_annualized": 0.10,
            "sharpe_net": 0.40,
            "max_drawdown": -0.07,
        }
        result = evaluate_decision_rule(_make_ledger(26))
        self.assertEqual(result.verdict, "FAIL_26W")

    @patch("alphalens.paper_trade.verdict._per_sub_period_alpha_ts", return_value=[])
    @patch("alphalens.paper_trade.verdict.compute_running_stats")
    def test_sharpe_below_threshold(self, mock_stats, mock_subs):
        mock_stats.return_value = {
            "n_obs": 26,
            "alpha_t": 2.20,
            "alpha_annualized": 0.18,
            "sharpe_net": 0.20,  # below 0.30
            "max_drawdown": -0.10,
        }
        result = evaluate_decision_rule(_make_ledger(26))
        self.assertEqual(result.verdict, "FAIL_26W")


class Pass52WTests(unittest.TestCase):
    @patch("alphalens.paper_trade.verdict._per_sub_period_alpha_ts")
    @patch("alphalens.paper_trade.verdict.compute_running_stats")
    def test_clean_pass_at_52w(self, mock_stats, mock_subs):
        mock_stats.return_value = {
            "n_obs": 52,
            "alpha_t": 3.20,
            "alpha_annualized": 0.20,
            "sharpe_net": 0.55,
            "max_drawdown": -0.08,
        }
        # 4 sub-periods × 13 weeks; all above 0.5 floor.
        mock_subs.return_value = [1.20, 1.50, 0.80, 2.10]
        result = evaluate_decision_rule(_make_ledger(52))
        self.assertEqual(result.verdict, "PASS_52W")

    @patch("alphalens.paper_trade.verdict._per_sub_period_alpha_ts")
    @patch("alphalens.paper_trade.verdict.compute_running_stats")
    def test_fail_when_sub_period_below_floor(self, mock_stats, mock_subs):
        mock_stats.return_value = {
            "n_obs": 52,
            "alpha_t": 3.20,
            "alpha_annualized": 0.20,
            "sharpe_net": 0.55,
            "max_drawdown": -0.08,
        }
        # One sub-period below 0.5 floor → FAIL even though aggregate passes.
        mock_subs.return_value = [1.20, 1.50, 0.30, 2.10]
        result = evaluate_decision_rule(_make_ledger(52))
        self.assertEqual(result.verdict, "FAIL_52W")


class ResultShapeTests(unittest.TestCase):
    @patch("alphalens.paper_trade.verdict._per_sub_period_alpha_ts", return_value=[])
    @patch("alphalens.paper_trade.verdict.compute_running_stats")
    def test_result_is_dataclass(self, mock_stats, mock_subs):
        mock_stats.return_value = {
            "n_obs": 26,
            "alpha_t": 2.50,
            "alpha_annualized": 0.18,
            "sharpe_net": 0.45,
            "max_drawdown": -0.07,
        }
        result = evaluate_decision_rule(_make_ledger(26))
        self.assertIsInstance(result, DecisionRuleResult)
        self.assertIn(result.verdict, {"PASS_26W", "FAIL_26W"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
