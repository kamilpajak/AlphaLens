"""SEC EDGAR Form-4 daily-incremental ingest engine.

Keeps the hive-partitioned Form-4 parquet store at ``~/.alphalens/form4_parquet/``
COMPLETE and FRESH after the one-time historical bulk backfill (the seed) froze.
Strategy (locked memo ``docs/research/form4_daily_incremental_design_2026_06_07.md``):

1. Walk each UTC date in ``[start_date, end_date]`` via the SEC daily form index
   (``form.{YYYYMMDD}.idx``). One index lists every Form-4/4-A filed that day, so
   coverage is complete with no stale-roster risk, and a past-date ``.idx`` is
   immutable so re-runs are cheap.
2. For each distinct filer CIK that filed a Form-4/4-A that day, fetch
   ``submissions/CIK{cik}.json`` ONCE (recent block only — a same-week Form-4 is
   always in ``recent``, so the overflow walk of the bulk backfill is not needed)
   and reuse :func:`iter_form4_filings` to resolve the raw-XML ``primaryDocument``
   path (``_strip_xsl_prefix`` applied).
3. Keep only the :class:`FilingMetadata` whose ``accession_number`` is in the
   day's daily-index accession set (so a CIK that filed an old 4 last year + a new
   4 today contributes only today's accession).
4. Fetch + parse the XML via :func:`parse_form4_xml`, write via
   :func:`write_records_to_parquet`, then :func:`compact_root` so overlapping
   re-fetches collapse on the unique ``accession_number``.

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
    iter_form4_filings,
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

    For each distinct filer CIK, fetch submissions once and intersect by the day's
    accession set (so an unrelated older 4 from the same CIK is not re-written).
    Returns ``(records, accessions_written, other_errors)``.
    """
    # accession -> filing_date string (authoritative PIT date from the index).
    acc_to_date: dict[str, str] = {r.accession_number: r.filing_date for r in rows}
    ciks = sorted({r.cik for r in rows})

    records: list = []
    accessions_written: set[str] = set()
    other_errors = 0

    for cik in ciks:
        try:
            submissions = client.fetch_submissions(cik)
        except SecForbiddenError:
            raise  # transient under shared-IP load — propagate so the day is counted
        except SecEdgarError as exc:
            other_errors += 1
            logger.warning("form4-incremental submissions fetch failed cik=%s: %s", cik, exc)
            continue

        for meta in iter_form4_filings(submissions, cik=cik):
            if meta.accession_number not in acc_to_date:
                continue  # not filed on a date in this day's index set
            try:
                xml = client.fetch_form4_xml(
                    cik=cik,
                    accession_number=meta.accession_number,
                    primary_doc=meta.primary_document,
                )
                parsed = parse_form4_xml(
                    xml,
                    accession_number=meta.accession_number,
                    filing_date=meta.filing_date,
                )
            except SecForbiddenError:
                raise  # transient — propagate so the day is counted, not silently dropped
            except (SecEdgarError, Form4ParseError) as exc:
                other_errors += 1
                logger.warning(
                    "form4-incremental skip filing cik=%s acc=%s: %s",
                    cik,
                    meta.accession_number,
                    exc,
                )
                continue
            records.extend(parsed)
            accessions_written.add(meta.accession_number)

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
        except Exception as exc:
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
            latest_filing_date = max(latest_filing_date or d, d)

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
