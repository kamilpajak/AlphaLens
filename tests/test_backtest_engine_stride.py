"""Verify BacktestEngine.rebalance_stride + downstream scaling work for both
daily (stride=1) and weekly (stride=5) modes introduced 2026-04-22.

Regression test to ensure future refactors do not break either cadence.
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd

from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.history_store import HistoryStore


def _make_store(n_days: int = 100, n_tickers: int = 5) -> HistoryStore:
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    histories: dict[str, pd.DataFrame] = {}
    rng = np.random.default_rng(0)
    for i in range(n_tickers):
        prices = 100 * np.exp(np.cumsum(0.001 + 0.01 * rng.standard_normal(n_days)))
        histories[f"T{i}"] = pd.DataFrame(
            {
                "open": prices,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": 1_000_000,
            },
            index=dates,
        )
    # Benchmark
    bench_px = 400 * np.exp(np.cumsum(0.0004 + 0.008 * rng.standard_normal(n_days)))
    histories["SPY"] = pd.DataFrame(
        {
            "open": bench_px,
            "high": bench_px,
            "low": bench_px,
            "close": bench_px,
            "volume": 0,
        },
        index=dates,
    )
    return HistoryStore(histories)


def _constant_scorer(histories, config):
    """Deterministic scorer — score = last-close."""
    rows = []
    benchmark = config.get("benchmark") if config else None
    for t, df in histories.items():
        if t == benchmark:
            continue
        if df.empty:
            continue
        rows.append({"ticker": t, "score": float(df["close"].iloc[-1])})
    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])
    return pd.DataFrame(rows)


_constant_scorer.MIN_BARS_REQUIRED = 5


class TestRebalanceStride(unittest.TestCase):
    def test_stride_default_is_daily(self):
        store = _make_store(n_days=60)
        engine = BacktestEngine(
            store,
            scorer=_constant_scorer,
            scorer_config={"benchmark": "SPY"},
            holding_period=5,
            top_n=3,
            benchmark="SPY",
        )
        self.assertEqual(engine.rebalance_stride, 1)

    def test_stride_5_samples_every_fifth_day(self):
        store = _make_store(n_days=60)
        daily_engine = BacktestEngine(
            store,
            scorer=_constant_scorer,
            scorer_config={"benchmark": "SPY"},
            holding_period=5,
            top_n=3,
            benchmark="SPY",
            rebalance_stride=1,
        )
        weekly_engine = BacktestEngine(
            store,
            scorer=_constant_scorer,
            scorer_config={"benchmark": "SPY"},
            holding_period=5,
            top_n=3,
            benchmark="SPY",
            rebalance_stride=5,
        )
        daily_report = daily_engine.run(date(2020, 1, 2), date(2020, 3, 30))
        weekly_report = weekly_engine.run(date(2020, 1, 2), date(2020, 3, 30))
        # Weekly should produce ~1/5 the snapshots.
        self.assertGreater(len(daily_report.rebalance_results), 0)
        self.assertGreater(len(weekly_report.rebalance_results), 0)
        ratio = len(weekly_report.rebalance_results) / len(daily_report.rebalance_results)
        self.assertGreater(ratio, 0.15)
        self.assertLess(ratio, 0.25)

    def test_stride_below_one_clamps_to_one(self):
        store = _make_store(n_days=30)
        engine = BacktestEngine(
            store,
            scorer=_constant_scorer,
            scorer_config={"benchmark": "SPY"},
            holding_period=5,
            top_n=3,
            benchmark="SPY",
            rebalance_stride=0,
        )
        self.assertEqual(engine.rebalance_stride, 1)


class TestNetAlphaScaling(unittest.TestCase):
    """compute_net_alpha must scale cost drag at the requested cadence,
    otherwise weekly and daily backtests produce mis-scaled net-alpha numbers.
    """

    def test_weekly_drag_is_one_fifth_of_daily_for_same_turnover(self):
        from scripts.run_layer2d_backtest import compute_net_alpha

        gross = 0.10  # 10% gross annualized
        turnover = 0.5  # 50% of top-N churns per rebalance

        _, _, daily_drag, _ = compute_net_alpha(gross, turnover, rebalances_per_year=252)
        _, _, weekly_drag, _ = compute_net_alpha(gross, turnover, rebalances_per_year=252 / 5)

        # Ratio daily:weekly should be exactly 5 (252 / (252/5)).
        self.assertAlmostEqual(daily_drag / weekly_drag, 5.0, places=4)

    def test_net_alpha_applies_drag(self):
        from scripts.run_layer2d_backtest import compute_net_alpha

        gross = 0.10
        net_p, net_s, drag_p, drag_s = compute_net_alpha(
            gross, avg_rebal_turnover=0.0, rebalances_per_year=252
        )
        # Zero turnover → zero drag → net equals gross.
        self.assertAlmostEqual(net_p, gross)
        self.assertAlmostEqual(net_s, gross)
        self.assertAlmostEqual(drag_p, 0.0)
        self.assertAlmostEqual(drag_s, 0.0)

    def test_stress_drag_exceeds_primary(self):
        from scripts.run_layer2d_backtest import compute_net_alpha

        _, _, dp, ds = compute_net_alpha(0.10, avg_rebal_turnover=0.5, rebalances_per_year=50.4)
        self.assertGreater(ds, dp)


class TestSharpeAutocorrAdjusted(unittest.TestCase):
    """Autocorrelation-adjusted Sharpe (Lo 2002, Perplexity R11 + Zen CR fix).

    Formula (Lo 2002 Theorem 2):
        VR(k) = 1 + 2 * Σ(1 - j/k) * ρ(j)
        k_eff = k / VR(k)
        SR_ann = SR_per_period * sqrt(k_eff)

    Sign convention:
    - Negative ρ (mean reversion) → VR < 1 → k_eff > k → adj Sharpe > naive
    - Positive ρ (momentum) → VR > 1 → k_eff < k → adj Sharpe < naive
    - ρ ≈ 0 (iid) → VR ≈ 1 → k_eff ≈ k → adj ≈ naive
    """

    def test_iid_matches_naive_within_tolerance(self):
        from alphalens.backtest.metrics import sharpe, sharpe_autocorr_adjusted

        rng = np.random.default_rng(0)
        iid = rng.standard_normal(2000) * 0.01 + 0.0003
        s_naive = sharpe(iid.tolist(), periods_per_year=252)
        s_adj = sharpe_autocorr_adjusted(iid.tolist(), periods_per_year=252)
        self.assertAlmostEqual(s_naive, s_adj, delta=abs(s_naive) * 0.10 + 0.2)

    def test_negative_autocorrelation_increases_sharpe(self):
        """Mean reversion: realized annual vol is lower than sqrt(k)*daily_vol
        → annualized Sharpe is HIGHER than naive sqrt(k) scaling."""
        from alphalens.backtest.metrics import sharpe, sharpe_autocorr_adjusted

        rng = np.random.default_rng(42)
        n = 3000
        eps = rng.standard_normal(n) * 0.01
        x = np.empty(n)
        x[0] = eps[0]
        phi = -0.3  # AR(1) mean-reverting
        for i in range(1, n):
            x[i] = phi * x[i - 1] + eps[i]
        x += 0.0003

        s_naive = sharpe(x.tolist(), periods_per_year=252)
        s_adj = sharpe_autocorr_adjusted(x.tolist(), periods_per_year=252)
        self.assertGreater(s_adj, s_naive)

    def test_positive_autocorrelation_shrinks_sharpe_magnitude(self):
        """Momentum: realized annual vol is higher than sqrt(k)*daily_vol
        → |annualised Sharpe| is LOWER than naive |sqrt(k)| scaling.

        (Uses magnitude comparison so the assertion works for either sign
        of mean return — the effect is a vol-scaling adjustment, not a
        directional one.)
        """
        from alphalens.backtest.metrics import sharpe, sharpe_autocorr_adjusted

        rng = np.random.default_rng(11)
        n = 3000
        eps = rng.standard_normal(n) * 0.01
        x = np.empty(n)
        x[0] = eps[0]
        phi = 0.3  # AR(1) positive autocorr (persistence)
        for i in range(1, n):
            x[i] = phi * x[i - 1] + eps[i]
        x += 0.002  # larger positive drift so both Sharpes are positive

        s_naive = sharpe(x.tolist(), periods_per_year=252)
        s_adj = sharpe_autocorr_adjusted(x.tolist(), periods_per_year=252)
        self.assertLess(abs(s_adj), abs(s_naive))

    def test_matches_lo2002_variance_ratio(self):
        """Numerical sanity: for AR(1) phi=-0.5, k=52, the variance ratio
        VR(k) = 1 + 2*Σ(1-j/k)*ρ(j) should approximate sum_{j} (1-j/k)*phi^j
        and k_eff/k should diverge significantly from 1.0."""
        from alphalens.backtest.metrics import sharpe, sharpe_autocorr_adjusted

        rng = np.random.default_rng(123)
        n = 5000
        eps = rng.standard_normal(n) * 0.01
        x = np.empty(n)
        x[0] = eps[0]
        phi = -0.5
        for i in range(1, n):
            x[i] = phi * x[i - 1] + eps[i]
        x += 0.001

        s_naive = sharpe(x.tolist(), periods_per_year=252)
        s_adj = sharpe_autocorr_adjusted(x.tolist(), periods_per_year=252, max_lag=5)
        # Strong mean reversion (phi=-0.5) + positive drift → adj Sharpe must be
        # clearly larger. Direction: scale multiplier = sqrt(k_eff / k) where
        # k_eff > k for ρ<0; concrete ratio depends on stochastic sample.
        self.assertGreater(s_adj, s_naive * 1.05)

    def test_returns_zero_on_empty_or_constant(self):
        from alphalens.backtest.metrics import sharpe_autocorr_adjusted

        self.assertEqual(sharpe_autocorr_adjusted([], periods_per_year=52), 0.0)
        self.assertEqual(sharpe_autocorr_adjusted([0.01] * 20, periods_per_year=52), 0.0)


if __name__ == "__main__":
    unittest.main()
