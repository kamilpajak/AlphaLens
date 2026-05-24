"""Tests for the SIC-code-based ticker/industry/sector resolver.

This module replaces the former SimFin bulk-metadata `sector_peers` data path
(broken after PR #161 removed SimFin). Source of truth is a small parquet
file shipped alongside the module at
``alphalens_pipeline/data/fundamentals/sic_index.parquet``; it is regenerated from
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
from alphalens_pipeline.data.fundamentals import sic_index


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
        sic_index._load_sic3_peers.cache_clear()
        # Without these on teardown the parsed table from this test's
        # TemporaryDirectory would persist into subsequent tests and either
        # crash on a deleted path or silently serve the synthetic fixture.
        self.addCleanup(sic_index._load_index.cache_clear)
        self.addCleanup(sic_index._load_lookup_dicts.cache_clear)
        self.addCleanup(sic_index._load_sic3_peers.cache_clear)


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

    def test_returned_list_is_defensive_copy(self) -> None:
        """Regression: ``iter_sic_peers`` must return a copy of the cached
        peer list, not a reference to it.

        ``_load_lookup_dicts`` is ``@lru_cache``-memoized, so the inner
        ``sic_to_peers`` dict and its list values are reused across every
        call within a process. Returning a direct reference means that a
        downstream caller doing ``peers.append(...)`` /  ``peers.pop()``
        / ``peers.sort()`` silently corrupts the global cache for that
        SIC — every subsequent call for the same code would see the
        mutated list. Defensive copy at the boundary closes the leak.
        """
        first = sic_index.iter_sic_peers(3674)
        self.assertEqual(sorted(first), ["IONQ", "QUBT", "RGTI"])
        first.append("BOGUS")
        first.sort()
        second = sic_index.iter_sic_peers(3674)
        self.assertEqual(sorted(second), ["IONQ", "QUBT", "RGTI"])
        self.assertNotIn("BOGUS", second)


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


class TestIterSicPeersFallback(_PatchedIndexTestCase):
    # SIC 7372 + 7373 + 7374 are different 4-digit codes but share the
    # 3-digit prefix 737 ("Computer Services"). Quantum-computing tickers
    # under PR #197 motivating example. A small 4-digit cohort (n=2) plus
    # neighbours under 737 should aggregate to a respectable 3-digit cohort.
    rows = [
        {
            "ticker": "QUBT",
            "cik": "1",
            "sic": 7372,
            "sic_description": "Services-Prepackaged Software",
        },
        {
            "ticker": "MSFT",
            "cik": "2",
            "sic": 7372,
            "sic_description": "Services-Prepackaged Software",
        },
        {
            "ticker": "IONQ",
            "cik": "3",
            "sic": 7373,
            "sic_description": "Services-Computer Integrated Systems Design",
        },
        {
            "ticker": "ACN",
            "cik": "4",
            "sic": 7373,
            "sic_description": "Services-Computer Integrated Systems Design",
        },
        {
            "ticker": "RGTI",
            "cik": "5",
            "sic": 7374,
            "sic_description": "Services-Computer Processing",
        },
        {
            "ticker": "PEER6",
            "cik": "6",
            "sic": 7374,
            "sic_description": "Services-Computer Processing",
        },
        {
            "ticker": "PEER7",
            "cik": "7",
            "sic": 7372,
            "sic_description": "Services-Prepackaged Software",
        },
        {
            "ticker": "PEER8",
            "cik": "8",
            "sic": 7373,
            "sic_description": "Services-Computer Integrated Systems Design",
        },
        # Unrelated 4-digit cohort kept large to verify fallback does not
        # silently leak across 3-digit prefixes.
        {"ticker": "BIG1", "cik": "10", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "BIG2", "cik": "11", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "BIG3", "cik": "12", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "BIG4", "cik": "13", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "BIG5", "cik": "14", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "BIG6", "cik": "15", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "BIG7", "cik": "16", "sic": 3674, "sic_description": "Semiconductors"},
        {"ticker": "BIG8", "cik": "17", "sic": 3674, "sic_description": "Semiconductors"},
    ]

    def test_returns_sic4_when_cohort_meets_min(self) -> None:
        peers, level = sic_index.iter_sic_peers_fallback(3674, min_cohort=8)
        self.assertEqual(level, "sic4")
        self.assertEqual(sorted(peers), sorted(f"BIG{i}" for i in range(1, 9)))

    def test_falls_back_to_sic3_when_sic4_below_min(self) -> None:
        # SIC 7372 has only 3 tickers (QUBT, MSFT, PEER7). 3-digit prefix
        # 737 unions 7372/7373/7374 to 8 tickers, meeting min_cohort=8.
        peers, level = sic_index.iter_sic_peers_fallback(7372, min_cohort=8)
        self.assertEqual(level, "sic3")
        self.assertEqual(
            sorted(peers),
            sorted(["QUBT", "MSFT", "IONQ", "ACN", "RGTI", "PEER6", "PEER7", "PEER8"]),
        )

    def test_returns_thin_when_neither_sic4_nor_sic3_meets_min(self) -> None:
        peers, level = sic_index.iter_sic_peers_fallback(3674, min_cohort=100)
        self.assertEqual(level, "thin")
        self.assertEqual(peers, [])

    def test_none_sic_returns_thin(self) -> None:
        peers, level = sic_index.iter_sic_peers_fallback(None, min_cohort=8)
        self.assertEqual(level, "thin")
        self.assertEqual(peers, [])

    def test_never_falls_back_to_sic2(self) -> None:
        # Per Bhojraj-Lee-Oler 2003, 2-digit SIC is too heterogeneous for
        # cohort comparison (SIC 73 mixes temp staffing + software +
        # printing). A 2-digit hop would gather BIG1..BIG8 + 737 peers,
        # but the contract says STOP at 3-digit.
        peers, level = sic_index.iter_sic_peers_fallback(7372, min_cohort=100)
        self.assertEqual(level, "thin")
        self.assertEqual(peers, [])

    def test_sic3_excludes_unrelated_prefixes(self) -> None:
        peers, _ = sic_index.iter_sic_peers_fallback(7372, min_cohort=8)
        # BIG1..BIG8 share SIC 3674 — 3-digit prefix is 367, NOT 737.
        for big in (f"BIG{i}" for i in range(1, 9)):
            self.assertNotIn(big, peers)

    def test_returned_list_is_defensive_copy(self) -> None:
        peers, _ = sic_index.iter_sic_peers_fallback(3674, min_cohort=8)
        peers.append("BOGUS")
        peers2, _ = sic_index.iter_sic_peers_fallback(3674, min_cohort=8)
        self.assertNotIn("BOGUS", peers2)

    def test_peer_filter_applied_before_min_cohort_check(self) -> None:
        # Scenario from Gemini 3 Pro PR-215 review: SIC 3674 has 8 raw
        # peers; if the filter strips 7 of them (shells / penny stocks),
        # the cohort is effectively 1 — the resolver must NOT call this
        # sic4 just because the raw cohort cleared the floor. With 7
        # dropped and no sic3 backup, expect ``thin``.
        keep_only = {"BIG1"}

        def shell_filter(peers: list[str]) -> list[str]:
            return [p for p in peers if p in keep_only]

        peers, level = sic_index.iter_sic_peers_fallback(
            3674, min_cohort=8, peer_filter=shell_filter
        )
        self.assertEqual(level, "thin")
        self.assertEqual(peers, [])

    def test_peer_filter_returns_sic4_when_filtered_cohort_meets_floor(self) -> None:
        peers, level = sic_index.iter_sic_peers_fallback(
            3674, min_cohort=8, peer_filter=lambda ps: ps
        )
        self.assertEqual(level, "sic4")
        self.assertEqual(len(peers), 8)

    def test_peer_filter_applied_to_sic3_fallback_too(self) -> None:
        # SIC 7372 raw cohort = 3 (below floor 8); raw 3-digit cohort
        # 737 = 8 (meets floor). Apply a filter that drops half of the
        # 3-digit pool — final size 4 < 8 → ``thin``.
        keep_three = {"QUBT", "IONQ", "RGTI"}

        def filter_three(peers: list[str]) -> list[str]:
            return [p for p in peers if p in keep_three]

        peers, level = sic_index.iter_sic_peers_fallback(
            7372, min_cohort=8, peer_filter=filter_three
        )
        self.assertEqual(level, "thin")
        self.assertEqual(peers, [])


class TestMissingIndexFile(unittest.TestCase):
    """If the parquet artifact is missing the resolver must still degrade safely."""

    def setUp(self) -> None:
        nonexistent = Path("/tmp/__alphalens_nonexistent__/sic_index.parquet")
        self._patch = patch.object(sic_index, "_SIC_INDEX_PATH", nonexistent)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        sic_index._load_index.cache_clear()
        sic_index._load_lookup_dicts.cache_clear()
        sic_index._load_sic3_peers.cache_clear()
        self.addCleanup(sic_index._load_index.cache_clear)
        self.addCleanup(sic_index._load_lookup_dicts.cache_clear)
        self.addCleanup(sic_index._load_sic3_peers.cache_clear)

    def test_get_sic_returns_none_when_index_absent(self) -> None:
        self.assertIsNone(sic_index.get_sic("AAPL"))

    def test_iter_peers_returns_empty_when_index_absent(self) -> None:
        self.assertEqual(sic_index.iter_sic_peers(3674), [])

    def test_fallback_returns_thin_when_index_absent(self) -> None:
        peers, level = sic_index.iter_sic_peers_fallback(3674, min_cohort=8)
        self.assertEqual(level, "thin")
        self.assertEqual(peers, [])


if __name__ == "__main__":
    unittest.main()
