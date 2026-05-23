"""Fetch missing SEC XBRL companyfacts and persist to ~/.alphalens/companyfacts_parquet/.

Operator helper for warming the cache before a thematic pipeline run on
a fresh VPS, or for catching CIKs that surfaced in new candidates.
Idempotent — skips CIKs whose parquet already exists.

Reuses :class:`alphalens_pipeline.data.alt_data.sec_edgar_client.SecEdgarClient`
for throttling (10 req/s polite), retry/backoff on 429/5xx, FIFO cache,
and the SEC-mandated descriptive ``User-Agent``.

Usage:

    # By ticker list
    python scripts/edgar_companyfacts_backfill.py --tickers MANH,SYM,JPM,CAT,UNH

    # By scanning the current thematic candidates universe
    python scripts/edgar_companyfacts_backfill.py --from-candidates

    # Dry-run (no SEC calls; just report what would be fetched)
    python scripts/edgar_companyfacts_backfill.py --tickers MANH,SYM --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import pyarrow.parquet as pq
from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
    companyfacts_json_to_parquet_table,
)
from alphalens_pipeline.data.store.edgar_fundamentals import (
    DEFAULT_PARQUET_DIR,
    DEFAULT_USER_AGENT,
    USER_AGENT_ENV,
)

logger = logging.getLogger("edgar_companyfacts_backfill")


def _resolve_user_agent() -> str:
    return os.environ.get(USER_AGENT_ENV) or DEFAULT_USER_AGENT


def _load_candidate_tickers() -> list[str]:
    """Distinct tickers across ``~/.alphalens/thematic_candidates/*.parquet``."""
    import pandas as pd

    candidates_dir = Path.home() / ".alphalens" / "thematic_candidates"
    if not candidates_dir.exists():
        return []
    tickers: set[str] = set()
    for path in candidates_dir.glob("*.parquet"):
        try:
            df = pd.read_parquet(path, columns=["ticker"])
        except Exception as exc:
            logger.warning("could not read %s: %s", path, exc)
            continue
        tickers.update(str(t).upper() for t in df["ticker"].dropna().unique())
    return sorted(tickers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--tickers", help="Comma-separated list of tickers (e.g. MANH,SYM,JPM)")
    src.add_argument(
        "--from-candidates",
        action="store_true",
        help="Scan ~/.alphalens/thematic_candidates/ and backfill every distinct ticker",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_PARQUET_DIR,
        help=f"Parquet cache root (default: {DEFAULT_PARQUET_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fetched; don't call SEC",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    args.cache_dir.mkdir(parents=True, exist_ok=True)

    if args.from_candidates:
        tickers = _load_candidate_tickers()
        if not tickers:
            logger.error("no candidate parquets found under ~/.alphalens/thematic_candidates/")
            return 1
    else:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    client = SecEdgarClient(user_agent=_resolve_user_agent())

    # Resolve tickers to CIKs once.
    try:
        payload = client.fetch_company_tickers()
    except Exception as exc:
        logger.error("SEC company_tickers.json fetch failed: %s", exc)
        return 1
    ticker_to_cik: dict[str, str] = {}
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        t = entry.get("ticker")
        cik = entry.get("cik_str")
        if t and cik is not None:
            ticker_to_cik[str(t).upper()] = str(cik).zfill(10)

    missing: list[tuple[str, str]] = []
    skipped_unresolved: list[str] = []
    skipped_present: list[str] = []
    for ticker in tickers:
        cik = ticker_to_cik.get(ticker)
        if cik is None:
            skipped_unresolved.append(ticker)
            continue
        if (args.cache_dir / f"{cik}.parquet").exists():
            skipped_present.append(ticker)
            continue
        missing.append((ticker, cik))

    logger.info(
        "input=%d  unresolved=%d  already-cached=%d  to-fetch=%d",
        len(tickers),
        len(skipped_unresolved),
        len(skipped_present),
        len(missing),
    )
    if skipped_unresolved:
        logger.info("unresolved tickers: %s", skipped_unresolved)

    if args.dry_run:
        for ticker, cik in missing:
            logger.info("[dry-run] would fetch %s (CIK %s)", ticker, cik)
        return 0

    fetched = 0
    failed: list[tuple[str, str, str]] = []
    for ticker, cik in missing:
        try:
            facts = client.fetch_company_facts(cik)
        except Exception as exc:
            logger.warning("fetch failed for %s/%s: %s", ticker, cik, exc)
            failed.append((ticker, cik, str(exc)))
            continue
        table = companyfacts_json_to_parquet_table(facts)
        pq.write_table(table, args.cache_dir / f"{cik}.parquet")
        fetched += 1
        logger.info("wrote %s.parquet (%s, %d rows)", cik, ticker, table.num_rows)

    logger.info("done: fetched=%d failed=%d", fetched, len(failed))
    if failed:
        for ticker, cik, exc in failed:
            logger.error("FAIL %s (CIK %s): %s", ticker, cik, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
