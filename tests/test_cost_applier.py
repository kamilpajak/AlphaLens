"""TDD for CostApplier — applies PerTickerCostModel to BacktestReport."""

import unittest
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd


def _report_with_trades(portfolio_value: float = 100_000.0):
    """Construct a minimal BacktestReport with 2 days of trades for testing."""
    from alphalens.backtest.engine import BacktestReport, DailyResult, Trade

    day1 = pd.Timestamp("2024-04-01")
    day2 = pd.Timestamp("2024-04-02")

    report = BacktestReport(
        scorer_config={},
        holding_period=5,
        top_n=2,
        start=date(2024, 4, 1),
        end=date(2024, 4, 2),
        benchmark="SPY",
        universe_ticker_count=3,
        portfolio_value=portfolio_value,
    )

    report.daily_results = [
        DailyResult(
            date=day1,
            scored_count=2,
            top_n_tickers=["AAA", "BBB"],
            top_n_scores=[1.0, 0.5],
            top_n_forward_returns=[0.02, 0.01],
            portfolio_return=0.015,
            portfolio_return_holding=0.05,
            universe_median_return=0.01,
            ic=0.7,
            top_n_weights=[0.5, 0.5],
            trades=[
                Trade(date=day1, ticker="AAA", notional=50_000.0, direction="enter", price=100.0),
                Trade(date=day1, ticker="BBB", notional=50_000.0, direction="enter", price=50.0),
            ],
        ),
        DailyResult(
            date=day2,
            scored_count=2,
            top_n_tickers=["AAA", "CCC"],
            top_n_scores=[1.0, 0.5],
            top_n_forward_returns=[0.02, 0.03],
            portfolio_return=0.025,
            portfolio_return_holding=0.06,
            universe_median_return=0.015,
            ic=0.6,
            top_n_weights=[0.5, 0.5],
            trades=[
                Trade(date=day2, ticker="BBB", notional=50_000.0, direction="exit", price=50.5),
                Trade(date=day2, ticker="CCC", notional=50_000.0, direction="enter", price=75.0),
            ],
        ),
    ]
    return report


class TestCostApplierBasic(unittest.TestCase):
    def test_returns_application_result(self):
        from alphalens.backtest.cost_applier import CostApplier
        from alphalens.backtest.cost_model import PerTickerCostModel

        report = _report_with_trades()
        chars = MagicMock()
        chars.spread_at.return_value = 0.005  # 50 bps in decimal
        chars.volatility_at.return_value = 0.02
        chars.adv_dollar_at.return_value = 10_000_000.0

        applier = CostApplier(
            market_chars=chars,
            cost_model=PerTickerCostModel(commission_per_share=0.0, impact_kappa=0.1),
        )
        result = applier.apply(report)

        self.assertIsNotNone(result.net_returns)
        self.assertIsInstance(result.net_returns, pd.Series)
        self.assertEqual(len(result.net_returns), 2)
        self.assertIsInstance(result.per_ticker_breakdown, pd.DataFrame)
        self.assertIsInstance(result.per_theme_breakdown, pd.DataFrame)

    def test_net_returns_equal_gross_minus_cost(self):
        from alphalens.backtest.cost_applier import CostApplier
        from alphalens.backtest.cost_model import PerTickerCostModel

        report = _report_with_trades()
        chars = MagicMock()
        chars.spread_at.return_value = 0.005
        chars.volatility_at.return_value = 0.02
        chars.adv_dollar_at.return_value = 10_000_000.0

        applier = CostApplier(
            market_chars=chars,
            cost_model=PerTickerCostModel(commission_per_share=0.0, impact_kappa=0.0),
        )
        result = applier.apply(report)

        # Pure spread mode: per trade = 50 bps / 2 × notional = $125; 2 trades/day = $250
        # Net = gross − 250/100000 = gross − 0.0025
        expected_cost_fraction = 0.0025
        self.assertAlmostEqual(
            result.net_returns.iloc[0], 0.015 - expected_cost_fraction, places=6
        )
        self.assertAlmostEqual(
            result.net_returns.iloc[1], 0.025 - expected_cost_fraction, places=6
        )


class TestCostApplierBreakdown(unittest.TestCase):
    def test_per_ticker_breakdown_aggregates_correctly(self):
        from alphalens.backtest.cost_applier import CostApplier
        from alphalens.backtest.cost_model import PerTickerCostModel

        report = _report_with_trades()
        chars = MagicMock()
        chars.spread_at.return_value = 0.005
        chars.volatility_at.return_value = 0.02
        chars.adv_dollar_at.return_value = 10_000_000.0

        applier = CostApplier(
            market_chars=chars,
            cost_model=PerTickerCostModel(commission_per_share=0.0, impact_kappa=0.0),
        )
        result = applier.apply(report)
        df = result.per_ticker_breakdown

        # AAA enters once (day 1), no exit → 1 enter, 0 exit.
        row_aaa = df[df["ticker"] == "AAA"].iloc[0]
        self.assertEqual(int(row_aaa["enter_count"]), 1)
        self.assertEqual(int(row_aaa["exit_count"]), 0)

        # BBB enters day 1, exits day 2 → 1 enter, 1 exit.
        row_bbb = df[df["ticker"] == "BBB"].iloc[0]
        self.assertEqual(int(row_bbb["enter_count"]), 1)
        self.assertEqual(int(row_bbb["exit_count"]), 1)

        # Each trade costs $125; BBB has 2 trades = $250
        self.assertAlmostEqual(float(row_bbb["total_cost_usd"]), 250.0, places=2)

    def test_per_theme_breakdown_when_theme_map_provided(self):
        from alphalens.backtest.cost_applier import CostApplier
        from alphalens.backtest.cost_model import PerTickerCostModel

        report = _report_with_trades()
        chars = MagicMock()
        chars.spread_at.return_value = 0.005
        chars.volatility_at.return_value = 0.02
        chars.adv_dollar_at.return_value = 10_000_000.0

        theme_map = {"AAA": "theme_x", "BBB": "theme_y", "CCC": "theme_y"}

        applier = CostApplier(
            market_chars=chars,
            cost_model=PerTickerCostModel(commission_per_share=0.0, impact_kappa=0.0),
            theme_map=theme_map,
        )
        result = applier.apply(report)
        df = result.per_theme_breakdown

        self.assertIn("theme_x", df["theme"].tolist())
        self.assertIn("theme_y", df["theme"].tolist())
        total_cost = df["total_cost_usd"].sum()
        # 4 trades × $125 = $500
        self.assertAlmostEqual(total_cost, 500.0, places=2)


class TestCostApplierFallback(unittest.TestCase):
    def test_missing_spread_uses_fallback(self):
        from alphalens.backtest.cost_applier import CostApplier
        from alphalens.backtest.cost_model import PerTickerCostModel

        report = _report_with_trades()
        chars = MagicMock()
        # Spread not available for any ticker.
        chars.spread_at.return_value = None
        chars.volatility_at.return_value = 0.02
        chars.adv_dollar_at.return_value = 10_000_000.0

        applier = CostApplier(
            market_chars=chars,
            cost_model=PerTickerCostModel(commission_per_share=0.0, impact_kappa=0.0, min_spread_bps=0.0),
            fallback_spread_bps=100.0,  # 100 bps fallback
        )
        result = applier.apply(report)
        # Cost per trade = 100 bps / 2 × 50_000 = $250; 2 trades/day = $500
        self.assertAlmostEqual(
            result.net_returns.iloc[0], 0.015 - 0.005, places=6
        )


if __name__ == "__main__":
    unittest.main()
