"""Tests for the Fama-French 48-industry resolver.

Hermetic — every test monkey-patches the module-level parquet paths so
the shipped artifacts are not read. The end-to-end "shipped parquet is
parseable" guard lives in ``test_build_ff48_index.py``.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.data.fundamentals import ff_industries, sic_index


def _write_crosswalk(path: Path, rows: list[tuple[int, int, int, str, str]]) -> None:
    table = pa.Table.from_pylist(
        [
            {
                "sic_low": lo,
                "sic_high": hi,
                "ff48_id": fid,
                "ff48_short": short,
                "ff48_name": name,
            }
            for lo, hi, fid, short, name in rows
        ],
        schema=pa.schema(
            [
                ("sic_low", pa.int32()),
                ("sic_high", pa.int32()),
                ("ff48_id", pa.int8()),
                ("ff48_short", pa.string()),
                ("ff48_name", pa.string()),
            ]
        ),
    )
    pq.write_table(table, path)


def _write_sic_index(path: Path, rows: list[tuple[str, str, int, str]]) -> None:
    table = pa.Table.from_pylist(
        [{"ticker": t, "cik": c, "sic": s, "sic_description": d} for t, c, s, d in rows],
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


class _CrosswalkFixture(unittest.TestCase):
    """Common fixture: a tiny crosswalk + sic_index pinned via monkey-patch.

    Crosswalk: industry 34 BusSv covers SIC 7370-7399 (the DFIN +
    quantum-computing case); industry 35 Comps covers SIC 3570-3579;
    industry 1 Agric covers SIC 100-199. Anything outside these
    ranges falls to industry 48 Other (conventional FF-48 catch-all).
    """

    CROSSWALK_ROWS = [
        (100, 199, 1, "Agric", "Agriculture"),
        (3570, 3579, 35, "Comps", "Computers"),
        (7370, 7379, 34, "BusSv", "Business Services"),
        (7380, 7389, 34, "BusSv", "Business Services"),
        (7390, 7399, 34, "BusSv", "Business Services"),
    ]
    SIC_ROWS = [
        # BusSv cohort — diverse SICs that should all collapse to ff48=34.
        ("QUBT", "0001", 7372, "Computer programming services"),
        ("IONQ", "0002", 7373, "Computer integrated systems design"),
        ("RGTI", "0003", 7371, "Computer services"),
        ("DFIN", "0004", 7380, "Services-Misc business services"),
        # Comps cohort.
        ("AAPL", "0005", 3571, "Electronic computers"),
        # Agric.
        ("FARM", "0006", 100, "Agricultural production"),
        # SIC outside any FF-48 explicit range → ff48 catch-all (48).
        ("XYZS", "0007", 9999, "Unmapped"),
    ]

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        tmp = Path(self._tmp.name)
        cw = tmp / "ff48_crosswalk.parquet"
        sic = tmp / "sic_index.parquet"
        _write_crosswalk(cw, self.CROSSWALK_ROWS)
        _write_sic_index(sic, self.SIC_ROWS)
        # Patch in-process artifact paths + invalidate the per-process
        # @lru_cache so each test sees the fixture.
        self._orig_ff48 = ff_industries._FF48_CROSSWALK_PATH
        self._orig_sic = sic_index._SIC_INDEX_PATH
        ff_industries._FF48_CROSSWALK_PATH = cw
        sic_index._SIC_INDEX_PATH = sic
        ff_industries._load_ranges.cache_clear()
        ff_industries._load_ff48_lookups.cache_clear()
        ff_industries._load_ff48_peers.cache_clear()
        sic_index._load_index.cache_clear()
        sic_index._load_lookup_dicts.cache_clear()
        sic_index._load_sic3_peers.cache_clear()

    def tearDown(self) -> None:
        ff_industries._FF48_CROSSWALK_PATH = self._orig_ff48
        sic_index._SIC_INDEX_PATH = self._orig_sic
        ff_industries._load_ranges.cache_clear()
        ff_industries._load_ff48_lookups.cache_clear()
        ff_industries._load_ff48_peers.cache_clear()
        sic_index._load_index.cache_clear()
        sic_index._load_lookup_dicts.cache_clear()
        sic_index._load_sic3_peers.cache_clear()
        self._tmp.cleanup()


class TestSicToFf48(_CrosswalkFixture):
    def test_known_range_returns_industry_id(self) -> None:
        self.assertEqual(ff_industries.sic_to_ff48(7372), 34)
        self.assertEqual(ff_industries.sic_to_ff48(7380), 34)
        self.assertEqual(ff_industries.sic_to_ff48(7399), 34)
        self.assertEqual(ff_industries.sic_to_ff48(3571), 35)
        self.assertEqual(ff_industries.sic_to_ff48(150), 1)

    def test_unmapped_sic_falls_to_other_48(self) -> None:
        # SIC outside every range in the crosswalk → FF-48 #48 ("Other").
        # This is the Fama-French convention — industry 48 is the residual.
        self.assertEqual(ff_industries.sic_to_ff48(9999), 48)
        self.assertEqual(ff_industries.sic_to_ff48(5000), 48)

    def test_none_input_returns_none(self) -> None:
        # Distinguish "no SIC available" (caller has no peer cohort to widen)
        # from "SIC present but unmapped" (legitimately FF-48 #48).
        self.assertIsNone(ff_industries.sic_to_ff48(None))

    def test_range_boundary_inclusive(self) -> None:
        # Both endpoints of an explicit range must match — off-by-one bugs
        # in the lookup would lose every boundary SIC.
        self.assertEqual(ff_industries.sic_to_ff48(3570), 35)
        self.assertEqual(ff_industries.sic_to_ff48(3579), 35)


class TestGetFf48(_CrosswalkFixture):
    def test_known_ticker_resolves_via_sic(self) -> None:
        self.assertEqual(ff_industries.get_ff48("QUBT"), 34)
        self.assertEqual(ff_industries.get_ff48("AAPL"), 35)

    def test_unknown_ticker_returns_none(self) -> None:
        self.assertIsNone(ff_industries.get_ff48("NEVER"))

    def test_ticker_with_unmapped_sic_returns_48(self) -> None:
        # Ticker exists with SIC=9999; SIC is real but not in any FF-48
        # explicit range → industry 48 (Other), not None.
        self.assertEqual(ff_industries.get_ff48("XYZS"), 48)

    def test_case_insensitive_ticker_lookup(self) -> None:
        self.assertEqual(ff_industries.get_ff48("qubt"), 34)


class TestGetFf48Label(_CrosswalkFixture):
    def test_returns_short_and_long_name(self) -> None:
        self.assertEqual(ff_industries.get_ff48_label(34), ("BusSv", "Business Services"))
        self.assertEqual(ff_industries.get_ff48_label(35), ("Comps", "Computers"))

    def test_unknown_industry_returns_none(self) -> None:
        # 99 isn't in the FF-48 taxonomy at all — distinct from "Other".
        self.assertIsNone(ff_industries.get_ff48_label(99))


class TestIterFf48Peers(_CrosswalkFixture):
    def test_aggregates_across_member_sics(self) -> None:
        # All four BusSv tickers (across SIC 7371-7380) collapse into one
        # cohort — the cohort-splitting fix that motivates #198.
        peers = ff_industries.iter_ff48_peers(34)
        self.assertEqual(set(peers), {"QUBT", "IONQ", "RGTI", "DFIN"})

    def test_single_member_cohort(self) -> None:
        peers = ff_industries.iter_ff48_peers(35)
        self.assertEqual(peers, ["AAPL"])

    def test_unknown_industry_returns_empty(self) -> None:
        self.assertEqual(ff_industries.iter_ff48_peers(99), [])

    def test_other_bucket_contains_unmapped_sics(self) -> None:
        # XYZS has SIC 9999 (unmapped) → falls into FF-48 #48 Other.
        self.assertEqual(ff_industries.iter_ff48_peers(48), ["XYZS"])

    def test_returned_list_is_defensive_copy(self) -> None:
        # Mirror of the same invariant on sic_index.iter_sic_peers — the
        # internal lookup dict is process-wide cached, so a caller's mutation
        # would corrupt every subsequent call.
        first = ff_industries.iter_ff48_peers(34)
        first.append("EVIL")
        second = ff_industries.iter_ff48_peers(34)
        self.assertNotIn("EVIL", second)

    def test_none_input_returns_empty(self) -> None:
        self.assertEqual(ff_industries.iter_ff48_peers(None), [])


if __name__ == "__main__":
    unittest.main()
