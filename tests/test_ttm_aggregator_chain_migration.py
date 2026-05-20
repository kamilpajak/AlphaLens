"""TTM aggregator must not silently fall back across concept families.

Issue #172 Bug 2 (AVAV-shaped): the new ``RevenueFromContractWithCustomer-
ExcludingAssessedTax`` concept had FY26 Q3 9-month YTD ($1.335B post-
BlueHalo-merger) but no FY25 anchor row (the FY25 10-K wasn't tagged
under the new concept). Per-concept ``_ttm_at_end`` returned ``None``
because the Compustat ``current_YTD + prior_FY − prior_YTD`` formula
couldn't find a prior_FY in that family. The aggregator then silently
fell through to the legacy ``Revenues`` concept, which last had data in
2021, and returned an ancient TTM (~$297M). Brief consumed it as truth.

Fix: ``compute_ttm`` builds a semantic family series via
``compute_per_quarter_series`` (which already merges across the chain)
and takes the trailing-4-quarter sum. Falls back to per-concept Compustat
identity only when fewer than 4 quarters are available in the merged
family.
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
    form: str = "10-Q",
    fp: str | None = "Q1",
    taxonomy: str = "us-gaap",
    unit: str = "USD",
) -> dict:
    return {
        "taxonomy": taxonomy,
        "concept": concept,
        "unit": unit,
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
    if not rows:
        return pa.table({c: pa.array([], type=_dtype(c)) for c in _SCHEMA_COLUMNS})
    cols = {c: [r.get(c) for r in rows] for c in _SCHEMA_COLUMNS}
    return pa.table({c: pa.array(cols[c], type=_dtype(c)) for c in _SCHEMA_COLUMNS})


def _stub_reader(table: pa.Table) -> MagicMock:
    r = MagicMock()
    r.get_cik_table.return_value = table
    return r


class TestCrossConceptFallback(unittest.TestCase):
    def test_4q_sum_used_when_new_concept_has_quarters_no_fy(self):
        """AVAV-shaped: new concept Q-rows post-merger, no FY anchor in family.

        4-quarter sum (in $M): 408 (FY26 Q3 standalone) + 472 (FY26 Q2
        standalone) + 455 (FY26 Q1 standalone) + 168 (FY25 Q3 standalone)
        = 1503. Old ``Revenues`` 2020 TTM (~$297M) must NOT win.
        """
        from alphalens.data.fundamentals.ttm_aggregator import compute_ttm

        rows = [
            # New concept: per-quarter standalone rows post-merger (FY26).
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2025-11-02",
                period_end="2026-01-31",
                val=408_000_000.0,
                filed_date="2026-03-11",
                form="10-Q",
                fp="Q3",
            ),
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2025-08-03",
                period_end="2025-11-01",
                val=472_000_000.0,
                filed_date="2025-12-10",
                form="10-Q",
                fp="Q2",
            ),
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2025-05-01",
                period_end="2025-08-02",
                val=455_000_000.0,
                filed_date="2025-09-10",
                form="10-Q",
                fp="Q1",
            ),
            # FY25 Q3 standalone (pre-merger AVAV).
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2024-10-27",
                period_end="2025-01-25",
                val=168_000_000.0,
                filed_date="2025-03-11",
                form="10-Q",
                fp="Q3",
            ),
            # Old concept: stale 2020 data that previously won by fallback.
            _row(
                concept="Revenues",
                period_start="2020-01-01",
                period_end="2020-12-31",
                val=297_000_000.0,
                filed_date="2021-03-04",
                form="10-K",
                fp="FY",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_ttm(
            reader,
            cik="x",
            chain=(
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
            ),
            asof=date(2026, 5, 19),
        )
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out, 1_503_000_000.0, places=-3)

    def test_falls_back_to_compustat_identity_when_fewer_than_4_quarters(self):
        """When 4Q sum is impossible, Compustat formula is the secondary path.

        Two quarters of new concept + FY anchor of new concept → Compustat
        identity is valid; old-concept fallback still suppressed.
        """
        from alphalens.data.fundamentals.ttm_aggregator import compute_ttm

        rows = [
            # New concept: only 1 standalone Q + FY anchor — not enough for 4Q sum.
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2025-01-01",
                period_end="2025-03-31",
                val=100.0,
                filed_date="2025-05-01",
                form="10-Q",
                fp="Q1",
            ),
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2025-01-01",
                period_end="2025-03-31",
                val=100.0,
                filed_date="2025-05-01",
                form="10-Q",
                fp="Q1",
            ),
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=400.0,
                filed_date="2025-02-15",
                form="10-K",
                fp="FY",
            ),
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=90.0,
                filed_date="2024-05-01",
                form="10-Q",
                fp="Q1",
            ),
            # Old concept stale.
            _row(
                concept="Revenues",
                period_start="2020-01-01",
                period_end="2020-12-31",
                val=999.0,
                filed_date="2021-03-04",
                form="10-K",
                fp="FY",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_ttm(
            reader,
            cik="x",
            chain=(
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
            ),
            asof=date(2025, 5, 19),
        )
        # Compustat: 100 (Q1 2025) + 400 (FY 2024) - 90 (Q1 2024) = 410
        self.assertAlmostEqual(out, 410.0, places=2)
