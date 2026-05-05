"""Tests for ``_refresh_one_ticker`` + ``_backfill_one_ticker`` helpers.

These per-ticker helpers were extracted from ``incremental_refresh_smd`` and
``backfill_smd_history`` during the SonarCloud cognitive-complexity refactor
(commit aabc586). The full bulk fetchers iterate over a list and aggregate
counts; the helpers handle a single ticker's read → fetch → write pipeline
with all error paths.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from alphalens.paper_trade.scorer_v9d import (
    _backfill_one_ticker,
    _refresh_one_ticker,
    backfill_smd_history,
    incremental_refresh_smd,
)


def _make_existing(
    n_days: int = 10, start: str = "2024-01-02", with_close: bool = True
) -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=n_days)
    rows = {
        "tradeDate": [d.strftime("%Y-%m-%d") for d in dates],
        "symbol": ["AAPL"] * n_days,
    }
    if with_close:
        rows["close"] = [100.0 + i for i in range(n_days)]
    return pd.DataFrame(rows)


def _make_new_rows(n_days: int = 5, start: str = "2024-01-16") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=n_days)
    return pd.DataFrame(
        {
            "tradeDate": [d.strftime("%Y-%m-%d") for d in dates],
            "symbol": ["AAPL"] * n_days,
            "close": [200.0 + i for i in range(n_days)],
        }
    )


class TestRefreshOneTicker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_skipped_missing_when_parquet_absent(self):
        fetcher = MagicMock()
        result = _refresh_one_ticker("AAPL", self.cache_dir, fetcher, date(2024, 2, 1))
        self.assertEqual(result, "skipped_missing")
        fetcher.assert_not_called()

    def test_skipped_uptodate_when_max_date_at_or_above_target(self):
        existing = _make_existing(n_days=10)
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock()
        result = _refresh_one_ticker("AAPL", self.cache_dir, fetcher, date(2024, 1, 5))
        self.assertEqual(result, "skipped_uptodate")
        fetcher.assert_not_called()

    def test_refreshed_when_fetcher_returns_new_rows(self):
        existing = _make_existing(n_days=10)
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(return_value=_make_new_rows())
        result = _refresh_one_ticker("AAPL", self.cache_dir, fetcher, date(2024, 1, 22))
        self.assertEqual(result, "refreshed")
        fetcher.assert_called_once()
        df = pd.read_parquet(self.cache_dir / "AAPL.parquet")
        self.assertEqual(len(df), 15)  # 10 existing + 5 new

    def test_skipped_uptodate_when_fetcher_returns_empty(self):
        existing = _make_existing(n_days=10)
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(return_value=pd.DataFrame())
        result = _refresh_one_ticker("AAPL", self.cache_dir, fetcher, date(2024, 1, 22))
        self.assertEqual(result, "skipped_uptodate")

    def test_skipped_uptodate_when_fetcher_returns_none(self):
        existing = _make_existing(n_days=10)
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(return_value=None)
        result = _refresh_one_ticker("AAPL", self.cache_dir, fetcher, date(2024, 1, 22))
        self.assertEqual(result, "skipped_uptodate")

    def test_errors_when_fetcher_raises(self):
        existing = _make_existing(n_days=10)
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(side_effect=RuntimeError("503"))
        result = _refresh_one_ticker("AAPL", self.cache_dir, fetcher, date(2024, 1, 22))
        self.assertEqual(result, "errors")

    def test_errors_when_existing_lacks_tradedate_column(self):
        bad = pd.DataFrame({"close": [100.0]})  # no tradeDate
        bad.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock()
        result = _refresh_one_ticker("AAPL", self.cache_dir, fetcher, date(2024, 1, 22))
        self.assertEqual(result, "errors")


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
        existing = _make_existing(n_days=10, start="2018-01-02")
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock()
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 5))
        self.assertEqual(result, "skipped_already_covered")
        fetcher.assert_not_called()

    def test_backfilled_when_fetcher_returns_older_rows(self):
        existing = _make_existing(n_days=10, start="2024-01-02")
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(return_value=_make_new_rows(n_days=5, start="2023-12-15"))
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2023, 12, 1))
        self.assertEqual(result, "backfilled")
        df = pd.read_parquet(self.cache_dir / "AAPL.parquet")
        self.assertEqual(len(df), 15)

    def test_skipped_no_coverage_when_fetcher_returns_empty(self):
        existing = _make_existing(n_days=10, start="2024-01-02")
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(return_value=pd.DataFrame())
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 1))
        self.assertEqual(result, "skipped_no_coverage")

    def test_errors_on_fetch_exception(self):
        existing = _make_existing(n_days=10, start="2024-01-02")
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(side_effect=RuntimeError("bad"))
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 1))
        self.assertEqual(result, "errors")

    def test_errors_on_invalid_tradedate_column(self):
        bad = pd.DataFrame({"tradeDate": [None, None], "close": [1.0, 2.0]})
        bad.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock()
        result = _backfill_one_ticker("AAPL", self.cache_dir, fetcher, date(2018, 1, 1))
        self.assertEqual(result, "errors")


class TestBulkFetchersAggregateCounts(unittest.TestCase):
    """Smoke-coverage of incremental_refresh_smd + backfill_smd_history (the
    public entry points that just iterate over the helpers)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_incremental_refresh_aggregates_counts(self):
        # AAPL exists + needs refresh; MISSING absent → skipped_missing.
        existing = _make_existing(n_days=5, start="2024-01-02")
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
        fetcher = MagicMock(side_effect=[_make_new_rows(n_days=2, start="2024-01-09"), None])
        counts = incremental_refresh_smd(
            ["AAPL", "MISSING"],
            target_end=date(2024, 1, 22),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0.0,
        )
        self.assertEqual(counts["refreshed"], 1)
        self.assertEqual(counts["skipped_missing"], 1)

    def test_backfill_aggregates_counts(self):
        existing = _make_existing(n_days=5, start="2024-01-02")
        existing.to_parquet(self.cache_dir / "AAPL.parquet")
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


if __name__ == "__main__":
    unittest.main()
