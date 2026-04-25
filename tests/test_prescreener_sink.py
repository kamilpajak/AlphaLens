"""Phase B: prescreener exposes `to_candidates()` and no longer constructs TradingAgentsGraph."""

import unittest
from unittest.mock import patch

import pandas as pd


class TestPrescreenerToCandidates(unittest.TestCase):
    def test_to_candidates_emits_prescreener_candidates(self):
        from alphalens.candidates import Candidate
        from alphalens.screeners.prescreener.integration import PrescreenerPipeline

        df = pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "rank": 1,
                    "composite_score": 0.87,
                    "technical_score": 0.9,
                    "fundamental_score": 0.8,
                    "volume_score": 0.85,
                },
                {
                    "ticker": "MSFT",
                    "rank": 2,
                    "composite_score": 0.78,
                    "technical_score": 0.7,
                    "fundamental_score": 0.75,
                    "volume_score": 0.85,
                },
            ]
        )

        pipeline = PrescreenerPipeline("2026-04-17")
        candidates = pipeline.to_candidates(df)
        self.assertEqual(len(candidates), 2)

        for c in candidates:
            self.assertIsInstance(c, Candidate)
            self.assertEqual(c.source, "prescreener")
            self.assertEqual(c.priority, 20)

        first = candidates[0]
        self.assertEqual(first.ticker, "AAPL")
        self.assertAlmostEqual(first.payload["composite_score"], 0.87)
        self.assertEqual(first.payload["rank"], 1)

    def test_prescreener_no_longer_imports_tradingagents_graph(self):
        """The integration module must not construct TradingAgentsGraph directly anymore."""
        import alphalens.screeners.prescreener.integration as integ

        self.assertFalse(
            hasattr(integ, "TradingAgentsGraph"),
            "PrescreenerPipeline should hand off via sink, not own TradingAgentsGraph",
        )

    def test_screen_and_analyze_removed(self):
        from alphalens.screeners.prescreener.integration import PrescreenerPipeline

        pipeline = PrescreenerPipeline("2026-04-17")
        self.assertFalse(
            hasattr(pipeline, "screen_and_analyze"),
            "screen_and_analyze() replaced by screen() + to_candidates() + sink",
        )

    @patch("alphalens.screeners.prescreener.integration.BatchDataFetcher")
    @patch("alphalens.screeners.prescreener.integration.get_sp500_tickers")
    def test_screen_still_returns_dataframe_only(self, mock_universe, mock_fetcher_cls):
        """screen() must remain a pure ranking step — no TA calls, no network side effects."""
        from alphalens.screeners.prescreener.integration import PrescreenerPipeline

        mock_universe.return_value = ["AAPL"]
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
        fetcher.fetch_prices.return_value = {"AAPL": prices}
        fetcher.fetch_fundamentals.return_value = {
            "AAPL": {"trailingPE": 20, "marketCap": 3e12, "averageVolume": 50e6}
        }

        result = PrescreenerPipeline("2024-01-15").screen()
        self.assertIsInstance(result, pd.DataFrame)
        self.assertIn("composite_score", result.columns)


if __name__ == "__main__":
    unittest.main()
