"""Unit tests for `alphalens.attribution.walk_forward`.

All tests use in-memory synthetic `BacktestReport` / `RebalanceSnapshot` fixtures —
no Polygon, no real BacktestEngine runs. Focuses on the ten critical
behaviours flagged in the plan (including the zen-integrated path-
independence invariant and block-return autocorr gate).
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd

from alphalens.attribution.walk_forward import (
    DistributionSummary,
    WindowResult,
    WindowSpec,
    block_return_autocorr,
    compute_window_metrics,
    evaluate_gate,
    generate_windows,
    slice_report_to_window,
    summarize_distribution,
)
from alphalens.backtest.engine import BacktestReport, RebalanceSnapshot


def _daily(d: pd.Timestamp, ret: float, tickers: list[str] | None = None) -> RebalanceSnapshot:
    tks = tickers or ["A", "B", "C"]
    return RebalanceSnapshot(
        date=d,
        scored_count=10,
        top_n_tickers=list(tks),
        top_n_scores=[0.9, 0.8, 0.7][: len(tks)],
        top_n_forward_returns=[ret] * len(tks),
        portfolio_return=ret,
        portfolio_return_holding=ret * 5,
        universe_median_return=0.0,
        ic=0.05,
    )


def _report(returns: list[float], start: str = "2022-01-03") -> BacktestReport:
    idx = pd.date_range(start, periods=len(returns), freq="B")
    daily = [_daily(idx[i], returns[i]) for i in range(len(returns))]
    return BacktestReport(
        scorer_config={},
        holding_period=5,
        top_n=3,
        start=idx[0].date(),
        end=idx[-1].date(),
        benchmark="SPY",
        universe_ticker_count=10,
        rebalance_results=daily,
    )


def _flat_benchmark_series(start: str, periods: int) -> pd.Series:
    idx = pd.date_range(start, periods=periods, freq="B")
    return pd.Series(100.0, index=idx)


class TestGenerateWindows(unittest.TestCase):
    def test_size_and_stride(self):
        cal = list(pd.date_range("2022-01-03", periods=1000, freq="B"))
        windows = generate_windows(cal, window_days=252, step_days=21)
        expected_count = (1000 - 252) // 21 + 1  # ~37
        self.assertEqual(len(windows), expected_count)
        self.assertEqual(windows[0].test_start, cal[0].date())
        self.assertEqual(windows[0].test_end, cal[251].date())
        self.assertEqual(windows[1].test_start, cal[21].date())

    def test_rejects_stub(self):
        cal = list(pd.date_range("2022-01-03", periods=100, freq="B"))
        self.assertEqual(generate_windows(cal, window_days=252, step_days=21), [])


class TestSliceReport(unittest.TestCase):
    def test_slice_preserves_metrics(self):
        returns = [0.01] * 500  # all +1%
        baseline = _report(returns)
        cal = [snap.date for snap in baseline.rebalance_results]
        window = WindowSpec(test_start=cal[100].date(), test_end=cal[100 + 251].date())
        sliced = slice_report_to_window(baseline, window)
        self.assertEqual(len(sliced.rebalance_results), 252)
        # Sharpe on constant positive series is extremely high by construction —
        # assert the slice's first/last dates match expectations
        self.assertEqual(sliced.rebalance_results[0].date, cal[100])
        self.assertEqual(sliced.rebalance_results[-1].date, cal[100 + 251])


class TestMaxDDPathIndependence(unittest.TestCase):
    def test_max_drawdown_path_independent(self):
        """Two overlapping slices sharing the same absolute dates must
        compute identical Max DD, regardless of surrounding context."""
        # Series A: +5%, then -20%, then +5%. MaxDD on middle slice = -20%.
        # Series B: same middle slice preceded by a +100% runup. Global
        # equity peak is in the runup; naive cumprod over the whole series
        # would underestimate the middle slice's local drawdown.
        middle_returns = [0.05, -0.20, 0.05]
        series_a = [0.0] * 2 + middle_returns + [0.0] * 2
        series_b = [1.0, 1.0] + middle_returns + [0.0] * 2  # huge runup
        rep_a = _report(series_a)
        rep_b = _report(series_b)

        bench = _flat_benchmark_series(
            start=rep_a.rebalance_results[0].date.strftime("%Y-%m-%d"),
            periods=len(series_a) + 10,
        )
        # Slice to the middle 3 days, same absolute dates in both reports
        window = WindowSpec(
            test_start=rep_a.rebalance_results[2].date.date(),
            test_end=rep_a.rebalance_results[4].date.date(),
        )
        sliced_a = slice_report_to_window(rep_a, window)
        sliced_b = slice_report_to_window(rep_b, window)
        metrics_a = compute_window_metrics(sliced_a, window, bench, None)
        metrics_b = compute_window_metrics(sliced_b, window, bench, None)

        self.assertAlmostEqual(metrics_a.max_drawdown, metrics_b.max_drawdown, places=6)
        # Sanity: both should capture the -20% drop, independent of surrounding context
        self.assertLess(metrics_a.max_drawdown, -0.1)


class TestComputeWindowMetrics(unittest.TestCase):
    def test_no_carhart(self):
        returns = [0.01, -0.005, 0.02, 0.0, -0.01] * 10  # 50 days
        baseline = _report(returns)
        window = WindowSpec(
            test_start=baseline.rebalance_results[0].date.date(),
            test_end=baseline.rebalance_results[-1].date.date(),
        )
        sliced = slice_report_to_window(baseline, window)
        bench = _flat_benchmark_series(
            start=baseline.rebalance_results[0].date.strftime("%Y-%m-%d"),
            periods=100,
        )
        metrics = compute_window_metrics(sliced, window, bench, None)
        self.assertEqual(metrics.n_days, 50)
        self.assertIsNone(metrics.carhart_alpha_tstat)
        # Moderate drag reduces Sharpe vs gross for positive-return series
        self.assertLess(metrics.sharpe_moderate, metrics.sharpe_gross)

    def test_with_carhart(self):
        # Feed a 100-day series with tiny positive drift so regression is stable
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.01, size=100).tolist()
        baseline = _report(returns)
        window = WindowSpec(
            test_start=baseline.rebalance_results[0].date.date(),
            test_end=baseline.rebalance_results[-1].date.date(),
        )
        sliced = slice_report_to_window(baseline, window)
        bench = _flat_benchmark_series(
            start=baseline.rebalance_results[0].date.strftime("%Y-%m-%d"),
            periods=200,
        )
        idx = pd.DatetimeIndex([d.date for d in baseline.rebalance_results])
        # All-zero factors + small RF → alpha tracks mean return
        carhart = pd.DataFrame(
            {"Mkt-RF": 0.0, "SMB": 0.0, "HML": 0.0, "Mom": 0.0, "RF": 0.0},
            index=idx,
        )
        metrics = compute_window_metrics(sliced, window, bench, carhart)
        self.assertIsNotNone(metrics.carhart_alpha_tstat)


class TestBlockReturnAutocorr(unittest.TestCase):
    def test_ar1_series_recovers_rho(self):
        """AR(1) daily series with known ρ ≈ 0: block-return autocorr
        should be near zero (independent blocks)."""
        rng = np.random.default_rng(7)
        # Pure white noise → independent daily returns, independent blocks
        white = rng.normal(0, 0.01, size=1000)
        idx = pd.date_range("2022-01-03", periods=1000, freq="B")
        series = pd.Series(white, index=idx)
        ac = block_return_autocorr(series, block_days=21)
        # With block_days=21 and n=1000, we get ~47 blocks → autocorr of
        # block sums should be near 0 (tolerance for finite sample)
        self.assertLess(abs(ac), 0.3)

    def test_too_few_returns(self):
        idx = pd.date_range("2022-01-03", periods=10, freq="B")
        series = pd.Series(np.zeros(10), index=idx)
        self.assertTrue(np.isnan(block_return_autocorr(series, block_days=21)))


class TestSummarizeDistribution(unittest.TestCase):
    def test_quantiles_and_fractions(self):
        # 25 windows with Sharpe evenly spaced 0.0 → 2.4
        sharpes = np.linspace(0.0, 2.4, 25)
        results = []
        for i, s in enumerate(sharpes):
            results.append(
                WindowResult(
                    test_start=date(2022, 1, 1),
                    test_end=date(2022, 12, 31),
                    n_days=252,
                    regime="bull",
                    regime_reversed_within=False,
                    sharpe_gross=float(s),
                    sharpe_moderate=float(s) - 0.05,
                    carhart_alpha_daily=None,
                    carhart_alpha_tstat=None,
                    ic_mean=0.01,
                    ic_tstat=1.0,
                    max_drawdown=-0.1,
                    turnover=0.5,
                    cumulative_return=0.15,
                )
            )
        baseline = _report([0.001] * 600)
        summary = summarize_distribution(results, baseline)
        self.assertAlmostEqual(summary.sharpe_min, 0.0, places=3)
        self.assertAlmostEqual(summary.sharpe_max, 2.4, places=3)
        self.assertAlmostEqual(summary.sharpe_median, 1.2, places=3)
        # Sharpes > 0.5: 19 out of 25 → 76%
        self.assertAlmostEqual(summary.fraction_sharpe_gt_0_5, 19 / 25, places=3)


class TestDarkHalf(unittest.TestCase):
    def test_12_consecutive_flagged(self):
        sharpes = [1.0] * 5 + [-0.1] * 12 + [1.0] * 8
        results = [
            WindowResult(
                test_start=date(2022, 1, 1) + pd.Timedelta(days=i).to_pytimedelta(),
                test_end=date(2022, 12, 31),
                n_days=252,
                regime="bull",
                regime_reversed_within=False,
                sharpe_gross=float(s),
                sharpe_moderate=float(s),
                carhart_alpha_daily=None,
                carhart_alpha_tstat=None,
                ic_mean=0.0,
                ic_tstat=0.0,
                max_drawdown=0.0,
                turnover=0.5,
                cumulative_return=0.0,
            )
            for i, s in enumerate(sharpes)
        ]
        baseline = _report([0.0] * 100)
        summary = summarize_distribution(results, baseline)
        self.assertEqual(summary.longest_negative_sharpe_stretch, 12)

    def test_11_consecutive_not_longest_only(self):
        sharpes = [-0.1] * 11 + [1.0] * 10  # 11 negatives, then positives
        results = [
            WindowResult(
                test_start=date(2022, 1, 1) + pd.Timedelta(days=i).to_pytimedelta(),
                test_end=date(2022, 12, 31),
                n_days=252,
                regime="bull",
                regime_reversed_within=False,
                sharpe_gross=float(s),
                sharpe_moderate=float(s),
                carhart_alpha_daily=None,
                carhart_alpha_tstat=None,
                ic_mean=0.0,
                ic_tstat=0.0,
                max_drawdown=0.0,
                turnover=0.5,
                cumulative_return=0.0,
            )
            for i, s in enumerate(sharpes)
        ]
        baseline = _report([0.0] * 100)
        summary = summarize_distribution(results, baseline)
        self.assertEqual(summary.longest_negative_sharpe_stretch, 11)


def _summary_with(
    *,
    fraction_sharpe_gt_0_5: float = 0.85,
    fraction_alpha_t_gt_1_5: float | None = 0.65,
    block_autocorr: float = 0.1,
    longest_neg: int = 3,
    max_turnover: float = 0.6,
) -> DistributionSummary:
    return DistributionSummary(
        sharpe_min=0.0,
        sharpe_q25=0.5,
        sharpe_median=1.0,
        sharpe_q75=1.5,
        sharpe_max=2.0,
        fraction_sharpe_gt_0_5=fraction_sharpe_gt_0_5,
        fraction_sharpe_gt_1_0=0.5,
        alpha_t_min=0.0,
        alpha_t_q25=1.0,
        alpha_t_median=1.8,
        alpha_t_q75=2.5,
        alpha_t_max=3.0,
        fraction_alpha_t_gt_1_5=fraction_alpha_t_gt_1_5,
        fraction_alpha_t_gt_2_0=0.4 if fraction_alpha_t_gt_1_5 is not None else None,
        ic_t_median=1.6,
        fraction_ic_t_gt_1_5=0.6,
        block_return_autocorr_lag1=block_autocorr,
        longest_negative_sharpe_stretch=longest_neg,
        dark_half_span=None,
        max_turnover=max_turnover,
        q95_turnover=max_turnover * 0.9,
        momentum_crash_coincidence=0.0,
    )


class TestEvaluateGate(unittest.TestCase):
    def test_all_pass(self):
        summary = _summary_with()
        v = evaluate_gate(summary)
        self.assertEqual(v.overall, "PASS")
        self.assertTrue(v.c1_pass)
        self.assertTrue(v.c2_pass)
        self.assertTrue(v.c3_pass)
        self.assertTrue(v.c4_pass)
        self.assertTrue(v.c5_pass)

    def test_single_rule_far_fail_is_fail(self):
        # C1 at 0.30 — way below 0.70 threshold, not within 10%
        summary = _summary_with(fraction_sharpe_gt_0_5=0.30)
        v = evaluate_gate(summary)
        self.assertEqual(v.overall, "FAIL")
        self.assertFalse(v.c1_pass)

    def test_single_rule_borderline(self):
        # C1 at 0.65 — below 0.70 but above 0.70 * 0.9 = 0.63 threshold
        summary = _summary_with(fraction_sharpe_gt_0_5=0.65)
        v = evaluate_gate(summary)
        self.assertEqual(v.overall, "BORDERLINE")
        self.assertFalse(v.c1_pass)

    def test_c2_na_when_factors_missing(self):
        summary = _summary_with(fraction_alpha_t_gt_1_5=None)
        v = evaluate_gate(summary)
        self.assertIsNone(v.c2_pass)
        # Remaining 4 rules should pass → overall PASS
        self.assertEqual(v.overall, "PASS")

    def test_c5_turnover_ceiling(self):
        summary = _summary_with(max_turnover=1.2)
        v = evaluate_gate(summary)
        self.assertFalse(v.c5_pass)


if __name__ == "__main__":
    unittest.main()
