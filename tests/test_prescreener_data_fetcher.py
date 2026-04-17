import unittest
from unittest.mock import MagicMock, patch

import pandas as pd


class TestBatchDataFetcher(unittest.TestCase):
    def setUp(self):
        self.tickers = ["AAPL", "MSFT", "GOOGL"]
        self.curr_date = "2024-01-15"

    @patch("alphalens.prescreener.data_fetcher.yf.download")
    def test_fetch_prices_returns_dict_of_dataframes(self, mock_download):
        from alphalens.prescreener.data_fetcher import BatchDataFetcher

        dates = pd.bdate_range("2023-01-01", "2024-01-15")
        n = len(dates)
        cols = pd.MultiIndex.from_product(
            [["Close", "High", "Low", "Open", "Volume"], ["AAPL", "MSFT", "GOOGL"]]
        )
        mock_data = pd.DataFrame(100.0, index=dates, columns=cols)
        mock_download.return_value = mock_data

        fetcher = BatchDataFetcher(self.tickers, self.curr_date)
        result = fetcher.fetch_prices()
        self.assertIsInstance(result, dict)
        self.assertIn("AAPL", result)
        self.assertIsInstance(result["AAPL"], pd.DataFrame)

    @patch("alphalens.prescreener.data_fetcher.yf.download")
    def test_no_look_ahead_bias(self, mock_download):
        from alphalens.prescreener.data_fetcher import BatchDataFetcher

        dates = pd.bdate_range("2023-12-01", "2024-02-15")
        cols = pd.MultiIndex.from_product(
            [["Close", "High", "Low", "Open", "Volume"], ["AAPL"]],
            names=["Price", "Ticker"],
        )
        mock_data = pd.DataFrame(100.0, index=dates, columns=cols)
        mock_download.return_value = mock_data

        fetcher = BatchDataFetcher(["AAPL"], self.curr_date)
        result = fetcher.fetch_prices()
        max_date = result["AAPL"].index.max()
        self.assertLessEqual(max_date, pd.Timestamp(self.curr_date))

    @patch("alphalens.prescreener.data_fetcher.yf.Ticker")
    def test_fetch_fundamentals_returns_dict(self, mock_ticker_cls):
        from alphalens.prescreener.data_fetcher import BatchDataFetcher

        mock_instance = MagicMock()
        mock_instance.info = {"trailingPE": 25.0, "pegRatio": 1.2, "marketCap": 3e12}
        mock_ticker_cls.return_value = mock_instance

        fetcher = BatchDataFetcher(["AAPL"], self.curr_date)
        result = fetcher.fetch_fundamentals()
        self.assertIsInstance(result, dict)
        self.assertIn("AAPL", result)
        self.assertEqual(result["AAPL"]["trailingPE"], 25.0)

    @patch("alphalens.prescreener.data_fetcher.yf.Ticker")
    def test_fetch_fundamentals_handles_error(self, mock_ticker_cls):
        from alphalens.prescreener.data_fetcher import BatchDataFetcher

        mock_ticker_cls.return_value.info = {}
        mock_ticker_cls.side_effect = [Exception("API error")]

        fetcher = BatchDataFetcher(["BAD"], self.curr_date)
        result = fetcher.fetch_fundamentals()
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("BAD", {}), {})


if __name__ == "__main__":
    unittest.main()
