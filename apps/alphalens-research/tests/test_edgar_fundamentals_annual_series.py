"""Integration test for EdgarFundamentalsStore.annual_series_as_of.

Drives the store's public multi-year accessor against a synthetic
companyfacts parquet on disk, confirming the ticker->CIK resolution,
the dataclass shape, and the empty-list contract for an unknown ticker.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq

_CIK = "1234567890"

_EXPECTED_FIELDS = {
    "fiscal_year_end",
    "fy",
    "filed_date",
    "revenue",
    "operating_income",
    "net_income",
    "ocf",
    "capex",
    "da",
    "total_equity",
    "long_term_debt",
    "short_term_debt",
    "cash_and_equivalents",
    "shares_outstanding",
}


def _row(**kw):
    return {
        "taxonomy": kw.get("taxonomy", "us-gaap"),
        "concept": kw["concept"],
        "unit": kw.get("unit", "USD"),
        "period_start": date.fromisoformat(kw["period_start"]) if kw.get("period_start") else None,
        "period_end": date.fromisoformat(kw["period_end"]),
        "val": float(kw["val"]),
        "accn": kw.get("accn", "x"),
        "fy": kw.get("fy", 2024),
        "fp": kw.get("fp", "FY"),
        "form": kw.get("form", "10-K"),
        "filed_date": date.fromisoformat(kw["filed_date"]),
        "frame": kw.get("frame"),
    }


def _write_parquet(path: Path, rows: list[dict]) -> None:
    table = pa.table(
        {
            "taxonomy": pa.array([r["taxonomy"] for r in rows], type=pa.string()),
            "concept": pa.array([r["concept"] for r in rows], type=pa.string()),
            "unit": pa.array([r["unit"] for r in rows], type=pa.string()),
            "period_start": pa.array([r["period_start"] for r in rows], type=pa.date32()),
            "period_end": pa.array([r["period_end"] for r in rows], type=pa.date32()),
            "val": pa.array([r["val"] for r in rows], type=pa.float64()),
            "accn": pa.array([r["accn"] for r in rows], type=pa.string()),
            "fy": pa.array([r["fy"] for r in rows], type=pa.int32()),
            "fp": pa.array([r["fp"] for r in rows], type=pa.string()),
            "form": pa.array([r["form"] for r in rows], type=pa.string()),
            "filed_date": pa.array([r["filed_date"] for r in rows], type=pa.date32()),
            "frame": pa.array([r["frame"] for r in rows], type=pa.string()),
        }
    )
    pq.write_table(table, path)


def _fy(concept: str, year: int, val: float) -> dict:
    return _row(
        concept=concept,
        period_start=f"{year}-01-01",
        period_end=f"{year}-12-31",
        val=val,
        filed_date=f"{year + 1}-02-15",
    )


class TestEdgarFundamentalsAnnualSeries(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cache = Path(self._tmp.name)
        rows = []
        for yr, rev in ((2020, 100.0), (2021, 200.0), (2022, 300.0)):
            rows.append(_fy("Revenues", yr, rev))
            rows.append(_fy("NetIncomeLoss", yr, rev / 20))
            rows.append(
                _row(
                    concept="StockholdersEquity",
                    period_start=None,
                    period_end=f"{yr}-12-31",
                    val=rev * 5,
                    filed_date=f"{yr + 1}-02-15",
                )
            )
        _write_parquet(self._cache / f"{_CIK}.parquet", rows)

    def tearDown(self):
        self._tmp.cleanup()

    def _build_store(self):
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

        store = EdgarFundamentalsStore(cache_dir=self._cache, sec_client=MagicMock())
        store._cik_for = lambda ticker: _CIK if ticker == "TEST" else None
        return store

    def test_happy_path_returns_annual_statements(self):
        store = self._build_store()
        series = store.annual_series_as_of("TEST", date(2024, 1, 1))
        self.assertEqual(len(series), 3)
        self.assertEqual(series[0].fiscal_year_end, date(2022, 12, 31))
        self.assertEqual(series[0].revenue, 300.0)
        self.assertEqual(series[0].total_equity, 1500.0)

    def test_unknown_ticker_returns_empty(self):
        store = self._build_store()
        self.assertEqual(store.annual_series_as_of("NOPE", date(2024, 1, 1)), [])

    def test_dataclass_has_expected_fields(self):
        store = self._build_store()
        series = store.annual_series_as_of("TEST", date(2024, 1, 1))
        field_names = {f.name for f in dataclasses.fields(series[0])}
        self.assertEqual(field_names, _EXPECTED_FIELDS)


if __name__ == "__main__":
    unittest.main()
