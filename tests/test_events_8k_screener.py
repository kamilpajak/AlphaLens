"""TDD tests for 8-K go/no-go screener (per Perplexity 4th-attempt plan)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import numpy as np
import pandas as pd


class TestParseItemsString(unittest.TestCase):
    def test_single_item(self):
        from alphalens.archive.events.eightk_screener import parse_items_string

        self.assertEqual(parse_items_string("5.02"), ["5.02"])

    def test_multiple_items_comma_separated(self):
        from alphalens.archive.events.eightk_screener import parse_items_string

        self.assertEqual(
            parse_items_string("5.07,9.01"),
            ["5.07", "9.01"],
        )

    def test_items_with_whitespace(self):
        from alphalens.archive.events.eightk_screener import parse_items_string

        self.assertEqual(parse_items_string(" 1.01 , 8.01 "), ["1.01", "8.01"])

    def test_empty_string_returns_empty_list(self):
        from alphalens.archive.events.eightk_screener import parse_items_string

        self.assertEqual(parse_items_string(""), [])
        self.assertEqual(parse_items_string(None), [])


class TestEightKFiling(unittest.TestCase):
    def test_is_frozen_dataclass(self):
        from alphalens.archive.events.eightk_screener import EightKFiling

        f = EightKFiling(
            cik="0000320193",
            ticker="AAPL",
            filing_date=pd.Timestamp("2024-05-02"),
            accession="0000320193-24-000052",
            items=("5.02", "9.01"),
        )
        with self.assertRaises(Exception):
            f.items = ("1.01",)


class TestExtractFilingsFromSubmissions(unittest.TestCase):
    def test_filters_only_8k_forms(self):
        from alphalens.archive.events.eightk_screener import extract_8k_filings

        submissions = {
            "filings": {
                "recent": {
                    "form": ["10-K", "8-K", "4", "8-K", "S-1"],
                    "filingDate": [
                        "2024-01-10",
                        "2024-02-15",
                        "2024-02-16",
                        "2024-03-20",
                        "2024-04-01",
                    ],
                    "accessionNumber": ["a", "b", "c", "d", "e"],
                    "items": ["", "5.02,9.01", "", "1.01", ""],
                }
            }
        }

        filings = extract_8k_filings(submissions=submissions, cik="0000320193", ticker="AAPL")

        self.assertEqual(len(filings), 2)
        self.assertEqual(filings[0].items, ("5.02", "9.01"))
        self.assertEqual(filings[0].filing_date, pd.Timestamp("2024-02-15"))
        self.assertEqual(filings[1].items, ("1.01",))

    def test_filters_by_date_range(self):
        from alphalens.archive.events.eightk_screener import extract_8k_filings

        submissions = {
            "filings": {
                "recent": {
                    "form": ["8-K", "8-K", "8-K"],
                    "filingDate": ["2023-01-10", "2024-06-15", "2025-02-20"],
                    "accessionNumber": ["a", "b", "c"],
                    "items": ["5.02", "1.01", "8.01"],
                }
            }
        }

        filings = extract_8k_filings(
            submissions=submissions,
            cik="0000320193",
            ticker="AAPL",
            start=pd.Timestamp("2024-01-01"),
            end=pd.Timestamp("2025-01-01"),
        )

        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0].items, ("1.01",))

    def test_empty_submissions_returns_empty(self):
        from alphalens.archive.events.eightk_screener import extract_8k_filings

        self.assertEqual(
            extract_8k_filings(submissions={"filings": {"recent": {}}}, cik="x", ticker="X"),
            [],
        )


class TestComputeAbnormalReturn(unittest.TestCase):
    def _flat_series(self, daily_ret: float, n: int = 400) -> pd.Series:
        idx = pd.date_range("2024-01-02", periods=n, freq="B")
        close = (1.0 + daily_ret) ** np.arange(n) * 100.0
        return pd.Series(close, index=idx)

    def test_abnormal_return_is_ticker_minus_benchmark(self):
        from alphalens.archive.events.eightk_screener import compute_abnormal_return

        ticker_close = self._flat_series(0.001)  # +0.1%/day
        bench_close = self._flat_series(0.0004)  # +0.04%/day

        # Event on day 50, 20-day window → CAR = (1.001^20 - 1) - (1.0004^20 - 1)
        event_date = ticker_close.index[50]
        car = compute_abnormal_return(
            ticker_close, bench_close, event_date=event_date, window_days=20
        )

        expected = (1.001**20 - 1.0) - (1.0004**20 - 1.0)
        self.assertAlmostEqual(car, expected, places=6)

    def test_returns_nan_when_insufficient_data_after_event(self):
        from alphalens.archive.events.eightk_screener import compute_abnormal_return

        ticker_close = self._flat_series(0.001, n=100)
        bench_close = self._flat_series(0.0004, n=100)

        event_date = ticker_close.index[90]  # only 9 bars forward
        car = compute_abnormal_return(
            ticker_close, bench_close, event_date=event_date, window_days=20
        )

        self.assertTrue(np.isnan(car))

    def test_handles_event_date_not_in_series(self):
        """Event filed on weekend/holiday — use next trading day as entry."""
        from alphalens.archive.events.eightk_screener import compute_abnormal_return

        ticker_close = self._flat_series(0.001)
        bench_close = self._flat_series(0.0004)

        # Saturday — series has business days only
        sat = ticker_close.index[50] + pd.Timedelta(days=1)  # Sunday
        car = compute_abnormal_return(ticker_close, bench_close, event_date=sat, window_days=20)
        self.assertFalse(np.isnan(car))


class TestAggregateCarByItem(unittest.TestCase):
    def test_aggregates_mean_std_count_per_item_per_window(self):
        from alphalens.archive.events.eightk_screener import aggregate_car_by_item

        # Fake records: 3 events of Item 1.01 with CARs [0.01, 0.015, 0.02] at 20d
        #               2 events of Item 8.01 with CARs [-0.005, 0.003] at 20d
        records = pd.DataFrame(
            {
                "item": ["1.01", "1.01", "1.01", "8.01", "8.01"],
                "window_days": [20, 20, 20, 20, 20],
                "car": [0.01, 0.015, 0.02, -0.005, 0.003],
            }
        )

        summary = aggregate_car_by_item(records)

        self.assertIn("1.01", summary["item"].values)
        self.assertIn("8.01", summary["item"].values)
        row_101 = summary[summary["item"] == "1.01"].iloc[0]
        # mean CAR = (0.01+0.015+0.02)/3 = 0.015 → 150 bps
        self.assertAlmostEqual(row_101["mean_car_bps"], 150.0, places=1)
        self.assertEqual(row_101["n"], 3)

    def test_multiple_windows_per_item(self):
        from alphalens.archive.events.eightk_screener import aggregate_car_by_item

        records = pd.DataFrame(
            {
                "item": ["1.01", "1.01", "1.01", "1.01"],
                "window_days": [5, 5, 20, 20],
                "car": [0.001, 0.002, 0.01, 0.015],
            }
        )

        summary = aggregate_car_by_item(records)

        self.assertEqual(len(summary), 2)  # (1.01 × 5), (1.01 × 20)
        row_5 = summary[(summary["item"] == "1.01") & (summary["window_days"] == 5)].iloc[0]
        row_20 = summary[(summary["item"] == "1.01") & (summary["window_days"] == 20)].iloc[0]
        # mean(0.001, 0.002) = 0.0015 → 15 bps
        self.assertAlmostEqual(row_5["mean_car_bps"], 15.0, places=1)
        # mean(0.01, 0.015) = 0.0125 → 125 bps
        self.assertAlmostEqual(row_20["mean_car_bps"], 125.0, places=1)

    def test_verdict_column_based_on_thresholds(self):
        """New verdict requires winsorized mean + t-stat + std bound."""
        from alphalens.archive.events.eightk_screener import aggregate_car_by_item

        # 50 records per item, low-noise signals
        rng = np.random.default_rng(42)
        records = pd.concat(
            [
                pd.DataFrame(
                    {
                        "item": ["strong"] * 50,
                        "window_days": [20] * 50,
                        "car": rng.normal(0.012, 0.005, 50).tolist(),
                    }
                ),
                pd.DataFrame(
                    {
                        "item": ["weak"] * 50,
                        "window_days": [20] * 50,
                        "car": rng.normal(0.002, 0.005, 50).tolist(),
                    }
                ),
                pd.DataFrame(
                    {
                        "item": ["middle"] * 50,
                        "window_days": [20] * 50,
                        "car": rng.normal(0.006, 0.005, 50).tolist(),
                    }
                ),
            ]
        )

        summary = aggregate_car_by_item(records)

        verdict_map = dict(zip(summary["item"], summary["verdict"]))
        self.assertEqual(verdict_map["strong"], "PROCEED")  # mean ~120 bps, tight std, high t
        self.assertEqual(verdict_map["weak"], "KILL")  # mean ~20 bps < 50 bps floor
        self.assertEqual(verdict_map["middle"], "GRAY")  # 60 bps: below 80 bps PROCEED floor


class TestRunScreen(unittest.TestCase):
    def _flat_price(self, daily_ret: float, n: int = 500) -> pd.Series:
        idx = pd.date_range("2023-01-02", periods=n, freq="B")
        return pd.Series((1.0 + daily_ret) ** np.arange(n) * 100.0, index=idx)

    def test_runs_end_to_end_with_mocked_client(self):
        from alphalens.archive.events.eightk_screener import run_screen

        # Mock SEC client returning one 8-K filing for AAPL
        client = MagicMock()
        client.fetch_submissions.return_value = {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "filingDate": ["2023-06-15"],
                    "accessionNumber": ["x"],
                    "items": ["1.01"],
                }
            }
        }

        # Price loader returns same synthetic series for ticker + benchmark
        ticker_close = self._flat_price(0.001)
        bench_close = self._flat_price(0.0004)

        def _loader(ticker: str) -> pd.Series:
            if ticker == "SPY":
                return bench_close
            return ticker_close

        result = run_screen(
            ticker_cik_pairs=[("AAPL", "0000320193")],
            sec_client=client,
            price_loader=_loader,
            benchmark="SPY",
            start=pd.Timestamp("2023-01-01"),
            end=pd.Timestamp("2024-01-01"),
            windows=(5, 20, 60),
        )

        self.assertIn("summary", result)
        summary = result["summary"]
        self.assertIn("item", summary.columns)
        self.assertIn("mean_car_bps", summary.columns)
        self.assertIn("n", summary.columns)
        # One filing × 3 windows = 3 rows
        self.assertEqual(len(summary), 3)
        self.assertTrue(all(summary["item"] == "1.01"))


class TestRobustStatistics(unittest.TestCase):
    def test_aggregate_reports_median_and_tstat(self):
        from alphalens.archive.events.eightk_screener import aggregate_car_by_item

        # 100 filings: 99 modest, 1 extreme outlier
        cars = [0.005] * 99 + [1.0]
        records = pd.DataFrame(
            {
                "item": ["1.01"] * 100,
                "window_days": [20] * 100,
                "car": cars,
            }
        )

        summary = aggregate_car_by_item(records)
        row = summary.iloc[0]

        self.assertIn("median_car_bps", summary.columns)
        self.assertIn("tstat", summary.columns)
        self.assertIn("winsorized_mean_bps", summary.columns)
        # Median = 50 bps (robust to outlier); mean much higher
        self.assertAlmostEqual(row["median_car_bps"], 50.0, places=1)
        # t-stat = mean / SEM; with 1 extreme outlier inflating std, t should be modest
        self.assertLess(abs(row["tstat"]), 2.0)
        # Winsorized (5-95% with n=100 clips top 5 values) → close to 50 bps
        self.assertLess(abs(row["winsorized_mean_bps"] - 50.0), 10.0)

    def test_verdict_requires_both_mean_and_robustness(self):
        """Per Perplexity: 'CAR > 80 bps AND std < 2%' for PROCEED.

        Implementation: also require t-stat > 2 so we never promote outlier-
        driven mean without underlying consistency.
        """
        from alphalens.archive.events.eightk_screener import aggregate_car_by_item

        # Clean high-signal: mean=200bps, low std, high t-stat → PROCEED
        clean = pd.DataFrame(
            {
                "item": ["clean"] * 100,
                "window_days": [20] * 100,
                "car": np.random.default_rng(1).normal(0.02, 0.01, 100).tolist(),
            }
        )
        # Noisy high-mean: one huge outlier dominates → std large, t-stat low
        noisy = pd.DataFrame(
            {
                "item": ["noisy"] * 100,
                "window_days": [20] * 100,
                "car": [0.0] * 99 + [2.0],  # mean ~200 bps but all from outlier
            }
        )

        summary = aggregate_car_by_item(pd.concat([clean, noisy], ignore_index=True))

        verdict = dict(zip(summary["item"], summary["verdict"]))
        # Clean signal gets PROCEED
        self.assertEqual(verdict["clean"], "PROCEED")
        # Noisy outlier-driven signal must NOT get PROCEED despite high mean
        self.assertNotEqual(verdict["noisy"], "PROCEED")


if __name__ == "__main__":
    unittest.main()
