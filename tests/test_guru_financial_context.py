"""Tests for alphalens.guru.financial_context — Polygon-backed builder."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

_OVERVIEW = {
    "Symbol": "AAPL",
    "Name": "Apple Inc.",
    "Sector": "ELECTRONIC COMPUTERS",
    "Industry": "ELECTRONIC COMPUTERS",
    "Exchange": "XNAS",
    "Currency": "USD",
}

_FINANCIALS = [
    {
        "fiscal_period": "FY",
        "period_of_report_date": "2017-09-30",
        "filing_date": "2017-11-03",
        "revenue": 229234000000.0,
        "net_income": 48351000000.0,
        "operating_income": 61344000000.0,
        "total_assets": 375319000000.0,
        "total_liabilities": 241272000000.0,
        "total_equity": 134047000000.0,
        "long_term_debt": 97207000000.0,
        "operating_cashflow": 63598000000.0,
    },
    {
        "fiscal_period": "FY",
        "period_of_report_date": "2016-09-30",
        "filing_date": "2016-10-26",
        "revenue": 215639000000.0,
        "net_income": 45687000000.0,
        "operating_income": 60024000000.0,
        "total_assets": None,
        "total_liabilities": None,
        "total_equity": None,
        "long_term_debt": None,
        "operating_cashflow": None,
    },
]


def _fake_price_series(n_bars: int = 260, daily_ret: float = 0.0004) -> pd.Series:
    idx = pd.date_range("2017-01-02", periods=n_bars, freq="B")
    return pd.Series(
        (1.0 + daily_ret) ** np.arange(n_bars) * 100.0,
        index=idx,
    )


def _fake_polygon_client(overview: dict | None = None, financials: list | None = None):
    client = MagicMock()
    client.get_overview.return_value = overview if overview is not None else _OVERVIEW
    client.fetch_financials.return_value = financials if financials is not None else _FINANCIALS
    return client


class TestBuildContext(unittest.TestCase):
    def test_context_includes_overview_and_latest_fy(self):
        from alphalens.guru.financial_context import build_context

        client = _fake_polygon_client()
        ctx = build_context(
            ticker="AAPL",
            asof=pd.Timestamp("2018-01-01"),
            price_series=_fake_price_series(),
            client=client,
        )

        self.assertEqual(ctx["ticker"], "AAPL")
        self.assertEqual(ctx["sector"], "ELECTRONIC COMPUTERS")
        self.assertAlmostEqual(ctx["income"]["latest_fy_revenue"], 229234000000.0)
        self.assertAlmostEqual(ctx["balance"]["total_equity"], 134047000000.0)
        self.assertEqual(ctx["income"]["latest_fy_period"], "2017-09-30")

    def test_context_includes_price_summary(self):
        from alphalens.guru.financial_context import build_context

        price = _fake_price_series()
        client = _fake_polygon_client()
        ctx = build_context(
            ticker="AAPL",
            asof=price.index[-1],
            price_series=price,
            client=client,
        )

        ps = ctx["price_summary"]
        self.assertIn("trailing_12m_return", ps)
        self.assertIn("max_drawdown_12m", ps)
        self.assertAlmostEqual(ps["max_drawdown_12m"], 0.0, places=6)

    def test_context_includes_history(self):
        from alphalens.guru.financial_context import build_context

        client = _fake_polygon_client()
        ctx = build_context(
            ticker="AAPL",
            asof=pd.Timestamp("2018-01-01"),
            price_series=_fake_price_series(),
            client=client,
        )

        history = ctx.get("history", [])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["period"], "2017-09-30")
        self.assertEqual(history[1]["period"], "2016-09-30")

    def test_returns_none_when_overview_missing(self):
        from alphalens.guru.financial_context import build_context

        client = _fake_polygon_client(overview={})
        ctx = build_context(
            ticker="UNKNOWN",
            asof=pd.Timestamp("2018-01-01"),
            price_series=_fake_price_series(),
            client=client,
        )

        self.assertIsNone(ctx)

    def test_polygon_filter_invokes_with_asof(self):
        from alphalens.guru.financial_context import build_context

        client = _fake_polygon_client()
        build_context(
            ticker="AAPL",
            asof=pd.Timestamp("2018-01-01"),
            price_series=_fake_price_series(),
            client=client,
        )

        # Verify client called with asof
        call = client.fetch_financials.call_args
        self.assertEqual(call.kwargs["ticker"], "AAPL")
        self.assertEqual(call.kwargs["asof"], pd.Timestamp("2018-01-01"))

    def test_context_to_prompt_renders_compact_text(self):
        from alphalens.guru.financial_context import build_context, context_to_prompt

        client = _fake_polygon_client()
        ctx = build_context(
            ticker="AAPL",
            asof=pd.Timestamp("2018-01-01"),
            price_series=_fake_price_series(),
            client=client,
        )
        text = context_to_prompt(ctx)

        self.assertIn("AAPL", text)
        self.assertIn("Apple Inc.", text)
        self.assertIn("Profit margin", text)
        self.assertIn("ROE", text)
        # Should be compact — < 3500 chars
        self.assertLess(len(text), 3500)


if __name__ == "__main__":
    unittest.main()
