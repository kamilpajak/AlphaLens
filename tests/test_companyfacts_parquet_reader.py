"""Tests for CompanyfactsParquetReader: per-CIK Arrow Table accessor with cache.

Stores call ``reader.get_cik_table(cik)`` once per CIK and filter the resulting
table in-memory via vectorized pyarrow.compute. The reader's job is exactly
two things: (1) load the CIK's parquet file lazily and cheaply, (2) cache the
loaded Arrow Table so subsequent calls are free.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa

from alphalens.data.fundamentals.companyfacts_parquet import (
    SCHEMA,
    CompanyfactsParquetReader,
    filter_concept,
)
from tests.fixtures.companyfacts_fixtures import (
    APPLE_CIK,
    IPO_CIK,
    SPARSE_CIK,
    write_all_fixtures_as_parquet,
)


class _FixtureCase(unittest.TestCase):
    """Provides a temp parquet directory pre-populated with the three fixtures."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.parquet_dir = Path(self._tmp.name) / "companyfacts_parquet"
        write_all_fixtures_as_parquet(self.parquet_dir)
        self.reader = CompanyfactsParquetReader(self.parquet_dir)

    def tearDown(self):
        self._tmp.cleanup()


class TestGetCikTableHappyPath(_FixtureCase):
    def test_returns_arrow_table_for_apple_fixture(self):
        table = self.reader.get_cik_table(APPLE_CIK)
        self.assertIsInstance(table, pa.Table)
        self.assertEqual(table.schema, SCHEMA)
        self.assertGreater(table.num_rows, 0)
        # Apple fixture exercises EPS Basic + Diluted + 7 Sloan concepts.
        concepts = {r["concept"] for r in table.to_pylist()}
        self.assertIn("EarningsPerShareBasic", concepts)
        self.assertIn("EarningsPerShareDiluted", concepts)
        self.assertIn("Assets", concepts)

    def test_returns_arrow_table_for_sparse_fixture(self):
        table = self.reader.get_cik_table(SPARSE_CIK)
        self.assertIsInstance(table, pa.Table)
        concepts = {r["concept"] for r in table.to_pylist()}
        self.assertIn("EarningsPerShareBasic", concepts)
        self.assertNotIn("DepreciationAndAmortization", concepts)

    def test_returns_arrow_table_for_ipo_fixture(self):
        table = self.reader.get_cik_table(IPO_CIK)
        self.assertEqual(table.num_rows, 2)


class TestGetCikTableMissingCik(_FixtureCase):
    def test_missing_cik_returns_none(self):
        self.assertIsNone(self.reader.get_cik_table("0000000001"))

    def test_repeated_missing_cik_does_not_raise(self):
        # Should be idempotent / cacheable on the negative side too.
        self.assertIsNone(self.reader.get_cik_table("0000000001"))
        self.assertIsNone(self.reader.get_cik_table("0000000001"))


class TestGetCikTableCacheHit(_FixtureCase):
    def test_second_call_for_same_cik_does_not_re_read_file(self):
        # First call hits disk; second call must use cache. We patch the
        # internal pyarrow.parquet.read_table call so we can count invocations.
        original_read = self.reader._read_table_from_disk  # type: ignore[attr-defined]
        with patch.object(self.reader, "_read_table_from_disk", side_effect=original_read) as spy:
            self.reader.get_cik_table(APPLE_CIK)
            self.reader.get_cik_table(APPLE_CIK)
            self.reader.get_cik_table(APPLE_CIK)
            self.assertEqual(spy.call_count, 1)

    def test_different_ciks_each_trigger_a_disk_read(self):
        original_read = self.reader._read_table_from_disk  # type: ignore[attr-defined]
        with patch.object(self.reader, "_read_table_from_disk", side_effect=original_read) as spy:
            self.reader.get_cik_table(APPLE_CIK)
            self.reader.get_cik_table(SPARSE_CIK)
            self.reader.get_cik_table(IPO_CIK)
            self.assertEqual(spy.call_count, 3)


class TestCacheEviction(unittest.TestCase):
    """FIFO eviction once cache_capacity is exceeded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.parquet_dir = Path(self._tmp.name) / "companyfacts_parquet"
        write_all_fixtures_as_parquet(self.parquet_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_capacity_two_evicts_first_entry_when_third_loaded(self):
        reader = CompanyfactsParquetReader(self.parquet_dir, cache_capacity=2)
        original_read = reader._read_table_from_disk  # type: ignore[attr-defined]
        with patch.object(reader, "_read_table_from_disk", side_effect=original_read) as spy:
            reader.get_cik_table(APPLE_CIK)  # disk #1
            reader.get_cik_table(SPARSE_CIK)  # disk #2
            reader.get_cik_table(IPO_CIK)  # disk #3 -> APPLE evicted
            reader.get_cik_table(APPLE_CIK)  # disk #4 (re-read after eviction)
            self.assertEqual(spy.call_count, 4)

    def test_capacity_zero_disables_caching(self):
        reader = CompanyfactsParquetReader(self.parquet_dir, cache_capacity=0)
        original_read = reader._read_table_from_disk  # type: ignore[attr-defined]
        with patch.object(reader, "_read_table_from_disk", side_effect=original_read) as spy:
            reader.get_cik_table(APPLE_CIK)
            reader.get_cik_table(APPLE_CIK)
            self.assertEqual(spy.call_count, 2)


class TestFilterConcept(_FixtureCase):
    """Vectorized in-memory filter helper used by stores after get_cik_table."""

    def test_filter_returns_only_rows_matching_taxonomy_and_concept(self):
        table = self.reader.get_cik_table(APPLE_CIK)
        filtered = filter_concept(table, "us-gaap", "EarningsPerShareBasic")
        self.assertGreater(filtered.num_rows, 0)
        self.assertEqual(set(filtered["concept"].to_pylist()), {"EarningsPerShareBasic"})
        self.assertEqual(set(filtered["taxonomy"].to_pylist()), {"us-gaap"})

    def test_filter_with_unit_constrains_to_one_unit(self):
        # Apple Assets concept uses USD only, so filter with USD == filter without unit.
        table = self.reader.get_cik_table(APPLE_CIK)
        all_units = filter_concept(table, "us-gaap", "Assets")
        usd_only = filter_concept(table, "us-gaap", "Assets", unit="USD")
        self.assertEqual(usd_only.num_rows, all_units.num_rows)
        self.assertEqual(set(usd_only["unit"].to_pylist()), {"USD"})

    def test_filter_for_absent_concept_returns_empty_table_with_canonical_schema(self):
        table = self.reader.get_cik_table(SPARSE_CIK)
        result = filter_concept(table, "us-gaap", "DepreciationAndAmortization")
        self.assertIsInstance(result, pa.Table)
        self.assertEqual(result.num_rows, 0)
        self.assertEqual(result.schema, SCHEMA)

    def test_filter_for_absent_taxonomy_returns_empty_table(self):
        table = self.reader.get_cik_table(APPLE_CIK)
        result = filter_concept(table, "ifrs-full", "Revenue")
        self.assertEqual(result.num_rows, 0)

    def test_filter_with_wrong_unit_returns_empty(self):
        table = self.reader.get_cik_table(APPLE_CIK)
        result = filter_concept(table, "us-gaap", "Assets", unit="EUR")
        self.assertEqual(result.num_rows, 0)


if __name__ == "__main__":
    unittest.main()
