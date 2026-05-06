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

from alphalens.data.alt_data.form4_bulk_backfill import (  # noqa: E402
    BackfillManifest,
    fetch_all_form4_metadata,
    write_records_to_parquet,
)
from alphalens.data.alt_data.form4_records import (  # noqa: E402
    Form4ParseError,
    parse_form4_xml,
)
from alphalens.data.alt_data.sec_edgar_client import (  # noqa: E402
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


def _process_cik(
    cik: str,
    *,
    client: SecEdgarClient,
    parquet_root: Path,
    start_year: int,
    end_year: int,
) -> int:
    """Fetch submissions + Form-4 XMLs for one CIK; write parquet. Returns count.

    Walks both ``recent`` and ``files`` overflow blocks via
    :func:`fetch_all_form4_metadata` — required for prolific issuers whose
    historical Form-4s spill out of the most-recent-1000 ``recent`` window.
    """
    try:
        records_metadata = list(fetch_all_form4_metadata(client, cik=cik))
    except SecEdgarError as exc:
        logger.warning("submissions fetch failed for cik=%s: %s", cik, exc)
        return 0

    records_buffer = []
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

    if records_buffer:
        write_records_to_parquet(records_buffer, parquet_root=parquet_root)
    return len(records_buffer)


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
        "--checkpoint-every",
        type=int,
        default=20,
        help="Save manifest every N CIKs (default 20).",
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

    total_records = 0
    for i, cik in enumerate(ciks, start=1):
        if manifest.is_complete(cik):
            continue
        try:
            n = _process_cik(
                cik,
                client=client,
                parquet_root=args.parquet_root,
                start_year=args.start_year,
                end_year=args.end_year,
            )
            total_records += n
            manifest.mark_complete(cik)
            logger.info(
                "[%d/%d] cik=%s wrote %d records (running total %d)",
                i,
                len(ciks),
                cik,
                n,
                total_records,
            )
        except Exception:
            logger.exception("unhandled error on cik=%s; continuing", cik)

        if i % max(1, args.checkpoint_every) == 0:
            manifest.save()
            logger.info("checkpoint: manifest saved at i=%d", i)

    manifest.save()
    logger.info(
        "Backfill complete. Total records this run: %d. Completed CIKs: %d.",
        total_records,
        len(manifest.completed_ciks),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
