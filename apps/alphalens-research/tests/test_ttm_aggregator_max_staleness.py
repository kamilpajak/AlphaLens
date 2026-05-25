"""Max-staleness gate on ``compute_ttm``.

Issue #172 Bug 2 (root mechanism): once the silent cross-concept fallback
is removed, the aggregator must still refuse to emit a TTM whose freshest
component is far behind ``asof``. A 2-year-old TTM in a daily brief is
worse than ``None`` because downstream consumers cannot tell the value is
stale.
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock

import pyarrow as pa

_SCHEMA_COLUMNS = (
    "taxonomy",
    "concept",
    "unit",
    "period_start",
    "period_end",
    "val",
    "accn",
    "fy",
    "fp",
    "form",
    "filed_date",
    "frame",
)


def _row(
    *,
    concept: str,
    period_start: str | None,
    period_end: str,
    val: float,
    filed_date: str,
    fp: str = "FY",
    form: str = "10-K",
) -> dict:
    return {
        "taxonomy": "us-gaap",
        "concept": concept,
        "unit": "USD",
        "period_start": date.fromisoformat(period_start) if period_start else None,
        "period_end": date.fromisoformat(period_end),
        "val": float(val),
        "accn": "x",
        "fy": 2024,
        "fp": fp,
        "form": form,
        "filed_date": date.fromisoformat(filed_date),
        "frame": None,
    }


def _dtype(name: str) -> pa.DataType:
    if name in ("period_start", "period_end", "filed_date"):
        return pa.date32()
    if name == "val":
        return pa.float64()
    if name == "fy":
        return pa.int32()
    return pa.string()


def _arrow_table(rows: list[dict]) -> pa.Table:
    cols = {c: [r.get(c) for r in rows] for c in _SCHEMA_COLUMNS}
    return pa.table({c: pa.array(cols[c], type=_dtype(c)) for c in _SCHEMA_COLUMNS})


def _stub_reader(table: pa.Table) -> MagicMock:
    r = MagicMock()
    r.get_cik_table.return_value = table
    return r


class TestTtmMaxStaleness(unittest.TestCase):
    def test_default_gate_drops_two_year_old_ttm(self):
        """Default ``max_staleness_days=270`` (~9 months) drops a 2-year-old TTM."""
        from alphalens_pipeline.data.fundamentals.ttm_aggregator import compute_ttm

        rows = [
            _row(
                concept="Revenues",
                period_start="2023-01-01",
                period_end="2023-12-31",
                val=500_000_000.0,
                filed_date="2024-02-15",
            ),
        ]
        out = compute_ttm(
            _stub_reader(_arrow_table(rows)),
            cik="x",
            chain=("Revenues",),
            asof=date(2026, 5, 19),
        )
        self.assertIsNone(out)

    def test_fresh_ttm_passes_default_gate(self):
        """A TTM whose latest component is recent enough returns a value."""
        from alphalens_pipeline.data.fundamentals.ttm_aggregator import compute_ttm

        # 4 quarters of standalone-Q rows ending 2026-03-31 → 4Q sum path.
        rows = [
            _row(
                concept="Revenues",
                period_start="2025-04-01",
                period_end="2025-06-30",
                val=100.0,
                filed_date="2025-08-01",
                fp="Q2",
                form="10-Q",
            ),
            _row(
                concept="Revenues",
                period_start="2025-07-01",
                period_end="2025-09-30",
                val=110.0,
                filed_date="2025-11-01",
                fp="Q3",
                form="10-Q",
            ),
            _row(
                concept="Revenues",
                period_start="2025-10-01",
                period_end="2025-12-31",
                val=120.0,
                filed_date="2026-02-15",
                fp="FY",
                form="10-K",
            ),
            _row(
                concept="Revenues",
                period_start="2026-01-01",
                period_end="2026-03-31",
                val=130.0,
                filed_date="2026-05-01",
                fp="Q1",
                form="10-Q",
            ),
        ]
        out = compute_ttm(
            _stub_reader(_arrow_table(rows)),
            cik="x",
            chain=("Revenues",),
            asof=date(2026, 5, 19),
        )
        self.assertAlmostEqual(out, 460.0, places=2)

    def test_explicit_max_staleness_param_overrides_default(self):
        """Callers can tighten or relax the window."""
        from alphalens_pipeline.data.fundamentals.ttm_aggregator import compute_ttm

        rows = [
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=500.0,
                filed_date="2025-02-15",
            ),
        ]
        # ~17 months stale: pass with 600d window.
        out_relaxed = compute_ttm(
            _stub_reader(_arrow_table(rows)),
            cik="x",
            chain=("Revenues",),
            asof=date(2026, 5, 19),
            max_staleness_days=600,
        )
        self.assertEqual(out_relaxed, 500.0)
        # Fail with 60d window.
        out_strict = compute_ttm(
            _stub_reader(_arrow_table(rows)),
            cik="x",
            chain=("Revenues",),
            asof=date(2026, 5, 19),
            max_staleness_days=60,
        )
        self.assertIsNone(out_strict)


if __name__ == "__main__":
    unittest.main()
