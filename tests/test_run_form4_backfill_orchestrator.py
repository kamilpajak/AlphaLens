"""Form-4 backfill orchestrator — manifest checkpoint + error continuation.

These tests guard the long-running 5-10 day SEC walk against silent
regressions in the loop control flow:

  * Already-completed CIKs are skipped (resume safety after crash).
  * A CIK that raises in :func:`_process_cik` does not abort the whole loop.
  * The manifest reflects only successfully-completed CIKs.
  * The walker now traverses both ``recent`` and ``files`` overflow blocks
    via :func:`fetch_all_form4_metadata` (verified end-to-end).

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
from scripts.run_form4_backfill import _process_cik


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


class TestProcessCikSubmissionsError(unittest.TestCase):
    def test_submissions_fetch_failure_returns_zero_no_crash(self):
        # Network blip on submissions fetch must NOT abort the loop — the
        # outer driver continues to the next CIK.
        client = _FakeClient(submissions_errors={"0000000001"})
        with tempfile.TemporaryDirectory() as tmp:
            n = _process_cik(
                "0000000001",
                client=client,
                parquet_root=Path(tmp),
                start_year=2006,
                end_year=2026,
            )
            self.assertEqual(n, 0)
            # No parquet files written on submissions failure.
            self.assertFalse(any(Path(tmp).iterdir()))


class TestProcessCikYearFilter(unittest.TestCase):
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
        with tempfile.TemporaryDirectory() as tmp:
            n = _process_cik(
                cik,
                client=client,
                parquet_root=Path(tmp),
                start_year=2006,
                end_year=2026,
            )
            self.assertEqual(n, 1)
            # Only the in-window XML was fetched.
            self.assertEqual(client.xml_calls, [(cik, "RECENT-2010")])


class TestProcessCikContinuesPastFilingError(unittest.TestCase):
    def test_filing_xml_fetch_failure_does_not_abort_cik(self):
        # When ONE filing's XML 404s, the CIK loop continues to the next
        # filing — only that one record is dropped, not the whole CIK.
        cik = "0000000001"
        submissions = _submissions_with_form4([("BAD-1", "2020-06-01"), ("GOOD-1", "2020-07-01")])
        client = _FakeClient(
            submissions_payloads={cik: submissions},
            xml_payloads={(cik, "GOOD-1"): _xml_for(date(2020, 7, 1))},
        )
        with tempfile.TemporaryDirectory() as tmp:
            n = _process_cik(
                cik,
                client=client,
                parquet_root=Path(tmp),
                start_year=2006,
                end_year=2026,
            )
            self.assertEqual(n, 1)
            # Both XML fetches attempted; only GOOD-1 written.
            self.assertEqual(set(client.xml_calls), {(cik, "BAD-1"), (cik, "GOOD-1")})

            # Verify parquet contents.
            dataset = ds.dataset(
                str(Path(tmp) / "transaction_year=2020"),
                partitioning=None,
                format="parquet",
            )
            df = dataset.to_table().to_pandas()
            self.assertEqual(set(df["accession_number"]), {"GOOD-1"})


class TestProcessCikWalksOverflow(unittest.TestCase):
    def test_overflow_files_walked_when_present(self):
        # CRITICAL regression guard: overflow files must be fetched and their
        # Form-4 entries processed. Without this, filers with >1000 filings
        # silently lose historical data — the bug that wasted the first
        # 6-day backfill run.
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
        with tempfile.TemporaryDirectory() as tmp:
            n = _process_cik(
                cik,
                client=client,
                parquet_root=Path(tmp),
                start_year=2006,
                end_year=2026,
            )
            self.assertEqual(n, 2)
            # Both submissions and overflow fetched.
            self.assertEqual(client.submissions_calls, [cik])
            self.assertEqual(
                client.overflow_calls,
                ["CIK0000000001-submissions-001.json"],
            )
            self.assertEqual(
                set(client.xml_calls),
                {(cik, "RECENT-1"), (cik, "OVERFLOW-1")},
            )


class TestProcessCikEmptyResults(unittest.TestCase):
    def test_no_form4_filings_writes_no_parquet(self):
        # CIK with submissions but zero Form-4s should still return 0 cleanly
        # and NOT create a parquet partition.
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
        with tempfile.TemporaryDirectory() as tmp:
            n = _process_cik(
                cik,
                client=client,
                parquet_root=Path(tmp),
                start_year=2006,
                end_year=2026,
            )
            self.assertEqual(n, 0)
            self.assertFalse(any(Path(tmp).iterdir()))


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
