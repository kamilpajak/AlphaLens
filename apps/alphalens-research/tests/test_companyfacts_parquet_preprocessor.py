"""Tests for the SEC companyfacts JSON -> long-format Arrow Table preprocessor.

Replaces the dict-of-parsed-JSON cache pattern of FosterSUEStore /
SloanAccrualsStore / AnnouncementDateProvider that hit 90 GB peak RSS at
S&P 1500 universe (3x duplication across stores). The new path persists each
CIK as a single long-format parquet file; stores read via Arrow Tables and
filter in-memory via vectorized pyarrow.compute.
"""

from __future__ import annotations

import unittest
from datetime import date

import pyarrow as pa
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
    companyfacts_json_to_parquet_table,
)


def _entry(
    *,
    start: str | None = None,
    end: str,
    val: float,
    accn: str,
    fy: int | None,
    fp: str | None,
    form: str,
    filed: str,
    frame: str | None = None,
) -> dict:
    """Build a single companyfacts entry mirroring SEC bulk-dump shape."""
    out: dict = {"end": end, "val": val, "accn": accn, "form": form, "filed": filed}
    if start is not None:
        out["start"] = start
    if fy is not None:
        out["fy"] = fy
    if fp is not None:
        out["fp"] = fp
    if frame is not None:
        out["frame"] = frame
    return out


class TestCompanyfactsJsonToParquetTableHappyPath(unittest.TestCase):
    def test_single_concept_single_duration_entry(self):
        facts = {
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {
                "us-gaap": {
                    "EarningsPerShareBasic": {
                        "label": "EPS Basic",
                        "description": "...",
                        "units": {
                            "USD/shares": [
                                _entry(
                                    start="2023-04-02",
                                    end="2023-07-01",
                                    val=1.27,
                                    accn="0000320193-23-000077",
                                    fy=2023,
                                    fp="Q3",
                                    form="10-Q",
                                    filed="2023-08-04",
                                    frame="CY2023Q2",
                                ),
                            ],
                        },
                    },
                },
            },
        }

        table = companyfacts_json_to_parquet_table(facts)

        self.assertIsInstance(table, pa.Table)
        self.assertEqual(table.num_rows, 1)
        self.assertEqual(
            table.column_names,
            [
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
            ],
        )
        # Date columns must be date32 for compact storage and natural compares.
        self.assertEqual(table.schema.field("period_start").type, pa.date32())
        self.assertEqual(table.schema.field("period_end").type, pa.date32())
        self.assertEqual(table.schema.field("filed_date").type, pa.date32())
        self.assertEqual(table.schema.field("val").type, pa.float64())
        self.assertEqual(table.schema.field("fy").type, pa.int32())
        # Optional string fields must be nullable.
        self.assertTrue(table.schema.field("frame").nullable)
        self.assertTrue(table.schema.field("fp").nullable)
        self.assertTrue(table.schema.field("fy").nullable)
        self.assertTrue(table.schema.field("period_start").nullable)

        row = table.to_pylist()[0]
        self.assertEqual(row["taxonomy"], "us-gaap")
        self.assertEqual(row["concept"], "EarningsPerShareBasic")
        self.assertEqual(row["unit"], "USD/shares")
        self.assertEqual(row["period_start"], date(2023, 4, 2))
        self.assertEqual(row["period_end"], date(2023, 7, 1))
        self.assertEqual(row["val"], 1.27)
        self.assertEqual(row["accn"], "0000320193-23-000077")
        self.assertEqual(row["fy"], 2023)
        self.assertEqual(row["fp"], "Q3")
        self.assertEqual(row["form"], "10-Q")
        self.assertEqual(row["filed_date"], date(2023, 8, 4))
        self.assertEqual(row["frame"], "CY2023Q2")

    def test_multiple_concepts_multiple_units_multiple_entries(self):
        facts = {
            "cik": 320193,
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                _entry(
                                    end="2023-07-01",
                                    val=335_000_000_000.0,
                                    accn="0000320193-23-000077",
                                    fy=2023,
                                    fp="Q3",
                                    form="10-Q",
                                    filed="2023-08-04",
                                ),
                                _entry(
                                    end="2023-09-30",
                                    val=352_000_000_000.0,
                                    accn="0000320193-23-000106",
                                    fy=2023,
                                    fp="FY",
                                    form="10-K",
                                    filed="2023-11-03",
                                ),
                            ],
                        },
                    },
                    "EarningsPerShareBasic": {
                        "units": {
                            "USD/shares": [
                                _entry(
                                    start="2023-04-02",
                                    end="2023-07-01",
                                    val=1.27,
                                    accn="0000320193-23-000077",
                                    fy=2023,
                                    fp="Q3",
                                    form="10-Q",
                                    filed="2023-08-04",
                                ),
                            ],
                        },
                    },
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "units": {
                            "shares": [
                                _entry(
                                    end="2023-07-21",
                                    val=15_634_232_000.0,
                                    accn="0000320193-23-000077",
                                    fy=2023,
                                    fp="Q3",
                                    form="10-Q",
                                    filed="2023-08-04",
                                ),
                            ],
                        },
                    },
                },
            },
        }

        table = companyfacts_json_to_parquet_table(facts)

        self.assertEqual(table.num_rows, 4)
        rows = table.to_pylist()
        # Assert we covered both taxonomies and all three concepts.
        self.assertEqual(
            sorted({(r["taxonomy"], r["concept"]) for r in rows}),
            [
                ("dei", "EntityCommonStockSharesOutstanding"),
                ("us-gaap", "Assets"),
                ("us-gaap", "EarningsPerShareBasic"),
            ],
        )
        # Instant entries (Assets, shares-outstanding) must have NULL period_start.
        instants = [r for r in rows if r["concept"] != "EarningsPerShareBasic"]
        self.assertTrue(all(r["period_start"] is None for r in instants))
        # Duration entry (EPS Q3) has both start and end populated.
        eps = next(r for r in rows if r["concept"] == "EarningsPerShareBasic")
        self.assertEqual(eps["period_start"], date(2023, 4, 2))
        self.assertEqual(eps["period_end"], date(2023, 7, 1))


class TestCompanyfactsJsonToParquetTableEdgeCases(unittest.TestCase):
    def test_top_level_missing_facts_returns_empty_table(self):
        table = companyfacts_json_to_parquet_table({"cik": 1, "entityName": "X"})
        self.assertEqual(table.num_rows, 0)
        self.assertEqual(table.schema, _expected_schema())

    def test_facts_block_with_no_taxonomies_returns_empty_table(self):
        table = companyfacts_json_to_parquet_table({"facts": {}})
        self.assertEqual(table.num_rows, 0)

    def test_concept_without_units_block_is_skipped(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "Assets": {"label": "Assets", "description": "..."},  # no "units"
                },
            },
        }
        self.assertEqual(companyfacts_json_to_parquet_table(facts).num_rows, 0)

    def test_concept_with_empty_unit_list_is_skipped(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "Assets": {"units": {"USD": []}},
                },
            },
        }
        self.assertEqual(companyfacts_json_to_parquet_table(facts).num_rows, 0)

    def test_truncated_entry_missing_val_is_skipped(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                {
                                    "end": "2023-12-31",
                                    "accn": "0001-23-456",
                                    "fy": 2023,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2024-02-15",
                                },  # no "val"
                            ],
                        },
                    },
                },
            },
        }
        self.assertEqual(companyfacts_json_to_parquet_table(facts).num_rows, 0)

    def test_truncated_entry_missing_filed_is_skipped(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                {
                                    "end": "2023-12-31",
                                    "val": 100.0,
                                    "accn": "0001-23-456",
                                    "fy": 2023,
                                    "fp": "FY",
                                    "form": "10-K",
                                },  # no "filed"
                            ],
                        },
                    },
                },
            },
        }
        self.assertEqual(companyfacts_json_to_parquet_table(facts).num_rows, 0)

    def test_optional_fields_default_to_null(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                _entry(
                                    end="2023-12-31",
                                    val=100.0,
                                    accn="0001-23-456",
                                    fy=None,  # missing fy is allowed
                                    fp=None,  # missing fp is allowed
                                    form="10-K",
                                    filed="2024-02-15",
                                    # frame and start omitted -> NULL
                                ),
                            ],
                        },
                    },
                },
            },
        }
        rows = companyfacts_json_to_parquet_table(facts).to_pylist()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsNone(row["fy"])
        self.assertIsNone(row["fp"])
        self.assertIsNone(row["frame"])
        self.assertIsNone(row["period_start"])
        self.assertEqual(row["period_end"], date(2023, 12, 31))
        self.assertEqual(row["filed_date"], date(2024, 2, 15))

    def test_irregular_fp_values_pass_through_verbatim(self):
        # SEC bulk dumps include H1 (semi-annual), H2, CY (calendar-year)
        # in addition to the canonical Q1/Q2/Q3/Q4/FY values.
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContract": {
                        "units": {
                            "USD": [
                                _entry(
                                    start="2023-01-01",
                                    end="2023-06-30",
                                    val=1.0e9,
                                    accn="0002-23-001",
                                    fy=2023,
                                    fp="H1",
                                    form="20-F",
                                    filed="2023-08-01",
                                ),
                                _entry(
                                    start="2023-01-01",
                                    end="2023-12-31",
                                    val=2.5e9,
                                    accn="0002-24-001",
                                    fy=2023,
                                    fp="CY",
                                    form="10-K",
                                    filed="2024-02-01",
                                ),
                            ],
                        },
                    },
                },
            },
        }
        rows = companyfacts_json_to_parquet_table(facts).to_pylist()
        self.assertEqual(sorted(r["fp"] for r in rows), ["CY", "H1"])

    def test_non_dict_concept_block_skipped_safely(self):
        # Defensive: SEC has been known to ship malformed records during
        # transient mid-update fetches.
        facts = {
            "facts": {
                "us-gaap": {
                    "Assets": "this should be a dict",  # malformed
                    "Liabilities": {
                        "units": {
                            "USD": [
                                _entry(
                                    end="2023-12-31",
                                    val=50.0,
                                    accn="0003-23-001",
                                    fy=2023,
                                    fp="FY",
                                    form="10-K",
                                    filed="2024-02-15",
                                ),
                            ],
                        },
                    },
                },
            },
        }
        rows = companyfacts_json_to_parquet_table(facts).to_pylist()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["concept"], "Liabilities")

    def test_non_numeric_fy_value_does_not_abort_table(self):
        # Defensive: zen pre-merge review 2026-05-20 flagged that int(fy_value)
        # would ValueError on hypothetical SEC values like "2024A" or
        # "Transition", aborting the whole parquet build. Safe-cast keeps the
        # row with fy=None instead.
        facts = {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                {
                                    "end": "2023-12-31",
                                    "val": 100.0,
                                    "accn": "0001-23-456",
                                    "fy": "Transition",
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2024-02-15",
                                },
                            ],
                        },
                    },
                },
            },
        }
        rows = companyfacts_json_to_parquet_table(facts).to_pylist()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["fy"])


def _expected_schema() -> pa.Schema:
    """Snapshot of the canonical schema used to assert empty-table shape."""
    from alphalens_pipeline.data.fundamentals.companyfacts_parquet import SCHEMA

    return SCHEMA


if __name__ == "__main__":
    unittest.main()
