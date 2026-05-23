"""SEC EDGAR Form-4 bulk backfill runner — long-running orchestrator.

Walks a CIK universe (typically union of R3000 PIT membership across the
study window 2006-2026) and for each CIK:

  1. Skips if ``BackfillManifest.is_complete(cik)`` is set.
  2. Fetches ``/submissions/CIK{cik}.json`` via ``SecEdgarClient``.
  3. Iterates Form-4/4-A entries via ``iter_form4_filings``.
  4. For each filing: fetch XML, parse via ``parse_form4_xml``, accumulate
     records in a per-CIK buffer.
  5. After all filings for the CIK are processed, write the buffer to the
     hive-partitioned parquet store via ``write_records_to_parquet`` and mark
     CIK complete in the manifest.

Resumable: rerun the script and previously-completed CIKs are skipped.

Wall-time on runpod CPU pod: ~3-5 days for full 2006-2026 backfill on the
~5000 unique CIKs that have ever been in R3000. SEC rate limit (10 req/s)
is the bottleneck.

Usage::

  .venv/bin/python scripts/run_form4_backfill.py \\
      --user-agent "Kamil Pajak research kamil.pajak@example.com" \\
      --cik-list path/to/ciks.txt \\
      --parquet-root ~/.alphalens/form4_parquet \\
      --manifest ~/.alphalens/form4_backfill_manifest.json \\
      --start-year 2006 --end-year 2026
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from alphalens_pipeline.data.alt_data.form4_bulk_backfill import (  # noqa: E402
    BackfillManifest,
    fetch_all_form4_metadata,
    write_records_to_parquet,
)
from alphalens_pipeline.data.alt_data.form4_records import (  # noqa: E402
    Form4ParseError,
    parse_form4_xml,
)
from alphalens_pipeline.data.alt_data.sec_edgar_client import (  # noqa: E402
    SecEdgarClient,
    SecEdgarError,
)

logger = logging.getLogger(__name__)


def _load_cik_list(path: Path) -> list[str]:
    """One CIK per line, 10-digit zero-padded; lines starting with '#' are comments."""
    out: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Pad to 10 digits.
        try:
            cik_int = int(line)
        except ValueError:
            logger.warning("skipping non-numeric CIK line: %r", line)
            continue
        out.append(f"{cik_int:010d}")
    return out


def _fetch_records_for_cik(
    cik: str,
    *,
    client: SecEdgarClient,
    start_year: int,
    end_year: int,
) -> list:
    """Fetch + parse all Form-4 records for one CIK. Returns list (no write).

    Walks both ``recent`` and ``files`` overflow blocks via
    :func:`fetch_all_form4_metadata` — required for prolific issuers whose
    historical Form-4s spill out of the most-recent-1000 ``recent`` window.

    Errors at any layer (submissions JSON, individual filing XML, parser)
    are logged and that filing/CIK is skipped — the caller continues.
    Per-CIK fault isolation matches the older ``_process_cik`` contract;
    only the write step has been hoisted to a separate batched flush
    (:func:`_flush_batch`) so multiple CIKs share a single parquet file.
    """
    try:
        records_metadata = list(fetch_all_form4_metadata(client, cik=cik))
    except SecEdgarError as exc:
        logger.warning("submissions fetch failed for cik=%s: %s", cik, exc)
        return []

    records_buffer: list = []
    for meta in records_metadata:
        if not (start_year <= meta.filing_date.year <= end_year):
            continue
        try:
            xml = client.fetch_form4_xml(
                cik=cik,
                accession_number=meta.accession_number,
                primary_doc=meta.primary_document,
            )
            records = parse_form4_xml(
                xml,
                accession_number=meta.accession_number,
                filing_date=meta.filing_date,
            )
        except (SecEdgarError, Form4ParseError) as exc:
            logger.warning("skip filing cik=%s acc=%s: %s", cik, meta.accession_number, exc)
            continue
        records_buffer.extend(records)
    return records_buffer


def _flush_batch(
    records: list,
    *,
    ciks: list[str],
    parquet_root: Path,
    manifest: BackfillManifest,
) -> None:
    """Write buffered records to parquet, mark all CIKs complete, save manifest.

    Atomic-ish: parquet write happens first; if it fails, the manifest is
    not updated and the next run will refetch the CIKs (idempotent because
    accession_number is unique). After a successful write, all CIKs in the
    batch are marked complete in one go and the manifest is saved.

    Empty ``records`` is allowed — CIKs with zero Form-4s in the requested
    window must still be marked complete so the next run skips them.
    """
    if records:
        write_records_to_parquet(records, parquet_root=parquet_root)
    for cik in ciks:
        manifest.mark_complete(cik)
    manifest.save()


def _run_backfill_loop(
    ciks: list[str],
    *,
    client: SecEdgarClient,
    parquet_root: Path,
    manifest: BackfillManifest,
    start_year: int,
    end_year: int,
    flush_threshold: int,
) -> None:
    """Iterate CIKs, accumulating records until the buffer hits ``flush_threshold``.

    Without batching, per-CIK flush creates one parquet file per CIK per
    transaction-year — projected ~50-100k tiny files for the 8000-CIK
    universe, which then dominates ``pyarrow.dataset`` open time during
    scorer reads. Batched flush keeps the file count proportional to
    ``total_records / flush_threshold``.

    Trade-off vs per-CIK flush: if the run dies between flushes, all CIKs
    in the pending buffer must be refetched on resume (manifest only
    advances per flush). At threshold=5000 records and ~50 records/CIK
    average, that's ~100 CIKs of refetch — minutes, not hours.
    """
    pending_records: list = []
    pending_ciks: list[str] = []
    total_records = 0
    total_processed = 0

    for i, cik in enumerate(ciks, start=1):
        if manifest.is_complete(cik):
            continue
        try:
            records = _fetch_records_for_cik(
                cik,
                client=client,
                start_year=start_year,
                end_year=end_year,
            )
        except Exception:
            logger.exception("unhandled error on cik=%s; continuing", cik)
            records = []

        pending_records.extend(records)
        pending_ciks.append(cik)
        total_records += len(records)
        total_processed += 1

        logger.info(
            "[%d/%d] cik=%s fetched %d records (buffer=%d, total=%d)",
            i,
            len(ciks),
            cik,
            len(records),
            len(pending_records),
            total_records,
        )

        if len(pending_records) >= flush_threshold:
            _flush_batch(
                pending_records,
                ciks=pending_ciks,
                parquet_root=parquet_root,
                manifest=manifest,
            )
            logger.info(
                "flushed %d records across %d CIKs (manifest now %d complete)",
                len(pending_records),
                len(pending_ciks),
                len(manifest.completed_ciks),
            )
            pending_records = []
            pending_ciks = []

    # End-of-run flush for residual buffer.
    if pending_ciks:
        _flush_batch(
            pending_records,
            ciks=pending_ciks,
            parquet_root=parquet_root,
            manifest=manifest,
        )
        logger.info(
            "final flush: %d records across %d CIKs",
            len(pending_records),
            len(pending_ciks),
        )

    logger.info(
        "Backfill loop complete. Processed %d CIKs, total records %d.",
        total_processed,
        total_records,
    )


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--user-agent",
        required=True,
        help="SEC requires a non-empty User-Agent including a contact (email or URL).",
    )
    ap.add_argument(
        "--cik-list",
        type=Path,
        required=True,
        help="Text file with one 10-digit CIK per line (or numeric, will be zero-padded).",
    )
    ap.add_argument(
        "--parquet-root",
        type=Path,
        default=Path.home() / ".alphalens" / "form4_parquet",
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        default=Path.home() / ".alphalens" / "form4_backfill_manifest.json",
    )
    ap.add_argument("--start-year", type=int, default=2006)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument(
        "--flush-threshold",
        type=int,
        default=5000,
        help=(
            "Flush buffered records to parquet when buffer exceeds N records "
            "(default 5000). Lower values = more files but smaller crash "
            "window; higher values = fewer files but more refetch on crash. "
            "5000 typical avg ~100 CIKs of refetch on resume."
        ),
    )
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ciks = _load_cik_list(args.cik_list)
    logger.info("Loaded %d CIKs from %s", len(ciks), args.cik_list)

    manifest = BackfillManifest.load_or_create(args.manifest)
    completed_at_start = len(manifest.completed_ciks)
    logger.info(
        "Manifest: %d CIKs already complete; %d remaining",
        completed_at_start,
        len(ciks) - completed_at_start,
    )

    client = SecEdgarClient(user_agent=args.user_agent)

    _run_backfill_loop(
        ciks,
        client=client,
        parquet_root=args.parquet_root,
        manifest=manifest,
        start_year=args.start_year,
        end_year=args.end_year,
        flush_threshold=args.flush_threshold,
    )
    logger.info(
        "Backfill complete. Manifest now %d CIKs complete (was %d at start).",
        len(manifest.completed_ciks),
        completed_at_start,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
