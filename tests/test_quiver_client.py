"""Tests for alphalens.archive.quiver_screener.client schema-normalization layer."""

from __future__ import annotations

import unittest

import pandas as pd


class TestNormalizeInsiders(unittest.TestCase):
    """The mapping from raw Quiver transaction codes → canonical 'A'/'D' must
    recognise known BUY indicators explicitly. The old version mapped first-char
    unconditionally ('Purchase' → 'P' → default 'D') which inverted the signal.
    """

    def test_sec_form4_code_P_maps_to_buy(self):
        from alphalens.archive.quiver_screener.client import normalize_insiders

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Date": ["2024-01-10"],
                "Name": ["CEO"],
                "Transaction": ["P"],
                "Shares": [1000],
                "PricePerShare": [500.0],
            }
        )
        out = normalize_insiders(raw)
        self.assertEqual(out["transaction"].iloc[0], "A")

    def test_sec_form4_code_S_maps_to_sell(self):
        from alphalens.archive.quiver_screener.client import normalize_insiders

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Date": ["2024-01-10"],
                "Name": ["CEO"],
                "Transaction": ["S"],
                "Shares": [1000],
                "PricePerShare": [500.0],
            }
        )
        out = normalize_insiders(raw)
        self.assertEqual(out["transaction"].iloc[0], "D")

    def test_verbose_purchase_maps_to_buy(self):
        from alphalens.archive.quiver_screener.client import normalize_insiders

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Date": ["2024-01-10"],
                "Name": ["CEO"],
                "Transaction": ["Purchase"],
                "Shares": [1000],
                "PricePerShare": [500.0],
            }
        )
        out = normalize_insiders(raw)
        self.assertEqual(out["transaction"].iloc[0], "A")

    def test_verbose_sale_maps_to_sell(self):
        from alphalens.archive.quiver_screener.client import normalize_insiders

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Date": ["2024-01-10"],
                "Name": ["CEO"],
                "Transaction": ["Sale"],
                "Shares": [1000],
                "PricePerShare": [500.0],
            }
        )
        out = normalize_insiders(raw)
        self.assertEqual(out["transaction"].iloc[0], "D")

    def test_acquired_maps_to_buy(self):
        from alphalens.archive.quiver_screener.client import normalize_insiders

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Date": ["2024-01-10"],
                "Name": ["CEO"],
                "Transaction": ["Acquired"],
                "Shares": [1000],
                "PricePerShare": [500.0],
            }
        )
        out = normalize_insiders(raw)
        self.assertEqual(out["transaction"].iloc[0], "A")

    def test_code_A_maps_to_buy(self):
        from alphalens.archive.quiver_screener.client import normalize_insiders

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Date": ["2024-01-10"],
                "Name": ["CEO"],
                "Transaction": ["A"],
                "Shares": [1000],
                "PricePerShare": [500.0],
            }
        )
        out = normalize_insiders(raw)
        self.assertEqual(out["transaction"].iloc[0], "A")

    def test_unknown_code_defaults_to_sell(self):
        """Conservative default: unrecognised codes → 'D'. Misclassifying a buy
        as sell biases signal to zero, safer than the reverse."""
        from alphalens.archive.quiver_screener.client import normalize_insiders

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Date": ["2024-01-10"],
                "Name": ["CEO"],
                "Transaction": ["F"],
                "Shares": [1000],
                "PricePerShare": [500.0],
            }
        )
        out = normalize_insiders(raw)
        self.assertEqual(out["transaction"].iloc[0], "D")

    def test_value_equals_shares_times_price(self):
        from alphalens.archive.quiver_screener.client import normalize_insiders

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Date": ["2024-01-10"],
                "Name": ["CEO"],
                "Transaction": ["A"],
                "Shares": [1500],
                "PricePerShare": [200.0],
            }
        )
        out = normalize_insiders(raw)
        self.assertAlmostEqual(out["value"].iloc[0], 300_000.0)


class TestNormalizeCongress(unittest.TestCase):
    def test_range_string_parsed_to_midpoint(self):
        from alphalens.archive.quiver_screener.client import normalize_congress

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA", "AMD"],
                "TransactionDate": ["2024-01-10", "2024-01-15"],
                "Representative": ["Pelosi", "Crenshaw"],
                "Transaction": ["Purchase", "Sale (Partial)"],
                "Range": ["$1,001 - $15,000", "$50,001 - $100,000"],
            }
        )
        out = normalize_congress(raw)
        self.assertAlmostEqual(out["amount_mid"].iloc[0], 8000.5, places=1)
        self.assertAlmostEqual(out["amount_mid"].iloc[1], 75000.5, places=1)

    def test_partial_sale_canonicalised_to_sale(self):
        from alphalens.archive.quiver_screener.client import normalize_congress

        raw = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "TransactionDate": ["2024-01-10"],
                "Representative": ["X"],
                "Transaction": ["Sale (Partial)"],
                "Range": ["$1,001 - $15,000"],
            }
        )
        out = normalize_congress(raw)
        self.assertEqual(out["transaction"].iloc[0], "SALE")

    def test_empty_raw_returns_schema_with_no_rows(self):
        from alphalens.archive.quiver_screener.client import normalize_congress

        out = normalize_congress(pd.DataFrame())
        self.assertEqual(
            list(out.columns),
            ["ticker", "date", "representative", "transaction", "amount_mid"],
        )
        self.assertEqual(len(out), 0)


if __name__ == "__main__":
    unittest.main()
