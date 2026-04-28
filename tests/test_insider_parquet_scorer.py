"""Tests for alphalens.screeners.insider.parquet_scorer.ParquetInsiderScorer."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from alphalens.screeners.insider.parquet_scorer import ParquetInsiderScorer


def _write_test_dataset(root: Path) -> None:
    """Build a tiny hive-partitioned parquet matching the migration tool's schema.

    Three rows: one cluster hit, one cache-miss-no-cluster, one row with a
    different ticker. Partitioned by year=2020.
    """
    year_dir = root / "year=2020"
    year_dir.mkdir(parents=True)
    table = pa.table(
        {
            "ticker": ["AAPL", "MSFT", "TSLA"],
            "date": [date(2020, 6, 15), date(2020, 7, 1), date(2020, 8, 12)],
            "has_features": [True, False, True],
            "insider_count": [4, None, 7],
            "aggregate_dollar": [12500.0, None, 88000.0],
            "cluster_window_days": [30, None, 30],
            "asof": [date(2020, 6, 15), None, date(2020, 8, 12)],
            "cached_at": [None, None, None],
        }
    )
    pq.write_table(table, year_dir / "part-0.parquet")


class TestParquetInsiderScorer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "insider_form4.parquet"
        self.root.mkdir()
        _write_test_dataset(self.root)
        self.scorer = ParquetInsiderScorer(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            ParquetInsiderScorer(Path("/nonexistent/parquet/path"))

    def test_returns_features_for_cluster_hit(self):
        feat = self.scorer.features_as_of("AAPL", date(2020, 6, 15))

        self.assertIsNotNone(feat)
        self.assertEqual(feat["insider_count"], 4)
        self.assertEqual(feat["aggregate_dollar"], 12500.0)
        self.assertEqual(feat["cluster_window_days"], 30)

    def test_returns_none_for_cache_miss_with_no_cluster(self):
        feat = self.scorer.features_as_of("MSFT", date(2020, 7, 1))

        self.assertIsNone(feat)

    def test_returns_none_for_unknown_ticker(self):
        feat = self.scorer.features_as_of("ZZZZ", date(2020, 6, 15))

        self.assertIsNone(feat)

    def test_returns_none_for_unknown_date(self):
        feat = self.scorer.features_as_of("AAPL", date(2021, 1, 1))

        self.assertIsNone(feat)

    def test_ticker_lookup_is_case_insensitive(self):
        feat = self.scorer.features_as_of("aapl", date(2020, 6, 15))

        self.assertIsNotNone(feat)
        self.assertEqual(feat["insider_count"], 4)

    def test_asof_in_returned_dict_echoes_caller_supplied_argument(self):
        # Contract match with InsiderScorer: returned 'asof' is the user's
        # asof, not the parquet's stored asof. Still ISO-format string.
        feat = self.scorer.features_as_of("AAPL", date(2020, 6, 15))

        self.assertEqual(feat["asof"], "2020-06-15")

    def test_stats_reports_row_counts(self):
        stats = self.scorer.stats

        self.assertEqual(stats["total_rows"], 3)
        self.assertEqual(stats["with_features"], 2)
        self.assertEqual(stats["no_cluster"], 1)


if __name__ == "__main__":
    unittest.main()
