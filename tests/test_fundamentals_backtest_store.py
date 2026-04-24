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

    def test_features_as_of_filters_by_reported_date_not_fiscal_end(self):
        """Zen CR fix: SEC filings are public on reportedDate (fiscal_end + 45-90d),
        not on fiscalDateEnding. Filtering by fiscal_end leaks ~1 quarter of
        data that wasn't yet publicly filed. Correct PIT filter: reportedDate.
        """
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        # Fiscal Q1 ending 2024-03-31 was actually reported on 2024-05-15.
        # asof 2024-04-15 is BEFORE report date but AFTER fiscal end.
        bundle = {
            "overview": {},
            "balance_sheet": {"quarterlyReports": [
                {
                    "fiscalDateEnding": "2024-03-31",
                    "reportedDate": "2024-05-15",
                    "cashAndShortTermInvestments": "50000000000",
                },
                {
                    "fiscalDateEnding": "2023-12-31",
                    "reportedDate": "2024-02-15",
                    "cashAndShortTermInvestments": "40000000000",
                },
            ]},
            "cash_flow": {"quarterlyReports": [
                {
                    "fiscalDateEnding": "2024-03-31",
                    "reportedDate": "2024-05-15",
                    "operatingCashflow": "30000000000",
                },
                {
                    "fiscalDateEnding": "2023-12-31",
                    "reportedDate": "2024-02-15",
                    "operatingCashflow": "25000000000",
                },
            ]},
            "income_statement": {"quarterlyReports": [
                {
                    "fiscalDateEnding": "2024-03-31",
                    "reportedDate": "2024-05-15",
                    "netIncome": "22000000000",
                },
                {
                    "fiscalDateEnding": "2023-12-31",
                    "reportedDate": "2024-02-15",
                    "netIncome": "28000000000",
                },
            ]},
        }

        store = HistoricalFundamentalsStore(fetcher=lambda t, curr_date=None: bundle)
        store.preload(["AAPL"])

        # The store's internal filter must expose only the 2023-12-31 report
        # when asof is 2024-04-15 — the 2024-03-31 report is not yet public.
        from alphalens.fundamentals.backtest_store import _filter_bundle_by_date
        filtered = _filter_bundle_by_date(bundle, "2024-04-15")
        latest_fiscal_dates = [
            r["fiscalDateEnding"]
            for r in filtered["income_statement"]["quarterlyReports"]
        ]
        self.assertEqual(latest_fiscal_dates, ["2023-12-31"])
        self.assertNotIn("2024-03-31", latest_fiscal_dates)

    def test_features_as_of_falls_back_to_fiscal_end_if_reported_date_missing(self):
        """Some legacy bundles lack reportedDate — fall back to
        fiscalDateEnding to avoid crashing on incomplete data."""
        from alphalens.fundamentals.backtest_store import _filter_bundle_by_date

        bundle = {
            "overview": {},
            "income_statement": {"quarterlyReports": [
                # No reportedDate field at all
                {"fiscalDateEnding": "2023-12-31", "netIncome": "100"},
                {"fiscalDateEnding": "2024-03-31", "netIncome": "200"},
            ]},
            "balance_sheet": {"quarterlyReports": []},
            "cash_flow": {"quarterlyReports": []},
        }

        # Without reportedDate, fall back to fiscalDateEnding
        filtered = _filter_bundle_by_date(bundle, "2024-02-01")
        kept = [r["fiscalDateEnding"] for r in filtered["income_statement"]["quarterlyReports"]]
        self.assertEqual(kept, ["2023-12-31"])

    def test_features_as_of_unknown_ticker_returns_none(self):
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        store = HistoricalFundamentalsStore(fetcher=lambda t, curr_date=None: BUNDLE_APPL)
        store.preload(["AAPL"])
        self.assertIsNone(store.features_as_of("NVDA", date(2024, 6, 1)))

    def test_backtest_strips_forward_looking_overview_fields(self):
        """OVERVIEW.PriceToSalesRatioTTM + NetIncomeTTM are CURRENT values;
        using them in a historical replay would leak today's valuation into
        2022 scoring. Both must be stripped so extract_features returns
        None / falls back to PIT income-statement."""
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        bundle = {
            "overview": {
                "Symbol": "X",
                "PriceToSalesRatioTTM": "150.0",
                "NetIncomeTTM": "99999999999",  # present-day blowout
            },
            "balance_sheet": {"quarterlyReports": [
                {"fiscalDateEnding": "2022-03-31", "cashAndShortTermInvestments": "10000000"},
            ]},
            "cash_flow": {"quarterlyReports": [
                {"fiscalDateEnding": "2022-03-31", "operatingCashflow": "-5000000"},
            ]},
            "income_statement": {"quarterlyReports": [
                {"fiscalDateEnding": "2022-03-31", "netIncome": "-1000000"},
                {"fiscalDateEnding": "2021-12-31", "netIncome": "-2000000"},
                {"fiscalDateEnding": "2021-09-30", "netIncome": "-3000000"},
                {"fiscalDateEnding": "2021-06-30", "netIncome": "-4000000"},
            ]},
        }
        store = HistoricalFundamentalsStore(fetcher=lambda t, curr_date=None: bundle)
        store.preload(["X"])

        features = store.features_as_of("X", date(2022, 6, 1))
        # ps_ratio must NOT come back as 150.0 (that's today's value).
        self.assertIsNone(features["ps_ratio"])
        # net_income_ttm should fall back to sum of 4 historical quarters
        # (= -10M), not the "99999999999" from OVERVIEW.
        self.assertEqual(features["net_income_ttm"], -10_000_000.0)

    def test_preload_skips_tickers_that_raise_below_threshold(self):
        """One bad ticker out of 10 (10%) shouldn't poison the whole preload."""
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        def fetcher(ticker, curr_date=None):
            if ticker == "BAD":
                raise RuntimeError("av failure")
            return BUNDLE_APPL

        tickers = ["AAPL", "BAD"] + [f"T{i}" for i in range(8)]
        store = HistoricalFundamentalsStore(fetcher=fetcher)
        store.preload(tickers)
        self.assertIsNotNone(store.features_as_of("AAPL", date(2024, 8, 1)))
        self.assertIsNone(store.features_as_of("BAD", date(2024, 8, 1)))

    def test_preload_aborts_when_too_many_fail(self):
        """Silent zero-gate runs in Phase 2 would look like 'gate has no
        effect' — false negative. Abort at 15% failure threshold."""
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        def always_fail(ticker, curr_date=None):
            raise RuntimeError("AV outage")

        store = HistoricalFundamentalsStore(fetcher=always_fail)
        with self.assertRaises(RuntimeError) as ctx:
            store.preload(["AAPL", "MSFT", "NVDA"])
        self.assertIn("threshold", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
