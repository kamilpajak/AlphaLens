"""Unit tests for experiment_idiosyncratic_momentum.assess() + _RawMonthlyMomentumScorer.

assess() invariants mirror the H1 cost-sensitivity pattern from
``test_experiment_ev_fcff_yield_assess`` — net Carhart-4F t-stat MUST move
with cost_bps (orchestrator G4 reads ``t_net_4f``).

§5.1 BAB-confound diagnostics are also exercised: ``beta_mkt_capm``,
``alpha_ff5_umd``, ``attenuation_carhart_to_ff5_umd``, and the boolean
flags must be present on the returned dict.

``_RawMonthlyMomentumScorer`` is covered for: scoring logic correctness,
benchmark filtering, price-floor filtering, and short-history dropout.
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
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=n_days)
    rets = rng.normal(loc=0.0012, scale=0.008, size=n_days)
    return pd.Series(rets, index=dates)


def _synthetic_carhart(dates: pd.DatetimeIndex) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = len(dates)
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.0003, 0.009, n),
            "SMB": rng.normal(0.0001, 0.005, n),
            "HML": rng.normal(0.0, 0.005, n),
            "Mom": rng.normal(0.0001, 0.006, n),
            "RF": np.full(n, 0.00008),
        },
        index=dates,
    )


def _synthetic_ff5_umd(dates: pd.DatetimeIndex) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    n = len(dates)
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.0003, 0.009, n),
            "SMB": rng.normal(0.0001, 0.005, n),
            "HML": rng.normal(0.0, 0.005, n),
            "RMW": rng.normal(0.0001, 0.004, n),
            "CMA": rng.normal(0.0, 0.004, n),
            "Mom": rng.normal(0.0001, 0.006, n),
            "RF": np.full(n, 0.00008),
        },
        index=dates,
    )


def _synthetic_report(n_rebal: int = 8, top_n: int = 200) -> SimpleNamespace:
    pool = [f"T{i:03d}" for i in range(top_n + n_rebal * 50)]
    snaps = [SimpleNamespace(top_n_tickers=pool[i * 50 : i * 50 + top_n]) for i in range(n_rebal)]
    return SimpleNamespace(rebalance_results=snaps)


class TestIdiosyncraticMomentumAssess(unittest.TestCase):
    def _run_assess_at_cost(self, cost_bps: float) -> dict:
        import scripts.experiment_idiosyncratic_momentum as exp

        rets = _synthetic_daily_returns()
        factors = _synthetic_carhart(rets.index)
        ff5_umd = _synthetic_ff5_umd(rets.index)
        bench_rets = pd.Series(np.zeros(len(rets)), index=rets.index)
        report = _synthetic_report()

        with patch.object(exp, "daily_continuous_returns", return_value=rets):
            return exp.assess(
                report,
                factors,
                ff5_umd,
                rebalance_stride=21,
                cost_bps=cost_bps,
                bench_rets_daily=bench_rets,
                history_store=SimpleNamespace(),
                benchmark="IWM",
                end_date=date(2020, 12, 31),
            )

    def test_t_net_4f_is_cost_sensitive(self):
        """G4 invariant: net-regression t-stat must decrease as cost rises."""
        stats_5 = self._run_assess_at_cost(5.0)
        stats_15 = self._run_assess_at_cost(15.0)
        self.assertGreater(stats_5["t_net_4f"], stats_15["t_net_4f"])

    def test_t_4f_gross_is_cost_invariant(self):
        stats_5 = self._run_assess_at_cost(5.0)
        stats_15 = self._run_assess_at_cost(15.0)
        self.assertAlmostEqual(stats_5["t_4f"], stats_15["t_4f"], places=8)

    def test_bab_diagnostics_keys_present(self):
        """§5.1 BAB-confound diagnostics emit on every assess call."""
        stats = self._run_assess_at_cost(5.0)
        for k in (
            "beta_mkt_capm",
            "alpha_ff5_umd",
            "t_ff5_umd",
            "attenuation_carhart_to_ff5_umd",
            "bab_beta_flag",
            "bab_attenuation_flag",
        ):
            self.assertIn(k, stats)

    def test_dict_keys_present_and_well_typed(self):
        stats = self._run_assess_at_cost(5.0)
        for k in (
            "alpha_gross_4f",
            "t_4f",
            "alpha_net_4f",
            "t_net_4f",
            "cost_drag_ann",
            "sharpe_gross",
            "sharpe_net",
            "turnover_per_rebal",
        ):
            self.assertIn(k, stats)
            self.assertIsInstance(stats[k], float)

    def test_empty_returns_handled(self):
        import scripts.experiment_idiosyncratic_momentum as exp

        empty = pd.Series(dtype=float)
        with patch.object(exp, "daily_continuous_returns", return_value=empty):
            out = exp.assess(
                _synthetic_report(),
                _synthetic_carhart(pd.bdate_range("2020-01-02", periods=10)),
                _synthetic_ff5_umd(pd.bdate_range("2020-01-02", periods=10)),
                rebalance_stride=21,
                cost_bps=5.0,
                bench_rets_daily=pd.Series(dtype=float),
                history_store=SimpleNamespace(),
                benchmark="IWM",
                end_date=date(2020, 12, 31),
            )
        self.assertEqual(out, {"n": 0})


class TestRawMonthlyMomentumScorer(unittest.TestCase):
    def _hist(self, tickers: list[str], n_days: int = 1000, seed: int = 0) -> dict:
        rng = np.random.default_rng(seed)
        idx = pd.bdate_range("2017-01-02", periods=n_days)
        out = {}
        for i, t in enumerate(tickers):
            closes = 100.0 * (1.0 + rng.normal(0.0005 * (i + 1), 0.01, n_days)).cumprod()
            out[t] = pd.DataFrame({"close": closes}, index=idx)
        return out

    def test_min_bars_required_post_zen_bump(self):
        from scripts.experiment_idiosyncratic_momentum import _RawMonthlyMomentumScorer

        self.assertEqual(_RawMonthlyMomentumScorer.MIN_BARS_REQUIRED, 900)

    def test_returns_dataframe_sorted_descending(self):
        from scripts.experiment_idiosyncratic_momentum import _RawMonthlyMomentumScorer

        scorer = _RawMonthlyMomentumScorer()
        hist = self._hist(["A", "B", "C", "D"], n_days=1000, seed=1)
        out = scorer(hist, {})
        self.assertIsInstance(out, pd.DataFrame)
        self.assertEqual(list(out.columns), ["ticker", "score"])
        scores = out["score"].tolist()
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_benchmark_filtered(self):
        from scripts.experiment_idiosyncratic_momentum import _RawMonthlyMomentumScorer

        scorer = _RawMonthlyMomentumScorer()
        hist = self._hist(["A", "B", "IWM"], n_days=1000, seed=2)
        out = scorer(hist, {"benchmark": "IWM"})
        self.assertNotIn("IWM", set(out["ticker"]))

    def test_price_floor_drops_penny_stocks(self):
        from scripts.experiment_idiosyncratic_momentum import _RawMonthlyMomentumScorer

        scorer = _RawMonthlyMomentumScorer(price_floor=10_000.0)
        hist = self._hist(["A", "B"], n_days=1000)
        out = scorer(hist, {})
        self.assertTrue(out.empty)

    def test_short_history_ticker_dropped(self):
        from scripts.experiment_idiosyncratic_momentum import _RawMonthlyMomentumScorer

        scorer = _RawMonthlyMomentumScorer()
        # Only 5 months of data — below formation_lookback=12 → dropped.
        hist = self._hist(["SHORT"], n_days=110)
        out = scorer(hist, {})
        self.assertTrue(out.empty)


if __name__ == "__main__":
    unittest.main()
