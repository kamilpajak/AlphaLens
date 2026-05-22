import unittest
from unittest.mock import patch

import pandas as pd


class TestPrescreenerPipeline(unittest.TestCase):
    @patch("alphalens_research.screeners.prescreener.integration.BatchDataFetcher")
    @patch("alphalens_research.screeners.prescreener.integration.get_sp500_tickers")
    def test_screen_returns_ranked_dataframe(self, mock_universe, mock_fetcher_cls):
        from alphalens_research.screeners.prescreener.integration import PrescreenerPipeline

        mock_universe.return_value = ["AAPL", "MSFT"]
        fetcher = mock_fetcher_cls.return_value

        dates = pd.bdate_range(end="2024-01-15", periods=250)
        prices = pd.DataFrame(
            {
                "Open": [100.0] * 250,
                "High": [101.0] * 250,
                "Low": [99.0] * 250,
                "Close": [100.0] * 250,
                "Volume": [1_000_000] * 250,
            },
            index=dates,
        )
        fetcher.fetch_prices.return_value = {"AAPL": prices, "MSFT": prices}
        fetcher.fetch_fundamentals.return_value = {
            "AAPL": {"trailingPE": 20, "marketCap": 3e12, "averageVolume": 50e6},
            "MSFT": {"trailingPE": 30, "marketCap": 2e12, "averageVolume": 30e6},
        }

        pipeline = PrescreenerPipeline("2024-01-15")
        result = pipeline.screen()
        self.assertIsInstance(result, pd.DataFrame)
        self.assertIn("composite_score", result.columns)
        self.assertIn("rank", result.columns)
        self.assertEqual(len(result), 2)

    @patch("alphalens_research.screeners.prescreener.integration.BatchDataFetcher")
    @patch("alphalens_research.screeners.prescreener.integration.get_sp500_tickers")
    def test_screen_with_custom_tickers(self, mock_universe, mock_fetcher_cls):
        from alphalens_research.screeners.prescreener.integration import PrescreenerPipeline

        fetcher = mock_fetcher_cls.return_value
        dates = pd.bdate_range(end="2024-01-15", periods=250)
        prices = pd.DataFrame(
            {
                "Open": [100.0] * 250,
                "High": [101.0] * 250,
                "Low": [99.0] * 250,
                "Close": [100.0] * 250,
                "Volume": [1_000_000] * 250,
            },
            index=dates,
        )
        fetcher.fetch_prices.return_value = {"TSLA": prices}
        fetcher.fetch_fundamentals.return_value = {
            "TSLA": {"trailingPE": 50, "marketCap": 800e9, "averageVolume": 100e6}
        }

        pipeline = PrescreenerPipeline("2024-01-15", tickers=["TSLA"])
        result = pipeline.screen()
        mock_universe.assert_not_called()
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
