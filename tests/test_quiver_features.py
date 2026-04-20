"""Tests for alphalens.quiver_screener.features.

Feature functions operate on a NORMALIZED DataFrame schema so tests don't couple
to Quiver SDK response details. A separate normalizer (in client.py / fetch
script) is responsible for Quiver-raw → normalized conversion.

Normalized schemas:

congress_trades:
    ticker:          str
    date:            datetime (transaction date)
    representative:  str
    transaction:     str ('PURCHASE' | 'SALE' | 'EXCHANGE')
    amount_mid:      float ($, midpoint of disclosed range)

insider_trades:
    ticker:      str
    date:        datetime
    name:        str (insider name, unique person identifier)
    transaction: str ('A' = acquired/buy, 'D' = disposed/sell)
    shares:      int
    price:       float
    value:       float (shares * price)
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd


def _congress_trades(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["ticker", "date", "representative", "transaction", "amount_mid"],
    ).assign(date=lambda df: pd.to_datetime(df["date"]))


def _insider_trades(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["ticker", "date", "name", "transaction", "shares", "price", "value"],
    ).assign(date=lambda df: pd.to_datetime(df["date"]))


class TestCongressNetFlow(unittest.TestCase):
    def test_net_flow_is_buys_minus_sells_in_window(self):
        from alphalens.quiver_screener.features import congress_net_flow

        trades = _congress_trades([
            ("NVDA", "2024-01-10", "Pelosi",    "PURCHASE", 250_000),
            ("NVDA", "2024-01-15", "Crenshaw",  "SALE",      50_000),
            ("NVDA", "2024-01-20", "Tuberville", "PURCHASE", 100_000),
        ])
        result = congress_net_flow(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertAlmostEqual(result, 300_000.0, places=2)  # 250k + 100k - 50k

    def test_net_flow_excludes_trades_outside_window(self):
        from alphalens.quiver_screener.features import congress_net_flow

        trades = _congress_trades([
            ("NVDA", "2024-01-01", "Pelosi", "PURCHASE", 500_000),  # 30 days before as_of=2024-02-10 → inside [2024-01-11, 2024-02-10]
            ("NVDA", "2024-02-05", "Pelosi", "PURCHASE", 100_000),  # inside
        ])
        result = congress_net_flow(trades, "NVDA", pd.Timestamp("2024-02-10"), lookback_days=30)
        # 2024-01-01 is outside [2024-01-11, 2024-02-10] → excluded
        self.assertAlmostEqual(result, 100_000.0, places=2)

    def test_net_flow_ignores_other_tickers(self):
        from alphalens.quiver_screener.features import congress_net_flow

        trades = _congress_trades([
            ("NVDA", "2024-01-15", "Pelosi", "PURCHASE", 200_000),
            ("AMD",  "2024-01-16", "Pelosi", "PURCHASE", 500_000),  # different ticker
        ])
        result = congress_net_flow(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertAlmostEqual(result, 200_000.0, places=2)

    def test_net_flow_zero_when_no_trades(self):
        from alphalens.quiver_screener.features import congress_net_flow

        trades = _congress_trades([])
        result = congress_net_flow(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertEqual(result, 0.0)

    def test_exchange_transaction_contributes_zero(self):
        from alphalens.quiver_screener.features import congress_net_flow

        trades = _congress_trades([
            ("NVDA", "2024-01-10", "X", "EXCHANGE", 1_000_000),
            ("NVDA", "2024-01-11", "Y", "PURCHASE", 100_000),
        ])
        result = congress_net_flow(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertAlmostEqual(result, 100_000.0, places=2)


class TestCongressUniqueMembers(unittest.TestCase):
    def test_counts_distinct_representatives(self):
        from alphalens.quiver_screener.features import congress_unique_members

        trades = _congress_trades([
            ("NVDA", "2024-01-10", "Pelosi",     "PURCHASE", 100_000),
            ("NVDA", "2024-01-15", "Crenshaw",   "PURCHASE", 50_000),
            ("NVDA", "2024-01-20", "Tuberville", "PURCHASE", 200_000),
        ])
        result = congress_unique_members(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertEqual(result, 3)

    def test_same_representative_twice_counts_once(self):
        from alphalens.quiver_screener.features import congress_unique_members

        trades = _congress_trades([
            ("NVDA", "2024-01-10", "Pelosi", "PURCHASE", 100_000),
            ("NVDA", "2024-01-15", "Pelosi", "PURCHASE", 50_000),
            ("NVDA", "2024-01-20", "Pelosi", "SALE",    200_000),
        ])
        result = congress_unique_members(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertEqual(result, 1)

    def test_zero_when_no_trades(self):
        from alphalens.quiver_screener.features import congress_unique_members

        trades = _congress_trades([])
        result = congress_unique_members(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertEqual(result, 0)


class TestInsiderClusterFlag(unittest.TestCase):
    def test_true_when_3_plus_distinct_insider_buys_in_window(self):
        from alphalens.quiver_screener.features import insider_cluster_flag

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "CEO",  "A", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-10", "CFO",  "A",  500, 500.0, 250_000.0),
            ("NVDA", "2024-01-15", "COO",  "A",  300, 500.0, 150_000.0),
        ])
        result = insider_cluster_flag(
            trades, "NVDA", pd.Timestamp("2024-01-31"),
            lookback_days=30, min_insiders=3,
        )
        self.assertTrue(result)

    def test_false_when_only_2_distinct_insider_buys(self):
        from alphalens.quiver_screener.features import insider_cluster_flag

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "CEO", "A", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-10", "CFO", "A",  500, 500.0, 250_000.0),
        ])
        result = insider_cluster_flag(
            trades, "NVDA", pd.Timestamp("2024-01-31"),
            lookback_days=30, min_insiders=3,
        )
        self.assertFalse(result)

    def test_false_when_3_insiders_but_all_sells(self):
        """Cluster signal is BUY-specific. Three execs dumping is not a cluster buy."""
        from alphalens.quiver_screener.features import insider_cluster_flag

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "CEO", "D", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-10", "CFO", "D",  500, 500.0, 250_000.0),
            ("NVDA", "2024-01-15", "COO", "D",  300, 500.0, 150_000.0),
        ])
        result = insider_cluster_flag(
            trades, "NVDA", pd.Timestamp("2024-01-31"),
            lookback_days=30, min_insiders=3,
        )
        self.assertFalse(result)

    def test_same_insider_multiple_buys_counts_as_one(self):
        from alphalens.quiver_screener.features import insider_cluster_flag

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "CEO", "A", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-10", "CEO", "A",  500, 500.0, 250_000.0),
            ("NVDA", "2024-01-15", "CEO", "A",  300, 500.0, 150_000.0),
        ])
        result = insider_cluster_flag(
            trades, "NVDA", pd.Timestamp("2024-01-31"),
            lookback_days=30, min_insiders=3,
        )
        self.assertFalse(result)  # 3 buys, but 1 distinct insider

    def test_honours_custom_min_insiders(self):
        from alphalens.quiver_screener.features import insider_cluster_flag

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "A", "A", 100, 500.0, 50_000.0),
            ("NVDA", "2024-01-10", "B", "A", 100, 500.0, 50_000.0),
        ])
        result = insider_cluster_flag(
            trades, "NVDA", pd.Timestamp("2024-01-31"),
            lookback_days=30, min_insiders=2,
        )
        self.assertTrue(result)


class TestInsiderBuyRatio(unittest.TestCase):
    def test_ratio_is_buy_dollars_over_total_dollars_in_window(self):
        from alphalens.quiver_screener.features import insider_buy_ratio

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "CEO", "A", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-10", "CFO", "D",  500, 500.0, 250_000.0),
        ])
        result = insider_buy_ratio(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertAlmostEqual(result, 500_000 / (500_000 + 250_000), places=6)

    def test_all_buys_returns_one(self):
        from alphalens.quiver_screener.features import insider_buy_ratio

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "CEO", "A", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-10", "CFO", "A",  500, 500.0, 250_000.0),
        ])
        result = insider_buy_ratio(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertAlmostEqual(result, 1.0)

    def test_all_sells_returns_zero(self):
        from alphalens.quiver_screener.features import insider_buy_ratio

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "CEO", "D", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-10", "CFO", "D",  500, 500.0, 250_000.0),
        ])
        result = insider_buy_ratio(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertAlmostEqual(result, 0.0)

    def test_no_trades_returns_nan(self):
        from alphalens.quiver_screener.features import insider_buy_ratio

        trades = _insider_trades([])
        result = insider_buy_ratio(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertTrue(np.isnan(result))


class TestInsiderNetFlow(unittest.TestCase):
    def test_buys_minus_sells_by_dollar_value(self):
        from alphalens.quiver_screener.features import insider_net_flow

        trades = _insider_trades([
            ("NVDA", "2024-01-05", "CEO", "A", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-10", "CFO", "D",  400, 500.0, 200_000.0),
        ])
        result = insider_net_flow(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertAlmostEqual(result, 300_000.0, places=2)

    def test_zero_when_no_trades(self):
        from alphalens.quiver_screener.features import insider_net_flow

        trades = _insider_trades([])
        result = insider_net_flow(trades, "NVDA", pd.Timestamp("2024-01-31"), lookback_days=30)
        self.assertEqual(result, 0.0)


class TestInsiderFeaturePanel(unittest.TestCase):
    def test_builds_date_ticker_panel_with_net_flow(self):
        from alphalens.quiver_screener.features import build_insider_feature_panel

        trades = _insider_trades([
            ("NVDA", "2024-01-10", "CEO", "A", 1000, 500.0, 500_000.0),
            ("NVDA", "2024-01-11", "CFO", "D",  200, 500.0, 100_000.0),
            ("AMD",  "2024-01-12", "CEO", "A",  500, 200.0, 100_000.0),
        ])
        dates = pd.date_range("2024-01-15", "2024-01-16", freq="B")
        panel = build_insider_feature_panel(
            trades, tickers=["NVDA", "AMD", "INTC"], dates=dates,
            lookback_days=30, feature="net_flow",
        )
        self.assertEqual(list(panel.columns), ["NVDA", "AMD", "INTC"])
        self.assertAlmostEqual(panel.loc["2024-01-15", "NVDA"], 400_000.0)  # 500k buy - 100k sell
        self.assertAlmostEqual(panel.loc["2024-01-15", "AMD"], 100_000.0)
        self.assertAlmostEqual(panel.loc["2024-01-15", "INTC"], 0.0)


class TestFeaturePanelBuilder(unittest.TestCase):
    """Cross-sectional panel: (date × ticker) → feature value.

    Used to build factor time-series for run_regression IC test.
    """

    def test_builds_date_indexed_frame_with_ticker_columns(self):
        from alphalens.quiver_screener.features import build_congress_feature_panel

        trades = _congress_trades([
            ("NVDA", "2024-01-10", "X", "PURCHASE", 100_000),
            ("AMD",  "2024-01-12", "Y", "PURCHASE",  50_000),
        ])
        dates = pd.date_range("2024-01-15", "2024-01-20", freq="B")
        panel = build_congress_feature_panel(
            trades, tickers=["NVDA", "AMD", "INTC"], dates=dates, lookback_days=30,
        )
        self.assertEqual(list(panel.columns), ["NVDA", "AMD", "INTC"])
        self.assertEqual(len(panel), len(dates))
        self.assertAlmostEqual(panel.loc["2024-01-15", "NVDA"], 100_000.0)
        self.assertAlmostEqual(panel.loc["2024-01-15", "AMD"], 50_000.0)
        self.assertAlmostEqual(panel.loc["2024-01-15", "INTC"], 0.0)


if __name__ == "__main__":
    unittest.main()
