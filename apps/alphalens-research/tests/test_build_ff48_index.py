"""Tests for the FF-48 crosswalk parquet builder script.

Focus is the pure-Python parser — the network-bound ``_fetch_siccodes48``
helper is exercised manually whenever the build is rerun. The shipped
parquet itself is verified by ``TestShippedCrosswalkSchema``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_ff48_index  # noqa: E402

_FIXTURE_TEXT = """
 1 Agric  Agriculture
          0100-0199 Agricultural production - crops
          0700-0799 Agricultural services
          2048-2048 Prepared feeds for animals

 2 Food   Food Products
          2000-2009 Food and kindred products
          2010-2019 Meat products

34 BusSv  Business Services
          7370-7379 Computer services
          7380-7389 Misc business services
          7390-7399 Computer services and related

48 Other  Almost Nothing
          4950-4959 Sanitary services
          4990-4991 Cogeneration - SM power producer
"""


class TestParseSiccodes48(unittest.TestCase):
    def test_extracts_industry_header_id_short_and_long_name(self) -> None:
        industries, _ = build_ff48_index._parse_siccodes48(_FIXTURE_TEXT)
        ids = [i["id"] for i in industries]
        shorts = [i["short"] for i in industries]
        names = [i["name"] for i in industries]
        self.assertEqual(ids, [1, 2, 34, 48])
        self.assertEqual(shorts, ["Agric", "Food", "BusSv", "Other"])
        self.assertIn("Business Services", names)
        self.assertIn("Almost Nothing", names)

    def test_parses_every_sic_range_under_each_header(self) -> None:
        _, ranges = build_ff48_index._parse_siccodes48(_FIXTURE_TEXT)
        # 3 + 2 + 3 + 2 = 10 range rows in the fixture.
        self.assertEqual(len(ranges), 10)
        # Spot-check the DFIN range (the issue #198 motivation).
        dfin_range = next(r for r in ranges if r["sic_low"] == 7380 and r["sic_high"] == 7389)
        self.assertEqual(dfin_range["ff48_id"], 34)

    def test_range_lines_use_inclusive_bounds(self) -> None:
        _, ranges = build_ff48_index._parse_siccodes48(_FIXTURE_TEXT)
        agric_range = next(r for r in ranges if r["sic_low"] == 100 and r["sic_high"] == 199)
        self.assertEqual(agric_range["ff48_id"], 1)

    def test_blank_lines_do_not_break_parsing(self) -> None:
        # The Ken French file uses blank lines as section separators —
        # the parser must tolerate them anywhere without emitting bogus
        # ranges or losing the current_id state.
        noisy = _FIXTURE_TEXT.replace("\n\n", "\n\n\n\n")
        industries, ranges = build_ff48_index._parse_siccodes48(noisy)
        self.assertEqual(len(industries), 4)
        self.assertEqual(len(ranges), 10)

    def test_returns_empty_lists_for_empty_input(self) -> None:
        industries, ranges = build_ff48_index._parse_siccodes48("")
        self.assertEqual(industries, [])
        self.assertEqual(ranges, [])

    def test_parses_when_leading_whitespace_is_stripped(self) -> None:
        # Defensive guard against a hypothetical upstream formatting
        # change. Both regexes use ``^\s*`` so range vs header dispatch
        # falls back to digit-count discrimination (``\d{1,2}\s+...``
        # headers cannot collide with ``\d{4}-\d{4}`` ranges because the
        # dash terminates the leading token). Verified with PR #216 zen
        # review.
        stripped = (
            "1 Agric  Agriculture\n"
            "0100-0199 Agricultural production - crops\n"
            "34 BusSv  Business Services\n"
            "7380-7389 Misc business services\n"
        )
        industries, ranges = build_ff48_index._parse_siccodes48(stripped)
        self.assertEqual([i["id"] for i in industries], [1, 34])
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[0]["ff48_id"], 1)
        self.assertEqual(ranges[1]["ff48_id"], 34)
        self.assertEqual(ranges[1]["sic_low"], 7380)


class TestJoinIndustryLabels(unittest.TestCase):
    def test_decorates_each_range_with_short_and_name(self) -> None:
        industries, ranges = build_ff48_index._parse_siccodes48(_FIXTURE_TEXT)
        rows = build_ff48_index._join_industry_labels(industries, ranges)
        self.assertEqual(len(rows), len(ranges))
        bus_row = next(r for r in rows if r["sic_low"] == 7380)
        self.assertEqual(bus_row["ff48_short"], "BusSv")
        self.assertEqual(bus_row["ff48_name"], "Business Services")


class TestShippedCrosswalkSchema(unittest.TestCase):
    """Guard: the committed ``ff48_crosswalk.parquet`` must satisfy
    the contract the runtime resolver depends on.

    Refresh path: rerun ``scripts/build_ff48_index.py`` and commit the
    new artifact. A regression here means the source file format changed
    upstream, or the builder lost a column.
    """

    def test_shipped_parquet_has_48_distinct_industries(self) -> None:
        import pyarrow.parquet as pq
        from alphalens_pipeline.data.fundamentals import ff_industries

        if not ff_industries._FF48_CROSSWALK_PATH.exists():
            self.skipTest("ff48_crosswalk.parquet not present in this checkout")
        table = pq.read_table(ff_industries._FF48_CROSSWALK_PATH)
        ids = {int(x) for x in table.column("ff48_id").to_pylist()}
        # Ken French FF-48 covers ids 1..48 inclusive; missing any
        # signals a parser bug or a truncated upstream file.
        self.assertEqual(ids, set(range(1, 49)))

    def test_shipped_parquet_has_expected_columns(self) -> None:
        import pyarrow.parquet as pq
        from alphalens_pipeline.data.fundamentals import ff_industries

        if not ff_industries._FF48_CROSSWALK_PATH.exists():
            self.skipTest("ff48_crosswalk.parquet not present in this checkout")
        schema = pq.read_schema(ff_industries._FF48_CROSSWALK_PATH)
        self.assertEqual(
            set(schema.names),
            {"sic_low", "sic_high", "ff48_id", "ff48_short", "ff48_name"},
        )


if __name__ == "__main__":
    unittest.main()
