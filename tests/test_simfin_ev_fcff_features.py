"""Unit tests for ``SimFinFundamentalsStore.ev_fcff_features_as_of`` and helpers.

Synthetic frames; no network / no SimFin bulk download. Tests cover:
- TTM aggregation (exact 4-quarter sum, drop if < 4 non-NaN)
- Effective tax rate clamping (negative / >35% / zero pretax → 0.21 default)
- 5y FCF margin median (skip zero-revenue quarters, require ≥ 8 obs)
- PIT filter (Publish Date ≤ asof; future filings excluded)
- Output dict shape (11 expected keys)
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from alphalens.data.store.simfin import (
    SimFinFundamentalsStore,
    _effective_tax_rate,
    _fcf_margin_median,
    _latest_value,
    _ttm_sum,
)


def _make_quarterly_frame(ticker: str, rows: list[dict]) -> pd.DataFrame:
    """Build a SimFin-shaped frame: MultiIndex (Ticker, Report Date)."""
    df = pd.DataFrame(rows)
    df["Ticker"] = ticker
    df["Report Date"] = pd.to_datetime(df["Report Date"])
    if "Publish Date" in df.columns:
        df["Publish Date"] = pd.to_datetime(df["Publish Date"])
    return df.set_index(["Ticker", "Report Date"])


class TestTtmSum(unittest.TestCase):
    def test_exact_four_quarters_sums(self):
        df = pd.DataFrame({"col": [10, 20, 30, 40]})
        self.assertEqual(_ttm_sum(df, "col"), 100)

    def test_more_than_four_takes_last_four(self):
        df = pd.DataFrame({"col": [1, 2, 3, 10, 20, 30, 40]})
        self.assertEqual(_ttm_sum(df, "col"), 100)  # last 4: 10+20+30+40

    def test_fewer_than_four_returns_none(self):
        df = pd.DataFrame({"col": [10, 20, 30]})
        self.assertIsNone(_ttm_sum(df, "col"))

    def test_with_nans_uses_only_non_nan(self):
        # 4 non-NaN among the last 4 → sums; if less than 4 non-NaN, None
        df = pd.DataFrame({"col": [10, 20, float("nan"), 40]})
        self.assertIsNone(_ttm_sum(df, "col"))

    def test_empty_frame_returns_none(self):
        self.assertIsNone(_ttm_sum(pd.DataFrame(), "col"))

    def test_missing_column_returns_none(self):
        df = pd.DataFrame({"other": [1, 2, 3, 4]})
        self.assertIsNone(_ttm_sum(df, "col"))


class TestLatestValue(unittest.TestCase):
    def test_returns_last_non_nan(self):
        df = pd.DataFrame({"col": [10, 20, float("nan"), 40]})
        self.assertEqual(_latest_value(df, "col"), 40)

    def test_skips_trailing_nan(self):
        df = pd.DataFrame({"col": [10, 20, 30, float("nan")]})
        self.assertEqual(_latest_value(df, "col"), 30)

    def test_empty_returns_none(self):
        self.assertIsNone(_latest_value(pd.DataFrame(), "col"))

    def test_all_nan_returns_none(self):
        df = pd.DataFrame({"col": [float("nan")] * 4})
        self.assertIsNone(_latest_value(df, "col"))


class TestEffectiveTaxRate(unittest.TestCase):
    def test_basic_clamped_range(self):
        # Pretax=100, NI=75 → tax = 1 - 0.75 = 0.25 (within [0, 0.35])
        self.assertAlmostEqual(_effective_tax_rate(100.0, 75.0), 0.25)

    def test_negative_pretax_returns_default(self):
        self.assertEqual(_effective_tax_rate(-50.0, -30.0), 0.21)

    def test_zero_pretax_returns_default(self):
        self.assertEqual(_effective_tax_rate(0.0, -10.0), 0.21)

    def test_ni_exceeds_pretax_returns_default(self):
        # NI > Pretax → negative tax → invalid → default
        self.assertEqual(_effective_tax_rate(100.0, 120.0), 0.21)

    def test_above_ceiling_clamped_to_ceiling(self):
        # tax = 1 - 50/100 = 0.50 → clamped to 0.35
        self.assertEqual(_effective_tax_rate(100.0, 50.0), 0.35)

    def test_none_inputs_return_default(self):
        self.assertEqual(_effective_tax_rate(None, 50.0), 0.21)
        self.assertEqual(_effective_tax_rate(100.0, None), 0.21)


class TestFcfMarginMedian(unittest.TestCase):
    def test_basic_median(self):
        # 10 quarters of constant margin 0.08
        cf = pd.DataFrame(
            {
                "Net Cash from Operating Activities": [100.0] * 10,
                "Change in Fixed Assets & Intangibles": [-20.0] * 10,  # capex=20
            }
        )
        # FCF = 100 + (-20) = 80; margin = 80 / revenue
        inc = pd.DataFrame(
            {
                "Revenue": [1000.0] * 10,
            }
        )
        # Index alignment matters; use Report Date as index
        idx = pd.date_range("2020-01-01", periods=10, freq="QE")
        cf.index = idx
        inc.index = idx
        # margin = 80/1000 = 0.08 each quarter
        self.assertAlmostEqual(_fcf_margin_median(cf, inc), 0.08)

    def test_fewer_than_min_quarters_returns_none(self):
        cf = pd.DataFrame(
            {
                "Net Cash from Operating Activities": [100.0] * 5,
                "Change in Fixed Assets & Intangibles": [-20.0] * 5,
            }
        )
        inc = pd.DataFrame({"Revenue": [1000.0] * 5})
        idx = pd.date_range("2020-01-01", periods=5, freq="QE")
        cf.index = idx
        inc.index = idx
        # 5 quarters < _MIN_QTRS_FCF_MARGIN_MEDIAN (8) → None
        self.assertIsNone(_fcf_margin_median(cf, inc))

    def test_skips_zero_revenue_quarters(self):
        # 10 quarters total but 2 have revenue=0 → only 8 valid → just meets min
        cf = pd.DataFrame(
            {
                "Net Cash from Operating Activities": [80.0] * 10,
                "Change in Fixed Assets & Intangibles": [0.0] * 10,
            }
        )
        rev = [1000.0] * 8 + [0.0, 0.0]
        inc = pd.DataFrame({"Revenue": rev})
        idx = pd.date_range("2020-01-01", periods=10, freq="QE")
        cf.index = idx
        inc.index = idx
        # 8 valid samples (margin=0.08) → median = 0.08
        self.assertAlmostEqual(_fcf_margin_median(cf, inc), 0.08)

    def test_missing_frame_returns_none(self):
        self.assertIsNone(_fcf_margin_median(None, pd.DataFrame()))
        self.assertIsNone(_fcf_margin_median(pd.DataFrame(), None))


class TestEvFcffFeaturesAsOf(unittest.TestCase):
    """Integration of helpers + PIT filter + dict assembly."""

    def setUp(self):
        # Build a synthetic 8-quarter history for ticker XYZ.
        # Q1 2022 ... Q4 2023, each with consistent values.
        report_dates = pd.date_range("2022-03-31", periods=8, freq="QE")
        publish_dates = report_dates + pd.Timedelta(days=45)

        cf_rows = [
            {
                "Report Date": rd,
                "Publish Date": pd,
                "Net Cash from Operating Activities": 200_000_000,
                "Change in Fixed Assets & Intangibles": -50_000_000,
            }
            for rd, pd in zip(report_dates, publish_dates)
        ]
        inc_rows = [
            {
                "Report Date": rd,
                "Publish Date": pd_,
                "Revenue": 1_000_000_000,
                "Interest Expense, Net": 10_000_000,
                "Pretax Income (Loss)": 100_000_000,
                "Net Income": 75_000_000,
            }
            for rd, pd_ in zip(report_dates, publish_dates)
        ]
        bs_rows = [
            {
                "Report Date": rd,
                "Publish Date": pd_,
                "Long Term Debt": 300_000_000,
                "Short Term Debt": 100_000_000,
                "Cash, Cash Equivalents & Short Term Investments": 200_000_000,
            }
            for rd, pd_ in zip(report_dates, publish_dates)
        ]
        self.cf = _make_quarterly_frame("XYZ", cf_rows)
        self.inc = _make_quarterly_frame("XYZ", inc_rows)
        self.bs = _make_quarterly_frame("XYZ", bs_rows)

        # Build store, bypass __init__ network/api-key check via patching env.
        with patch.dict("os.environ", {"SIMFIN_API_KEY": "test"}):
            self.store = SimFinFundamentalsStore()
        self.store._cashflow = self.cf
        self.store._income = self.inc
        self.store._balance = self.bs
        # Synthetic price for XYZ
        prices_idx = pd.date_range("2022-01-01", "2024-12-31", freq="B")
        self.store._prices_by_ticker = {
            "XYZ": pd.DataFrame(
                {"Close": 50.0, "Shares Outstanding": 80_000_000},
                index=prices_idx,
            ),
        }

    def test_returns_complete_dict(self):
        snap = self.store.ev_fcff_features_as_of("XYZ", date(2024, 6, 30))
        self.assertIsNotNone(snap)
        expected_keys = {
            "ocf_ttm",
            "capex_ttm",
            "interest_expense_ttm",
            "tax_rate",
            "revenue_ttm",
            "fcf_margin_5y_median",
            "price",
            "shares_outstanding",
            "long_term_debt",
            "short_term_debt",
            "cash_and_equivalents",
        }
        self.assertEqual(set(snap.keys()), expected_keys)

    def test_ttm_aggregation_correct(self):
        snap = self.store.ev_fcff_features_as_of("XYZ", date(2024, 6, 30))
        # 4 quarters × OCF 200M = 800M
        self.assertAlmostEqual(snap["ocf_ttm"], 800_000_000)
        # capex sign flipped: raw was -50M, becomes +50M; ×4 = 200M
        self.assertAlmostEqual(snap["capex_ttm"], 200_000_000)
        self.assertAlmostEqual(snap["interest_expense_ttm"], 40_000_000)
        self.assertAlmostEqual(snap["revenue_ttm"], 4_000_000_000)

    def test_tax_rate_clamped(self):
        snap = self.store.ev_fcff_features_as_of("XYZ", date(2024, 6, 30))
        # 1 - 75/100 = 0.25, within [0, 0.35]
        self.assertAlmostEqual(snap["tax_rate"], 0.25)

    def test_balance_latest(self):
        snap = self.store.ev_fcff_features_as_of("XYZ", date(2024, 6, 30))
        self.assertEqual(snap["long_term_debt"], 300_000_000)
        self.assertEqual(snap["short_term_debt"], 100_000_000)
        self.assertEqual(snap["cash_and_equivalents"], 200_000_000)

    def test_price_lookup(self):
        snap = self.store.ev_fcff_features_as_of("XYZ", date(2024, 6, 28))
        self.assertEqual(snap["price"], 50.0)
        self.assertEqual(snap["shares_outstanding"], 80_000_000)

    def test_pit_filter_excludes_future_publish_date(self):
        # asof BEFORE any Publish Date → None (no fundamentals known yet).
        # Earliest Publish Date is 2022-03-31 + 45 days = 2022-05-15.
        snap = self.store.ev_fcff_features_as_of("XYZ", date(2022, 5, 1))
        self.assertIsNone(snap)

    def test_unknown_ticker_returns_none(self):
        self.assertIsNone(self.store.ev_fcff_features_as_of("UNKNOWN", date(2024, 6, 30)))

    def test_fcf_margin_returns_none_with_too_few_history(self):
        # 8 quarters available; min is 8 → at edge it should compute
        snap = self.store.ev_fcff_features_as_of("XYZ", date(2024, 6, 30))
        self.assertIsNotNone(snap["fcf_margin_5y_median"])
        # FCF = 200M - 50M = 150M per quarter; margin = 150M / 1B = 0.15
        self.assertAlmostEqual(snap["fcf_margin_5y_median"], 0.15)


if __name__ == "__main__":
    unittest.main()
