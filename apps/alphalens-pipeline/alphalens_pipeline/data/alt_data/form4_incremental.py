"""SEC EDGAR Form-4 daily-incremental ingest engine.

Keeps the hive-partitioned Form-4 parquet store at ``~/.alphalens/form4_parquet/``
COMPLETE and FRESH after the one-time historical bulk backfill (the seed) froze.
Strategy (locked memo ``docs/research/form4_daily_incremental_design_2026_06_07.md``):

1. Walk each UTC date in ``[start_date, end_date]`` via the SEC daily form index
   (``form.{YYYYMMDD}.idx``). One index lists every Form-4/4-A filed that day, so
   coverage is complete with no stale-roster risk, and a past-date ``.idx`` is
   immutable so re-runs are cheap.
2. For each Form-4/4-A row in that day's index, fetch the deterministic
   full-submission ``.txt`` (``{acc_no_dashes}/{acc_dashed}.txt``) ONCE and slice
   out the inline ``<ownershipDocument>`` block. One HTTP request per accession,
   no per-CIK ``submissions/CIK{cik}.json`` roundtrip: the daily ``.idx`` already
   lists EXACTLY the filings filed that day, so the submissions intersection
   ("don't re-write an old 4 from the same CIK") is redundant on this path, and
   the full-submission ``.txt`` path is derivable from CIK + accession alone (the
   primary-document filename, which submissions was the only way to learn, is NOT
   needed). Dropping submissions halves the request count and removes the entire
   per-CIK request class that made a 34-day catch-up exceed the 45-min timeout.
3. Parse the extracted XML via :func:`parse_form4_xml` with the PIT
   ``filing_date`` taken from the index row (not the XML).
4. Write via :func:`write_records_to_parquet`, then :func:`compact_root` so
   overlapping re-fetches collapse on the unique ``accession_number``.

State is STATELESS: each run re-reads a fixed lookback window, and dedup is by
``accession_number`` uniqueness collapsing under ``compact_partition``'s full-row
``drop_duplicates``. No last-success-date file to corrupt.

Degrades gracefully per-date: a :class:`SecForbiddenError` (shared-IP 403) or any
other fetch/parse miss on one date is counted and skipped, never raised — the next
run's overlapping window + the immutable ``.idx`` are the recovery mechanism.

All SEC HTTP goes through the canonical :class:`SecEdgarClient` (User-Agent +
10 req/s throttle + 429/5xx retry + cross-process rate coordinator). No raw HTTP.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from alphalens_pipeline.data.alt_data.form4_bulk_backfill import (
    write_records_to_parquet,
)
from alphalens_pipeline.data.alt_data.form4_compaction import compact_root
from alphalens_pipeline.data.alt_data.form4_records import (
    Form4ParseError,
    parse_form4_xml,
)
from alphalens_pipeline.data.alt_data.sec_edgar_client import (
    SecEdgarClient,
    SecEdgarError,
    SecForbiddenError,
    get_default_sec_client,
)

logger = logging.getLogger(__name__)

_ARCHIVES_BASE = "https://www.sec.gov"

# Form types kept from the daily index — Form-4 and its amendment 4/A. Mirror of
# the set the bulk-backfill walker (iter_form4_filings) keeps.
_KEPT_FORM_TYPES = frozenset({"4", "4/A"})

# Minimum tokens in a daily-index row: Form Type ... CIK Date-Filed File-Name.
_MIN_INDEX_ROW_TOKENS = 4

# The Form-4 ownership XML is embedded inline in the full-submission .txt inside
# the SGML <DOCUMENT> wrapper. The primary-document filename is filer-chosen and
# NOT derivable from CIK + accession (that is the only thing submissions JSON
# resolved), but the full-submission .txt path IS deterministic, and it carries
# the raw <ownershipDocument> element verbatim — so we slice it out instead of
# fetching submissions to learn the filename.
_OWNERSHIP_OPEN_TAG = "<ownershipDocument>"
_OWNERSHIP_CLOSE_TAG = "</ownershipDocument>"


@dataclass(frozen=True)
class Form4IndexRow:
    """One Form-4 / 4-A row parsed from a SEC daily form ``.idx``."""

    form: str  # "4" or "4/A"
    cik: str  # 10-digit zero-padded
    accession_number: str  # dashed form, e.g. 0000320193-26-000050
    filing_date: str  # YYYY-MM-DD (daily-index "Date Filed" column)


@dataclass(frozen=True)
class IncrementalResult:
    """Per-run counts the runner projects into Prometheus textfile gauges."""

    rows_written: int
    distinct_accessions: int
    latest_filing_date: date | None
    transient_errors: int
    other_errors: int


def _daily_index_url(*, date: date) -> str:
    quarter = (date.month - 1) // 3 + 1
    return (
        f"{_ARCHIVES_BASE}/Archives/edgar/daily-index/"
        f"{date.year}/QTR{quarter}/form.{date:%Y%m%d}.idx"
    )


def _full_submission_txt_url(*, cik: str, accession_number: str) -> str:
    """Deterministic full-submission ``.txt`` URL from CIK + accession.

    SEC archive layout uses the CIK without leading zeros and the accession both
    without dashes (directory) and with dashes (leaf): e.g.
    ``edgar/data/320193/000032019326000050/0000320193-26-000050.txt``.
    """
    cik_no_zeros = str(int(cik))
    acc_no_dashes = accession_number.replace("-", "")
    return (
        f"{_ARCHIVES_BASE}/Archives/edgar/data/{cik_no_zeros}/"
        f"{acc_no_dashes}/{accession_number}.txt"
    )


def _extract_ownership_document(submission_txt: str) -> bytes:
    """Slice the inline ``<ownershipDocument>...</ownershipDocument>`` XML block.

    The full-submission ``.txt`` wraps every document in SGML ``<DOCUMENT>`` tags
    plus SEC headers, so the whole body is not parseable XML; the Form-4 XML
    parser needs just the ownership element. We take the FIRST opening tag
    through the FIRST matching closing tag. Raises :class:`Form4ParseError` when
    the block is absent (older / malformed packaging) so the caller counts the
    miss and degrades per-filing instead of writing a junk row.
    """
    start = submission_txt.find(_OWNERSHIP_OPEN_TAG)
    if start == -1:
        raise Form4ParseError("no <ownershipDocument> block in full-submission .txt")
    end = submission_txt.find(_OWNERSHIP_CLOSE_TAG, start)
    if end == -1:
        raise Form4ParseError("unterminated <ownershipDocument> block in full-submission .txt")
    end += len(_OWNERSHIP_CLOSE_TAG)
    # A single Form-4 primary document carries exactly one <ownershipDocument>
    # (joint filers share it via multiple <reportingOwner> children, which
    # parse_form4_xml already handles). A SECOND block would mean only the first
    # is parsed — not expected for Form-4, so surface it rather than silently
    # under-count.
    if _OWNERSHIP_OPEN_TAG in submission_txt[end:]:
        logger.warning(
            "full-submission .txt has >1 <ownershipDocument> block; only the first is "
            "parsed (unexpected for a single Form-4 primary document)"
        )
    return submission_txt[start:end].encode()


def parse_form4_index_rows(idx_text: str) -> list[Form4IndexRow]:
    """Parse a SEC daily form ``.idx``, returning only Form-4 / 4-A rows.

    The ``.idx`` is fixed-width-ish text: header lines, then a dashed-separator
    line, then one row per filing. Parsing starts after the separator. Each kept
    row is anchored on the Form Type (column 0) and the last three tokens (CIK,
    Date Filed, File Name) — the Company Name column may contain spaces, so a
    naive positional split would mis-key it. Mirrors the proven shape of
    ``thematic.sources.edgar_press_release.parse_form_index_8k`` but keeps the
    Form-4 / 4-A set instead of the 8-K set.
    """
    rows: list[Form4IndexRow] = []
    started = False
    for line in idx_text.splitlines():
        if not started:
            stripped = line.strip()
            if stripped and set(stripped) == {"-"}:
                started = True
            continue
        if not line.strip():
            continue
        parsed = _parse_index_row(line)
        if parsed is not None:
            rows.append(parsed)
    return rows


def _parse_index_row(line: str) -> Form4IndexRow | None:
    parts = line.split()
    if len(parts) < _MIN_INDEX_ROW_TOKENS:
        return None
    form_type = parts[0]
    if form_type not in _KEPT_FORM_TYPES:
        return None
    file_name = parts[-1]
    date_filed = parts[-2]
    cik_raw = parts[-3]
    if not cik_raw.isdigit():
        return None
    accession = _accession_from_index_filename(file_name)
    if not accession:
        return None
    return Form4IndexRow(
        form=form_type,
        cik=cik_raw.zfill(10),
        accession_number=accession,
        filing_date=date_filed,
    )


def _accession_from_index_filename(file_name: str) -> str | None:
    """``edgar/data/320193/0000320193-26-000050-index.htm`` -> ``0000320193-26-000050``."""
    leaf = file_name.rsplit("/", 1)[-1]
    leaf = leaf.removesuffix("-index.htm").removesuffix(".txt")
    bits = leaf.split("-")
    if len(bits) < 3:
        return None
    return "-".join(bits[:3])


def _iter_window_dates(start_date: date, end_date: date) -> Iterator[date]:
    """Yield every UTC date in the inclusive ``[start_date, end_date]`` range."""
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _fetch_index_rows_for_date(client: SecEdgarClient, *, date: date) -> list[Form4IndexRow]:
    """Fetch + parse one date's daily index. Raises on fetch failure (caller counts)."""
    idx = client.get_text(_daily_index_url(date=date))
    return parse_form4_index_rows(idx)


def _records_for_date(
    client: SecEdgarClient,
    *,
    rows: list[Form4IndexRow],
) -> tuple[list, set[str], int]:
    """Resolve a day's Form-4 index rows to parsed Form4Records.

    For each Form-4/4-A row, fetch the deterministic full-submission ``.txt``
    ONCE, slice out the inline ``<ownershipDocument>`` XML, and parse it with the
    PIT ``filing_date`` from the index row. No per-CIK ``submissions`` roundtrip:
    the daily index already lists exactly the filings filed that day, so there is
    no old-filing-from-the-same-CIK to intersect away, and the ``.txt`` path is
    deterministic from CIK + accession (the only thing submissions resolved — the
    primary-document filename — is not needed). Returns
    ``(records, accessions_written, other_errors)``.
    """
    records: list = []
    accessions_written: set[str] = set()
    other_errors = 0

    for row in rows:
        try:
            submission_txt = client.get_text(
                _full_submission_txt_url(cik=row.cik, accession_number=row.accession_number)
            )
            xml = _extract_ownership_document(submission_txt)
            parsed = parse_form4_xml(
                xml,
                accession_number=row.accession_number,
                filing_date=date.fromisoformat(row.filing_date),
            )
        except SecForbiddenError:
            raise  # transient — propagate so the day is counted, not silently dropped
        except (SecEdgarError, Form4ParseError, ValueError) as exc:
            other_errors += 1
            logger.warning(
                "form4-incremental skip filing cik=%s acc=%s: %s",
                row.cik,
                row.accession_number,
                exc,
            )
            continue
        records.extend(parsed)
        accessions_written.add(row.accession_number)

    return records, accessions_written, other_errors


def fetch_form4_records_for_window(
    client: SecEdgarClient | None,
    *,
    start_date: date,
    end_date: date,
    parquet_root: Path,
) -> IncrementalResult:
    """Ingest every Form-4/4-A filed in ``[start_date, end_date]`` (inclusive UTC).

    Writes each date's parsed records to the hive-partitioned store as it goes,
    then compacts ONCE so overlapping re-fetches collapse on the unique
    ``accession_number``. Per-date flushing keeps memory bounded to a single
    day's filings: accumulating a whole self-sizing catch-up window in memory
    OOM-killed a 34-day first run at the unit's 1G cap. Degrades gracefully
    per-date: a 403 (or other index/submissions/XML failure) on one date is
    counted and skipped, never raised. Returns per-run counts for metrics.
    """
    sec = client or get_default_sec_client()

    rows_written = 0
    all_accessions: set[str] = set()
    latest_filing_date: date | None = None
    transient_errors = 0
    other_errors = 0
    wrote_any = False

    for d in _iter_window_dates(start_date, end_date):
        try:
            rows = _fetch_index_rows_for_date(sec, date=d)
        except SecForbiddenError as exc:
            transient_errors += 1
            logger.warning("form4-incremental daily-index 403 for %s: %s", d, exc)
            continue
        except (SecEdgarError, OSError) as exc:
            # Network / HTTP / index-read failure for this date — degrade and let
            # the overlapping next run re-pull. A bare ``except`` here would mask
            # programmer errors (AttributeError, KeyError, ...) as transient and
            # retry them forever, so let anything else propagate and fail loud.
            transient_errors += 1
            logger.warning("form4-incremental daily-index fetch failed for %s: %s", d, exc)
            continue

        try:
            records, accessions, day_other = _records_for_date(sec, rows=rows)
        except SecForbiddenError as exc:
            transient_errors += 1
            logger.warning("form4-incremental per-filing 403 for %s: %s", d, exc)
            continue

        other_errors += day_other
        all_accessions.update(accessions)
        if records:
            # Flush this date immediately rather than accumulating the whole
            # window — bounds memory for a long catch-up. The duplicate part
            # files an overlapping window produces collapse at compact_root.
            write_records_to_parquet(records, parquet_root=parquet_root)
            rows_written += len(records)
            wrote_any = True
        if accessions:
            # The date being processed IS d; the guard already proves success.
            latest_filing_date = d

    if wrote_any:
        compact_root(parquet_root)

    return IncrementalResult(
        rows_written=rows_written,
        distinct_accessions=len(all_accessions),
        latest_filing_date=latest_filing_date,
        transient_errors=transient_errors,
        other_errors=other_errors,
    )


def today_utc() -> date:
    """Current UTC date (engine-local helper so the runner default is testable)."""
    return dt.datetime.now(dt.UTC).date()


def latest_filed_date_in_store(parquet_root: Path) -> date | None:
    """Newest ``filed_date`` currently in the store, or ``None`` when empty.

    Scans the ``filed_date`` column of every ``transaction_year`` partition (a
    late 4/A can file a recent correction against an old transaction year, so
    the newest filing is not necessarily in the newest partition). Used by the
    runner to auto-size the lookback window: the window start always reaches
    back to where the data ends, so a late first deploy or a missed daily run
    self-heals on the next run instead of leaving a permanent gap.
    """
    if not parquet_root.is_dir():
        return None
    # A corrupted far-future filed_date must not drive window sizing (it would
    # shrink the window, not open a gap, but ignore it defensively anyway).
    today = today_utc()
    latest: date | None = None
    for fp in sorted(parquet_root.glob("transaction_year=*/compacted.parquet")):
        try:
            col = pd.read_parquet(fp, columns=["filed_date"])["filed_date"]
        except Exception:  # unreadable/partial partition must not crash sizing
            continue
        parsed = pd.to_datetime(col, errors="coerce")
        parsed = parsed[parsed <= pd.Timestamp(today)]
        if parsed.empty:
            continue
        d = parsed.max().date()
        latest = d if latest is None else max(latest, d)
    return latest
