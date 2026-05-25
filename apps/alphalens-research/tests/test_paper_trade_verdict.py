"""Tests for ``alphalens_research.paper_trade.verdict.evaluate_decision_rule``.

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

import numpy as np
import pandas as pd
from alphalens_research.paper_trade.verdict import (
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
    @patch("alphalens_research.paper_trade.verdict.compute_running_stats")
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
    @patch("alphalens_research.paper_trade.verdict._per_sub_period_alpha_ts", return_value=[])
    @patch("alphalens_research.paper_trade.verdict.compute_running_stats")
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
    @patch("alphalens_research.paper_trade.verdict._per_sub_period_alpha_ts", return_value=[])
    @patch("alphalens_research.paper_trade.verdict.compute_running_stats")
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

    @patch("alphalens_research.paper_trade.verdict._per_sub_period_alpha_ts", return_value=[])
    @patch("alphalens_research.paper_trade.verdict.compute_running_stats")
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
    @patch("alphalens_research.paper_trade.verdict._per_sub_period_alpha_ts")
    @patch("alphalens_research.paper_trade.verdict.compute_running_stats")
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

    @patch("alphalens_research.paper_trade.verdict._per_sub_period_alpha_ts")
    @patch("alphalens_research.paper_trade.verdict.compute_running_stats")
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
    @patch("alphalens_research.paper_trade.verdict._per_sub_period_alpha_ts", return_value=[])
    @patch("alphalens_research.paper_trade.verdict.compute_running_stats")
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


class TestComputeRunningStatsIntegration(unittest.TestCase):
    """Coverage of the un-mocked Carhart-regression path in compute_running_stats.

    The class above mocks compute_running_stats to focus on decision-rule
    logic; this class exercises the real implementation with synthetic
    Carhart factors so the regression branches contribute to coverage.
    """

    def _make_ledger(self, n_weeks: int = 30) -> pd.DataFrame:
        rng = np.random.default_rng(42)
        asofs = pd.date_range("2024-01-05", periods=n_weeks, freq="W-FRI")
        return pd.DataFrame(
            {
                "asof": [d.date() for d in asofs],
                "realized_return_long_net": rng.normal(0.001, 0.01, n_weeks),
                "benchmark_return_mdy": rng.normal(0.0005, 0.008, n_weeks),
            }
        )

    def _patch_carhart(self):
        def fake_load(start, end):
            idx = pd.date_range(start, end, freq="B")
            rng = np.random.default_rng(7)
            return pd.DataFrame(
                {
                    "Mkt-RF": rng.normal(0.0004, 0.01, len(idx)),
                    "SMB": rng.normal(0.0001, 0.006, len(idx)),
                    "HML": rng.normal(0.0001, 0.005, len(idx)),
                    "Mom": rng.normal(0.0002, 0.008, len(idx)),
                    "RF": np.full(len(idx), 0.00002),
                },
                index=idx,
            )

        return patch(
            "alphalens_research.paper_trade.verdict.load_carhart_daily", side_effect=fake_load
        )

    def test_empty_ledger_returns_nans(self):
        from alphalens_research.paper_trade.verdict import compute_running_stats

        out = compute_running_stats(pd.DataFrame())
        self.assertEqual(out["n_obs"], 0)
        self.assertTrue(np.isnan(out["alpha_t"]))

    def test_under_20_obs_returns_unreliable_note(self):
        from alphalens_research.paper_trade.verdict import compute_running_stats

        ledger = self._make_ledger(n_weeks=10)
        with self._patch_carhart():
            out = compute_running_stats(ledger)
        self.assertEqual(out["n_obs"], 10)
        self.assertIn("note", out)
        self.assertIn("unreliable", out["note"])

    def test_full_regression_path(self):
        from alphalens_research.paper_trade.verdict import compute_running_stats

        ledger = self._make_ledger(n_weeks=52)
        with self._patch_carhart():
            out = compute_running_stats(ledger)
        self.assertEqual(out["n_obs"], 52)
        self.assertTrue(np.isfinite(out["alpha_t"]))
        self.assertTrue(np.isfinite(out["alpha_annualized"]))
        self.assertTrue(np.isfinite(out["sharpe_net"]))

    def test_per_sub_period_alpha_ts_real_path(self):
        from alphalens_research.paper_trade.verdict import _per_sub_period_alpha_ts

        ledger = self._make_ledger(n_weeks=52)
        with self._patch_carhart():
            ats = _per_sub_period_alpha_ts(ledger, length_weeks=13)
        self.assertLessEqual(len(ats), 4)
        for a in ats:
            self.assertTrue(np.isfinite(a))

    def test_per_sub_period_alpha_ts_short_ledger_returns_empty(self):
        from alphalens_research.paper_trade.verdict import _per_sub_period_alpha_ts

        ledger = self._make_ledger(n_weeks=5)
        with self._patch_carhart():
            ats = _per_sub_period_alpha_ts(ledger, length_weeks=13)
        self.assertEqual(ats, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
