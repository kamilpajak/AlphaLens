"""Unit test for experiment_ev_fcff_yield.assess() cost-sensitivity (issue #105 H1).

Pre-fix bug: Carhart regression ran on `rets_daily` (gross), so `t_4f` was
invariant to `cost_bps`, making the orchestrator's G4 cost-stress gate a
structural no-op duplicate of G1.

Post-fix invariants asserted here:
- `t_4f` (gross regression) IS cost-invariant — sanity check that the gross
  side is intact.
- `t_net_4f` (NEW key, net regression on rets - drag_per_day) IS cost-sensitive
  — strictly monotone-decreasing as `cost_bps` rises for a stable signal.
- `alpha_net_4f` is the net-regression intercept (annualised), NOT the broken
  scalar `alpha_gross - drag_ann` from the pre-fix code path.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _synthetic_daily_returns(seed: int = 42, n_days: int = 504) -> pd.Series:
    """Stable positive-drift series so the regression has non-trivial alpha."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=n_days)
    # ~12bps/day mean, ~80bps/day vol → ann α ≈ 30%, ann σ ≈ 12.7%
    rets = rng.normal(loc=0.0012, scale=0.008, size=n_days)
    return pd.Series(rets, index=dates)


def _synthetic_carhart(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Carhart 4F + RF noise factors aligned to the portfolio return index."""
    rng = np.random.default_rng(7)
    n = len(dates)
    df = pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.0003, 0.009, n),
            "SMB": rng.normal(0.0001, 0.005, n),
            "HML": rng.normal(0.0, 0.005, n),
            "Mom": rng.normal(0.0001, 0.006, n),
            "RF": np.full(n, 0.00008),  # ~2%/y
        },
        index=dates,
    )
    return df


def _synthetic_report(n_rebal: int = 8, top_n: int = 150) -> SimpleNamespace:
    """Mimic BacktestReport(rebalance_results=[RebalanceSnapshot(...)]).

    Rotate the basket window across snapshots so turnover is non-zero
    (~33%/rebalance). Zero turnover would zero out the cost drag and defeat
    the H1 invariants under test.
    """
    pool = [f"T{i:03d}" for i in range(top_n + n_rebal * 50)]
    snaps = [SimpleNamespace(top_n_tickers=pool[i * 50 : i * 50 + top_n]) for i in range(n_rebal)]
    return SimpleNamespace(rebalance_results=snaps)


class TestEvFcffYieldAssessCostSensitivity(unittest.TestCase):
    def _run_assess_at_cost(self, cost_bps: float) -> dict:
        import scripts.experiment_ev_fcff_yield as exp

        rets = _synthetic_daily_returns()
        factors = _synthetic_carhart(rets.index)
        bench_rets = pd.Series(np.zeros(len(rets)), index=rets.index)
        report = _synthetic_report()

        with patch.object(exp, "daily_continuous_returns", return_value=rets):
            return exp.assess(
                report,
                factors,
                rebalance_stride=63,
                cost_bps=cost_bps,
                bench_rets_daily=bench_rets,
                history_store=SimpleNamespace(),
                benchmark="IWM",
                end_date=date(2020, 12, 31),
            )

    def test_t_net_4f_is_cost_sensitive(self):
        """Core H1 fix: net-regression t-stat must decrease as cost rises."""
        stats_5 = self._run_assess_at_cost(5.0)
        stats_15 = self._run_assess_at_cost(15.0)

        self.assertIn("t_net_4f", stats_5)
        self.assertIn("t_net_4f", stats_15)
        # Strict monotone decrease in net t-stat as cost rises.
        self.assertGreater(stats_5["t_net_4f"], stats_15["t_net_4f"])
        # And by a noticeable margin (not floating-point noise).
        self.assertGreater(stats_5["t_net_4f"] - stats_15["t_net_4f"], 0.05)

    def test_t_4f_gross_is_cost_invariant(self):
        """Gross-regression t-stat must NOT change with cost (sanity)."""
        stats_5 = self._run_assess_at_cost(5.0)
        stats_15 = self._run_assess_at_cost(15.0)
        self.assertAlmostEqual(stats_5["t_4f"], stats_15["t_4f"], places=8)
        self.assertAlmostEqual(stats_5["alpha_gross_4f"], stats_15["alpha_gross_4f"], places=10)

    def test_alpha_net_equals_scalar_subtraction_but_t_does_not(self):
        """OLS sanity: shifting y by a constant shifts intercept by the same
        constant — so alpha_net (regression) ≡ alpha_gross - drag_ann to FP.
        The fix's real product is t_net_4f, which is NOT derivable from
        t_4f + scalar arithmetic and must come from re-running the regression.
        """
        stats = self._run_assess_at_cost(15.0)
        scalar = stats["alpha_gross_4f"] - stats["cost_drag_ann"]
        # alpha values coincide (mathematical identity).
        self.assertAlmostEqual(stats["alpha_net_4f"], scalar, places=10)
        # But the t-stat for net IS strictly less than the t-stat for gross,
        # because the numerator shrinks while the SE stays the same.
        self.assertLess(stats["t_net_4f"], stats["t_4f"])

    def test_dict_keys_present_and_well_typed(self):
        stats = self._run_assess_at_cost(5.0)
        for k in (
            "alpha_gross_4f",
            "t_4f",
            "alpha_net_4f",
            "t_net_4f",
            "cost_drag_ann",
        ):
            self.assertIn(k, stats)
            self.assertIsInstance(stats[k], float)


if __name__ == "__main__":
    unittest.main()
