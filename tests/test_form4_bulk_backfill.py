"""SEC EDGAR bulk Form-4 backfill — TDD.

Tests cover:
  * Form-4 records writer — writes hive-partitioned parquet matching the
    locked ``FORM4_SCHEMA_COLUMNS``.
  * Resume-safe per-CIK manifest — skip CIKs already completed, persist
    state across runs.
  * Submissions JSON walker — iterate Form-4/4-A entries from SEC
    submissions index payload.

The full SEC walk requires 3-5 days wall on runpod and is not exercised in
unit tests; mock-tested only here.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

from alphalens.data.alt_data.form4_bulk_backfill import (
    BackfillManifest,
    FilingMetadata,
    fetch_all_form4_metadata,
    iter_form4_filings,
    write_records_to_parquet,
)
from alphalens.data.alt_data.form4_records import Form4Record


def _mk_record(
    *,
    issuer_cik: str = "0000000001",
    ticker: str = "TEST",
    accession_number: str = "0000000001-00-000001",
    filing_date: date = date(2022, 6, 1),
    transaction_date: date = date(2022, 5, 15),
    transaction_code: str = "P",
    transaction_shares: float = 1000.0,
    transaction_price_per_share: float | None = 50.0,
    is_director: bool = False,
    is_officer: bool = True,
    is_ten_percent_owner: bool = False,
    is_amendment: bool = False,
) -> Form4Record:
    return Form4Record(
        issuer_cik=issuer_cik,
        ticker=ticker,
        accession_number=accession_number,
        filing_date=filing_date,
        reporting_owner_cik="0000000100",
        reporting_owner_name="Doe, John",
        is_director=is_director,
        is_officer=is_officer,
        is_ten_percent_owner=is_ten_percent_owner,
        is_other=False,
        officer_title="VP",
        transaction_date=transaction_date,
        transaction_code=transaction_code,
        transaction_shares=Decimal(str(transaction_shares)),
        transaction_price_per_share=(
            None
            if transaction_price_per_share is None
            else Decimal(str(transaction_price_per_share))
        ),
        acquired_disposed="A",
        is_amendment=is_amendment,
        footnotes=tuple(),
    )


class TestWriteRecordsToParquet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_records_partitioned_by_transaction_year(self):
        records = [
            _mk_record(transaction_date=date(2020, 5, 1), accession_number="ACC-2020"),
            _mk_record(transaction_date=date(2021, 6, 1), accession_number="ACC-2021"),
            _mk_record(transaction_date=date(2021, 8, 1), accession_number="ACC-2021B"),
        ]
        write_records_to_parquet(records, parquet_root=self.root)

        self.assertTrue((self.root / "transaction_year=2020").is_dir())
        self.assertTrue((self.root / "transaction_year=2021").is_dir())

        ds_2021 = ds.dataset(
            str(self.root / "transaction_year=2021"),
            partitioning=None,
            format="parquet",
        )
        df = ds_2021.to_table().to_pandas()
        self.assertEqual(len(df), 2)
        self.assertEqual(set(df["accession_number"]), {"ACC-2021", "ACC-2021B"})

    def test_schema_matches_locked_columns(self):
        from alphalens.data.store.form4_pit import FORM4_SCHEMA_COLUMNS

        records = [_mk_record(transaction_date=date(2022, 5, 1))]
        write_records_to_parquet(records, parquet_root=self.root)

        dataset = ds.dataset(
            str(self.root / "transaction_year=2022"),
            partitioning=None,
            format="parquet",
        )
        df = dataset.to_table().to_pandas()
        self.assertEqual(set(df.columns), set(FORM4_SCHEMA_COLUMNS))

    def test_decimal_shares_and_prices_persist_as_floats(self):
        records = [
            _mk_record(
                transaction_date=date(2022, 5, 1),
                transaction_shares=12345.67,
                transaction_price_per_share=42.123,
            )
        ]
        write_records_to_parquet(records, parquet_root=self.root)
        dataset = ds.dataset(
            str(self.root / "transaction_year=2022"),
            partitioning=None,
            format="parquet",
        )
        df = dataset.to_table().to_pandas()
        self.assertAlmostEqual(float(df.iloc[0]["transaction_shares"]), 12345.67)
        self.assertAlmostEqual(float(df.iloc[0]["transaction_price_per_share"]), 42.123)

    def test_null_price_persists_as_null(self):
        records = [_mk_record(transaction_date=date(2022, 5, 1), transaction_price_per_share=None)]
        write_records_to_parquet(records, parquet_root=self.root)
        dataset = ds.dataset(
            str(self.root / "transaction_year=2022"),
            partitioning=None,
            format="parquet",
        )
        df = dataset.to_table().to_pandas()
        self.assertTrue(pd.isna(df.iloc[0]["transaction_price_per_share"]))

    def test_empty_records_writes_nothing(self):
        write_records_to_parquet([], parquet_root=self.root)
        # No partition directory should be created when there's nothing to write.
        self.assertFalse(any(self.root.iterdir()))

    def test_drops_records_with_transaction_date_after_filing_date(self):
        # SEC Form-4 must be filed within 2 business days of the transaction —
        # transaction_date > filed_date is dirty data (typo, future-vesting
        # forecast). Observed in production: cik=0000025232 filed 2013-02-01
        # with transaction_date=2031-01-31, creating a phantom partition
        # transaction_year=2031. Drop these records pre-write and warn.
        records = [
            _mk_record(  # clean
                accession_number="GOOD",
                filing_date=date(2022, 6, 1),
                transaction_date=date(2022, 5, 15),
            ),
            _mk_record(  # transaction_date == filing_date is OK
                accession_number="EDGE",
                filing_date=date(2022, 6, 1),
                transaction_date=date(2022, 6, 1),
            ),
            _mk_record(  # dirty: 9-year future date
                accession_number="DIRTY",
                filing_date=date(2022, 6, 1),
                transaction_date=date(2031, 1, 31),
            ),
        ]
        with self.assertLogs("alphalens.data.alt_data.form4_bulk_backfill", level="WARNING") as cm:
            write_records_to_parquet(records, parquet_root=self.root)
        self.assertTrue(
            any("transaction_date > filed_date" in msg for msg in cm.output),
            f"expected warning about future-dated records, got: {cm.output}",
        )

        # No phantom 2031 partition — only 2022 should exist.
        partitions = sorted(p.name for p in self.root.iterdir() if p.is_dir())
        self.assertEqual(partitions, ["transaction_year=2022"])

        dataset = ds.dataset(
            str(self.root / "transaction_year=2022"),
            partitioning=None,
            format="parquet",
        )
        df = dataset.to_table().to_pandas()
        self.assertEqual(set(df["accession_number"]), {"GOOD", "EDGE"})

    def test_appends_to_existing_partition_with_unique_filename(self):
        # First batch creates part-0000.parquet.
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 5, 1), accession_number="A1")],
            parquet_root=self.root,
        )
        # Second batch should create a NEW file in the same partition, not overwrite.
        write_records_to_parquet(
            [_mk_record(transaction_date=date(2022, 6, 1), accession_number="A2")],
            parquet_root=self.root,
        )
        files = sorted((self.root / "transaction_year=2022").glob("*.parquet"))
        self.assertEqual(len(files), 2)
        dataset = ds.dataset(
            str(self.root / "transaction_year=2022"),
            partitioning=None,
            format="parquet",
        )
        df = dataset.to_table().to_pandas()
        self.assertEqual(set(df["accession_number"]), {"A1", "A2"})


class TestBackfillManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "manifest.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_manifest_has_no_completed_ciks(self):
        m = BackfillManifest.load_or_create(self.path)
        self.assertFalse(m.is_complete("0000320193"))

    def test_mark_complete_persists(self):
        m1 = BackfillManifest.load_or_create(self.path)
        m1.mark_complete("0000320193")
        m1.save()

        m2 = BackfillManifest.load_or_create(self.path)
        self.assertTrue(m2.is_complete("0000320193"))
        self.assertFalse(m2.is_complete("0000789019"))

    def test_corrupted_manifest_starts_fresh_with_warning(self):
        self.path.write_text("not valid json {{{")
        m = BackfillManifest.load_or_create(self.path)
        self.assertFalse(m.is_complete("0000320193"))


class TestIterForm4Filings(unittest.TestCase):
    """Walks a submissions JSON payload yielding Form-4/4-A FilingMetadata."""

    def test_yields_form4_entries_only(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["10-K", "4", "8-K", "4/A", "4"],
                    "accessionNumber": ["A0", "A1", "A2", "A3", "A4"],
                    "filingDate": [
                        "2022-01-15",
                        "2022-02-01",
                        "2022-02-15",
                        "2022-03-01",
                        "2022-03-15",
                    ],
                    "primaryDocument": [
                        "10k.htm",
                        "f4_a1.xml",
                        "8k.htm",
                        "f4_a3.xml",
                        "f4_a4.xml",
                    ],
                }
            }
        }
        results = list(iter_form4_filings(submissions, cik="0000320193"))
        accessions = [r.accession_number for r in results]
        self.assertEqual(accessions, ["A1", "A3", "A4"])
        forms = [r.form for r in results]
        self.assertEqual(forms, ["4", "4/A", "4"])

    def test_handles_missing_recent_block(self):
        results = list(iter_form4_filings({"filings": {}}, cik="0000320193"))
        self.assertEqual(results, [])

    def test_strips_xsl_prefix_from_primary_document(self):
        # SEC's submissions JSON returns the XSL-rendered HTML path for
        # primaryDocument (e.g. xslF345X06/form4.xml). The raw XML lives in
        # the parent directory; the runner must fetch THAT path or the
        # parser sees HTML and rejects every filing as malformed XML.
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4", "4", "4"],
                    "accessionNumber": ["A1", "A2", "A3"],
                    "filingDate": ["2022-01-15", "2022-02-15", "2022-03-15"],
                    "primaryDocument": [
                        "xslF345X06/form4.xml",  # modern XSL prefix
                        "xslF345X05/wf-form4_doc.xml",  # older XSL prefix
                        "wf-form4_doc.xml",  # already raw, no prefix
                    ],
                }
            }
        }
        results = list(iter_form4_filings(submissions, cik="0000320193"))
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].primary_document, "form4.xml")
        self.assertEqual(results[1].primary_document, "wf-form4_doc.xml")
        self.assertEqual(results[2].primary_document, "wf-form4_doc.xml")

    def test_returns_filing_metadata_with_filed_date(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "accessionNumber": ["0000320193-22-000001"],
                    "filingDate": ["2022-04-15"],
                    "primaryDocument": ["wf-form4_doc.xml"],
                }
            }
        }
        results = list(iter_form4_filings(submissions, cik="0000320193"))
        self.assertEqual(len(results), 1)
        meta = results[0]
        self.assertIsInstance(meta, FilingMetadata)
        self.assertEqual(meta.cik, "0000320193")
        self.assertEqual(meta.accession_number, "0000320193-22-000001")
        self.assertEqual(meta.filing_date, date(2022, 4, 15))
        self.assertEqual(meta.primary_document, "wf-form4_doc.xml")
        self.assertEqual(meta.form, "4")


class _FakeClient:
    """Test double for SecEdgarClient covering submissions + overflow fetches."""

    def __init__(
        self,
        submissions: dict,
        overflow_payloads: dict[str, dict] | None = None,
    ):
        self._submissions = submissions
        self._overflow = overflow_payloads or {}
        self.overflow_calls: list[str] = []

    def fetch_submissions(self, cik: str) -> dict:
        return self._submissions

    def fetch_submissions_overflow(self, name: str) -> dict:
        self.overflow_calls.append(name)
        if name not in self._overflow:
            raise KeyError(f"unexpected overflow fetch: {name}")
        return self._overflow[name]


class TestFetchAllForm4Metadata(unittest.TestCase):
    """fetch_all_form4_metadata walks both 'recent' and 'files' overflow blocks.

    SEC submissions JSON caps 'recent' at 1000 entries across all form types.
    For prolific issuers (AAPL, MSFT, GE, JPM with thousands of insider Form-4s
    over 22 years) the overflow lives in 'files' pointers like
    CIK0000320193-submissions-001.json. Overflow JSONs share the same
    {filings: {recent: {...}}} shape as the main file (verified against
    https://data.sec.gov/submissions/CIK0000320193-submissions-001.json).

    Without walking 'files', historical Form-4 data prior to the most recent
    1000 filings is silently lost — exactly the data Cohen-Malloy needs.
    """

    def test_yields_form4_from_recent_only_when_no_overflow(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4", "4"],
                    "accessionNumber": ["R1", "R2"],
                    "filingDate": ["2022-01-15", "2022-02-15"],
                    "primaryDocument": ["form4.xml", "form4.xml"],
                },
                "files": [],
            }
        }
        client = _FakeClient(submissions)
        results = list(fetch_all_form4_metadata(client, cik="0000320193"))
        self.assertEqual([r.accession_number for r in results], ["R1", "R2"])
        self.assertEqual(client.overflow_calls, [])

    def test_walks_overflow_files_and_concatenates_results(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "accessionNumber": ["RECENT-1"],
                    "filingDate": ["2024-01-15"],
                    "primaryDocument": ["form4.xml"],
                },
                "files": [
                    {
                        "name": "CIK0000320193-submissions-001.json",
                        "filingCount": 2,
                        "filingFrom": "2018-01-01",
                        "filingTo": "2019-06-15",
                    },
                    {
                        "name": "CIK0000320193-submissions-002.json",
                        "filingCount": 1,
                        "filingFrom": "2010-01-01",
                        "filingTo": "2017-12-31",
                    },
                ],
            }
        }
        overflow_payloads = {
            "CIK0000320193-submissions-001.json": {
                "filings": {
                    "recent": {
                        "form": ["4", "10-K", "4"],  # only Form-4s yielded
                        "accessionNumber": ["OF1-A", "OF1-B", "OF1-C"],
                        "filingDate": ["2018-05-01", "2018-06-01", "2019-01-01"],
                        "primaryDocument": [
                            "form4.xml",
                            "10k.htm",
                            "form4.xml",
                        ],
                    }
                }
            },
            "CIK0000320193-submissions-002.json": {
                "filings": {
                    "recent": {
                        "form": ["4"],
                        "accessionNumber": ["OF2-A"],
                        "filingDate": ["2012-08-15"],
                        "primaryDocument": ["form4.xml"],
                    }
                }
            },
        }
        client = _FakeClient(submissions, overflow_payloads)
        results = list(fetch_all_form4_metadata(client, cik="0000320193"))
        accessions = [r.accession_number for r in results]
        self.assertEqual(accessions, ["RECENT-1", "OF1-A", "OF1-C", "OF2-A"])
        self.assertEqual(
            client.overflow_calls,
            [
                "CIK0000320193-submissions-001.json",
                "CIK0000320193-submissions-002.json",
            ],
        )

    def test_handles_null_files_block(self):
        # Some SEC payloads (e.g. completed older filers) return files=null.
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "accessionNumber": ["R1"],
                    "filingDate": ["2022-01-15"],
                    "primaryDocument": ["form4.xml"],
                },
                "files": None,
            }
        }
        client = _FakeClient(submissions)
        results = list(fetch_all_form4_metadata(client, cik="0000320193"))
        self.assertEqual([r.accession_number for r in results], ["R1"])
        self.assertEqual(client.overflow_calls, [])

    def test_handles_missing_files_key(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "accessionNumber": ["R1"],
                    "filingDate": ["2022-01-15"],
                    "primaryDocument": ["form4.xml"],
                },
            }
        }
        client = _FakeClient(submissions)
        results = list(fetch_all_form4_metadata(client, cik="0000320193"))
        self.assertEqual([r.accession_number for r in results], ["R1"])
        self.assertEqual(client.overflow_calls, [])

    def test_skips_overflow_entry_with_missing_name(self):
        # Defensive: malformed files entry without 'name' is logged + skipped,
        # not crash-causing.
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "accessionNumber": ["R1"],
                    "filingDate": ["2022-01-15"],
                    "primaryDocument": ["form4.xml"],
                },
                "files": [
                    {"filingCount": 100},  # no 'name'
                    {"name": "CIK0000320193-submissions-001.json"},
                ],
            }
        }
        overflow_payloads = {
            "CIK0000320193-submissions-001.json": {
                "filings": {
                    "recent": {
                        "form": ["4"],
                        "accessionNumber": ["OF1"],
                        "filingDate": ["2018-05-01"],
                        "primaryDocument": ["form4.xml"],
                    }
                }
            },
        }
        client = _FakeClient(submissions, overflow_payloads)
        results = list(fetch_all_form4_metadata(client, cik="0000320193"))
        self.assertEqual([r.accession_number for r in results], ["R1", "OF1"])

    def test_overflow_xsl_prefix_stripped(self):
        # XSL stripping must apply to overflow entries too.
        submissions = {
            "filings": {
                "recent": {
                    "form": [],
                    "accessionNumber": [],
                    "filingDate": [],
                    "primaryDocument": [],
                },
                "files": [{"name": "CIK0000320193-submissions-001.json"}],
            }
        }
        overflow_payloads = {
            "CIK0000320193-submissions-001.json": {
                "filings": {
                    "recent": {
                        "form": ["4"],
                        "accessionNumber": ["OF1"],
                        "filingDate": ["2010-05-01"],
                        "primaryDocument": ["xslF345X02/form4.xml"],
                    }
                }
            },
        }
        client = _FakeClient(submissions, overflow_payloads)
        results = list(fetch_all_form4_metadata(client, cik="0000320193"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].primary_document, "form4.xml")


if __name__ == "__main__":
    unittest.main()
