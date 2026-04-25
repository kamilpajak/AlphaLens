"""Tests for alphalens.guru.polygon_fundamentals — Polygon-backed financials."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import pandas as pd

_SAMPLE_RESPONSE = {
    "results": [
        {
            "fiscal_period": "FY",
            "period_of_report_date": "2017-09-30",
            "filing_date": "2017-11-03",
            "financials": {
                "income_statement": {
                    "revenues": {"value": 229234000000.0},
                    "net_income_loss": {"value": 48351000000.0},
                    "operating_income_loss": {"value": 61344000000.0},
                },
                "balance_sheet": {
                    "assets": {"value": 375319000000.0},
                    "liabilities": {"value": 241272000000.0},
                    "equity": {"value": 134047000000.0},
                    "long_term_debt": {"value": 97207000000.0},
                },
                "cash_flow_statement": {
                    "net_cash_flow_from_operating_activities": {"value": 63598000000.0},
                },
            },
        }
    ]
}


def _mock_response(status: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    return resp


class TestFetchFinancials(unittest.TestCase):
    def test_fetches_annual_financials_filtered_by_asof(self):
        from alphalens.guru.polygon_fundamentals import PolygonFundamentalsClient

        session = MagicMock()
        session.get.return_value = _mock_response(200, _SAMPLE_RESPONSE)
        client = PolygonFundamentalsClient(
            api_key="test",
            session=session,
            sleep=MagicMock(),
        )

        result = client.fetch_financials(
            ticker="AAPL",
            asof=pd.Timestamp("2018-01-01"),
            timeframe="annual",
            limit=3,
        )

        self.assertEqual(len(result), 1)
        rec = result[0]
        self.assertEqual(rec["fiscal_period"], "FY")
        self.assertEqual(rec["period_of_report_date"], "2017-09-30")
        self.assertAlmostEqual(rec["revenue"], 229234000000.0, places=0)
        self.assertAlmostEqual(rec["net_income"], 48351000000.0, places=0)
        self.assertAlmostEqual(rec["total_assets"], 375319000000.0, places=0)
        self.assertAlmostEqual(rec["total_equity"], 134047000000.0, places=0)
        self.assertAlmostEqual(rec["operating_cashflow"], 63598000000.0, places=0)

        # Verify URL params
        call_args = session.get.call_args
        params = call_args.kwargs.get("params") or {}
        self.assertEqual(params.get("ticker"), "AAPL")
        self.assertEqual(params.get("timeframe"), "annual")
        self.assertEqual(params.get("limit"), 3)
        self.assertEqual(params.get("period_of_report_date.lte"), "2018-01-01")
        self.assertIn("apiKey", params)

    def test_returns_empty_list_when_no_results(self):
        from alphalens.guru.polygon_fundamentals import PolygonFundamentalsClient

        session = MagicMock()
        session.get.return_value = _mock_response(200, {"results": []})
        client = PolygonFundamentalsClient(
            api_key="test",
            session=session,
            sleep=MagicMock(),
        )

        result = client.fetch_financials(
            ticker="UNKNOWN",
            asof=pd.Timestamp("2018-01-01"),
        )

        self.assertEqual(result, [])

    def test_handles_missing_financial_fields_gracefully(self):
        from alphalens.guru.polygon_fundamentals import PolygonFundamentalsClient

        partial = {
            "results": [
                {
                    "fiscal_period": "FY",
                    "period_of_report_date": "2017-09-30",
                    "filing_date": "2017-11-03",
                    "financials": {
                        "income_statement": {
                            "revenues": {"value": 100.0},
                            # net_income_loss missing
                        },
                        # balance_sheet missing
                    },
                }
            ]
        }
        session = MagicMock()
        session.get.return_value = _mock_response(200, partial)
        client = PolygonFundamentalsClient(
            api_key="test",
            session=session,
            sleep=MagicMock(),
        )

        result = client.fetch_financials(ticker="AAPL", asof=pd.Timestamp("2018-01-01"))

        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["revenue"], 100.0)
        self.assertIsNone(result[0]["net_income"])
        self.assertIsNone(result[0]["total_assets"])

    def test_get_overview_returns_company_metadata(self):
        """Polygon ticker_details endpoint provides sector/industry/etc."""
        from alphalens.guru.polygon_fundamentals import PolygonFundamentalsClient

        details_response = {
            "results": {
                "ticker": "AAPL",
                "name": "Apple Inc.",
                "market": "stocks",
                "sic_description": "ELECTRONIC COMPUTERS",
                "type": "CS",
                "primary_exchange": "XNAS",
            }
        }
        session = MagicMock()
        session.get.return_value = _mock_response(200, details_response)
        client = PolygonFundamentalsClient(
            api_key="test",
            session=session,
            sleep=MagicMock(),
        )

        ov = client.get_overview("AAPL")

        self.assertEqual(ov["Symbol"], "AAPL")
        self.assertEqual(ov["Name"], "Apple Inc.")
        self.assertIn("Industry", ov)


if __name__ == "__main__":
    unittest.main()
