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
        from alphalens_pipeline.data.fundamentals.fetcher import extract_features

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
        from alphalens_pipeline.data.fundamentals.fetcher import extract_features

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
        from alphalens_pipeline.data.fundamentals.fetcher import extract_features

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
        from alphalens_pipeline.data.fundamentals.fetcher import extract_features

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
        from alphalens_pipeline.data.fundamentals.fetcher import extract_features

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
    @patch("alphalens_pipeline.data.fundamentals.fetcher._av_income_statement")
    @patch("alphalens_pipeline.data.fundamentals.fetcher._av_cashflow")
    @patch("alphalens_pipeline.data.fundamentals.fetcher._av_balance_sheet")
    @patch("alphalens_pipeline.data.fundamentals.fetcher._av_overview")
    def test_fetch_bundle_calls_all_four_endpoints(
        self,
        mock_ov,
        mock_bs,
        mock_cf,
        mock_is,
    ):
        from alphalens_pipeline.data.fundamentals.fetcher import fetch_ticker_bundle

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

    @patch("alphalens_pipeline.data.fundamentals.fetcher._av_overview")
    def test_fetch_bundle_tolerates_individual_endpoint_failure(self, mock_ov):
        """If one of four endpoints raises, bundle still contains {} for that
        key — we'd rather have partial data than crash the whole pipeline."""
        from alphalens_pipeline.data.fundamentals.fetcher import fetch_ticker_bundle

        mock_ov.side_effect = RuntimeError("av timeout")
        with (
            patch(
                "alphalens_pipeline.data.fundamentals.fetcher._av_balance_sheet", return_value={}
            ),
            patch("alphalens_pipeline.data.fundamentals.fetcher._av_cashflow", return_value={}),
            patch(
                "alphalens_pipeline.data.fundamentals.fetcher._av_income_statement", return_value={}
            ),
        ):
            bundle = fetch_ticker_bundle("X")

        self.assertEqual(bundle["overview"], {})
        self.assertEqual(bundle["balance_sheet"], {})

    @patch("alphalens_pipeline.data.fundamentals.fetcher._av_overview")
    def test_fetch_bundle_propagates_rate_limit(self, mock_ov):
        """Rate-limit must abort the batch (not degrade to null features)."""
        from alphalens_pipeline.data.fundamentals.fetcher import (
            AVRateLimitError,
            fetch_ticker_bundle,
        )

        mock_ov.side_effect = AVRateLimitError("daily quota exhausted")
        with self.assertRaises(AVRateLimitError):
            fetch_ticker_bundle("X")


class TestMakeAVRequest(unittest.TestCase):
    """Behaviour tests for the fetcher's thin adapter over the canonical
    AlphaVantageClient. Network is never hit — each test injects a client
    backed by a fake urlopen_fn so the adapter's soft-fail vs propagate
    contract is observable.
    """

    @staticmethod
    def _client_with_body(body: str):
        from unittest.mock import MagicMock

        from alphalens_pipeline.data.alt_data.alphavantage_client import AlphaVantageClient

        class _CM:
            def __enter__(self):
                class _Resp:
                    def read(self):
                        return body.encode("utf-8")

                return _Resp()

            def __exit__(self, *exc):
                return False

        return AlphaVantageClient(api_key="test", urlopen_fn=MagicMock(return_value=_CM()))

    def test_rate_limit_information_raises(self):
        from alphalens_pipeline.data.fundamentals.fetcher import (
            AVRateLimitError,
            _make_av_request,
        )

        client = self._client_with_body(
            '{"Information": "Thank you for using Alpha Vantage! ...rate limit..."}'
        )
        with self.assertRaises(AVRateLimitError):
            _make_av_request("OVERVIEW", "AAPL", client=client)

    def test_error_message_returns_empty(self):
        """Invalid ticker / malformed request → {} with warning, not exception."""
        from alphalens_pipeline.data.fundamentals.fetcher import _make_av_request

        client = self._client_with_body('{"Error Message": "Invalid API call. Please retry..."}')
        self.assertEqual(_make_av_request("OVERVIEW", "BOGUS", client=client), {})

    def test_non_json_response_returns_empty(self):
        from alphalens_pipeline.data.fundamentals.fetcher import _make_av_request

        client = self._client_with_body("<html>not json</html>")
        self.assertEqual(_make_av_request("OVERVIEW", "AAPL", client=client), {})

    def test_non_dict_json_returns_empty(self):
        """AV occasionally returns a JSON list / scalar — coerce to {}."""
        from alphalens_pipeline.data.fundamentals.fetcher import _make_av_request

        client = self._client_with_body("[1, 2, 3]")
        self.assertEqual(_make_av_request("OVERVIEW", "AAPL", client=client), {})


class TestFilterReportsByDate(unittest.TestCase):
    def test_no_curr_date_returns_input_unchanged(self):
        from alphalens_pipeline.data.fundamentals.fetcher import _filter_reports_by_date

        bundle = {"quarterlyReports": [{"fiscalDateEnding": "2024-09-30"}]}
        self.assertIs(_filter_reports_by_date(bundle, None), bundle)

    def test_non_dict_input_returned_unchanged(self):
        from alphalens_pipeline.data.fundamentals.fetcher import _filter_reports_by_date

        self.assertEqual(_filter_reports_by_date("not a dict", "2024-10-15"), "not a dict")

    def test_filters_quarterly_and_annual_reports_by_curr_date(self):
        from alphalens_pipeline.data.fundamentals.fetcher import _filter_reports_by_date

        bundle = {
            "annualReports": [
                {"fiscalDateEnding": "2023-12-31"},
                {"fiscalDateEnding": "2024-12-31"},  # after curr_date → drop
            ],
            "quarterlyReports": [
                {"fiscalDateEnding": "2024-06-30"},
                {"fiscalDateEnding": "2024-09-30"},  # after curr_date → drop
            ],
        }
        filtered = _filter_reports_by_date(bundle, "2024-08-31")
        self.assertEqual(len(filtered["annualReports"]), 1)
        self.assertEqual(filtered["annualReports"][0]["fiscalDateEnding"], "2023-12-31")
        self.assertEqual(len(filtered["quarterlyReports"]), 1)
        self.assertEqual(filtered["quarterlyReports"][0]["fiscalDateEnding"], "2024-06-30")


class TestSafeNetworkErrors(unittest.TestCase):
    def test_network_error_in_individual_endpoint_logs_and_returns_empty(self):
        """HTTPError / URLError must degrade per-endpoint without aborting the bundle."""
        from urllib.error import URLError

        from alphalens_pipeline.data.fundamentals.fetcher import fetch_ticker_bundle

        with (
            patch(
                "alphalens_pipeline.data.fundamentals.fetcher._av_overview",
                side_effect=URLError("DNS lookup failed"),
            ),
            patch(
                "alphalens_pipeline.data.fundamentals.fetcher._av_balance_sheet", return_value={}
            ),
            patch("alphalens_pipeline.data.fundamentals.fetcher._av_cashflow", return_value={}),
            patch(
                "alphalens_pipeline.data.fundamentals.fetcher._av_income_statement", return_value={}
            ),
        ):
            bundle = fetch_ticker_bundle("AAPL")

        self.assertEqual(bundle["overview"], {})


class TestEndpointWrappers(unittest.TestCase):
    """Cover the four single-line wrappers that delegate to _make_av_request."""

    def test_av_overview_calls_make_request(self):
        from alphalens_pipeline.data.fundamentals.fetcher import _av_overview

        with patch(
            "alphalens_pipeline.data.fundamentals.fetcher._make_av_request",
            return_value={"Symbol": "AAPL"},
        ) as m:
            self.assertEqual(_av_overview("AAPL"), {"Symbol": "AAPL"})
            m.assert_called_once_with("OVERVIEW", "AAPL")

    def test_av_balance_sheet_filters_by_date(self):
        from alphalens_pipeline.data.fundamentals.fetcher import _av_balance_sheet

        raw = {
            "quarterlyReports": [
                {"fiscalDateEnding": "2024-06-30"},
                {"fiscalDateEnding": "2024-09-30"},
            ]
        }
        with patch(
            "alphalens_pipeline.data.fundamentals.fetcher._make_av_request", return_value=raw
        ):
            result = _av_balance_sheet("AAPL", curr_date="2024-08-01")
        self.assertEqual(len(result["quarterlyReports"]), 1)

    def test_av_cashflow_filters_by_date(self):
        from alphalens_pipeline.data.fundamentals.fetcher import _av_cashflow

        raw = {"quarterlyReports": [{"fiscalDateEnding": "2024-06-30"}]}
        with patch(
            "alphalens_pipeline.data.fundamentals.fetcher._make_av_request", return_value=raw
        ):
            self.assertEqual(_av_cashflow("AAPL", curr_date="2024-12-31"), raw)

    def test_av_income_statement_filters_by_date(self):
        from alphalens_pipeline.data.fundamentals.fetcher import _av_income_statement

        raw = {"quarterlyReports": [{"fiscalDateEnding": "2024-06-30"}]}
        with patch(
            "alphalens_pipeline.data.fundamentals.fetcher._make_av_request", return_value=raw
        ):
            self.assertEqual(_av_income_statement("AAPL", curr_date="2024-12-31"), raw)


if __name__ == "__main__":
    unittest.main()
