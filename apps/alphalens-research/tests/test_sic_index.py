"""Tests for the SIC-code-based ticker/industry/sector resolver.

This module replaces the former SimFin bulk-metadata `sector_peers` data path
(broken after PR #161 removed SimFin). Source of truth is a small parquet
file shipped alongside the module at
``alphalens_research/data/fundamentals/sic_index.parquet``; it is regenerated from
EDGAR companyfacts by ``scripts/build_sic_index.py``.

The public API mirrors the contract that `scorer.py` and `sector_peers.py`
depended on:

- ``get_sic(ticker) -> int | None``
- ``iter_sic_peers(sic) -> list[str]``
- ``sic_label(sic) -> (industry_name, sector_name)``
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_research.data.fundamentals import sic_index


def _write_synthetic_index(path: Path, rows: list[dict]) -> None:
    """Materialise a tiny SIC-index parquet at ``path``.

    ``rows`` items must carry ticker/cik/sic/sic_description keys.
    """
    table = pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                ("ticker", pa.string()),
                ("cik", pa.string()),
                ("sic", pa.int32()),
                ("sic_description", pa.string()),
            ]
        ),
    )
    pq.write_table(table, path)


class _PatchedIndexTestCase(unittest.TestCase):
    """Base case that points ``sic_index`` at a synthetic parquet for the test."""

    rows: list[dict] = []

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        index_path = Path(self._tmp.name) / "sic_index.parquet"
        _write_synthetic_index(index_path, self.rows)
        self._patch = patch.object(sic_index, "_SIC_INDEX_PATH", index_path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        sic_index._load_index.cache_clear()
        sic_index._load_lookup_dicts.cache_clear()
        # Without these on teardown the parsed table from this test's
        # TemporaryDirectory would persist into subsequent tests and either
        # crash on a deleted path or silently serve the synthetic fixture.
        self.addCleanup(sic_index._load_index.cache_clear)
        self.addCleanup(sic_index._load_lookup_dicts.cache_clear)


class TestGetSic(_PatchedIndexTestCase):
    rows = [
        {"ticker": "QUBT", "cik": "0001758009", "sic": 3674, "sic_description": "Semiconductors"},
        {
            "ticker": "AAPL",
            "cik": "0000320193",
            "sic": 3571,
            "sic_description": "Electronic Computers",
        },
    ]

    def test_known_ticker_returns_sic(self) -> None:
        self.assertEqual(sic_index.get_sic("QUBT"), 3674)
        self.assertEqual(sic_index.get_sic("AAPL"), 3571)

    def test_case_insensitive_lookup(self) -> None:
        self.assertEqual(sic_index.get_sic("qubt"), 3674)
        self.assertEqual(sic_index.get_sic("aApL"), 3571)

    def test_missing_ticker_returns_none(self) -> None:
        self.assertIsNone(sic_index.get_sic("NVDA"))

    def test_empty_ticker_returns_none(self) -> None:
        self.assertIsNone(sic_index.get_sic(""))


class TestIterSicPeers(_PatchedIndexTestCase):
    rows = [
        {"ticker": "QUBT", "cik": "1", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "IONQ", "cik": "2", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "RGTI", "cik": "3", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "AAPL", "cik": "4", "sic": 3571, "sic_description": "Electronic Computers"},
    ]

    def test_returns_all_tickers_with_same_sic(self) -> None:
        self.assertEqual(sorted(sic_index.iter_sic_peers(3674)), ["IONQ", "QUBT", "RGTI"])

    def test_unknown_sic_returns_empty(self) -> None:
        self.assertEqual(sic_index.iter_sic_peers(9999), [])

    def test_none_sic_returns_empty(self) -> None:
        # `iter_sic_peers(None)` is the fallback contract used by
        # ``score_candidates._resolve_industry`` when ``get_sic`` misses.
        self.assertEqual(sic_index.iter_sic_peers(None), [])  # type: ignore[arg-type]


class TestSicLabel(_PatchedIndexTestCase):
    rows = [
        {
            "ticker": "QUBT",
            "cik": "1",
            "sic": 3674,
            "sic_description": "Semiconductors & Related Devices",
        },
    ]

    def test_returns_industry_description_and_division_name(self) -> None:
        industry, sector = sic_index.sic_label(3674)
        self.assertEqual(industry, "Semiconductors & Related Devices")
        self.assertEqual(sector, "Manufacturing")

    def test_unknown_sic_returns_none_pair(self) -> None:
        self.assertEqual(sic_index.sic_label(9999), (None, None))

    def test_none_sic_returns_none_pair(self) -> None:
        self.assertEqual(sic_index.sic_label(None), (None, None))  # type: ignore[arg-type]


class TestSicDivisionRanges(unittest.TestCase):
    """The 4-digit SIC division mapping is hardcoded; verify each canonical bucket."""

    def test_agriculture_division_a(self) -> None:
        self.assertEqual(sic_index._division_name(800), "Agriculture, Forestry and Fishing")

    def test_mining_division_b(self) -> None:
        self.assertEqual(sic_index._division_name(1400), "Mining")

    def test_construction_division_c(self) -> None:
        self.assertEqual(sic_index._division_name(1700), "Construction")

    def test_manufacturing_division_d(self) -> None:
        self.assertEqual(sic_index._division_name(3674), "Manufacturing")

    def test_transportation_utilities_division_e(self) -> None:
        self.assertEqual(
            sic_index._division_name(4900),
            "Transportation, Communications, Electric, Gas and Sanitary services",
        )

    def test_wholesale_division_f(self) -> None:
        self.assertEqual(sic_index._division_name(5100), "Wholesale Trade")

    def test_retail_division_g(self) -> None:
        self.assertEqual(sic_index._division_name(5400), "Retail Trade")

    def test_finance_division_h(self) -> None:
        self.assertEqual(sic_index._division_name(6020), "Finance, Insurance and Real Estate")

    def test_services_division_i(self) -> None:
        self.assertEqual(sic_index._division_name(7372), "Services")

    def test_public_administration_division_j(self) -> None:
        self.assertEqual(sic_index._division_name(9100), "Public Administration")

    def test_below_minimum_returns_none(self) -> None:
        self.assertIsNone(sic_index._division_name(50))

    def test_above_maximum_returns_none(self) -> None:
        self.assertIsNone(sic_index._division_name(10000))


class TestMissingIndexFile(unittest.TestCase):
    """If the parquet artifact is missing the resolver must still degrade safely."""

    def setUp(self) -> None:
        nonexistent = Path("/tmp/__alphalens_nonexistent__/sic_index.parquet")
        self._patch = patch.object(sic_index, "_SIC_INDEX_PATH", nonexistent)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        sic_index._load_index.cache_clear()
        sic_index._load_lookup_dicts.cache_clear()
        self.addCleanup(sic_index._load_index.cache_clear)
        self.addCleanup(sic_index._load_lookup_dicts.cache_clear)

    def test_get_sic_returns_none_when_index_absent(self) -> None:
        self.assertIsNone(sic_index.get_sic("AAPL"))

    def test_iter_peers_returns_empty_when_index_absent(self) -> None:
        self.assertEqual(sic_index.iter_sic_peers(3674), [])


if __name__ == "__main__":
    unittest.main()
