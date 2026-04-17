import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd


def _trending_df(slope: float = 0.05, days: int = 260) -> pd.DataFrame:
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    close = np.array([10.0 * (1 + slope) ** (i / days * 20) for i in range(days)])
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": [2_000_000.0] * days,
        },
        index=idx,
    )


def _flat_df(price: float = 10.0, days: int = 260) -> pd.DataFrame:
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    return pd.DataFrame(
        {
            "Open": [price] * days,
            "High": [price] * days,
            "Low": [price] * days,
            "Close": [price] * days,
            "Volume": [2_000_000.0] * days,
        },
        index=idx,
    )


class TestPipelineRun(unittest.TestCase):
    def setUp(self):
        self.themes = {"quantum": ["QUBT", "RGTI"], "ai": ["BBAI"]}
        self.prices = {
            "QUBT": _trending_df(slope=0.10),
            "RGTI": _trending_df(slope=0.06),
            "BBAI": _flat_df(),
            "SPY": _flat_df(),
        }
        self.fundamentals = {
            "QUBT": {"marketCap": 1_500_000_000, "averageVolume": 5_000_000},
            "RGTI": {"marketCap": 1_000_000_000, "averageVolume": 3_000_000},
            "BBAI": {"marketCap": 500_000_000, "averageVolume": 2_000_000},
        }

    def _build_pipeline(self):
        from alphalens.momentum_screener.pipeline import MomentumPipeline

        return MomentumPipeline()

    @patch("alphalens.momentum_screener.pipeline.BatchDataFetcher")
    @patch("alphalens.momentum_screener.pipeline.load_universe")
    def test_returns_top_n_ranked_by_momentum(self, mock_load, mock_fetcher_cls):
        mock_load.return_value = self.themes
        fetcher = mock_fetcher_cls.return_value
        fetcher.fetch_prices.return_value = self.prices
        fetcher.fetch_fundamentals.return_value = self.fundamentals

        pipeline = self._build_pipeline()
        result = pipeline.run(curr_date="2026-04-17", top_n=2)

        self.assertEqual(len(result), 2)
        self.assertIn("momentum_score", result.columns)
        # Sorted descending
        scores = result["momentum_score"].tolist()
        self.assertEqual(scores, sorted(scores, reverse=True))

    @patch("alphalens.momentum_screener.pipeline.BatchDataFetcher")
    @patch("alphalens.momentum_screener.pipeline.load_universe")
    def test_includes_theme_membership_in_output(self, mock_load, mock_fetcher_cls):
        mock_load.return_value = self.themes
        fetcher = mock_fetcher_cls.return_value
        fetcher.fetch_prices.return_value = self.prices
        fetcher.fetch_fundamentals.return_value = self.fundamentals

        result = self._build_pipeline().run(curr_date="2026-04-17", top_n=5)
        self.assertIn("themes", result.columns)
        qubt_row = result[result.ticker == "QUBT"]
        if not qubt_row.empty:
            self.assertEqual(qubt_row["themes"].iloc[0], ["quantum"])

    @patch("alphalens.momentum_screener.pipeline.BatchDataFetcher")
    @patch("alphalens.momentum_screener.pipeline.load_universe")
    def test_filters_out_guardrail_failures(self, mock_load, mock_fetcher_cls):
        mock_load.return_value = self.themes
        # BBAI fails guardrail (cap below $300M? Actually it's $500M, passes.
        # Force fail by removing price data).
        self.prices.pop("BBAI")
        fetcher = mock_fetcher_cls.return_value
        fetcher.fetch_prices.return_value = self.prices
        fetcher.fetch_fundamentals.return_value = self.fundamentals

        result = self._build_pipeline().run(curr_date="2026-04-17", top_n=5)
        self.assertNotIn("BBAI", result["ticker"].tolist())

    @patch("alphalens.momentum_screener.pipeline.BatchDataFetcher")
    @patch("alphalens.momentum_screener.pipeline.load_universe")
    def test_adds_benchmark_ticker_to_fetch_list(self, mock_load, mock_fetcher_cls):
        mock_load.return_value = self.themes
        fetcher = mock_fetcher_cls.return_value
        fetcher.fetch_prices.return_value = self.prices
        fetcher.fetch_fundamentals.return_value = self.fundamentals

        self._build_pipeline().run(curr_date="2026-04-17", top_n=5)

        # BatchDataFetcher should have been called with tickers + benchmark
        args, kwargs = mock_fetcher_cls.call_args
        called_tickers = args[0] if args else kwargs.get("tickers", [])
        self.assertIn("SPY", called_tickers)

    @patch("alphalens.momentum_screener.pipeline.BatchDataFetcher")
    @patch("alphalens.momentum_screener.pipeline.load_universe")
    def test_empty_universe_returns_empty_frame(self, mock_load, mock_fetcher_cls):
        mock_load.return_value = {}
        fetcher = mock_fetcher_cls.return_value
        fetcher.fetch_prices.return_value = {}
        fetcher.fetch_fundamentals.return_value = {}

        result = self._build_pipeline().run(curr_date="2026-04-17", top_n=5)
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()
