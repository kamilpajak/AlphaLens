"""Tests for fundamentals fetcher: Alpha Vantage wrapper + feature extraction.

The fetcher has two layers:
  1. `fetch_ticker_bundle(ticker, curr_date)` — calls upstream AV endpoints,
     returns a dict with keys 'overview', 'balance_sheet', 'cash_flow',
     'income_statement'. PIT-safe via curr_date filter.
  2. `extract_features(bundle)` — pure normalization from AV schema to
     canonical feature dict expected by gate.py.

Tests mock the upstream calls so we never hit the live API.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

OVERVIEW_FIXTURE = {
    "Symbol": "AAPL",
    "MarketCapitalization": "3000000000000",
    "RevenueTTM": "400000000000",
    "PriceToSalesRatioTTM": "7.5",
    "NetIncomeTTM": "100000000000",
}

BALANCE_SHEET_PREPROFIT = {
    "symbol": "INVZ",
    "quarterlyReports": [
        {
            "fiscalDateEnding": "2024-09-30",
            "cashAndShortTermInvestments": "50000000",  # $50M
        },
        {
            "fiscalDateEnding": "2024-06-30",
            "cashAndShortTermInvestments": "70000000",
        },
    ],
}

CASH_FLOW_PREPROFIT = {
    "symbol": "INVZ",
    "quarterlyReports": [
        {"fiscalDateEnding": "2024-09-30", "operatingCashflow": "-20000000"},
        {"fiscalDateEnding": "2024-06-30", "operatingCashflow": "-18000000"},
        {"fiscalDateEnding": "2024-03-31", "operatingCashflow": "-15000000"},
        {"fiscalDateEnding": "2023-12-31", "operatingCashflow": "-12000000"},
        {"fiscalDateEnding": "2023-09-30", "operatingCashflow": "-10000000"},
    ],
}

INCOME_STATEMENT_PREPROFIT = {
    "symbol": "INVZ",
    "quarterlyReports": [
        {"fiscalDateEnding": "2024-09-30", "netIncome": "-25000000"},
        {"fiscalDateEnding": "2024-06-30", "netIncome": "-22000000"},
        {"fiscalDateEnding": "2024-03-31", "netIncome": "-20000000"},
        {"fiscalDateEnding": "2023-12-31", "netIncome": "-18000000"},
    ],
}


class TestExtractFeatures(unittest.TestCase):
    def test_extract_profitable_company_features(self):
        from alphalens.fundamentals.fetcher import extract_features

        bundle = {
            "overview": OVERVIEW_FIXTURE,
            "balance_sheet": {
                "quarterlyReports": [
                    {
                        "fiscalDateEnding": "2024-09-30",
                        "cashAndShortTermInvestments": "50000000000",
                    },
                ]
            },
            "cash_flow": {
                "quarterlyReports": [
                    {
                        "fiscalDateEnding": "2024-09-30",
                        "operatingCashflow": "25000000000",
                    },
                ]
            },
            "income_statement": {
                "quarterlyReports": [
                    {"fiscalDateEnding": "2024-09-30", "netIncome": "24000000000"},
                    {"fiscalDateEnding": "2024-06-30", "netIncome": "22000000000"},
                    {"fiscalDateEnding": "2024-03-31", "netIncome": "26000000000"},
                    {"fiscalDateEnding": "2023-12-31", "netIncome": "28000000000"},
                ]
            },
        }

        f = extract_features(bundle)
        self.assertAlmostEqual(f["ps_ratio"], 7.5, places=2)
        self.assertGreater(f["net_income_ttm"], 0)
        # Profitable with +OCF → consecutive_neg_ocf = 0
        self.assertEqual(f["consecutive_neg_ocf_quarters"], 0)
        # Cash positive, OCF positive → runway is effectively infinite; expect None or very large.
        self.assertTrue(f["cash_runway_months"] is None or f["cash_runway_months"] >= 60)

    def test_extract_preprofit_cash_burn_features(self):
        """INVZ-like pre-profit company: negative OCF streak, limited runway."""
        from alphalens.fundamentals.fetcher import extract_features

        bundle = {
            "overview": {
                "Symbol": "INVZ",
                "MarketCapitalization": "500000000",
                "RevenueTTM": "40000000",
                "PriceToSalesRatioTTM": "12.5",
                "NetIncomeTTM": "-85000000",
            },
            "balance_sheet": BALANCE_SHEET_PREPROFIT,
            "cash_flow": CASH_FLOW_PREPROFIT,
            "income_statement": INCOME_STATEMENT_PREPROFIT,
        }

        f = extract_features(bundle)
        self.assertLess(f["net_income_ttm"], 0)
        # 5 consecutive negative OCF quarters in fixture
        self.assertEqual(f["consecutive_neg_ocf_quarters"], 5)
        # $50M cash / TTM avg burn of (20+18+15+12)/4 = 16.25M per quarter
        #  → 50/16.25 ≈ 3.077 quarters × 3 ≈ 9.23 months.
        # TTM avg (not latest quarter) defends against one-time items per CR M3.
        self.assertIsNotNone(f["cash_runway_months"])
        self.assertAlmostEqual(f["cash_runway_months"], 9.23, places=1)

    def test_extract_handles_missing_overview_fields(self):
        """If AV returns the ticker but is missing PriceToSalesRatioTTM, don't crash."""
        from alphalens.fundamentals.fetcher import extract_features

        bundle = {
            "overview": {"Symbol": "X"},
            "balance_sheet": {"quarterlyReports": []},
            "cash_flow": {"quarterlyReports": []},
            "income_statement": {"quarterlyReports": []},
        }
        f = extract_features(bundle)
        self.assertIsNone(f["ps_ratio"])
        self.assertIsNone(f["cash_runway_months"])
        self.assertEqual(f["consecutive_neg_ocf_quarters"], 0)

    def test_extract_av_string_none_values(self):
        """AV often returns the string 'None' instead of null. Must be treated as missing."""
        from alphalens.fundamentals.fetcher import extract_features

        bundle = {
            "overview": {
                "Symbol": "X",
                "PriceToSalesRatioTTM": "None",
                "NetIncomeTTM": "None",
            },
            "balance_sheet": {"quarterlyReports": []},
            "cash_flow": {"quarterlyReports": []},
            "income_statement": {"quarterlyReports": []},
        }
        f = extract_features(bundle)
        self.assertIsNone(f["ps_ratio"])
        self.assertIsNone(f["net_income_ttm"])

    def test_consecutive_neg_ocf_breaks_on_positive(self):
        """Streak counts from most recent quarter backwards; a positive breaks it."""
        from alphalens.fundamentals.fetcher import extract_features

        bundle = {
            "overview": {},
            "balance_sheet": {"quarterlyReports": []},
            "cash_flow": {
                "quarterlyReports": [
                    {"fiscalDateEnding": "2024-09-30", "operatingCashflow": "-5000000"},
                    {"fiscalDateEnding": "2024-06-30", "operatingCashflow": "-3000000"},
                    {
                        "fiscalDateEnding": "2024-03-31",
                        "operatingCashflow": "2000000",
                    },  # breaks streak
                    {"fiscalDateEnding": "2023-12-31", "operatingCashflow": "-1000000"},
                ]
            },
            "income_statement": {"quarterlyReports": []},
        }
        f = extract_features(bundle)
        self.assertEqual(f["consecutive_neg_ocf_quarters"], 2)


class TestFetchTickerBundle(unittest.TestCase):
    @patch("alphalens.fundamentals.fetcher._av_income_statement")
    @patch("alphalens.fundamentals.fetcher._av_cashflow")
    @patch("alphalens.fundamentals.fetcher._av_balance_sheet")
    @patch("alphalens.fundamentals.fetcher._av_overview")
    def test_fetch_bundle_calls_all_four_endpoints(
        self,
        mock_ov,
        mock_bs,
        mock_cf,
        mock_is,
    ):
        from alphalens.fundamentals.fetcher import fetch_ticker_bundle

        mock_ov.return_value = OVERVIEW_FIXTURE
        mock_bs.return_value = {"quarterlyReports": []}
        mock_cf.return_value = {"quarterlyReports": []}
        mock_is.return_value = {"quarterlyReports": []}

        bundle = fetch_ticker_bundle("AAPL", curr_date="2024-10-15")

        mock_ov.assert_called_once_with("AAPL", curr_date="2024-10-15")
        mock_bs.assert_called_once_with("AAPL", curr_date="2024-10-15")
        mock_cf.assert_called_once_with("AAPL", curr_date="2024-10-15")
        mock_is.assert_called_once_with("AAPL", curr_date="2024-10-15")
        self.assertEqual(bundle["overview"], OVERVIEW_FIXTURE)

    @patch("alphalens.fundamentals.fetcher._av_overview")
    def test_fetch_bundle_tolerates_individual_endpoint_failure(self, mock_ov):
        """If one of four endpoints raises, bundle still contains {} for that
        key — we'd rather have partial data than crash the whole pipeline."""
        from alphalens.fundamentals.fetcher import fetch_ticker_bundle

        mock_ov.side_effect = RuntimeError("av timeout")
        with (
            patch("alphalens.fundamentals.fetcher._av_balance_sheet", return_value={}),
            patch("alphalens.fundamentals.fetcher._av_cashflow", return_value={}),
            patch("alphalens.fundamentals.fetcher._av_income_statement", return_value={}),
        ):
            bundle = fetch_ticker_bundle("X")

        self.assertEqual(bundle["overview"], {})
        self.assertEqual(bundle["balance_sheet"], {})


if __name__ == "__main__":
    unittest.main()
