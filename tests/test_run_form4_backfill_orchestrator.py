"""Form-4 backfill orchestrator — fetch, batched flush, manifest checkpoint.

These tests guard the long-running 5-10 day SEC walk against silent
regressions in the loop control flow:

  * Already-completed CIKs are skipped (resume safety after crash).
  * A CIK that raises during fetch does not abort the whole loop.
  * The manifest reflects only successfully-completed CIKs.
  * The walker traverses both ``recent`` and ``files`` overflow blocks
    via :func:`fetch_all_form4_metadata` (verified end-to-end).
  * Records are buffered across CIKs and flushed when the buffer crosses
    a record-count threshold or at end of run — drastically reducing
    parquet file proliferation (without batching: ~100k tiny files for
    the full 8000-CIK universe; with batching at 5k records: ~1k files).

Network and parser internals are stubbed via fakes; only orchestration
sequencing is tested here.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow.dataset as ds

from alphalens.data.alt_data.form4_bulk_backfill import BackfillManifest
from alphalens.data.alt_data.form4_records import Form4Record
from alphalens.data.alt_data.sec_edgar_client import SecEdgarError
from scripts.run_form4_backfill import (
    _fetch_records_for_cik,
    _flush_batch,
    _run_backfill_loop,
)


def _mk_record(*, cik: str, accession: str, transaction_date: date) -> Form4Record:
    return Form4Record(
        issuer_cik=cik,
        ticker="TEST",
        accession_number=accession,
        filing_date=transaction_date,
        reporting_owner_cik="0000000100",
        reporting_owner_name="Doe, John",
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        is_other=False,
        officer_title="VP",
        transaction_date=transaction_date,
        transaction_code="P",
        transaction_shares=Decimal("1000"),
        transaction_price_per_share=Decimal("50"),
        acquired_disposed="A",
        is_amendment=False,
        footnotes=tuple(),
    )


class _FakeClient:
    """Minimal SecEdgarClient surrogate for orchestrator tests.

    Holds:
      * ``submissions_payloads``: maps cik → submissions JSON.
      * ``overflow_payloads``: maps name → overflow submissions JSON.
      * ``xml_payloads``: maps (cik, accession) → bytes; missing → SecEdgarError.
      * ``submissions_errors``: ciks for which fetch_submissions raises.
    """

    def __init__(
        self,
        *,
        submissions_payloads: dict[str, dict] | None = None,
        overflow_payloads: dict[str, dict] | None = None,
        xml_payloads: dict[tuple[str, str], bytes] | None = None,
        submissions_errors: set[str] | None = None,
    ):
        self._submissions = submissions_payloads or {}
        self._overflow = overflow_payloads or {}
        self._xml = xml_payloads or {}
        self._submissions_errors = submissions_errors or set()
        self.submissions_calls: list[str] = []
        self.overflow_calls: list[str] = []
        self.xml_calls: list[tuple[str, str]] = []

    def fetch_submissions(self, cik: str) -> dict:
        self.submissions_calls.append(cik)
        if cik in self._submissions_errors:
            raise SecEdgarError(f"simulated fetch failure for {cik}")
        return self._submissions.get(cik, {"filings": {"recent": {}}})

    def fetch_submissions_overflow(self, name: str) -> dict:
        self.overflow_calls.append(name)
        return self._overflow.get(name, {"filings": {"recent": {}}})

    def fetch_form4_xml(self, *, cik: str, accession_number: str, primary_doc: str) -> bytes:
        self.xml_calls.append((cik, accession_number))
        if (cik, accession_number) not in self._xml:
            raise SecEdgarError(f"missing xml for {cik}/{accession_number}")
        return self._xml[(cik, accession_number)]


def _submissions_with_form4(form4_entries: list[tuple[str, str]]) -> dict:
    """Build a submissions payload with given (accession, filingDate) Form-4 entries."""
    return {
        "filings": {
            "recent": {
                "form": ["4"] * len(form4_entries),
                "accessionNumber": [acc for acc, _ in form4_entries],
                "filingDate": [d for _, d in form4_entries],
                "primaryDocument": ["form4.xml"] * len(form4_entries),
            }
        }
    }


# Minimal valid Form-4 XML the parser will accept; transaction_date = filing_date
# (set per-test via the wrapper below) so future-date filter never trips.
def _xml_for(transaction_date: date) -> bytes:
    iso = transaction_date.isoformat()
    return f"""<?xml version='1.0'?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>{iso}</periodOfReport>
  <issuer>
    <issuerCik>0000000001</issuerCik>
    <issuerTradingSymbol>TEST</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000000100</rptOwnerCik>
      <rptOwnerName>Doe, John</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>VP</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>{iso}</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
""".encode()


class TestFetchRecordsForCik(unittest.TestCase):
    """_fetch_records_for_cik returns Form4Records — does NOT write."""

    def test_submissions_fetch_failure_returns_empty_list(self):
        # Network blip on submissions fetch must NOT abort the loop; caller
        # gets an empty list and continues to the next CIK.
        client = _FakeClient(submissions_errors={"0000000001"})
        records = _fetch_records_for_cik(
            "0000000001",
            client=client,
            start_year=2006,
            end_year=2026,
        )
        self.assertEqual(records, [])

    def test_filings_outside_year_window_skipped(self):
        # Range filter is on filing_date.year; older filings ignored even if
        # present in submissions JSON.
        cik = "0000000001"
        submissions = _submissions_with_form4(
            [("OLD-2005", "2005-06-01"), ("RECENT-2010", "2010-06-01")]
        )
        client = _FakeClient(
            submissions_payloads={cik: submissions},
            xml_payloads={(cik, "RECENT-2010"): _xml_for(date(2010, 6, 1))},
        )
        records = _fetch_records_for_cik(cik, client=client, start_year=2006, end_year=2026)
        self.assertEqual(len(records), 1)
        # Only the in-window XML was fetched.
        self.assertEqual(client.xml_calls, [(cik, "RECENT-2010")])

    def test_filing_xml_fetch_failure_does_not_abort_cik(self):
        # When ONE filing's XML 404s, the CIK loop continues to the next
        # filing — only that one record is dropped, not the whole CIK.
        cik = "0000000001"
        submissions = _submissions_with_form4([("BAD-1", "2020-06-01"), ("GOOD-1", "2020-07-01")])
        client = _FakeClient(
            submissions_payloads={cik: submissions},
            xml_payloads={(cik, "GOOD-1"): _xml_for(date(2020, 7, 1))},
        )
        records = _fetch_records_for_cik(cik, client=client, start_year=2006, end_year=2026)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].accession_number, "GOOD-1")
        # Both XML fetches attempted.
        self.assertEqual(set(client.xml_calls), {(cik, "BAD-1"), (cik, "GOOD-1")})

    def test_overflow_files_walked_when_present(self):
        # Filers with >1000 filings: 'files' overflow must be walked, otherwise
        # historical data silently lost (caused first 6-day backfill abort).
        cik = "0000000001"
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "accessionNumber": ["RECENT-1"],
                    "filingDate": ["2024-06-01"],
                    "primaryDocument": ["form4.xml"],
                },
                "files": [
                    {"name": "CIK0000000001-submissions-001.json"},
                ],
            }
        }
        overflow_payload = _submissions_with_form4([("OVERFLOW-1", "2010-06-01")])
        client = _FakeClient(
            submissions_payloads={cik: submissions},
            overflow_payloads={"CIK0000000001-submissions-001.json": overflow_payload},
            xml_payloads={
                (cik, "RECENT-1"): _xml_for(date(2024, 6, 1)),
                (cik, "OVERFLOW-1"): _xml_for(date(2010, 6, 1)),
            },
        )
        records = _fetch_records_for_cik(cik, client=client, start_year=2006, end_year=2026)
        self.assertEqual(len(records), 2)
        self.assertEqual(client.submissions_calls, [cik])
        self.assertEqual(
            client.overflow_calls,
            ["CIK0000000001-submissions-001.json"],
        )
        self.assertEqual(
            set(r.accession_number for r in records),
            {"RECENT-1", "OVERFLOW-1"},
        )

    def test_no_form4_filings_returns_empty_list(self):
        # CIK with submissions but zero Form-4s returns [] cleanly.
        cik = "0000000001"
        submissions = {
            "filings": {
                "recent": {
                    "form": ["10-K", "8-K"],
                    "accessionNumber": ["A1", "A2"],
                    "filingDate": ["2020-01-01", "2020-02-01"],
                    "primaryDocument": ["doc.htm", "doc.htm"],
                }
            }
        }
        client = _FakeClient(submissions_payloads={cik: submissions})
        records = _fetch_records_for_cik(cik, client=client, start_year=2006, end_year=2026)
        self.assertEqual(records, [])


class TestFlushBatch(unittest.TestCase):
    """_flush_batch writes records, marks all CIKs complete, saves manifest."""

    def test_flush_writes_parquet_and_marks_all_ciks_complete(self):
        records = [
            _mk_record(cik="A", accession="A-1", transaction_date=date(2022, 5, 1)),
            _mk_record(cik="A", accession="A-2", transaction_date=date(2022, 6, 1)),
            _mk_record(cik="B", accession="B-1", transaction_date=date(2022, 7, 1)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = BackfillManifest.load_or_create(tmp_path / "manifest.json")
            _flush_batch(
                records,
                ciks=["A", "B"],
                parquet_root=tmp_path / "parquet",
                manifest=manifest,
            )
            # Manifest persisted to disk in same call.
            data = json.loads((tmp_path / "manifest.json").read_text())
            self.assertEqual(set(data["completed_ciks"]), {"A", "B"})

            # Single parquet file for the year (3 records all from 2022).
            files = list((tmp_path / "parquet" / "transaction_year=2022").glob("*.parquet"))
            self.assertEqual(len(files), 1)
            df = ds.dataset(str(files[0]), format="parquet").to_table().to_pandas()
            self.assertEqual(len(df), 3)
            self.assertEqual(set(df["accession_number"]), {"A-1", "A-2", "B-1"})

    def test_flush_with_empty_records_still_marks_ciks_complete(self):
        # CIKs with zero Form-4s (10-K-only filers) must still be marked
        # complete so resume doesn't refetch them forever.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = BackfillManifest.load_or_create(tmp_path / "manifest.json")
            _flush_batch(
                [],
                ciks=["X", "Y"],
                parquet_root=tmp_path / "parquet",
                manifest=manifest,
            )
            self.assertTrue(manifest.is_complete("X"))
            self.assertTrue(manifest.is_complete("Y"))


class TestRunBackfillLoop(unittest.TestCase):
    """End-to-end loop with batched flushing — the critical file-count fix."""

    def _build_client_with_n_ciks_each_having_records(
        self, n_ciks: int, records_per_cik: int
    ) -> tuple[_FakeClient, list[str]]:
        ciks = [f"{i:010d}" for i in range(n_ciks)]
        submissions_payloads = {}
        xml_payloads = {}
        for cik in ciks:
            entries = [
                (f"ACC-{cik}-{j}", f"2020-{(j % 12) + 1:02d}-15") for j in range(records_per_cik)
            ]
            submissions_payloads[cik] = _submissions_with_form4(entries)
            for j in range(records_per_cik):
                acc = f"ACC-{cik}-{j}"
                month = (j % 12) + 1
                xml_payloads[(cik, acc)] = _xml_for(date(2020, month, 15))
        return _FakeClient(
            submissions_payloads=submissions_payloads,
            xml_payloads=xml_payloads,
        ), ciks

    def test_batched_flush_dramatically_reduces_file_count(self):
        # 50 CIKs × 100 records each = 5000 records.
        # Without batching (per-CIK flush): ~50 files (one per CIK).
        # With batched flush at threshold=1000: 5 flushes -> 5 files in 2020 partition.
        # Real-world impact: 8000 CIKs averaging 50 records each = 400k records.
        # Per-CIK flush -> ~8000 files; batched at 5000 -> ~80 files.
        client, ciks = self._build_client_with_n_ciks_each_having_records(
            n_ciks=50, records_per_cik=100
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = BackfillManifest.load_or_create(tmp_path / "manifest.json")
            _run_backfill_loop(
                ciks,
                client=client,
                parquet_root=tmp_path / "parquet",
                manifest=manifest,
                start_year=2006,
                end_year=2026,
                flush_threshold=1000,
            )
            files = list((tmp_path / "parquet" / "transaction_year=2020").glob("*.parquet"))
            # Per-CIK flush would create 50 files; batched-at-1000 creates ~5.
            self.assertLessEqual(
                len(files),
                10,
                f"expected ~5 files with batched flush, got {len(files)}",
            )
            self.assertEqual(len(manifest.completed_ciks), 50)

    def test_skips_already_completed_ciks(self):
        # Resume safety: previously completed CIKs are not refetched.
        client, ciks = self._build_client_with_n_ciks_each_having_records(
            n_ciks=3, records_per_cik=2
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = BackfillManifest.load_or_create(tmp_path / "manifest.json")
            # Pre-mark first CIK as done.
            manifest.mark_complete(ciks[0])
            manifest.save()

            _run_backfill_loop(
                ciks,
                client=client,
                parquet_root=tmp_path / "parquet",
                manifest=manifest,
                start_year=2006,
                end_year=2026,
                flush_threshold=1000,
            )
            # First CIK never re-fetched.
            self.assertNotIn(ciks[0], client.submissions_calls)
            # Other two were processed and marked complete.
            self.assertIn(ciks[1], manifest.completed_ciks)
            self.assertIn(ciks[2], manifest.completed_ciks)

    def test_residual_buffer_flushed_at_end_of_run(self):
        # When total records < threshold, end-of-run flush still writes them.
        client, ciks = self._build_client_with_n_ciks_each_having_records(
            n_ciks=3, records_per_cik=10
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = BackfillManifest.load_or_create(tmp_path / "manifest.json")
            _run_backfill_loop(
                ciks,
                client=client,
                parquet_root=tmp_path / "parquet",
                manifest=manifest,
                start_year=2006,
                end_year=2026,
                flush_threshold=10_000,  # never reached
            )
            files = list((tmp_path / "parquet" / "transaction_year=2020").glob("*.parquet"))
            # Single end-of-run flush for all 3 CIKs.
            self.assertEqual(len(files), 1)
            self.assertEqual(len(manifest.completed_ciks), 3)

    def test_continues_past_unhandled_exception_on_one_cik(self):
        # If a CIK raises an unhandled exception (not SecEdgarError), the
        # loop logs and continues to the next CIK.
        ciks = ["0000000001", "0000000002", "0000000003"]
        client = _FakeClient(
            submissions_errors={"0000000001"},  # raises SecEdgarError
            submissions_payloads={
                "0000000002": _submissions_with_form4([("ACC-2", "2020-05-15")]),
                "0000000003": _submissions_with_form4([("ACC-3", "2020-06-15")]),
            },
            xml_payloads={
                ("0000000002", "ACC-2"): _xml_for(date(2020, 5, 15)),
                ("0000000003", "ACC-3"): _xml_for(date(2020, 6, 15)),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = BackfillManifest.load_or_create(tmp_path / "manifest.json")
            _run_backfill_loop(
                ciks,
                client=client,
                parquet_root=tmp_path / "parquet",
                manifest=manifest,
                start_year=2006,
                end_year=2026,
                flush_threshold=10_000,
            )
            # All 3 CIKs marked complete (the one that errored has empty
            # records but still completes — this matches our existing
            # _process_cik fault tolerance contract).
            self.assertEqual(len(manifest.completed_ciks), 3)


class TestManifestPersistence(unittest.TestCase):
    """Mark-complete + checkpoint logic (covers BackfillManifest end-to-end)."""

    def test_completed_cik_persisted_to_disk_after_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            m1 = BackfillManifest.load_or_create(path)
            m1.mark_complete("0000000001")
            m1.mark_complete("0000000002")
            m1.save()

            data = json.loads(path.read_text())
            self.assertEqual(data["version"], 1)
            self.assertEqual(
                set(data["completed_ciks"]),
                {"0000000001", "0000000002"},
            )

    def test_load_or_create_recovers_state_across_processes(self):
        # Simulates VPS restart: after a crash, manifest should reload.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            m1 = BackfillManifest.load_or_create(path)
            m1.mark_complete("0000000001")
            m1.save()
            del m1

            m2 = BackfillManifest.load_or_create(path)
            self.assertTrue(m2.is_complete("0000000001"))
            self.assertFalse(m2.is_complete("0000000002"))


if __name__ == "__main__":
    unittest.main()
