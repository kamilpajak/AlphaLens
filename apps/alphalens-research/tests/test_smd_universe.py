"""Tests for retrospective_audit.smd_universe — pit_union + backfill_smd_history."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import yaml
from alphalens_research.retrospective_audit.smd_universe import (
    DEFAULT_ETFS,
    _backfill_one_ticker,
    backfill_smd_history,
    pit_union,
)


def _make_existing(n_days: int = 10, start: str = "2024-01-02") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=n_days)
    return pd.DataFrame(
        {
            "tradeDate": [d.strftime("%Y-%m-%d") for d in dates],
            "symbol": ["AAPL"] * n_days,
            "close": [100.0 + i for i in range(n_days)],
        }
    )


def _make_new_rows(n_days: int = 5, start: str = "2023-12-15") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=n_days)
    return pd.DataFrame(
        {
            "tradeDate": [d.strftime("%Y-%m-%d") for d in dates],
            "symbol": ["AAPL"] * n_days,
            "close": [200.0 + i for i in range(n_days)],
        }
    )


class TestPitUnion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pit_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_snapshot(self, year: int, tickers: list[str]) -> None:
        path = self.pit_dir / f"{year}-12-31.yaml"
        path.write_text(yaml.safe_dump({"tickers": tickers}))

    def test_unions_tickers_across_snapshots_at_or_after_start_year(self):
        self._write_snapshot(2017, ["OLD"])
        self._write_snapshot(2018, ["AAPL", "MSFT"])
        self._write_snapshot(2019, ["MSFT", "NVDA"])
        union = pit_union(start_year=2018, pit_dir=self.pit_dir, extra_etfs=())
        self.assertEqual(union, sorted(["AAPL", "MSFT", "NVDA"]))

    def test_extra_etfs_merged_in(self):
        self._write_snapshot(2024, ["AAPL"])
        union = pit_union(start_year=2024, pit_dir=self.pit_dir, extra_etfs=("SPY", "QQQ"))
        self.assertIn("SPY", union)
        self.assertIn("QQQ", union)
        self.assertIn("AAPL", union)

    def test_default_etfs_count(self):
        self.assertEqual(len(DEFAULT_ETFS), 8)
        self.assertIn("IWM", DEFAULT_ETFS)

    def test_skips_files_with_non_year_stem(self):
        (self.pit_dir / "garbage.yaml").write_text(yaml.safe_dump({"tickers": ["JUNK"]}))
        self._write_snapshot(2024, ["AAPL"])
        union = pit_union(start_year=2024, pit_dir=self.pit_dir, extra_etfs=())
        self.assertEqual(union, ["AAPL"])

    def test_empty_dir_yields_only_etfs(self):
        union = pit_union(start_year=2024, pit_dir=self.pit_dir, extra_etfs=("SPY",))
        self.assertEqual(union, ["SPY"])


class TestBackfillOneTicker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_skipped_missing_when_parquet_absent(self):
        fetcher = MagicMock()
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 1))
        self.assertEqual(result, "skipped_missing")
        fetcher.assert_not_called()

    def test_skipped_already_covered_when_min_at_or_below_target(self):
        _make_existing(n_days=10, start="2018-01-02").to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock()
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 5))
        self.assertEqual(result, "skipped_already_covered")
        fetcher.assert_not_called()

    def test_backfilled_when_fetcher_returns_older_rows(self):
        _make_existing(n_days=10, start="2024-01-02").to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(return_value=_make_new_rows(n_days=5, start="2023-12-15"))
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2023, 12, 1))
        self.assertEqual(result, "backfilled")
        df = pd.read_parquet(self.cache_dir / "AAPL.parquet")
        self.assertEqual(len(df), 15)

    def test_skipped_no_coverage_when_fetcher_returns_empty(self):
        _make_existing(n_days=10, start="2024-01-02").to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(return_value=pd.DataFrame())
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 1))
        self.assertEqual(result, "skipped_no_coverage")

    def test_errors_on_fetch_exception(self):
        _make_existing(n_days=10, start="2024-01-02").to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(side_effect=RuntimeError("bad"))
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 1))
        self.assertEqual(result, "errors")

    def test_errors_on_invalid_tradedate_column(self):
        pd.DataFrame({"tradeDate": [None, None], "close": [1.0, 2.0]}).to_parquet(
            self.cache_dir / "AAPL.parquet"
        )
        fetcher = MagicMock()
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 1))
        self.assertEqual(result, "errors")


class TestBackfillSmdHistoryAggregatesCounts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_aggregates_counts_across_tickers(self):
        _make_existing(n_days=5, start="2024-01-02").to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(side_effect=[_make_new_rows(n_days=2, start="2023-12-15"), None])
        counts = backfill_smd_history(
            ["AAPL", "MISSING"],
            target_start=date(2018, 1, 1),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0.0,
        )
        self.assertEqual(counts["backfilled"], 1)
        self.assertEqual(counts["skipped_missing"], 1)

    def test_creates_cache_dir_if_missing(self):
        nested = self.cache_dir / "nested" / "deeper"
        self.assertFalse(nested.exists())
        backfill_smd_history(
            [],
            target_start=date(2018, 1, 1),
            cache_dir=nested,
            fetcher=MagicMock(),
            sleep_between=0.0,
        )
        self.assertTrue(nested.exists())


if __name__ == "__main__":
    unittest.main()
