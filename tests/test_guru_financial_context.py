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


def _full_prompt_context() -> dict:
    return {
        "ticker": "AAPL",
        "asof": "2025-06-30",
        "name": "Apple Inc.",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "exchange": "NASDAQ",
        "income": {
            "latest_fy_period": "2024-09-28",
            "latest_fy_filing_date": "2024-11-01",
            "latest_fy_revenue": 391_035_000_000,
            "latest_fy_net_income": 93_736_000_000,
            "latest_fy_operating_income": 123_216_000_000,
        },
        "balance": {
            "latest_fy_period": "2024-09-28",
            "total_assets": 364_980_000_000,
            "total_liabilities": 308_030_000_000,
            "total_equity": 56_950_000_000,
            "long_term_debt": 85_750_000_000,
        },
        "cashflow": {
            "latest_fy_period": "2024-09-28",
            "operating_cashflow": 118_254_000_000,
        },
        "history": [
            {"period": "2024-09-28", "revenue": 391_035_000_000, "net_income": 93_736_000_000},
            {"period": "2023-09-30", "revenue": 383_285_000_000, "net_income": 96_995_000_000},
            {"period": "2022-09-24", "revenue": 394_328_000_000, "net_income": 99_803_000_000},
        ],
        "price_summary": {
            "latest_close": 213.55,
            "trailing_12m_return": 0.1842,
            "max_drawdown_12m": -0.1135,
        },
    }


class TestContextToPromptCharacterization(unittest.TestCase):
    """Locks down `context_to_prompt` behavior across the ratio-guard branches.

    These tests must keep passing after the planned cognitive-complexity
    refactor (issue #26 Tier 2): extract ratio computation + reuse format helper.
    """

    def test_full_context_renders_all_ratios_and_sections(self):
        from alphalens.guru.financial_context import context_to_prompt

        out = context_to_prompt(_full_prompt_context())

        # Identity
        self.assertIn("COMPANY: AAPL — Apple Inc.", out)
        self.assertIn("Sector: Technology", out)
        self.assertIn("Exchange: NASDAQ", out)
        self.assertIn("As-of evaluation: 2025-06-30", out)

        # Income line items rendered with thousand separators
        self.assertIn("Revenue: $391,035,000,000", out)
        self.assertIn("Operating cashflow: $118,254,000,000", out)

        # Balance
        self.assertIn("Long-term debt: $85,750,000,000", out)

        # Computed ratios (current implementation: percent for margins/ROE, 2dp for D/E and CF/NI)
        self.assertIn("Profit margin: 23.97%", out)
        self.assertIn("Operating margin: 31.51%", out)
        self.assertIn("ROE (basic): 164.59%", out)
        self.assertIn("Long-term D/E: 1.51", out)
        self.assertIn("Operating CF / Net Income: 1.26", out)

        # History block + ordering
        self.assertIn("Revenue / net income trend (last 3 fiscal years):", out)
        self.assertIn("2024-09-28: rev=$391,035,000,000, ni=$93,736,000,000", out)
        self.assertIn("2022-09-24: rev=$394,328,000,000, ni=$99,803,000,000", out)

        # Price summary
        self.assertIn("Latest close: $213.55", out)
        self.assertIn("12m return: 18.42%", out)
        self.assertIn("Max drawdown: -11.35%", out)

    def test_missing_income_yields_na_ratios(self):
        from alphalens.guru.financial_context import context_to_prompt

        ctx = _full_prompt_context()
        ctx["income"] = {}
        out = context_to_prompt(ctx)

        self.assertIn("Revenue: $n/a", out)
        self.assertIn("Profit margin: n/a", out)
        self.assertIn("Operating margin: n/a", out)
        self.assertIn("ROE (basic): n/a", out)
        self.assertIn("Operating CF / Net Income: n/a", out)

    def test_negative_equity_suppresses_roe_and_de(self):
        from alphalens.guru.financial_context import context_to_prompt

        ctx = _full_prompt_context()
        ctx["balance"]["total_equity"] = -100_000_000
        out = context_to_prompt(ctx)

        self.assertIn("ROE (basic): n/a", out)
        self.assertIn("Long-term D/E: n/a", out)
        self.assertIn("Total equity: $-100,000,000", out)

    def test_zero_revenue_suppresses_margin_ratios(self):
        from alphalens.guru.financial_context import context_to_prompt

        ctx = _full_prompt_context()
        ctx["income"]["latest_fy_revenue"] = 0
        out = context_to_prompt(ctx)

        self.assertIn("Profit margin: n/a", out)
        self.assertIn("Operating margin: n/a", out)

    def test_negative_net_income_suppresses_cf_ratio(self):
        from alphalens.guru.financial_context import context_to_prompt

        ctx = _full_prompt_context()
        ctx["income"]["latest_fy_net_income"] = -10_000_000
        out = context_to_prompt(ctx)

        self.assertIn("Operating CF / Net Income: n/a", out)

    def test_empty_history_omits_history_block(self):
        from alphalens.guru.financial_context import context_to_prompt

        ctx = _full_prompt_context()
        ctx["history"] = []
        out = context_to_prompt(ctx)

        self.assertNotIn("Revenue / net income trend", out)
        self.assertIn("Price summary", out)

    def test_none_context_returns_sentinel(self):
        from alphalens.guru.financial_context import context_to_prompt

        self.assertEqual(context_to_prompt(None), "NO DATA AVAILABLE")

    def test_missing_optional_metadata_renders_unknown(self):
        from alphalens.guru.financial_context import context_to_prompt

        ctx = _full_prompt_context()
        ctx["name"] = None
        ctx["sector"] = None
        ctx["exchange"] = None
        out = context_to_prompt(ctx)

        self.assertIn("COMPANY: AAPL — Unknown", out)
        self.assertIn("Sector: Unknown", out)
        self.assertIn("Exchange: Unknown", out)


if __name__ == "__main__":
    unittest.main()
