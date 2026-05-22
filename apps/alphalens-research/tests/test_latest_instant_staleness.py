"""Max-age gate on ``latest_instant``.

Issue #172 Bug 1: C3.ai (CIK 0001577526) has ``us-gaap:CommonStockSharesOutstanding``
with only 2 entries from 2021-06-25 (val=3,499,992 — the pre-IPO snapshot,
never updated). At asof 2026-05-19 these entries are 5 years stale yet the
aggregator returned them as the latest visible value, leading to a 37×
under-count of shares and a P/S of 0.10 (should be ≈ 4).

Fix: opt-in ``max_age_days`` parameter on :func:`latest_instant`. Entries
whose ``period_end`` is older than ``max_age_days`` before ``asof`` are
treated as if they were not present. Callers that don't need the gate
(equity, debt, cash today) pass nothing and preserve current behavior.
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
    taxonomy: str = "us-gaap",
    concept: str,
    unit: str = "USD",
    period_end: str,
    val: float,
    filed_date: str,
    form: str = "10-Q",
    fp: str | None = "Q1",
    period_start: str | None = None,
    accn: str = "x",
    fy: int | None = 2024,
    frame: str | None = None,
) -> dict:
    return {
        "taxonomy": taxonomy,
        "concept": concept,
        "unit": unit,
        "period_start": date.fromisoformat(period_start) if period_start else None,
        "period_end": date.fromisoformat(period_end),
        "val": float(val),
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed_date": date.fromisoformat(filed_date),
        "frame": frame,
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
    if not rows:
        return pa.table({c: pa.array([], type=_dtype(c)) for c in _SCHEMA_COLUMNS})
    cols = {c: [r.get(c) for r in rows] for c in _SCHEMA_COLUMNS}
    return pa.table({c: pa.array(cols[c], type=_dtype(c)) for c in _SCHEMA_COLUMNS})


def _stub_reader(table: pa.Table) -> MagicMock:
    r = MagicMock()
    r.get_cik_table.return_value = table
    return r


class TestLatestInstantMaxAge(unittest.TestCase):
    def test_default_behavior_unchanged_without_gate(self):
        """No ``max_age_days`` arg → 5-year-old entry still returned (legacy)."""
        from alphalens_research.data.fundamentals.ttm_aggregator import latest_instant

        rows = [
            _row(
                taxonomy="us-gaap",
                concept="CommonStockSharesOutstanding",
                unit="shares",
                period_end="2021-04-30",
                val=3_499_992.0,
                filed_date="2021-06-25",
                form="10-K",
                fp="FY",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        result = latest_instant(
            reader,
            cik="x",
            chain=("CommonStockSharesOutstanding",),
            asof=date(2026, 5, 19),
            unit="shares",
        )
        self.assertEqual(result, 3_499_992.0)

    def test_stale_entry_rejected_when_age_exceeds_gate(self):
        """C3.ai-shaped: entry period_end 5y before asof, gate 180d → None."""
        from alphalens_research.data.fundamentals.ttm_aggregator import latest_instant

        rows = [
            _row(
                taxonomy="us-gaap",
                concept="CommonStockSharesOutstanding",
                unit="shares",
                period_end="2021-04-30",
                val=3_499_992.0,
                filed_date="2021-06-25",
                form="10-K",
                fp="FY",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        result = latest_instant(
            reader,
            cik="x",
            chain=("CommonStockSharesOutstanding",),
            asof=date(2026, 5, 19),
            unit="shares",
            max_age_days=180,
        )
        self.assertIsNone(result)

    def test_fresh_entry_passes_gate(self):
        """Entry filed inside the window → returned."""
        from alphalens_research.data.fundamentals.ttm_aggregator import latest_instant

        rows = [
            _row(
                taxonomy="us-gaap",
                concept="CommonStockSharesOutstanding",
                unit="shares",
                period_end="2026-01-31",
                val=140_000_000.0,
                filed_date="2026-03-11",
                form="10-Q",
                fp="Q3",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        result = latest_instant(
            reader,
            cik="x",
            chain=("CommonStockSharesOutstanding",),
            asof=date(2026, 5, 19),
            unit="shares",
            max_age_days=180,
        )
        self.assertEqual(result, 140_000_000.0)

    def test_gate_falls_through_to_next_concept_in_chain(self):
        """Stale us-gaap → empty after gate; chain advances to dei (fresh)."""
        from alphalens_research.data.fundamentals.ttm_aggregator import latest_instant

        rows = [
            _row(
                taxonomy="us-gaap",
                concept="CommonStockSharesOutstanding",
                unit="shares",
                period_end="2021-04-30",
                val=3_499_992.0,
                filed_date="2021-06-25",
                form="10-K",
                fp="FY",
            ),
            _row(
                taxonomy="us-gaap",
                concept="CommonStockSharesOutstandingFallback",
                unit="shares",
                period_end="2026-01-31",
                val=140_000_000.0,
                filed_date="2026-03-11",
                form="10-Q",
                fp="Q3",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        result = latest_instant(
            reader,
            cik="x",
            chain=("CommonStockSharesOutstanding", "CommonStockSharesOutstandingFallback"),
            asof=date(2026, 5, 19),
            unit="shares",
            max_age_days=180,
        )
        self.assertEqual(result, 140_000_000.0)

    def test_gate_boundary_inclusive(self):
        """Entry exactly ``max_age_days`` old is accepted (≤ window)."""
        from alphalens_research.data.fundamentals.ttm_aggregator import latest_instant

        rows = [
            _row(
                taxonomy="us-gaap",
                concept="CommonStockSharesOutstanding",
                unit="shares",
                period_end="2025-11-20",
                val=100.0,
                filed_date="2025-12-01",
                form="10-Q",
                fp="Q3",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        result = latest_instant(
            reader,
            cik="x",
            chain=("CommonStockSharesOutstanding",),
            asof=date(2026, 5, 19),
            unit="shares",
            max_age_days=180,
        )
        self.assertEqual(result, 100.0)


if __name__ == "__main__":
    unittest.main()
