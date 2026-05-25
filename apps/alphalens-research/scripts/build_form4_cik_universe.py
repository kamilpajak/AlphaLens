"""Emit a CIK list file for the Form-4 backfill orchestrator.

Reads SEC's ``company_tickers.json`` (cached at
``~/.alphalens/edgar-detect/company_tickers.json`` by the Layer 1 EDGAR detector) and
emits one 10-digit zero-padded CIK per line.

Scope rationale: ``company_tickers.json`` lists ~10k US public issuers with
tickers — superset of R3000 across 2006-2026. Using the full list:
  * Captures historically-delisted CIKs that yfinance/IWM cache misses.
  * Empty Form-4 result for non-R3000 CIKs is cheap (~1 submissions fetch each).
  * Avoids R3000 reconstruction error at backfill time (~5000 → ~10000 CIK
    cost is ~17min extra at SEC 10 req/s).

PIT discipline applies at scorer time (R2000 PIT loader filters on mcap),
not at data-collection time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--source",
        type=Path,
        default=Path.home() / ".alphalens" / "edgar-detect" / "company_tickers.json",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path.home() / ".alphalens" / "form4_cik_universe.txt",
    )
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    if not args.source.is_file():
        sys.stderr.write(
            f"ERROR: SEC company_tickers cache missing at {args.source}.\n"
            "Either run the Layer 1 EDGAR detector once, or fetch manually:\n"
            "  curl -A 'YourName email@example.com' "
            "https://www.sec.gov/files/company_tickers.json > "
            f"{args.source}\n"
        )
        return 2

    payload = json.loads(args.source.read_text())
    ciks: set[str] = set()
    for _, row in payload.items():
        try:
            cik_int = int(row["cik_str"])
        except (KeyError, ValueError, TypeError):
            continue
        ciks.add(f"{cik_int:010d}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sorted(ciks)) + "\n")
    sys.stderr.write(f"Wrote {len(ciks)} CIKs to {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
