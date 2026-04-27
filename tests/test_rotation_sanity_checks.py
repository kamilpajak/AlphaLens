"""Tests for alphalens.rotation.sanity_checks — 4 kill-gate IS diagnostics.

Per Perplexity R12 follow-up consultation 2026-04-24 (post-IS-backtest):
  1. passive_correlation — strategy must NOT track 60/30/10 too tightly
  2. rolling_sharpe_stability — no 252d window collapses below 0.4
  3. per_regime_vs_passive — outperform in ≥2 of 3 regimes (bull/bear/flat)
  4. overlay_alpha — regression on passive; α>20bps/y AND t>1.0

All four must pass for OOS single-shot to be statistically justified.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from alphalens.backtest.history_store import HistoryStore


def _prices(n: int, daily_mu: float, daily_vol: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2015-01-02", periods=n, freq="B")
    rets = rng.normal(daily_mu, daily_vol, n)
    close = np.cumprod(1 + rets) * 100.0
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )


def _synthetic_store(n: int = 800):
    return HistoryStore(
        {
            "SPY": _prices(n, 0.0004, 0.010, seed=1),
            "QQQ": _prices(n, 0.0006, 0.012, seed=2),
            "IWM": _prices(n, 0.0003, 0.013, seed=3),
        }
    )


class TestBuildPassiveBenchmark(unittest.TestCase):
    def test_returns_weighted_buy_and_hold_daily_returns(self):
        """Deterministic test: constant daily returns per ticker → exact passive mean."""
        from alphalens.rotation.sanity_checks import build_passive_benchmark

        # Constant daily growth → exact pct_change per ticker
        n = 400
        idx = pd.date_range("2015-01-02", periods=n, freq="B")

        def _const(daily_ret: float) -> pd.DataFrame:
            close = (1.0 + daily_ret) ** np.arange(n) * 100.0
            return pd.DataFrame(
                {
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": np.full(n, 1e6),
                },
                index=idx,
            )

        store = HistoryStore({"SPY": _const(0.0004), "QQQ": _const(0.0006), "IWM": _const(0.0003)})
        core = {"SPY": 0.60, "QQQ": 0.30, "IWM": 0.10}

        passive = build_passive_benchmark(store, core_weights=core)

        self.assertIsInstance(passive, pd.Series)
        self.assertEqual(len(passive), 399)
        expected = 0.60 * 0.0004 + 0.30 * 0.0006 + 0.10 * 0.0003
        self.assertAlmostEqual(passive.mean(), expected, places=9)

    def test_raises_if_core_ticker_missing(self):
        from alphalens.rotation.sanity_checks import build_passive_benchmark

        store = HistoryStore(
            {"SPY": _prices(100, 0.0004, 0.01, 1), "QQQ": _prices(100, 0.0004, 0.01, 2)}
        )
        with self.assertRaises(KeyError):
            build_passive_benchmark(store, core_weights={"SPY": 0.6, "QQQ": 0.3, "IWM": 0.1})


class TestPassiveCorrelation(unittest.TestCase):
    def test_fails_when_correlation_above_threshold(self):
        """Strategy = passive + tiny noise → correlation ≈ 1.0 → reject."""
        from alphalens.rotation.sanity_checks import check_passive_correlation

        rng = np.random.default_rng(7)
        idx = pd.date_range("2015-01-02", periods=400, freq="B")
        passive = pd.Series(rng.normal(0.0004, 0.01, 400), index=idx)
        strategy = passive + rng.normal(0, 0.0001, 400)

        result = check_passive_correlation(strategy, passive, threshold=0.95)

        self.assertFalse(result.passed)
        self.assertGreater(result.value, 0.95)

    def test_passes_when_correlation_below_threshold(self):
        from alphalens.rotation.sanity_checks import check_passive_correlation

        rng = np.random.default_rng(8)
        idx = pd.date_range("2015-01-02", periods=400, freq="B")
        passive = pd.Series(rng.normal(0.0004, 0.01, 400), index=idx)
        # Heavy tactical tilts → lower correlation
        strategy = passive * 0.5 + rng.normal(0.0004, 0.01, 400) * 0.5

        result = check_passive_correlation(strategy, passive, threshold=0.95)

        self.assertTrue(result.passed)
        self.assertLess(result.value, 0.95)


class TestRollingSharpeStability(unittest.TestCase):
    def test_passes_when_sharpe_stable(self):
        from alphalens.rotation.sanity_checks import check_rolling_sharpe_stability

        rng = np.random.default_rng(9)
        idx = pd.date_range("2015-01-02", periods=800, freq="B")
        strong = pd.Series(rng.normal(0.002, 0.007, 800), index=idx)

        result = check_rolling_sharpe_stability(strong, window=252, min_sharpe=0.4)

        self.assertTrue(result.passed)
        self.assertGreater(result.value, 0.4)

    def test_fails_when_any_window_collapses(self):
        from alphalens.rotation.sanity_checks import check_rolling_sharpe_stability

        rng = np.random.default_rng(10)
        idx = pd.date_range("2015-01-02", periods=800, freq="B")
        data = np.concatenate([rng.normal(0.002, 0.007, 400), rng.normal(-0.0015, 0.007, 400)])
        s = pd.Series(data, index=idx)

        result = check_rolling_sharpe_stability(s, window=252, min_sharpe=0.4)

        self.assertFalse(result.passed)


def _three_regime_benchmark(n_per_regime: int = 400):
    """Benchmark fixture spanning bull → bear → flat regimes via daily drift.

    With classify_regime defaults (lookback=60, ±5% threshold), drift sequences
    of ~+0.10%/day, ~-0.10%/day, and ~0/day (with low noise) reliably classify
    into bull / bear / flat respectively after the lookback warmup window.
    """
    n = n_per_regime * 3
    idx = pd.date_range("2015-01-02", periods=n, freq="B")
    rng = np.random.default_rng(7)
    bull = rng.normal(0.0010, 0.0003, n_per_regime)  # ~+25%/yr
    bear = rng.normal(-0.0010, 0.0003, n_per_regime)  # ~-25%/yr
    flat = rng.normal(0.0000, 0.0003, n_per_regime)  # ~0%/yr drift
    bench_daily = np.concatenate([bull, bear, flat])
    bench_close = pd.Series(np.cumprod(1 + bench_daily) * 100, index=idx)
    passive = pd.Series(bench_daily * 0.5, index=idx)  # half-beta passive
    return idx, bench_close, passive


class TestPerRegimeVsPassive(unittest.TestCase):
    def test_passes_when_all_three_regimes_outperform(self):
        from alphalens.rotation.sanity_checks import check_per_regime_vs_passive

        _, bench_close, passive = _three_regime_benchmark()
        strategy = passive + 0.0005  # +5 bps/day across the board

        result = check_per_regime_vs_passive(
            strategy_returns=strategy,
            passive_returns=passive,
            benchmark_close=bench_close,
        )

        self.assertTrue(result.passed)

    def test_fails_when_outperforms_in_only_one_regime(self):
        from alphalens.rotation.sanity_checks import check_per_regime_vs_passive

        _, bench_close, passive = _three_regime_benchmark()
        strategy = passive.copy()
        # Outperform only in the first segment (bull), underperform in bear+flat
        n = len(strategy)
        third = n // 3
        strategy.iloc[:third] += 0.0010
        strategy.iloc[third:] -= 0.0010

        result = check_per_regime_vs_passive(
            strategy_returns=strategy,
            passive_returns=passive,
            benchmark_close=bench_close,
        )

        self.assertFalse(result.passed)

    @patch("alphalens.rotation.sanity_checks.classify_regime")
    def test_fails_when_classifier_emits_only_two_regime_labels(self, mock_classify):
        """Kill-gate per docstring: '≥ 2 of 3 regimes' demands all 3 are
        exercised. We force a 2-regime classifier output (no flat label)
        with both regimes outperforming — the strict gate must reject so
        the user extends the window until a flat regime appears.
        """
        from alphalens.rotation.sanity_checks import check_per_regime_vs_passive

        n = 600
        idx = pd.date_range("2015-01-02", periods=n, freq="B")
        mock_classify.return_value = pd.Series(["bull"] * 300 + ["bear"] * 300, index=idx)

        passive = pd.Series(np.full(n, 0.0005), index=idx)
        strategy = passive + 0.001  # uniform outperformance across both labels
        bench = pd.Series(np.cumprod(1 + np.full(n, 0.0005)) * 100, index=idx)

        result = check_per_regime_vs_passive(
            strategy_returns=strategy,
            passive_returns=passive,
            benchmark_close=bench,
        )

        self.assertFalse(result.passed)


class TestOverlayAlpha(unittest.TestCase):
    def test_passes_when_alpha_positive_and_t_above_threshold(self):
        from alphalens.rotation.sanity_checks import check_overlay_alpha

        rng = np.random.default_rng(11)
        idx = pd.date_range("2015-01-02", periods=1260, freq="B")
        passive = pd.Series(rng.normal(0.0004, 0.010, 1260), index=idx)
        # Strategy: passive + 30 bps/day uncorrelated premium → huge alpha
        strategy = passive + rng.normal(0.0003, 0.003, 1260)

        result = check_overlay_alpha(strategy, passive, min_alpha_bps=20, min_t=1.0)

        self.assertTrue(result.passed)
        self.assertGreater(result.value, 20)  # bps/yr

    def test_fails_when_alpha_below_threshold(self):
        from alphalens.rotation.sanity_checks import check_overlay_alpha

        rng = np.random.default_rng(12)
        idx = pd.date_range("2015-01-02", periods=1260, freq="B")
        passive = pd.Series(rng.normal(0.0004, 0.010, 1260), index=idx)
        strategy = passive + rng.normal(0, 0.001, 1260)  # pure noise

        result = check_overlay_alpha(strategy, passive, min_alpha_bps=20, min_t=1.0)

        self.assertFalse(result.passed)


class TestRunAllSanityChecks(unittest.TestCase):
    def test_aggregates_all_four_checks(self):
        from alphalens.rotation.sanity_checks import (
            SanityCheckReport,
            run_all_sanity_checks,
        )

        rng = np.random.default_rng(13)
        n = 800
        idx = pd.date_range("2015-01-02", periods=n, freq="B")
        passive = pd.Series(rng.normal(0.0004, 0.010, n), index=idx)
        strategy = passive + rng.normal(0, 0.0001, n)  # near-identical to passive
        bench_close = pd.Series(np.cumprod(1 + passive) * 100, index=idx)

        report = run_all_sanity_checks(
            strategy_returns=strategy,
            passive_returns=passive,
            benchmark_close=bench_close,
        )

        self.assertIsInstance(report, SanityCheckReport)
        self.assertEqual(len(report.checks), 4)
        # High-correlation strategy should fail at least the correlation gate
        self.assertFalse(report.passed)


if __name__ == "__main__":
    unittest.main()
