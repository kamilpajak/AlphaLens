"""Rebuild the SIC → Fama-French 48-industry crosswalk parquet consumed by
:mod:`alphalens_pipeline.data.fundamentals.ff_industries`.

Downloads ``Siccodes48.zip`` from Ken French's data library, parses the
plain-text definition file (598 SIC ranges over 48 industries), and
writes ``alphalens_pipeline/data/fundamentals/ff48_crosswalk.parquet``.

Source: Fama & French 1997 "Industry Costs of Equity"; the text file
itself is hand-curated by Ken French and is shipped via
https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Siccodes48.zip

Cadence: rerun manually whenever ``build_sic_index.py`` runs (FF-48
definitions are stable; the zip's modification time has not changed
since 2020). Pure offline build — no SEC quota burn.

Run:
    .venv/bin/python apps/alphalens-research/scripts/build_ff48_index.py
"""

from __future__ import annotations

import io
import logging
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("build_ff48_index")

KEN_FRENCH_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Siccodes48.zip"

# Output: package-internal so it ships in the Docker pipeline image.
OUTPUT_PATH = (
    Path(__file__).resolve().parents[2]
    / "alphalens-pipeline"
    / "alphalens_pipeline"
    / "data"
    / "fundamentals"
    / "ff48_crosswalk.parquet"
)

_SCHEMA = pa.schema(
    [
        ("sic_low", pa.int32()),
        ("sic_high", pa.int32()),
        ("ff48_id", pa.int8()),
        ("ff48_short", pa.string()),
        ("ff48_name", pa.string()),
    ]
)

# Two mutually-exclusive line shapes by digit count + structure:
# - headers lead with a 1-2 digit FF-48 industry id (1..48), then short
#   label, then long name (3 tokens, last is multi-word);
# - range lines lead with ``DDDD-DDDD`` (the dash terminates the leading
#   token, so the header regex cannot collapse a range into a header).
# Both accept zero leading whitespace so the parser stays robust if Ken
# French ever ships the file with leading spaces stripped or tab-indented
# (Gemini 3 Pro review on PR #216).
_HEADER_RE = re.compile(r"^\s*(\d{1,2})\s+(\S+)\s+(.+?)\s*$")
_RANGE_RE = re.compile(r"^\s*(\d{4})-(\d{4})\b")


def _parse_siccodes48(text: str) -> tuple[list[dict], list[dict]]:
    """Parse the Ken French ``Siccodes48.txt`` plain-text file.

    Returns ``(industries, ranges)`` where ``industries`` is a list of
    ``{id, short, name}`` dicts in source order and ``ranges`` is a list
    of ``{sic_low, sic_high, ff48_id}`` dicts also in source order
    (range order matters: the first matching range wins at lookup time,
    matching academic convention).

    The text format groups SIC ranges under each industry header. A SIC
    range line is matched before an industry header line — the
    ``DDDD-DDDD`` shape of a range cannot collapse into a header (the
    dash terminates the leading token), so the per-line dispatch is:
    try ``_RANGE_RE`` first; on miss, try ``_HEADER_RE``.
    """
    industries: list[dict] = []
    ranges: list[dict] = []
    current_id: int | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        rmatch = _RANGE_RE.match(line)
        if rmatch and current_id is not None:
            ranges.append(
                {
                    "sic_low": int(rmatch.group(1)),
                    "sic_high": int(rmatch.group(2)),
                    "ff48_id": current_id,
                }
            )
            continue
        hmatch = _HEADER_RE.match(line)
        if hmatch:
            current_id = int(hmatch.group(1))
            industries.append(
                {
                    "id": current_id,
                    "short": hmatch.group(2),
                    "name": hmatch.group(3).strip(),
                }
            )
    return industries, ranges


def _join_industry_labels(industries: list[dict], ranges: list[dict]) -> list[dict]:
    """Decorate each range with its industry short / long name."""
    by_id = {ind["id"]: ind for ind in industries}
    return [
        {
            "sic_low": r["sic_low"],
            "sic_high": r["sic_high"],
            "ff48_id": r["ff48_id"],
            "ff48_short": by_id[r["ff48_id"]]["short"],
            "ff48_name": by_id[r["ff48_id"]]["name"],
        }
        for r in ranges
    ]


def _fetch_siccodes48() -> str:
    """Download Siccodes48.zip from Ken French's data library, return text body."""
    logger.info("fetching %s", KEN_FRENCH_URL)
    with urllib.request.urlopen(KEN_FRENCH_URL, timeout=30) as resp:
        payload = resp.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
        if "Siccodes48.txt" not in names:
            raise RuntimeError(f"Siccodes48.txt missing from zip; got {names}")
        with zf.open("Siccodes48.txt") as fh:
            # Ken French's file is mostly ASCII but contains a few
            # Latin-1 punctuation bytes (e.g. en-dash 0xe2 inside the
            # "Investors, NEC" / amp-style descriptions). UTF-8 with
            # replacement handles those safely; the SIC ranges and
            # industry codes themselves are pure ASCII so the parse is
            # unaffected.
            return fh.read().decode("utf-8", errors="replace")


def main() -> int:
    text = _fetch_siccodes48()
    industries, raw_ranges = _parse_siccodes48(text)
    logger.info("parsed %d industries / %d SIC ranges", len(industries), len(raw_ranges))
    if len(industries) != 48:
        logger.error("expected 48 industries, got %d — refusing to write", len(industries))
        return 1
    if not raw_ranges:
        logger.error("no SIC ranges parsed — refusing to write")
        return 1
    rows = _join_industry_labels(industries, raw_ranges)
    table = pa.Table.from_pylist(rows, schema=_SCHEMA)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, OUTPUT_PATH)
    logger.info(
        "wrote %s (%d rows, %d bytes)",
        OUTPUT_PATH,
        len(rows),
        OUTPUT_PATH.stat().st_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
