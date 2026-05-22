"""Tests for per-quarter aggregation primitives on EDGAR companyfacts parquets.

Covers ``compute_per_quarter_series`` and ``fcf_margin_rolling_median`` via
synthetic Arrow tables — no live SEC calls.

Test-data shape mirrors what `companyfacts_json_to_parquet_table` emits:
one row per (taxonomy, concept, unit, period_start, period_end, filed_date)
with columns matching the canonical parquet schema (see
`alphalens_research.data.fundamentals.companyfacts_parquet`).
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock

import pyarrow as pa

# ---- helpers --------------------------------------------------------------

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
    period_start: str | None,
    period_end: str,
    val: float,
    filed_date: str,
    form: str = "10-Q",
    fp: str | None = "Q1",
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


def _arrow_table(rows: list[dict]) -> pa.Table:
    """Build an Arrow Table matching the parquet schema (defaults for absent fields)."""
    if not rows:
        # Empty table with the canonical schema so filter_concept works.
        return pa.table({c: pa.array([], type=_dtype(c)) for c in _SCHEMA_COLUMNS})
    cols = {c: [r.get(c) for r in rows] for c in _SCHEMA_COLUMNS}
    return pa.table({c: pa.array(cols[c], type=_dtype(c)) for c in _SCHEMA_COLUMNS})


def _dtype(name: str) -> pa.DataType:
    if name in ("period_start", "period_end", "filed_date"):
        return pa.date32()
    if name == "val":
        return pa.float64()
    if name == "fy":
        return pa.int32()
    return pa.string()


def _stub_reader(table: pa.Table) -> MagicMock:
    """A reader stand-in: returns the given table for any CIK."""
    r = MagicMock()
    r.get_cik_table.return_value = table
    return r


# ---- tests: compute_per_quarter_series ------------------------------------


class TestComputePerQuarterSeries(unittest.TestCase):
    def test_standalone_quarters_returned_in_chronological_order(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        # Three standalone 90-day quarters filed via 10-Q.
        rows = [
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=100.0,
                filed_date="2024-05-01",
                fp="Q1",
            ),
            _row(
                concept="Revenues",
                period_start="2024-04-01",
                period_end="2024-06-30",
                val=110.0,
                filed_date="2024-08-01",
                fp="Q2",
            ),
            _row(
                concept="Revenues",
                period_start="2024-07-01",
                period_end="2024-09-30",
                val=120.0,
                filed_date="2024-11-01",
                fp="Q3",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(reader, "0000", ("Revenues",), date(2024, 12, 31))
        self.assertEqual(out, [("2024-03-31", 100.0), ("2024-06-30", 110.0), ("2024-09-30", 120.0)])

    def test_q4_derived_from_fy_minus_ytd9m_same_fiscal_year(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            # YTD9M (Q3 fp, 9-month span)
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-09-30",
                val=900.0,
                filed_date="2024-11-01",
                fp="Q3",
            ),
            # FY (12-month span). Q4 = FY - YTD9M = 1200 - 900 = 300.
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=1200.0,
                filed_date="2025-02-14",
                fp="FY",
                form="10-K",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(reader, "0000", ("Revenues",), date(2025, 4, 1))
        self.assertEqual(out, [("2024-12-31", 300.0)])

    def test_q4_derivation_requires_matching_fiscal_year(self):
        """A YTD9M for 2024 plus an FY for 2023 must NOT be subtracted —
        fiscal year mismatch invalidates the derivation. Period_start is
        the match key (FY2024 and YTD9M-of-2024 both start 2024-01-01;
        FY2023 starts 2023-01-01)."""
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            # YTD9M for FY2024 (273-day span).
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-09-30",
                val=900.0,
                filed_date="2024-11-01",
                fp="Q3",
            ),
            # FY for a DIFFERENT fiscal year (2023) — period_start mismatch
            # means no derivation is triggered.
            _row(
                concept="Revenues",
                period_start="2023-01-01",
                period_end="2023-12-31",
                val=1100.0,
                filed_date="2024-02-15",
                fp="FY",
                form="10-K",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(reader, "0000", ("Revenues",), date(2025, 1, 1))
        # No standalone Q-rows in the fixture, no matching FY+YTD9M pair → empty.
        self.assertEqual(out, [])

    def test_standalone_q_preferred_over_derived(self):
        """When a real standalone Q4 row exists (rare but possible for some
        issuers) it must win over the FY-minus-YTD9M derivation."""
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            _row(
                concept="Revenues",
                period_start="2024-10-01",
                period_end="2024-12-31",
                val=305.0,
                filed_date="2025-02-14",
                fp=None,
                form="8-K",
            ),
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-09-30",
                val=900.0,
                filed_date="2024-11-01",
                fp="Q3",
            ),
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=1200.0,
                filed_date="2025-02-14",
                fp="FY",
                form="10-K",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(reader, "0000", ("Revenues",), date(2025, 4, 1))
        # 305 (standalone Q4 from 8-K) wins over 300 (derived FY-YTD9M).
        self.assertEqual(out, [("2024-12-31", 305.0)])

    def test_pit_cutoff_drops_post_asof_filings(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=100.0,
                filed_date="2024-05-01",
                fp="Q1",
            ),
            # This Q2 was filed AFTER the asof — must be invisible.
            _row(
                concept="Revenues",
                period_start="2024-04-01",
                period_end="2024-06-30",
                val=110.0,
                filed_date="2024-08-01",
                fp="Q2",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(reader, "0000", ("Revenues",), date(2024, 7, 1))
        self.assertEqual(out, [("2024-03-31", 100.0)])

    def test_restatement_uses_latest_filed(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            # Original Q1 filing.
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=100.0,
                filed_date="2024-05-01",
                fp="Q1",
            ),
            # Restated Q1 (same period, different val, filed later).
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=105.0,
                filed_date="2024-11-15",
                fp="Q1",
                form="10-Q/A",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(reader, "0000", ("Revenues",), date(2025, 1, 1))
        self.assertEqual(out, [("2024-03-31", 105.0)])

    def test_empty_cik_returns_empty_list(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        reader = MagicMock()
        reader.get_cik_table.return_value = None
        self.assertEqual(
            compute_per_quarter_series(reader, "missing", ("Revenues",), date(2025, 1, 1)),
            [],
        )

    def test_cross_concept_standalone_overrides_earlier_derived(self):
        """When concept A only supplies an FY+YTD9M (=> Q4 is DERIVED) and
        concept B supplies a STANDALONE row for the same end, the direct
        measurement from B must win over A's arithmetic derivation."""
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            # Concept A — derivation path for 2024-12-31 (FY 1200 - YTD9M 900 = 300).
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2024-01-01",
                period_end="2024-09-30",
                val=900.0,
                filed_date="2024-11-01",
                fp="Q3",
            ),
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=1200.0,
                filed_date="2025-02-14",
                fp="FY",
                form="10-K",
            ),
            # Concept B — standalone Q4 row (8-K recast). 305 is the direct
            # measurement that should win.
            _row(
                concept="Revenues",
                period_start="2024-10-01",
                period_end="2024-12-31",
                val=305.0,
                filed_date="2025-02-14",
                fp=None,
                form="8-K",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(
            reader,
            "0000",
            ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
            date(2025, 4, 1),
        )
        # 305 (standalone from later chain entry) wins over 300 (derived from earlier).
        self.assertEqual(out, [("2024-12-31", 305.0)])

    def test_cross_concept_derived_does_not_override_earlier_derived(self):
        """Derivation is the weakest evidence — once an end is filled by
        any concept's derivation, a later concept's derivation must NOT
        displace it. First-concept-wins protects against non-deterministic
        ordering when both concepts have equally-weak (derived) values."""
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            # Concept A derives Q4 = 300 (FY 1200 - YTD9M 900).
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2024-01-01",
                period_end="2024-09-30",
                val=900.0,
                filed_date="2024-11-01",
                fp="Q3",
            ),
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=1200.0,
                filed_date="2025-02-14",
                fp="FY",
                form="10-K",
            ),
            # Concept B also derives Q4 = 250 (different values, same end).
            # Must not override concept A's derived value.
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-09-30",
                val=750.0,
                filed_date="2024-11-01",
                fp="Q3",
            ),
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=1000.0,
                filed_date="2025-02-14",
                fp="FY",
                form="10-K",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(
            reader,
            "0000",
            ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
            date(2025, 4, 1),
        )
        # 300 (A's derivation) wins; B's derivation is ignored.
        self.assertEqual(out, [("2024-12-31", 300.0)])

    def test_period_start_restatement_uses_latest_filed(self):
        """A 10-Q/A that changes period_start (rare — fiscal-year boundary
        recast) creates a new (start, end) key so _latest_per_period keeps
        BOTH rows. The bucketing layer must apply a filed-date tiebreaker
        so the newest restatement wins, not Python dict insertion order."""
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            # Original Q1 row.
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=100.0,
                filed_date="2024-05-01",
                fp="Q1",
            ),
            # 10-Q/A restatement with a shifted period_start (transition
            # period). Same end, different start → different (start, end) tuple.
            # Bucket key is end=2024-03-31 in both cases; filed-date tiebreaker
            # must pick this newer one.
            _row(
                concept="Revenues",
                period_start="2024-01-02",
                period_end="2024-03-31",
                val=105.0,
                filed_date="2024-11-15",
                fp="Q1",
                form="10-Q/A",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(reader, "0000", ("Revenues",), date(2025, 1, 1))
        self.assertEqual(out, [("2024-03-31", 105.0)])

    def test_chain_traversal_picks_first_matching_concept(self):
        """When the chain offers a primary + a fallback and the primary has
        data, the fallback isn't consulted (per existing aggregator pattern)."""
        from alphalens_research.data.fundamentals.ttm_aggregator import compute_per_quarter_series

        rows = [
            _row(
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=100.0,
                filed_date="2024-05-01",
                fp="Q1",
            ),
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=999.0,
                filed_date="2024-05-01",
                fp="Q1",
            ),
        ]
        reader = _stub_reader(_arrow_table(rows))
        out = compute_per_quarter_series(
            reader,
            "0000",
            ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
            date(2025, 1, 1),
        )
        self.assertEqual(out, [("2024-03-31", 100.0)])


# ---- tests: fcf_margin_rolling_median -------------------------------------


def _build_synth_table(n_quarters: int, *, with_interest: bool = True) -> pa.Table:
    """Build a parquet table with n_quarters of OCF, CapEx, Revenue, Interest
    (all standalone-Q, span 90 days)."""
    rows = []
    for i in range(n_quarters):
        # Quarter i, ending at month=3*(i%4)+3 of year (2020 + i//4)
        yr = 2020 + i // 4
        q = i % 4
        q_start_month = q * 3 + 1
        q_end_month = q * 3 + 3
        end_day = {3: 31, 6: 30, 9: 30, 12: 31}[q_end_month]
        start = f"{yr}-{q_start_month:02d}-01"
        end = f"{yr}-{q_end_month:02d}-{end_day:02d}"
        filed = (
            f"{yr}-{q_end_month + 1 if q_end_month < 12 else 1:02d}-15"
            if q_end_month < 12
            else f"{yr + 1}-02-15"
        )
        fp = "FY" if q == 3 else f"Q{q + 1}"
        form = "10-K" if q == 3 else "10-Q"
        # Standalone-Q rows even for Q4 (some issuers file 8-K recasts).
        rows.append(
            _row(
                concept="NetCashProvidedByUsedInOperatingActivities",
                period_start=start,
                period_end=end,
                val=120.0,
                filed_date=filed,
                fp=fp,
                form=form,
            )
        )
        rows.append(
            _row(
                concept="PaymentsToAcquirePropertyPlantAndEquipment",
                period_start=start,
                period_end=end,
                val=20.0,
                filed_date=filed,
                fp=fp,
                form=form,
            )
        )
        rows.append(
            _row(
                concept="Revenues",
                period_start=start,
                period_end=end,
                val=500.0,
                filed_date=filed,
                fp=fp,
                form=form,
            )
        )
        if with_interest:
            rows.append(
                _row(
                    concept="InterestExpense",
                    period_start=start,
                    period_end=end,
                    val=10.0,
                    filed_date=filed,
                    fp=fp,
                    form=form,
                )
            )
    return _arrow_table(rows)


class TestFcfMarginRollingMedian(unittest.TestCase):
    def test_returns_median_when_enough_quarters(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import fcf_margin_rolling_median

        reader = _stub_reader(_build_synth_table(12))
        # Per-quarter margin: (120 - 20 - 10*0.79) / 500 = (100 - 7.9) / 500 = 0.1842
        m = fcf_margin_rolling_median(reader, "0000", date(2030, 1, 1), tax_rate=0.21)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m, (120 - 20 - 10 * 0.79) / 500, places=4)

    def test_returns_none_when_fewer_than_min_quarters(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import fcf_margin_rolling_median

        reader = _stub_reader(_build_synth_table(7))
        self.assertIsNone(fcf_margin_rolling_median(reader, "0000", date(2030, 1, 1)))

    def test_missing_interest_treated_as_zero(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import fcf_margin_rolling_median

        reader = _stub_reader(_build_synth_table(12, with_interest=False))
        # (120 - 20 - 0) / 500 = 0.20
        m = fcf_margin_rolling_median(reader, "0000", date(2030, 1, 1), tax_rate=0.21)
        self.assertAlmostEqual(m, 0.20, places=4)

    def test_revenue_zero_or_negative_quarters_dropped(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import fcf_margin_rolling_median

        # 9 good quarters, 3 bad-revenue quarters mixed in. Median of the 9 good.
        rows = []
        for i in range(12):
            yr = 2020 + i // 4
            q = i % 4
            q_start_month = q * 3 + 1
            q_end_month = q * 3 + 3
            end_day = {3: 31, 6: 30, 9: 30, 12: 31}[q_end_month]
            start = f"{yr}-{q_start_month:02d}-01"
            end = f"{yr}-{q_end_month:02d}-{end_day:02d}"
            filed = f"{yr}-{q_end_month + 1:02d}-15" if q_end_month < 12 else f"{yr + 1}-02-15"
            fp = "FY" if q == 3 else f"Q{q + 1}"
            rows.append(
                _row(
                    concept="NetCashProvidedByUsedInOperatingActivities",
                    period_start=start,
                    period_end=end,
                    val=120.0,
                    filed_date=filed,
                    fp=fp,
                )
            )
            rows.append(
                _row(
                    concept="PaymentsToAcquirePropertyPlantAndEquipment",
                    period_start=start,
                    period_end=end,
                    val=20.0,
                    filed_date=filed,
                    fp=fp,
                )
            )
            # First 3 quarters have rev=0 (must be dropped).
            rev = 0.0 if i < 3 else 500.0
            rows.append(
                _row(
                    concept="Revenues",
                    period_start=start,
                    period_end=end,
                    val=rev,
                    filed_date=filed,
                    fp=fp,
                )
            )
        reader = _stub_reader(_arrow_table(rows))
        m = fcf_margin_rolling_median(reader, "0000", date(2030, 1, 1))
        # 9 quarters survive, all margin = (120-20)/500 = 0.20 (no interest).
        self.assertAlmostEqual(m, 0.20, places=4)

    def test_window_caps_at_20_quarters(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import fcf_margin_rolling_median

        # 30 quarters; only last 20 should be considered (no impact on median
        # when all are identical, but verifies the window cap doesn't break).
        reader = _stub_reader(_build_synth_table(30))
        m = fcf_margin_rolling_median(reader, "0000", date(2030, 1, 1), tax_rate=0.21)
        self.assertAlmostEqual(m, (120 - 20 - 10 * 0.79) / 500, places=4)

    def test_pit_cutoff_truncates_visible_quarters(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import fcf_margin_rolling_median

        # 10 quarters but asof early — only 6 quarters filed by then, so <8 -> None.
        reader = _stub_reader(_build_synth_table(10))
        # Asof = 2021-06-30 means filings >= 2021-07-15 are invisible.
        # Quarters 0..5 visible (2020-Q1 through 2021-Q2 filed by 2021-07-15).
        self.assertIsNone(fcf_margin_rolling_median(reader, "0000", date(2021, 6, 30)))

    def test_returns_none_for_empty_cik(self):
        from alphalens_research.data.fundamentals.ttm_aggregator import fcf_margin_rolling_median

        reader = MagicMock()
        reader.get_cik_table.return_value = None
        self.assertIsNone(fcf_margin_rolling_median(reader, "missing", date(2030, 1, 1)))


if __name__ == "__main__":
    unittest.main()
