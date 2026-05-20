"""Form-whitelist gate on EDGAR companyfacts aggregator.

Issue #172 Bug 3a: SOUN's DEF 14A proxy statement (filed 2026-04-09) carried
``NetIncomeLoss`` with ``val=-14,006`` — scaled to $thousands at the source
but XBRL-labelled as plain USD. The aggregator picked it because
``_latest_per_period`` keys by ``(start, end)`` and tiebreaks on
``filed_date``, so the newer DEF 14A entry won over the canonical 10-K
``val=-14,006,000``. Result: ROE ≈ -3e-6%, rendered as ``-0.0%`` in the
brief.

Root cause: ``_arrow_table_to_entries`` accepted every form. The Compustat
TTM formula was designed for 10-K/10-Q only. Proxy / registration /
prospectus filings often carry scale-truncated or sample numbers that have
no place in a TTM rollup.

Fix: ``FORM_WHITELIST`` filter in ``_arrow_table_to_entries`` (one entry
point — protects ``compute_ttm``, ``latest_instant``,
``compute_per_quarter_series``, ``fcf_margin_rolling_median``).
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
    period_start: str | None,
    period_end: str,
    val: float,
    filed_date: str,
    form: str,
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


class TestFormWhitelist(unittest.TestCase):
    def test_def14a_proxy_dropped_when_competing_with_10k_same_period(self):
        """SOUN-shaped: 10-K val=-14_006_000, DEF 14A val=-14_006 same (start, end).

        Tiebreaker prefers latest filed_date — DEF 14A wins by 5 weeks. The
        form filter must drop DEF 14A so 10-K survives.
        """
        from alphalens.data.fundamentals.ttm_aggregator import _arrow_table_to_entries

        rows = [
            _row(
                concept="NetIncomeLoss",
                period_start="2025-01-01",
                period_end="2025-12-31",
                val=-14_006_000.0,
                filed_date="2026-03-02",
                form="10-K",
                fp="FY",
            ),
            _row(
                concept="NetIncomeLoss",
                period_start="2025-01-01",
                period_end="2025-12-31",
                val=-14_006.0,
                filed_date="2026-04-09",
                form="DEF 14A",
                fp=None,
            ),
        ]
        table = _arrow_table(rows)
        entries = _arrow_table_to_entries(table, "NetIncomeLoss")
        self.assertEqual(len(entries), 1, "DEF 14A should be filtered out")
        self.assertEqual(entries[0].form, "10-K")
        self.assertEqual(entries[0].val, -14_006_000.0)

    def test_amended_forms_pass_whitelist(self):
        """10-K/A, 10-Q/A are legitimate restatements — must survive."""
        from alphalens.data.fundamentals.ttm_aggregator import _arrow_table_to_entries

        rows = [
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-03-31",
                val=100.0,
                filed_date="2024-05-01",
                form="10-Q/A",
                fp="Q1",
            ),
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=400.0,
                filed_date="2025-02-01",
                form="10-K/A",
                fp="FY",
            ),
        ]
        entries = _arrow_table_to_entries(_arrow_table(rows), "Revenues")
        forms = sorted(e.form for e in entries)
        self.assertEqual(forms, ["10-K/A", "10-Q/A"])

    def test_s1_and_prospectus_forms_dropped(self):
        """Registration / prospectus forms carry illustrative numbers; drop them."""
        from alphalens.data.fundamentals.ttm_aggregator import _arrow_table_to_entries

        rows = [
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=999.0,
                filed_date="2025-01-01",
                form="S-1",
                fp="FY",
            ),
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=999.0,
                filed_date="2025-01-02",
                form="424B4",
                fp="FY",
            ),
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=500.0,
                filed_date="2025-02-15",
                form="10-K",
                fp="FY",
            ),
        ]
        entries = _arrow_table_to_entries(_arrow_table(rows), "Revenues")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].form, "10-K")

    def test_8k_recasts_preserved(self):
        """8-K and 8-K/A often carry earnings-recast standalone-Q rows; keep them."""
        from alphalens.data.fundamentals.ttm_aggregator import _arrow_table_to_entries

        rows = [
            _row(
                concept="Revenues",
                period_start="2024-04-01",
                period_end="2024-06-30",
                val=110.0,
                filed_date="2024-07-25",
                form="8-K",
                fp="Q2",
            ),
            _row(
                concept="Revenues",
                period_start="2024-04-01",
                period_end="2024-06-30",
                val=111.0,
                filed_date="2024-08-05",
                form="10-Q",
                fp="Q2",
            ),
        ]
        entries = _arrow_table_to_entries(_arrow_table(rows), "Revenues")
        forms = sorted(e.form for e in entries)
        self.assertEqual(forms, ["10-Q", "8-K"])

    def test_foreign_filers_20f_40f_kept(self):
        """Foreign private issuers report on 20-F / 40-F / 6-K — keep these."""
        from alphalens.data.fundamentals.ttm_aggregator import _arrow_table_to_entries

        rows = [
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=1000.0,
                filed_date="2025-04-01",
                form="20-F",
                fp="FY",
            ),
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=1000.0,
                filed_date="2025-04-15",
                form="40-F",
                fp="FY",
            ),
            _row(
                concept="Revenues",
                period_start="2024-07-01",
                period_end="2024-09-30",
                val=260.0,
                filed_date="2024-11-15",
                form="6-K",
                fp="Q3",
            ),
        ]
        entries = _arrow_table_to_entries(_arrow_table(rows), "Revenues")
        forms = sorted(e.form for e in entries)
        self.assertEqual(forms, ["20-F", "40-F", "6-K"])

    def test_form_filter_param_lets_callers_override(self):
        """Tests need to bypass the gate to assert legacy behavior."""
        from alphalens.data.fundamentals.ttm_aggregator import _arrow_table_to_entries

        rows = [
            _row(
                concept="Revenues",
                period_start="2024-01-01",
                period_end="2024-12-31",
                val=999.0,
                filed_date="2025-01-01",
                form="S-1",
                fp="FY",
            ),
        ]
        entries = _arrow_table_to_entries(_arrow_table(rows), "Revenues", form_whitelist=None)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].form, "S-1")


if __name__ == "__main__":
    unittest.main()
