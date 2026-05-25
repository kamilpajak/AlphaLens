"""Sanity tests for the companyfacts fixture builders used by store regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

from tests.fixtures.companyfacts_fixtures import (
    APPLE_CIK,
    IPO_CIK,
    SPARSE_CIK,
    all_fixture_ciks,
    build_apple_facts,
    build_recent_ipo_facts,
    build_sparse_smallcap_facts,
    write_all_fixtures_as_parquet,
)


class TestFixtureBuilders(unittest.TestCase):
    def test_apple_fixture_has_all_seven_sloan_concepts_plus_eps_basic_and_diluted(self):
        facts = build_apple_facts()
        gaap = facts["facts"]["us-gaap"]
        expected_concepts = {
            "EarningsPerShareBasic",
            "EarningsPerShareDiluted",
            "AssetsCurrent",
            "CashAndCashEquivalentsAtCarryingValue",
            "LiabilitiesCurrent",
            "LongTermDebtCurrent",
            "IncomeTaxesPayable",
            "DepreciationAndAmortization",
            "Assets",
        }
        self.assertEqual(set(gaap.keys()), expected_concepts)
        # EPS Basic carries 8 normal entries plus 1 restatement entry.
        self.assertEqual(len(gaap["EarningsPerShareBasic"]["units"]["USD/shares"]), 9)

    def test_sparse_fixture_omits_depreciation_concept(self):
        facts = build_sparse_smallcap_facts()
        gaap = facts["facts"]["us-gaap"]
        self.assertIn("AssetsCurrent", gaap)
        self.assertNotIn("DepreciationAndAmortization", gaap)
        # Sloan -> None; SUE works (6 quarters).
        self.assertEqual(len(gaap["EarningsPerShareBasic"]["units"]["USD/shares"]), 6)

    def test_ipo_fixture_has_only_two_eps_entries_no_balance_sheet(self):
        facts = build_recent_ipo_facts()
        gaap = facts["facts"]["us-gaap"]
        self.assertEqual(set(gaap.keys()), {"EarningsPerShareBasic"})
        self.assertEqual(len(gaap["EarningsPerShareBasic"]["units"]["USD/shares"]), 2)

    def test_write_all_fixtures_persists_three_parquet_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "companyfacts_parquet"
            written = write_all_fixtures_as_parquet(target)

            self.assertEqual(set(written.keys()), set(all_fixture_ciks()))
            for cik, path in written.items():
                self.assertTrue(path.exists(), msg=f"missing {path}")
                self.assertEqual(path.name, f"{cik}.parquet")
                # Parquet must be readable + non-empty.
                table = pq.read_table(path)
                self.assertGreater(table.num_rows, 0)

    def test_apple_fixture_round_trip_preserves_restatement_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cf_parquet"
            written = write_all_fixtures_as_parquet(target)
            table = pq.read_table(written[APPLE_CIK])
            # Two entries for period_end 2023-04-01 in EPS Basic: original + restatement.
            from datetime import date

            eps_basic_rows = [
                r
                for r in table.to_pylist()
                if r["concept"] == "EarningsPerShareBasic" and r["period_end"] == date(2023, 4, 1)
            ]
            self.assertEqual(len(eps_basic_rows), 2)
            filed_dates = sorted(r["filed_date"] for r in eps_basic_rows)
            self.assertEqual(filed_dates, [date(2023, 5, 15), date(2024, 1, 15)])

    def test_sparse_fixture_round_trip_does_not_emit_depreciation_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cf_parquet"
            written = write_all_fixtures_as_parquet(target)
            table = pq.read_table(written[SPARSE_CIK])
            concepts = {r["concept"] for r in table.to_pylist()}
            self.assertNotIn("DepreciationAndAmortization", concepts)

    def test_ipo_fixture_round_trip_emits_only_eps_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cf_parquet"
            written = write_all_fixtures_as_parquet(target)
            table = pq.read_table(written[IPO_CIK])
            self.assertEqual(table.num_rows, 2)
            self.assertEqual({r["concept"] for r in table.to_pylist()}, {"EarningsPerShareBasic"})


if __name__ == "__main__":
    unittest.main()
