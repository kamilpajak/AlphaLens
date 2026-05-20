"""Rebuild the ticker→SIC index parquet consumed by
:mod:`alphalens.data.fundamentals.sic_index`.

Walks the canonical ``alphalens/data/alt_data/data/ticker_cik_map.yaml``
universe (~10k SEC-registered US issuers), pulls each CIK's
submissions JSON via :class:`SecEdgarClient` (the project's single
canonical SEC HTTP client, throttled to 10 req/s), extracts the
top-level ``sic`` and ``sicDescription`` fields, and writes
``alphalens/data/fundamentals/sic_index.parquet``.

Note: ``sic`` lives on the submissions endpoint, NOT on the
companyfacts endpoint (companyfacts only carries the XBRL facts
themselves). Verified empirically 2026-05-20.

Cadence: manual, monthly. SIC reassignments are rare; the daily
thematic pipeline reads the shipped parquet, not the live SEC API.

Run:
    .venv/bin/python scripts/build_sic_index.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from alphalens.data.alt_data.sec_edgar_client import (
    SecEdgarError,
    get_default_sec_client,
)
from alphalens.data.alt_data.ticker_cik_map import _normalize_cik

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("build_sic_index")

REPO_ROOT = Path(__file__).resolve().parents[1]
TICKER_CIK_MAP_PATH = REPO_ROOT / "alphalens" / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"
OUTPUT_PATH = REPO_ROOT / "alphalens" / "data" / "fundamentals" / "sic_index.parquet"

_SCHEMA = pa.schema(
    [
        ("ticker", pa.string()),
        ("cik", pa.string()),
        ("sic", pa.int32()),
        ("sic_description", pa.string()),
    ]
)


def _extract_sic(submissions: dict) -> tuple[int | None, str | None]:
    """Pull (sic, sic_description) from raw submissions JSON.

    EDGAR returns SIC as either an int or a numeric string depending on the
    filer; coerce to int. ``sicDescription`` may be empty for filers
    without an assigned SIC.
    """
    raw_sic = submissions.get("sic")
    if raw_sic is None or raw_sic == "":
        return None, None
    try:
        sic = int(raw_sic)
    except (TypeError, ValueError):
        return None, None
    description = submissions.get("sicDescription")
    if description is not None:
        description = str(description)
    return sic, description


def main() -> int:
    logger.info("loading ticker→CIK map from %s", TICKER_CIK_MAP_PATH)
    raw = yaml.safe_load(TICKER_CIK_MAP_PATH.read_text()) or {}
    pairs = sorted((str(ticker).upper(), _normalize_cik(cik)) for ticker, cik in raw.items())
    logger.info("walking %d (ticker, CIK) pairs", len(pairs))

    client = get_default_sec_client()
    rows: list[dict] = []
    skipped_404 = 0
    skipped_no_sic = 0

    for i, (ticker, cik) in enumerate(pairs, start=1):
        if i % 250 == 0:
            logger.info(
                "progress: %d/%d (rows=%d, 404=%d, no_sic=%d)",
                i,
                len(pairs),
                len(rows),
                skipped_404,
                skipped_no_sic,
            )
        try:
            submissions = client.fetch_submissions(cik)
        except SecEdgarError as exc:
            # 404 = filer has no submissions (no longer registered);
            # other SecEdgarErrors are transient and worth surfacing.
            # SecEdgarError stringifies with the status code at the front,
            # so startswith is unambiguous (vs a substring match that would
            # collide with bodies that happen to contain "404").
            if str(exc).startswith("404"):
                skipped_404 += 1
                continue
            logger.warning("fetch failed for %s (CIK %s): %s", ticker, cik, exc)
            continue
        sic, description = _extract_sic(submissions)
        if sic is None:
            skipped_no_sic += 1
            continue
        rows.append(
            {"ticker": ticker, "cik": cik, "sic": sic, "sic_description": description or ""}
        )

    logger.info(
        "done: rows=%d, 404=%d, no_sic=%d (total %d)",
        len(rows),
        skipped_404,
        skipped_no_sic,
        len(pairs),
    )
    if not rows:
        logger.error("no SIC rows resolved; refusing to overwrite parquet")
        return 1

    table = pa.Table.from_pylist(rows, schema=_SCHEMA)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, OUTPUT_PATH)
    logger.info("wrote %s (%d rows, %d bytes)", OUTPUT_PATH, len(rows), OUTPUT_PATH.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
