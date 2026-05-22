import math
import unittest

import numpy as np


class TestSharpe(unittest.TestCase):
    def test_constant_return_is_zero_sharpe(self):
        from alphalens_research.backtest.metrics import sharpe

        self.assertEqual(sharpe([0.01] * 100), 0.0)

    def test_annualization(self):
        """Daily returns with mean 0.001, std 0.01 → annualised Sharpe ≈ √252 × 0.1."""
        from alphalens_research.backtest.metrics import sharpe

        rng = np.random.default_rng(0)
        returns = rng.normal(0.001, 0.01, 10_000)
        result = sharpe(returns)
        self.assertAlmostEqual(result, math.sqrt(252) * 0.1, delta=0.2)

    def test_empty_or_short_returns_zero(self):
        from alphalens_research.backtest.metrics import sharpe

        self.assertEqual(sharpe([]), 0.0)
        self.assertEqual(sharpe([0.01]), 0.0)

    def test_negative_sharpe_when_losing(self):
        from alphalens_research.backtest.metrics import sharpe

        rng = np.random.default_rng(1)
        returns = rng.normal(-0.001, 0.01, 1000)
        self.assertLess(sharpe(returns), 0)


class TestRankIC(unittest.TestCase):
    def test_perfect_rank_correlation(self):
        from alphalens_research.backtest.metrics import rank_ic

        # Ascending scores, ascending returns → IC = 1.
        scores = [1, 2, 3, 4, 5]
        returns = [0.01, 0.02, 0.03, 0.04, 0.05]
        self.assertAlmostEqual(rank_ic(scores, returns), 1.0)

    def test_inverse_correlation(self):
        from alphalens_research.backtest.metrics import rank_ic

        self.assertAlmostEqual(rank_ic([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]), -1.0)

    def test_zero_for_constant(self):
        from alphalens_research.backtest.metrics import rank_ic

        self.assertEqual(rank_ic([1, 1, 1, 1], [1, 2, 3, 4]), 0.0)

    def test_empty_returns_zero(self):
        from alphalens_research.backtest.metrics import rank_ic

        self.assertEqual(rank_ic([], []), 0.0)


class TestRankICTStat(unittest.TestCase):
    def test_zero_mean_returns_zero(self):
        from alphalens_research.backtest.metrics import rank_ic_tstat

        self.assertAlmostEqual(rank_ic_tstat([0.01, -0.01, 0.01, -0.01] * 50), 0.0, places=5)

    def test_strong_positive_signal(self):
        from alphalens_research.backtest.metrics import rank_ic_tstat

        rng = np.random.default_rng(0)
        # High mean vs modest noise → t-stat should be well above 2.
        ic = rng.normal(0.05, 0.05, 250)
        self.assertGreater(rank_ic_tstat(ic), 2.0)

    def test_short_series_returns_zero(self):
        from alphalens_research.backtest.metrics import rank_ic_tstat

        self.assertEqual(rank_ic_tstat([0.03]), 0.0)


class TestRankICRolling(unittest.TestCase):
    def test_rolling_matches_expected(self):
        from alphalens_research.backtest.metrics import rank_ic_rolling

        s = rank_ic_rolling([0.0, 1.0, 2.0, 3.0, 4.0], window=3)
        # Last window: mean of [2, 3, 4] = 3
        self.assertAlmostEqual(s.iloc[-1], 3.0)


class TestRankICPositivePct(unittest.TestCase):
    def test_all_positive(self):
        from alphalens_research.backtest.metrics import rank_ic_positive_pct

        self.assertAlmostEqual(rank_ic_positive_pct([0.1] * 50, window=5), 1.0)

    def test_all_negative(self):
        from alphalens_research.backtest.metrics import rank_ic_positive_pct

        self.assertAlmostEqual(rank_ic_positive_pct([-0.1] * 50, window=5), 0.0)


class TestDecileSpread(unittest.TestCase):
    def test_positive_alignment(self):
        from alphalens_research.backtest.metrics import decile_spread

        # Top-scored names also top-returning → positive spread.
        scores = list(range(1, 21))
        returns = [i * 0.01 for i in range(1, 21)]
        self.assertGreater(decile_spread(scores, returns, n_deciles=5), 0)

    def test_negative_alignment(self):
        from alphalens_research.backtest.metrics import decile_spread

        scores = list(range(1, 21))
        returns = [(21 - i) * 0.01 for i in range(1, 21)]
        self.assertLess(decile_spread(scores, returns, n_deciles=5), 0)

    def test_empty_returns_zero(self):
        from alphalens_research.backtest.metrics import decile_spread

        self.assertEqual(decile_spread([], []), 0.0)


class TestHitRate(unittest.TestCase):
    def test_beats_every_day(self):
        from alphalens_research.backtest.metrics import hit_rate

        self.assertAlmostEqual(hit_rate([0.05] * 10, [0.01] * 10), 1.0)

    def test_loses_every_day(self):
        from alphalens_research.backtest.metrics import hit_rate

        self.assertAlmostEqual(hit_rate([0.0] * 10, [0.01] * 10), 0.0)

    def test_mixed(self):
        from alphalens_research.backtest.metrics import hit_rate

        self.assertAlmostEqual(hit_rate([0.02, 0.00, 0.03, -0.01], [0.01, 0.01, 0.01, 0.01]), 0.5)


class TestTurnover(unittest.TestCase):
    def test_full_turnover(self):
        from alphalens_research.backtest.metrics import turnover_pct

        t = turnover_pct([["A", "B", "C"], ["X", "Y", "Z"]])
        self.assertAlmostEqual(t, 1.0)

    def test_no_turnover(self):
        from alphalens_research.backtest.metrics import turnover_pct

        t = turnover_pct([["A", "B", "C"], ["A", "B", "C"], ["A", "B", "C"]])
        self.assertAlmostEqual(t, 0.0)

    def test_partial_turnover(self):
        from alphalens_research.backtest.metrics import turnover_pct

        t = turnover_pct([["A", "B", "C", "D"], ["A", "B", "X", "Y"]])
        self.assertAlmostEqual(t, 0.5)

    def test_empty_or_single_snapshot(self):
        from alphalens_research.backtest.metrics import turnover_pct

        self.assertEqual(turnover_pct([]), 0.0)
        self.assertEqual(turnover_pct([["A", "B"]]), 0.0)


class TestMaxDrawdown(unittest.TestCase):
    def test_monotonic_up_no_drawdown(self):
        from alphalens_research.backtest.metrics import max_drawdown

        self.assertAlmostEqual(max_drawdown([1.0, 1.1, 1.2, 1.3]), 0.0)

    def test_peak_to_trough(self):
        from alphalens_research.backtest.metrics import max_drawdown

        # Peak 1.20 → trough 0.90 → drawdown = -25%
        self.assertAlmostEqual(max_drawdown([1.0, 1.2, 1.0, 0.9, 1.0]), -0.25, places=6)

    def test_empty_returns_zero(self):
        from alphalens_research.backtest.metrics import max_drawdown

        self.assertEqual(max_drawdown([]), 0.0)


class TestCalmarRatio(unittest.TestCase):
    def test_positive_calmar_when_profitable_with_dd(self):
        from alphalens_research.backtest.metrics import calmar_ratio

        rng = np.random.default_rng(0)
        returns = rng.normal(0.001, 0.01, 1000)
        self.assertIsInstance(calmar_ratio(returns), float)


class TestConcentration(unittest.TestCase):
    def test_equal_weight_30_names_top5_is_167pct(self):
        from alphalens_research.backtest.metrics import concentration_top_k

        weights = [1 / 30] * 30
        self.assertAlmostEqual(concentration_top_k(weights, k=5), 5 / 30, places=6)

    def test_concentrated_portfolio(self):
        from alphalens_research.backtest.metrics import concentration_top_k

        weights = [0.9, 0.01, 0.01, 0.01, 0.07]
        # Top-1 = 0.9 / 1.0 = 0.9
        self.assertAlmostEqual(concentration_top_k(weights, k=1), 0.9)

    def test_empty_returns_zero(self):
        from alphalens_research.backtest.metrics import concentration_top_k

        self.assertEqual(concentration_top_k([], k=5), 0.0)


class TestSummarisePortfolio(unittest.TestCase):
    def test_returns_sane_summary(self):
        from alphalens_research.backtest.metrics import summarise_portfolio

        rng = np.random.default_rng(0)
        port = rng.normal(0.001, 0.01, 252)
        median = rng.normal(0.0005, 0.01, 252)
        summary = summarise_portfolio(port, median)

        self.assertEqual(summary.days, 252)
        self.assertIsInstance(summary.sharpe, float)
        self.assertLessEqual(summary.max_drawdown, 0.0)
        self.assertGreaterEqual(summary.hit_rate, 0.0)
        self.assertLessEqual(summary.hit_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
