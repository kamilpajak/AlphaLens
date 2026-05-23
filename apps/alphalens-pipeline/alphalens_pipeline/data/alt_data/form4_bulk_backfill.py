"""SEC EDGAR Form-4 bulk backfill utilities.

Walks SEC's per-CIK ``submissions/CIK{CIK}.json`` payloads, yields Form-4/4-A
filing metadata, fetches per-filing XML via :class:`SecEdgarClient`, parses
via :func:`parse_form4_xml`, and writes to a hive-partitioned parquet store
(partitioned by ``transaction_year``) matching the locked
:data:`alphalens_pipeline.data.store.form4_pit.FORM4_SCHEMA_COLUMNS`.

Resume safety is provided by :class:`BackfillManifest`, a JSON-backed
per-CIK completion ledger that allows the multi-day SEC walk to recover
from interruptions without refetching.

The full-universe backfill (~3-5 days wall on runpod for 2006-2026) is an
orchestration concern; this module exposes the building blocks and the
walk-one-CIK driver, leaving universe iteration to the experiment driver.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from alphalens_pipeline.data.alt_data.form4_records import Form4Record
from alphalens_pipeline.data.store.form4_pit import FORM4_SCHEMA_COLUMNS, PARTITION_KEY

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilingMetadata:
    """Lightweight metadata yielded by submissions-JSON walker."""

    cik: str  # 10-digit zero-padded
    accession_number: str
    filing_date: date
    primary_document: str
    form: str  # "4" or "4/A"


def _strip_xsl_prefix(primary_document: str) -> str:
    """Normalize SEC submissions JSON ``primaryDocument`` to raw XML path.

    SEC's ``primaryDocument`` field for Form-4 typically points to the
    XSL-rendered HTML view (e.g. ``xslF345X06/form4.xml``,
    ``xslF345X05/wf-form4_doc.xml``). The raw XML — what our parser needs —
    lives in the PARENT directory. Fetching the XSL path serves HTML and
    every parse fails with "mismatched tag".

    This function strips any leading directory whose name starts with
    ``xsl`` (covers all observed SEC stylesheet folders), leaving the raw
    XML basename. Documents without an XSL prefix (older filings, Form-4/A,
    edge cases) are returned unchanged.
    """
    parts = primary_document.split("/")
    if len(parts) > 1 and parts[0].startswith("xsl"):
        return "/".join(parts[1:])
    return primary_document


def iter_form4_filings(submissions: dict, *, cik: str) -> Iterator[FilingMetadata]:
    """Yield :class:`FilingMetadata` for each Form-4/4-A in a submissions payload.

    SEC submissions JSON has the shape ``{"filings": {"recent": {"form":
    [...], "accessionNumber": [...], "filingDate": [...], "primaryDocument":
    [...]}, "files": [...]}}``. Only the ``recent`` block is walked here;
    the ``files`` block (for filers with >1000 historical filings) requires
    additional fetches and is handled by the orchestrator.

    ``primaryDocument`` is normalized via :func:`_strip_xsl_prefix` so the
    yielded path resolves to raw XML, not the XSL-rendered HTML view.
    """
    recent = submissions.get("filings", {}).get("recent")
    if not recent:
        return

    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    for form, acc, fdate_str, pdoc in zip(
        forms, accessions, filing_dates, primary_docs, strict=False
    ):
        if form not in {"4", "4/A"}:
            continue
        try:
            filing_date = date.fromisoformat(fdate_str)
        except (TypeError, ValueError):
            logger.warning("skipping filing with invalid date %r", fdate_str)
            continue
        yield FilingMetadata(
            cik=cik,
            accession_number=acc,
            filing_date=filing_date,
            primary_document=_strip_xsl_prefix(pdoc),
            form=form,
        )


def fetch_all_form4_metadata(client, cik: str) -> Iterator[FilingMetadata]:
    """Yield :class:`FilingMetadata` for every Form-4/4-A across all submissions blocks.

    SEC submissions JSON caps the ``recent`` block at 1000 entries across all
    form types. For prolific issuers (any large-cap with thousands of insider
    Form-4s over 22 years), older filings live in the ``files`` overflow
    pointers (e.g. ``CIK0000320193-submissions-001.json``). Walking only
    ``recent`` silently drops the bulk of historical Form-4 data — exactly
    the data Cohen-Malloy needs for 5-year insider history.

    Overflow JSONs share the same ``{filings: {recent: {...}}}`` shape as the
    main file (verified against
    https://data.sec.gov/submissions/CIK0000320193-submissions-001.json), so
    :func:`iter_form4_filings` can reused on each.

    The ``client`` parameter must implement ``fetch_submissions(cik)`` and
    ``fetch_submissions_overflow(name)`` (typed loosely to allow test doubles).
    """
    submissions = client.fetch_submissions(cik)
    seen_accessions: set[str] = set()

    for meta in iter_form4_filings(submissions, cik=cik):
        if meta.accession_number in seen_accessions:
            continue
        seen_accessions.add(meta.accession_number)
        yield meta

    files = submissions.get("filings", {}).get("files") or []
    n_overflow = sum(1 for e in files if isinstance(e, dict) and e.get("name"))
    overflow_idx = 0
    for entry in files:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            logger.warning("skipping malformed overflow entry for cik=%s: %r", cik, entry)
            continue
        overflow_idx += 1
        # Progress logging for big filers (5+ overflow JSONs cause 30s+ silent
        # fetch periods otherwise — Berkshire, JPM, MS).
        logger.info("fetching overflow %d/%d for cik=%s (%s)", overflow_idx, n_overflow, cik, name)
        overflow = client.fetch_submissions_overflow(name)
        for meta in iter_form4_filings(overflow, cik=cik):
            if meta.accession_number in seen_accessions:
                continue
            seen_accessions.add(meta.accession_number)
            yield meta


def write_records_to_parquet(records: Iterable[Form4Record], *, parquet_root: Path) -> None:
    """Write Form4Records to hive-partitioned parquet, partitioned by transaction_year.

    Appends to the partition with a unique filename so concurrent or repeated
    runs do not overwrite earlier files. ``Decimal`` fields (shares, price)
    are converted to ``float64`` for storage; downstream PIT reads accept the
    float representation.
    """
    rows: list[dict] = []
    dropped_future_dates = 0
    for r in records:
        # SEC Form-4 must be filed within 2 business days of the transaction.
        # transaction_date > filed_date is dirty data (typo or future vesting
        # date entered by mistake) and would create phantom partitions like
        # transaction_year=2031. Drop pre-write.
        if r.transaction_date > r.filing_date:
            dropped_future_dates += 1
            continue
        rows.append(
            {
                "issuer_cik": r.issuer_cik,
                "ticker": r.ticker or "",
                "accession_number": r.accession_number,
                "filed_date": r.filing_date,
                "reporting_owner_cik": r.reporting_owner_cik,
                "reporting_owner_name": r.reporting_owner_name,
                "transaction_date": r.transaction_date,
                "transaction_code": r.transaction_code,
                "transaction_shares": float(r.transaction_shares),
                "transaction_price_per_share": (
                    None
                    if r.transaction_price_per_share is None
                    else float(r.transaction_price_per_share)
                ),
                "is_director": r.is_director,
                "is_officer": r.is_officer,
                "is_ten_percent_owner": r.is_ten_percent_owner,
                "acquired_disposed": r.acquired_disposed,
                "is_amendment": r.is_amendment,
            }
        )
    if dropped_future_dates:
        logger.warning(
            "dropped %d record(s) with transaction_date > filed_date (dirty data)",
            dropped_future_dates,
        )
    if not rows:
        return

    df = pd.DataFrame.from_records(rows, columns=list(FORM4_SCHEMA_COLUMNS))
    df["transaction_year"] = df["transaction_date"].apply(lambda d: d.year)

    parquet_root.mkdir(parents=True, exist_ok=True)
    for year, group in df.groupby("transaction_year"):
        part_dir = parquet_root / f"{PARTITION_KEY}={year}"
        part_dir.mkdir(parents=True, exist_ok=True)
        # Unique filename: timestamp + random suffix prevents overwrites
        # without requiring a per-process counter (resumable + parallel-safe).
        suffix = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        out_path = part_dir / f"part-{suffix}.parquet"
        table = pa.Table.from_pandas(
            group.drop(columns=["transaction_year"]).reset_index(drop=True),
            preserve_index=False,
        )
        pq.write_table(table, out_path)


@dataclass
class BackfillManifest:
    """Per-CIK completion ledger persisted as JSON.

    Schema::

        {"version": 1, "completed_ciks": ["0000320193", "0000789019", ...]}

    Append-only: once a CIK is marked complete, it is skipped on future runs.
    """

    path: Path
    completed_ciks: set[str]

    @classmethod
    def load_or_create(cls, path: Path) -> BackfillManifest:
        try:
            data = json.loads(path.read_text())
            ciks = set(data.get("completed_ciks", []))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            if not isinstance(exc, FileNotFoundError):
                logger.warning("manifest at %s is corrupted (%s); starting fresh", path, exc)
            ciks = set()
        return cls(path=path, completed_ciks=ciks)

    def is_complete(self, cik: str) -> bool:
        return cik in self.completed_ciks

    def mark_complete(self, cik: str) -> None:
        self.completed_ciks.add(cik)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "completed_ciks": sorted(self.completed_ciks),
                },
                indent=2,
            )
        )
