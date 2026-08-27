import unittest
from unittest.mock import MagicMock, patch

import pandas as pd


class TestPrescreenerConfig(unittest.TestCase):
    def test_weights_sum_to_one(self):
        from alphalens_research.screeners.prescreener.config import PRESCREENER_DEFAULTS

        total = (
            PRESCREENER_DEFAULTS["weight_fundamental"]
            + PRESCREENER_DEFAULTS["weight_technical"]
            + PRESCREENER_DEFAULTS["weight_volume"]
        )
        self.assertAlmostEqual(total, 1.0)

    def test_thresholds_are_positive(self):
        from alphalens_research.screeners.prescreener.config import PRESCREENER_DEFAULTS

        for key in [
            "pe_max",
            "peg_max",
            "roe_min",
            "debt_ebitda_max",
            "eps_growth_min",
        ]:
            self.assertGreater(PRESCREENER_DEFAULTS[key], 0, f"{key} must be positive")

    def test_top_n_is_reasonable(self):
        from alphalens_research.screeners.prescreener.config import PRESCREENER_DEFAULTS

        self.assertGreaterEqual(PRESCREENER_DEFAULTS["top_n"], 5)
        self.assertLessEqual(PRESCREENER_DEFAULTS["top_n"], 50)


class TestSP500Universe(unittest.TestCase):
    @patch("requests.get")
    @patch("alphalens_research.screeners.prescreener.universe.pd.read_html")
    def test_fetch_returns_list_of_strings(self, mock_read_html, mock_get):
        """Both halves of the fetch are stubbed on purpose.

        Patching only ``pd.read_html`` left the REAL ``requests.get`` to
        Wikipedia running on every suite run — invisibly, because the mocked
        parse discarded the page anyway. The no-live-network guard (#1179) is
        what surfaced it: the request then raised, ``get_sp500_tickers``'s broad
        except swallowed it, and the assertion saw the 503-ticker fallback."""
        from alphalens_research.screeners.prescreener.universe import get_sp500_tickers

        mock_get.return_value = MagicMock(text="<table></table>")
        mock_read_html.return_value = [pd.DataFrame({"Symbol": ["AAPL", "MSFT", "GOOGL"]})]
        tickers = get_sp500_tickers()
        self.assertIsInstance(tickers, list)
        self.assertTrue(all(isinstance(t, str) for t in tickers))
        self.assertEqual(len(tickers), 3)

    @patch(
        "alphalens_research.screeners.prescreener.universe.pd.read_html",
        side_effect=Exception("Network error"),
    )
    def test_fallback_on_network_error(self, mock_read_html):
        from alphalens_research.screeners.prescreener.universe import get_sp500_tickers

        tickers = get_sp500_tickers()
        self.assertGreater(len(tickers), 400)

    def test_no_duplicates_in_fallback(self):
        from alphalens_research.screeners.prescreener.universe import SP500_FALLBACK

        self.assertEqual(len(SP500_FALLBACK), len(set(SP500_FALLBACK)))

    def test_tickers_use_hyphen_not_dot(self):
        from alphalens_research.screeners.prescreener.universe import SP500_FALLBACK

        for t in SP500_FALLBACK:
            self.assertNotRegex(t, r"\.[A-Z]$", f"{t} should use hyphen not dot")

    def test_fallback_contains_known_tickers(self):
        from alphalens_research.screeners.prescreener.universe import SP500_FALLBACK

        for ticker in ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]:
            self.assertIn(ticker, SP500_FALLBACK)


if __name__ == "__main__":
    unittest.main()
