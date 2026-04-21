"""Unit tests for SimFinFundamentalsStore — synthetic DataFrames, no network.

Covers the per-frame helpers (runway, ttm, streak, PIT P/S) in isolation
and the `features_as_of` integration by monkey-patching preloaded frames
directly (skipping the actual `simfin` bulk-download call).
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd


def _frame(ticker: str, reports: list[dict]) -> pd.DataFrame:
    """Build a SimFin-style DataFrame indexed by (Ticker, Report Date)."""
    rows = []
    idx = []
    for r in reports:
        rep_date = pd.Timestamp(r.pop("Report Date"))
        idx.append((ticker, rep_date))
        rows.append(r)
    frame = pd.DataFrame(rows)
    frame.index = pd.MultiIndex.from_tuples(idx, names=["Ticker", "Report Date"])
    return frame


def _prices_frame(ticker: str, rows: list[dict]) -> pd.DataFrame:
    idx = []
    data = []
    for r in rows:
        d = pd.Timestamp(r.pop("Date"))
        idx.append((ticker, d))
        data.append(r)
    f = pd.DataFrame(data)
    f.index = pd.MultiIndex.from_tuples(idx, names=["Ticker", "Date"])
    return f


class TestSimFinHelpers(unittest.TestCase):
    def test_runway_computed_from_bs_plus_trailing_ocf(self):
        from alphalens.fundamentals.simfin_store import _runway_from_frames

        bs = _frame("X", [
            {"Report Date": "2024-03-31", "Cash, Cash Equivalents & Short Term Investments": 40_000_000},
            {"Report Date": "2024-06-30", "Cash, Cash Equivalents & Short Term Investments": 50_000_000},
        ]).loc["X"]
        cf = _frame("X", [
            {"Report Date": "2023-09-30", "Net Cash from Operating Activities": -10_000_000},
            {"Report Date": "2023-12-31", "Net Cash from Operating Activities": -15_000_000},
            {"Report Date": "2024-03-31", "Net Cash from Operating Activities": -18_000_000},
            {"Report Date": "2024-06-30", "Net Cash from Operating Activities": -20_000_000},
        ]).loc["X"]

        # avg TTM OCF = (10 + 15 + 18 + 20)/4 = 15.75M burn per quarter
        # runway = 50M / 15.75M * 3 ≈ 9.52 months
        self.assertAlmostEqual(_runway_from_frames(bs, cf), 9.52, places=1)

    def test_runway_none_when_cash_flow_positive(self):
        from alphalens.fundamentals.simfin_store import _runway_from_frames

        bs = _frame("X", [
            {"Report Date": "2024-06-30", "Cash, Cash Equivalents & Short Term Investments": 50_000_000},
        ]).loc["X"]
        cf = _frame("X", [
            {"Report Date": "2024-03-31", "Net Cash from Operating Activities": 5_000_000},
            {"Report Date": "2024-06-30", "Net Cash from Operating Activities": 8_000_000},
        ]).loc["X"]
        self.assertIsNone(_runway_from_frames(bs, cf))

    def test_net_income_ttm_sums_last_4_quarters(self):
        from alphalens.fundamentals.simfin_store import _net_income_ttm_from_frame

        inc = _frame("X", [
            {"Report Date": "2023-09-30", "Net Income": -5_000_000},
            {"Report Date": "2023-12-31", "Net Income": -3_000_000},
            {"Report Date": "2024-03-31", "Net Income": -2_000_000},
            {"Report Date": "2024-06-30", "Net Income": -1_000_000},
        ]).loc["X"]
        self.assertEqual(_net_income_ttm_from_frame(inc), -11_000_000)

    def test_net_income_ttm_none_with_fewer_than_4_quarters(self):
        from alphalens.fundamentals.simfin_store import _net_income_ttm_from_frame

        inc = _frame("X", [
            {"Report Date": "2024-06-30", "Net Income": -1_000_000},
        ]).loc["X"]
        self.assertIsNone(_net_income_ttm_from_frame(inc))

    def test_consecutive_neg_ocf_counts_from_most_recent(self):
        from alphalens.fundamentals.simfin_store import _consecutive_neg_ocf_from_frame

        cf = _frame("X", [
            {"Report Date": "2023-06-30", "Net Cash from Operating Activities": -5_000_000},
            {"Report Date": "2023-09-30", "Net Cash from Operating Activities": 2_000_000},  # breaks
            {"Report Date": "2023-12-31", "Net Cash from Operating Activities": -1_000_000},
            {"Report Date": "2024-03-31", "Net Cash from Operating Activities": -3_000_000},
            {"Report Date": "2024-06-30", "Net Cash from Operating Activities": -4_000_000},
        ]).loc["X"]
        # Reports sorted ascending by date; newest is 2024-06-30. Iterate newest→oldest:
        # -4, -3, -1, +2 → streak 3
        self.assertEqual(_consecutive_neg_ocf_from_frame(cf), 3)

    def test_ps_ratio_pit_uses_close_and_shares_outstanding(self):
        from alphalens.fundamentals.simfin_store import _ps_ratio_pit

        prices_df = _prices_frame("X", [
            {"Date": "2024-06-28", "Close": 50.0, "Shares Outstanding": 10_000_000},
            {"Date": "2024-06-29", "Close": 55.0, "Shares Outstanding": 10_000_000},
            {"Date": "2024-07-01", "Close": 60.0, "Shares Outstanding": 10_000_000},
        ])
        # Pre-split into dict (mimicking what preload does).
        prices_by_ticker = {
            "X": prices_df.loc["X"].sort_index(),
        }
        inc = _frame("X", [
            {"Report Date": "2023-09-30", "Revenue": 20_000_000},
            {"Report Date": "2023-12-31", "Revenue": 22_000_000},
            {"Report Date": "2024-03-31", "Revenue": 25_000_000},
            {"Report Date": "2024-06-30", "Revenue": 33_000_000},
        ]).loc["X"]
        # asof 2024-06-29: close 55 × shares 10M = 550M market cap; TTM revenue
        # 20+22+25+33 = 100M → P/S = 5.5
        ps = _ps_ratio_pit(prices_by_ticker, inc, "X", pd.Timestamp("2024-06-29"))
        self.assertAlmostEqual(ps, 5.5, places=2)

    def test_ps_ratio_none_when_prices_not_loaded(self):
        from alphalens.fundamentals.simfin_store import _ps_ratio_pit

        inc = _frame("X", [
            {"Report Date": "2024-06-30", "Revenue": 100_000_000},
        ]).loc["X"]
        self.assertIsNone(_ps_ratio_pit(None, inc, "X", pd.Timestamp("2024-06-29")))


class TestSimFinFundamentalsStoreEndToEnd(unittest.TestCase):
    """Features_as_of correctness without touching the actual simfin package."""

    def _build_store_with_synthetic_data(self):
        from alphalens.fundamentals.simfin_store import SimFinFundamentalsStore

        with patch.dict(os.environ, {"SIMFIN_API_KEY": "testkey"}):
            store = SimFinFundamentalsStore()
        # Populate directly — skip the real sf.load_* calls.
        store._balance = _frame("ACME", [
            {"Report Date": "2024-03-31", "Cash, Cash Equivalents & Short Term Investments": 30_000_000},
            {"Report Date": "2024-06-30", "Cash, Cash Equivalents & Short Term Investments": 25_000_000},
        ])
        store._cashflow = _frame("ACME", [
            {"Report Date": "2023-09-30", "Net Cash from Operating Activities": -8_000_000},
            {"Report Date": "2023-12-31", "Net Cash from Operating Activities": -7_000_000},
            {"Report Date": "2024-03-31", "Net Cash from Operating Activities": -6_000_000},
            {"Report Date": "2024-06-30", "Net Cash from Operating Activities": -5_000_000},
        ])
        store._income = _frame("ACME", [
            {"Report Date": "2023-09-30", "Net Income": -4_000_000, "Revenue": 10_000_000},
            {"Report Date": "2023-12-31", "Net Income": -3_000_000, "Revenue": 11_000_000},
            {"Report Date": "2024-03-31", "Net Income": -2_000_000, "Revenue": 13_000_000},
            {"Report Date": "2024-06-30", "Net Income": -1_000_000, "Revenue": 16_000_000},
        ])
        # Optional: populate prices so the P/S branch is exercised in the
        # integration test. None by default — ps_ratio returns None.
        return store

    def test_features_as_of_integrates_runway_ttm_streak(self):
        store = self._build_store_with_synthetic_data()

        features = store.features_as_of("ACME", date(2024, 7, 15))
        # Balance: latest cash as of 2024-06-30 = 25M
        # OCF TTM avg = (8+7+6+5)/4 = 6.5M burn → runway = 25/6.5 × 3 ≈ 11.54 months
        self.assertAlmostEqual(features["cash_runway_months"], 11.54, places=1)
        # net_income_ttm = -4 - 3 - 2 - 1 = -10M
        self.assertEqual(features["net_income_ttm"], -10_000_000.0)
        # P/S not computed — shareprices dataset skipped for speed (deferred)
        self.assertIsNone(features["ps_ratio"])
        # Streak: last 4 quarters all negative → 4
        self.assertEqual(features["consecutive_neg_ocf_quarters"], 4)

    def test_features_as_of_filters_reports_after_asof(self):
        """On 2024-04-15 we should only see reports ≤ Q1 2024."""
        store = self._build_store_with_synthetic_data()
        features = store.features_as_of("ACME", date(2024, 4, 15))

        # Balance latest on or before 2024-04-15 is 2024-03-31 cash = 30M
        # OCF TTM (3 reports): (8+7+6)/3 = 7.0M → runway = 30/7 × 3 ≈ 12.86 months
        self.assertAlmostEqual(features["cash_runway_months"], 12.86, places=1)
        # Only 3 income reports pre-asof → net_income_ttm requires 4 → None
        self.assertIsNone(features["net_income_ttm"])

    def test_features_as_of_unknown_ticker_returns_all_none(self):
        store = self._build_store_with_synthetic_data()
        features = store.features_as_of("NVDA", date(2024, 6, 1))
        self.assertIsNone(features["cash_runway_months"])
        self.assertIsNone(features["ps_ratio"])

    def test_init_without_api_key_raises(self):
        from alphalens.fundamentals.simfin_store import SimFinFundamentalsStore

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                SimFinFundamentalsStore()
            self.assertIn("SIMFIN_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
