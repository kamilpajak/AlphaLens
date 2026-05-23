"""Production stores for distress_credit (companyfacts parquet readers).

Builds an inline synthetic CIK fixture with us-gaap:Liabilities (USD) and
us-gaap:CommonStockSharesOutstanding (shares) entries to exercise PIT
filtering, dei-fallback, missing-data paths, and the make_production_stores
wiring helper.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import pyarrow.parquet as pq
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    companyfacts_json_to_parquet_table,
)


def _build_facts(*, with_liab=True, with_us_gaap_shares=True, with_dei_shares=False) -> dict:
    """Two quarters of Liabilities + (optional) shares from either taxonomy."""
    quarters = [
        {"end": "2023-03-31", "filed": "2023-05-10", "fy": 2023, "fp": "Q1"},
        {"end": "2023-06-30", "filed": "2023-08-08", "fy": 2023, "fp": "Q2"},
    ]
    facts: dict = {"cik": 12345, "entityName": "Fix Inc.", "facts": {"us-gaap": {}, "dei": {}}}

    def _entry(*, end: str, filed: str, val: float, fy: int, fp: str) -> dict:
        return {
            "start": None,
            "end": end,
            "val": val,
            "accn": f"0000012345-{fp}",
            "fy": fy,
            "fp": fp,
            "form": "10-Q",
            "filed": filed,
        }

    if with_liab:
        liab_entries = []
        for idx, q in enumerate(quarters):
            liab_entries.append(
                _entry(
                    end=q["end"], filed=q["filed"], val=100e6 + idx * 10e6, fy=q["fy"], fp=q["fp"]
                )
            )
        facts["facts"]["us-gaap"]["Liabilities"] = {
            "label": "Total Liabilities",
            "units": {"USD": liab_entries},
        }

    if with_us_gaap_shares:
        s_entries = []
        for idx, q in enumerate(quarters):
            s_entries.append(
                _entry(
                    end=q["end"],
                    filed=q["filed"],
                    val=1_000_000 + idx * 50_000,
                    fy=q["fy"],
                    fp=q["fp"],
                )
            )
        facts["facts"]["us-gaap"]["CommonStockSharesOutstanding"] = {
            "label": "Shares Outstanding",
            "units": {"shares": s_entries},
        }

    if with_dei_shares:
        s_entries = []
        for idx, q in enumerate(quarters):
            s_entries.append(
                _entry(
                    end=q["end"],
                    filed=q["filed"],
                    val=2_000_000 + idx * 100_000,
                    fy=q["fy"],
                    fp=q["fp"],
                )
            )
        facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"] = {
            "label": "DEI Shares",
            "units": {"shares": s_entries},
        }

    return facts


def _persist_facts(facts: dict, root: Path, cik: str = "0000012345") -> Path:
    table = companyfacts_json_to_parquet_table(facts)
    path = root / f"{cik}.parquet"
    pq.write_table(table, path)
    return path


class _StaticTickerCikMap:
    """Minimal stand-in for TickerCikMap.lookup."""

    def __init__(self, mapping: dict[str, str]):
        self._m = mapping

    def lookup(self, ticker: str) -> str | None:
        return self._m.get(ticker.upper())


class CompanyfactsLiabilitiesStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _persist_facts(_build_facts(), self.root, cik="0000012345")
        from alphalens_research.screeners.distress_credit.features import (
            CompanyfactsLiabilitiesStore,
        )

        self.store = CompanyfactsLiabilitiesStore(
            ticker_cik_map=_StaticTickerCikMap({"FIX": "0000012345"}),
            reader=CompanyfactsParquetReader(self.root),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_latest_visible_value(self):
        # asof after second filing → returns Q2 value (110M)
        v = self.store.get("FIX", pd.Timestamp("2023-09-01"))
        self.assertEqual(v, 110_000_000.0)

    def test_strict_pit_filing_date_filter(self):
        # asof between Q1 filing (2023-05-10) and Q2 filing (2023-08-08) → Q1 value
        v = self.store.get("FIX", pd.Timestamp("2023-06-15"))
        self.assertEqual(v, 100_000_000.0)

    def test_returns_none_when_asof_before_first_filing(self):
        # asof before 2023-05-10 → no entries visible
        v = self.store.get("FIX", pd.Timestamp("2023-01-01"))
        self.assertIsNone(v)

    def test_returns_none_for_unknown_ticker(self):
        self.assertIsNone(self.store.get("ZZZ", pd.Timestamp("2023-09-01")))

    def test_returns_none_when_liabilities_concept_absent(self):
        from alphalens_research.screeners.distress_credit.features import (
            CompanyfactsLiabilitiesStore,
        )

        with TemporaryDirectory() as td:
            root = Path(td)
            _persist_facts(_build_facts(with_liab=False), root, cik="0000099999")
            store = CompanyfactsLiabilitiesStore(
                ticker_cik_map=_StaticTickerCikMap({"NOLIAB": "0000099999"}),
                reader=CompanyfactsParquetReader(root),
            )
            self.assertIsNone(store.get("NOLIAB", pd.Timestamp("2023-09-01")))

    def test_accepts_date_input_in_addition_to_timestamp(self):
        from datetime import date

        v = self.store.get("FIX", date(2023, 9, 1))
        self.assertEqual(v, 110_000_000.0)


class CompanyfactsShareCountStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_store(self, **kwargs):
        from alphalens_research.screeners.distress_credit.features import (
            CompanyfactsShareCountStore,
        )

        cik = "0000054321"
        _persist_facts(_build_facts(**kwargs), self.root, cik=cik)
        return CompanyfactsShareCountStore(
            ticker_cik_map=_StaticTickerCikMap({"FIX": cik}),
            reader=CompanyfactsParquetReader(self.root),
        )

    def test_returns_us_gaap_shares_when_present(self):
        store = self._make_store(with_us_gaap_shares=True, with_dei_shares=False)
        v = store.get("FIX", pd.Timestamp("2023-09-01"))
        self.assertEqual(v, 1_050_000.0)

    def test_falls_back_to_dei_when_us_gaap_absent(self):
        store = self._make_store(with_us_gaap_shares=False, with_dei_shares=True)
        v = store.get("FIX", pd.Timestamp("2023-09-01"))
        self.assertEqual(v, 2_100_000.0)

    def test_us_gaap_takes_precedence_over_dei(self):
        store = self._make_store(with_us_gaap_shares=True, with_dei_shares=True)
        v = store.get("FIX", pd.Timestamp("2023-09-01"))
        # us-gaap returns 1.05M; dei would return 2.1M — primary path wins
        self.assertEqual(v, 1_050_000.0)

    def test_returns_none_when_both_absent(self):
        store = self._make_store(with_us_gaap_shares=False, with_dei_shares=False)
        self.assertIsNone(store.get("FIX", pd.Timestamp("2023-09-01")))

    def test_returns_none_for_unknown_ticker(self):
        store = self._make_store(with_us_gaap_shares=True)
        self.assertIsNone(store.get("ZZZ", pd.Timestamp("2023-09-01")))

    def test_returns_none_before_first_filing(self):
        store = self._make_store(with_us_gaap_shares=True)
        self.assertIsNone(store.get("FIX", pd.Timestamp("2022-01-01")))


class MakeProductionStoresTests(unittest.TestCase):
    def test_factory_wires_both_stores_against_real_yaml_and_parquet_dir(self):
        """Smoke test for make_production_stores. Builds an isolated CIK
        parquet + a YAML ticker→CIK map then verifies both stores read
        them correctly. Exercises the factory's path-resolution + reader
        construction code path."""
        from alphalens_research.screeners.distress_credit.features import (
            make_production_stores,
        )

        with TemporaryDirectory() as td:
            root = Path(td)
            parquet_dir = root / "companyfacts_parquet"
            parquet_dir.mkdir()
            cik = "0000012345"
            _persist_facts(_build_facts(), parquet_dir, cik=cik)

            # Synthetic ticker→CIK YAML
            ticker_map_path = root / "ticker_cik_map.yaml"
            ticker_map_path.write_text("FIX: '0000012345'\n", encoding="utf-8")

            liab_store, share_store = make_production_stores(
                parquet_dir=parquet_dir, ticker_cik_map_path=ticker_map_path
            )
            asof = pd.Timestamp("2023-09-01")
            self.assertEqual(liab_store.get("FIX", asof), 110_000_000.0)
            self.assertEqual(share_store.get("FIX", asof), 1_050_000.0)


if __name__ == "__main__":
    unittest.main()
