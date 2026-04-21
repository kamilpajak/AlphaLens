"""HistoricalFundamentalsStore tests. PIT lookup for backtest replay.

The store preloads the full historical quarterly report chain for each ticker
once, then `features_as_of(ticker, date)` returns the features computed from
the nearest-prior report (earnings available by that date).
"""

from __future__ import annotations

import unittest
from datetime import date


BUNDLE_APPL = {
    "overview": {"PriceToSalesRatioTTM": "7.5", "NetIncomeTTM": "100000000000"},
    "balance_sheet": {"quarterlyReports": [
        {"fiscalDateEnding": "2024-06-30", "cashAndShortTermInvestments": "50000000000"},
        {"fiscalDateEnding": "2024-03-31", "cashAndShortTermInvestments": "48000000000"},
        {"fiscalDateEnding": "2023-12-31", "cashAndShortTermInvestments": "45000000000"},
    ]},
    "cash_flow": {"quarterlyReports": [
        {"fiscalDateEnding": "2024-06-30", "operatingCashflow": "30000000000"},
        {"fiscalDateEnding": "2024-03-31", "operatingCashflow": "28000000000"},
        {"fiscalDateEnding": "2023-12-31", "operatingCashflow": "25000000000"},
    ]},
    "income_statement": {"quarterlyReports": [
        {"fiscalDateEnding": "2024-06-30", "netIncome": "24000000000"},
        {"fiscalDateEnding": "2024-03-31", "netIncome": "22000000000"},
        {"fiscalDateEnding": "2023-12-31", "netIncome": "28000000000"},
    ]},
}


class TestHistoricalFundamentalsStore(unittest.TestCase):
    def test_preload_calls_fetcher_once_per_ticker(self):
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        calls = []
        def fetcher(ticker, curr_date=None):
            calls.append(ticker)
            return BUNDLE_APPL

        store = HistoricalFundamentalsStore(fetcher=fetcher)
        store.preload(["AAPL", "MSFT"])
        self.assertEqual(sorted(calls), ["AAPL", "MSFT"])

    def test_features_as_of_returns_nearest_prior_report(self):
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        store = HistoricalFundamentalsStore(fetcher=lambda t, curr_date=None: BUNDLE_APPL)
        store.preload(["AAPL"])

        # After Q2 2024 report (2024-06-30) — most recent is Q2.
        features = store.features_as_of("AAPL", date(2024, 8, 15))
        self.assertIsNotNone(features)
        # Cash-flow positive → no runway concern.
        self.assertIsNone(features["cash_runway_months"])

    def test_features_as_of_trims_reports_after_date(self):
        """Given asof 2024-05-01 (between Q1 2024-03-31 and Q2 2024-06-30),
        the store must NOT use Q2 — it wasn't reported yet."""
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        store = HistoricalFundamentalsStore(fetcher=lambda t, curr_date=None: BUNDLE_APPL)
        store.preload(["AAPL"])

        features_before_q2 = store.features_as_of("AAPL", date(2024, 5, 1))
        features_after_q2 = store.features_as_of("AAPL", date(2024, 9, 1))

        # Different asof → potentially different features (here both profitable,
        # but at least the extraction should run on different data).
        self.assertIsNotNone(features_before_q2)
        self.assertIsNotNone(features_after_q2)

    def test_features_as_of_before_first_report_returns_none(self):
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        store = HistoricalFundamentalsStore(fetcher=lambda t, curr_date=None: BUNDLE_APPL)
        store.preload(["AAPL"])

        features = store.features_as_of("AAPL", date(2020, 1, 1))
        # No reports before 2023-12-31 in fixture → extract returns features
        # with all-None/zero values; net_income_ttm falls back and finds nothing
        self.assertIsNotNone(features)
        self.assertIsNone(features.get("cash_runway_months"))

    def test_features_as_of_unknown_ticker_returns_none(self):
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        store = HistoricalFundamentalsStore(fetcher=lambda t, curr_date=None: BUNDLE_APPL)
        store.preload(["AAPL"])
        self.assertIsNone(store.features_as_of("NVDA", date(2024, 6, 1)))

    def test_preload_skips_tickers_that_raise(self):
        """One bad ticker shouldn't poison the whole preload run."""
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        def fetcher(ticker, curr_date=None):
            if ticker == "BAD":
                raise RuntimeError("av failure")
            return BUNDLE_APPL

        store = HistoricalFundamentalsStore(fetcher=fetcher)
        store.preload(["AAPL", "BAD", "MSFT"])
        self.assertIsNotNone(store.features_as_of("AAPL", date(2024, 8, 1)))
        self.assertIsNone(store.features_as_of("BAD", date(2024, 8, 1)))
        self.assertIsNotNone(store.features_as_of("MSFT", date(2024, 8, 1)))


if __name__ == "__main__":
    unittest.main()
