"""Unit tests for the multi-year annual (FY) statement accessor.

Drives :func:`annual_statements` directly against a synthetic companyfacts
parquet on disk via :class:`CompanyfactsParquetReader`. Confirms:

- raw FY values (NOT TTM) are returned, newest fiscal_year_end first;
- the PIT contract (``filed_date <= asof``) holds, including restatements;
- instant concepts (equity / debt / cash / shares) are anchored on each
  year's fiscal-year end, not the latest snapshot;
- concept fallback chains and the D&A component-sum fallback work;
- quarterly / non-whitelisted-form rows never create a phantom year;
- ``max_years`` caps the series and the reader is hit exactly once.

The synthetic parquet matches the canonical schema emitted by
``companyfacts_json_to_parquet_table`` so the reader needs no shimming.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.data.fundamentals.annual_aggregator import annual_statements
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import CompanyfactsParquetReader

_CIK = "1234567890"


def _row(**kw):
    """Build a parquet-schema-compatible row dict (mirrors the store
    integration test helper)."""
    return {
        "taxonomy": kw.get("taxonomy", "us-gaap"),
        "concept": kw["concept"],
        "unit": kw.get("unit", "USD"),
        "period_start": date.fromisoformat(kw["period_start"]) if kw.get("period_start") else None,
        "period_end": date.fromisoformat(kw["period_end"]),
        "val": float(kw["val"]),
        "accn": kw.get("accn", "x"),
        "fy": kw.get("fy", 2024),
        "fp": kw.get("fp", "Q1"),
        "form": kw.get("form", "10-Q"),
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


def _fy_duration(concept: str, year: int, val: float, *, filed: str | None = None, **kw) -> dict:
    """An annual (FY) duration row: full calendar year, fp=FY, form 10-K."""
    return _row(
        concept=concept,
        period_start=f"{year}-01-01",
        period_end=f"{year}-12-31",
        val=val,
        fp=kw.pop("fp", "FY"),
        form=kw.pop("form", "10-K"),
        filed_date=filed or f"{year + 1}-02-15",
        **kw,
    )


def _instant(concept: str, end: str, val: float, *, filed: str | None = None, **kw) -> dict:
    """A balance-sheet instant row (no period_start)."""
    return _row(
        concept=concept,
        period_start=None,
        period_end=end,
        val=val,
        fp=kw.pop("fp", "FY"),
        form=kw.pop("form", "10-K"),
        filed_date=filed or f"{end[:4]}-02-15",
        **kw,
    )


class _AnnualBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _reader(self, rows: list[dict]) -> CompanyfactsParquetReader:
        _write_parquet(self._dir / f"{_CIK}.parquet", rows)
        return CompanyfactsParquetReader(self._dir)


class TestAnnualStatementsHappyPath(_AnnualBase):
    def test_three_annual_filings_newest_first(self):
        rows = []
        for yr, rev in ((2020, 100.0), (2021, 200.0), (2022, 300.0)):
            rows.append(_fy_duration("Revenues", yr, rev))
            rows.append(_fy_duration("OperatingIncomeLoss", yr, rev / 10))
            rows.append(_fy_duration("NetIncomeLoss", yr, rev / 20))
        reader = self._reader(rows)

        series = annual_statements(reader, _CIK, date(2024, 1, 1))

        self.assertEqual(
            [s.fiscal_year_end for s in series],
            [date(2022, 12, 31), date(2021, 12, 31), date(2020, 12, 31)],
        )
        self.assertEqual(series[0].revenue, 300.0)
        self.assertEqual(series[0].operating_income, 30.0)
        self.assertEqual(series[0].net_income, 15.0)
        self.assertEqual(series[2].revenue, 100.0)

    def test_concept_period_end_drift_merges_into_one_year(self):
        # Revenue ends 2022-12-28, NI ends 2022-12-31 (restatement / recast
        # drift within tolerance) -> ONE fiscal year, not two partial rows.
        rows = [
            _row(
                concept="Revenues",
                period_start="2022-01-01",
                period_end="2022-12-28",
                val=300.0,
                fp="FY",
                form="10-K",
                filed_date="2023-02-15",
            ),
            _fy_duration("NetIncomeLoss", 2022, 15.0),  # ends 2022-12-31
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].fiscal_year_end, date(2022, 12, 31))  # newest member = canonical
        self.assertEqual(series[0].revenue, 300.0)
        self.assertEqual(series[0].net_income, 15.0)

    def test_fy_value_is_raw_not_ttm(self):
        # A single FY row's value must pass through verbatim.
        reader = self._reader([_fy_duration("Revenues", 2022, 987.0)])
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].revenue, 987.0)


class TestAnnualStatementsPIT(_AnnualBase):
    def test_restatement_filed_after_asof_ignored(self):
        rows = [
            _fy_duration("Revenues", 2022, 100.0, filed="2023-02-15"),
            _fy_duration("Revenues", 2022, 120.0, filed="2023-06-01", form="10-K/A"),
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2023, 3, 1))
        self.assertEqual(series[0].revenue, 100.0)

    def test_restatement_filed_before_asof_wins(self):
        rows = [
            _fy_duration("Revenues", 2022, 100.0, filed="2023-02-15"),
            _fy_duration("Revenues", 2022, 120.0, filed="2023-06-01", form="10-K/A"),
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2023, 7, 1))
        self.assertEqual(series[0].revenue, 120.0)


class TestAnnualStatementsMaxYears(_AnnualBase):
    def test_caps_to_newest_n_years(self):
        rows = [_fy_duration("Revenues", yr, float(yr)) for yr in range(2008, 2020)]  # 12 FY
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2021, 1, 1), max_years=10)
        self.assertEqual(len(series), 10)
        self.assertEqual(series[0].fiscal_year_end, date(2019, 12, 31))
        self.assertEqual(series[-1].fiscal_year_end, date(2010, 12, 31))


class TestAnnualStatementsFyDetection(_AnnualBase):
    def test_52_53_week_fp_none_included(self):
        # fp=None, ~362-day span -> FY-like by the >=11-month rule.
        row = _row(
            concept="Revenues",
            period_start="2021-01-03",
            period_end="2021-12-31",
            val=555.0,
            fp=None,
            form="10-K",
            filed_date="2022-02-15",
        )
        reader = self._reader([row])
        series = annual_statements(reader, _CIK, date(2023, 1, 1))
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].revenue, 555.0)

    def test_quarterly_only_yields_no_year(self):
        rows = [
            _row(
                concept="Revenues",
                period_start=f"2022-{m:02d}-01",
                period_end=f"2022-{m + 2:02d}-28",
                val=50.0,
                fp=f"Q{(m // 3) + 1}",
                form="10-Q",
                filed_date=f"2022-{m + 3:02d}-15",
            )
            for m in (1, 4, 7)
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        self.assertEqual(series, [])

    def test_non_whitelisted_form_excluded(self):
        rows = [
            _fy_duration("Revenues", 2022, 300.0, form="10-K"),
            # A DEF 14A proxy carrying a 12-month span must be dropped.
            _fy_duration("Revenues", 2021, 999.0, form="DEF 14A"),
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        self.assertEqual([s.fiscal_year_end for s in series], [date(2022, 12, 31)])


class TestAnnualStatementsConceptFallback(_AnnualBase):
    def test_revenue_chain_fallback(self):
        # No RevenueFromContract... row; only the legacy "Revenues" tag.
        reader = self._reader([_fy_duration("Revenues", 2022, 321.0)])
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        self.assertEqual(series[0].revenue, 321.0)

    def test_da_component_sum_fallback(self):
        rows = [
            _fy_duration("Revenues", 2022, 300.0),
            _fy_duration("Depreciation", 2022, 40.0),
            _fy_duration("AmortizationOfIntangibleAssets", 2022, 10.0),
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        self.assertEqual(series[0].da, 50.0)


class TestAnnualStatementsMissingAndEmpty(_AnnualBase):
    def test_missing_concept_yields_none_field_year_kept(self):
        rows = [
            _fy_duration("Revenues", 2022, 300.0),
            _fy_duration("NetIncomeLoss", 2022, 15.0),
            # no capex row this year
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].revenue, 300.0)
        self.assertIsNone(series[0].capex)

    def test_unknown_cik_returns_empty_list(self):
        reader = CompanyfactsParquetReader(self._dir)  # nothing on disk
        self.assertEqual(annual_statements(reader, "0000000000", date(2024, 1, 1)), [])


class TestAnnualStatementsInstantAnchoring(_AnnualBase):
    def test_instant_anchored_on_each_fy_end_not_latest(self):
        rows = [
            _fy_duration("Revenues", 2021, 200.0),
            _fy_duration("Revenues", 2022, 300.0),
            _instant("StockholdersEquity", "2021-12-31", 1000.0),
            _instant("StockholdersEquity", "2022-12-31", 1500.0),
            # A later Q1-2023 balance sheet must NOT leak into either FY row.
            _instant(
                "StockholdersEquity", "2023-03-31", 9999.0, fp="Q1", form="10-Q", filed="2023-05-01"
            ),
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        by_year = {s.fiscal_year_end: s for s in series}
        self.assertEqual(by_year[date(2021, 12, 31)].total_equity, 1000.0)
        self.assertEqual(by_year[date(2022, 12, 31)].total_equity, 1500.0)

    def test_shares_outstanding_dei_anchored(self):
        rows = [
            _fy_duration("Revenues", 2022, 300.0),
            _instant(
                "EntityCommonStockSharesOutstanding",
                "2022-12-31",
                5_000_000.0,
                taxonomy="dei",
                unit="shares",
            ),
        ]
        reader = self._reader(rows)
        series = annual_statements(reader, _CIK, date(2024, 1, 1))
        self.assertEqual(series[0].shares_outstanding, 5_000_000.0)


class TestAnnualStatementsCaching(_AnnualBase):
    def test_reader_hit_once_for_many_concepts(self):
        rows = [
            _fy_duration("Revenues", 2022, 300.0),
            _fy_duration("OperatingIncomeLoss", 2022, 30.0),
            _fy_duration("NetIncomeLoss", 2022, 15.0),
            _instant("StockholdersEquity", "2022-12-31", 1500.0),
            _instant("Cash", "2022-12-31", 200.0),
        ]
        _write_parquet(self._dir / f"{_CIK}.parquet", rows)
        reader = CompanyfactsParquetReader(self._dir)
        reader.get_cik_table = MagicMock(wraps=reader.get_cik_table)

        annual_statements(reader, _CIK, date(2024, 1, 1))

        self.assertEqual(reader.get_cik_table.call_count, 1)


if __name__ == "__main__":
    unittest.main()
