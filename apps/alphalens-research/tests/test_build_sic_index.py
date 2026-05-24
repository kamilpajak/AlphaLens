"""Tests for the SIC-index parquet builder script.

Focus is the pure-Python helpers — the SEC-fetching ``main()`` is exercised
manually (network-bound) per the monthly refresh cadence.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_sic_index  # noqa: E402


class TestDedupByCikKeepShortest(unittest.TestCase):
    def test_keeps_one_row_per_cik(self) -> None:
        pairs = [
            ("ASPS", "0001462418"),
            ("ASPSW", "0001462418"),
            ("ASPSZ", "0001462418"),
            ("AAPL", "0000320193"),
        ]
        out = build_sic_index._dedup_by_cik_keep_shortest(pairs)
        ciks = [cik for _, cik in out]
        self.assertEqual(len(ciks), len(set(ciks)))

    def test_picks_shortest_ticker_per_cik(self) -> None:
        # ASPS (4 chars) wins over ASPSW (5) and ASPSZ (5).
        pairs = [
            ("ASPSW", "0001462418"),
            ("ASPSZ", "0001462418"),
            ("ASPS", "0001462418"),
        ]
        out = build_sic_index._dedup_by_cik_keep_shortest(pairs)
        self.assertEqual(out, [("ASPS", "0001462418")])

    def test_alphabetical_tiebreak_on_equal_length(self) -> None:
        pairs = [("BAC", "0000070858"), ("AAC", "0000070858")]
        out = build_sic_index._dedup_by_cik_keep_shortest(pairs)
        self.assertEqual(out, [("AAC", "0000070858")])

    def test_collapses_warrant_and_preferred_to_common(self) -> None:
        # Freddie Mac (CIK 0001026214) has FMCC + ~24 preferred series.
        pairs = [
            ("FMCCH", "0001026214"),
            ("FMCCI", "0001026214"),
            ("FMCCK", "0001026214"),
            ("FMCC", "0001026214"),
            ("FMCCP", "0001026214"),
        ]
        out = build_sic_index._dedup_by_cik_keep_shortest(pairs)
        self.assertEqual(out, [("FMCC", "0001026214")])

    def test_output_sorted_by_cik(self) -> None:
        pairs = [
            ("Z", "0000000003"),
            ("A", "0000000001"),
            ("M", "0000000002"),
        ]
        out = build_sic_index._dedup_by_cik_keep_shortest(pairs)
        self.assertEqual([cik for _, cik in out], ["0000000001", "0000000002", "0000000003"])

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(build_sic_index._dedup_by_cik_keep_shortest([]), [])


class TestShippedParquetIsCikDeduplicated(unittest.TestCase):
    """Guard: the committed ``sic_index.parquet`` must satisfy CIK uniqueness.

    Refresh path: rerun ``scripts/build_sic_index.py`` and commit the new
    artifact. A regression here means dedup logic was bypassed (e.g.
    someone removed the call in ``main()`` and the parquet was rebuilt).
    """

    def test_committed_parquet_has_one_row_per_cik(self) -> None:
        import pyarrow.parquet as pq
        from alphalens_pipeline.data.fundamentals import sic_index

        if not sic_index._SIC_INDEX_PATH.exists():
            self.skipTest("sic_index.parquet not present in this checkout")
        table = pq.read_table(sic_index._SIC_INDEX_PATH)
        ciks = table.column("cik").to_pylist()
        self.assertEqual(
            len(ciks),
            len(set(ciks)),
            f"sic_index.parquet has {len(ciks) - len(set(ciks))} duplicate CIK rows "
            "— rerun scripts/build_sic_index.py to regenerate",
        )


if __name__ == "__main__":
    unittest.main()
