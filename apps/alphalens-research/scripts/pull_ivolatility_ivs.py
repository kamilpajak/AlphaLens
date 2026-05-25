"""Per-ticker iVolatility IV Surface puller — pre-built scaffold.

Triggers a real run only when an iVolatility trial / Lab subscription is
active. Skipped during the 2026-05-07 cache snapshot session because the
trial expiring 2026-05-08 left only ~24h, insufficient for a 1626-ticker
async-polling pull. The scaffold is committed in advance so the next
trial window can be used immediately without re-derivation.

The IVS endpoint (`/equities/eod/ivs`) returns the full implied-volatility
surface — moneyness × maturity grid — that SMD's IVX columns aggregate
away. Each (date, period, strike, Call/Put) row carries IV + delta.
Tenors: 7, 14, 21, 30, 60, 90, 120, 150, 180, 270, 360, 720, 1080 days
(7/14/21/270/360/720/1080-day tenors only start 2019-10-10 per docs).

Reuses async file-mode machinery from
:mod:`scripts.pull_ivolatility_calendars` (``fetch_calendar_endpoint``)
because the protocol is identical: query → urlForDetails poll →
gzipped CSV download.

Outputs to ``~/.alphalens/ivolatility_ivs/{TICKER}.parquet``, one
parquet per ticker (mirrors ``ivolatility_smd`` cache layout).

Usage::

    .venv/bin/python scripts/pull_ivolatility_ivs.py \\
        --tickers-file ~/.alphalens/v7_universe.txt \\
        --from 2018-01-01 --to 2026-05-07

Idempotent: tickers whose existing parquet already covers the requested
range are skipped without an API call (mirrors
``backfill_ivolatility_pre_2018.py`` pattern).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd
from scripts.pull_ivolatility_calendars import (
    CalendarFetchError,
    fetch_calendar_endpoint,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_ivs"
IVS_ENDPOINT = "/equities/eod/ivs"

# A real IVS parquet has at minimum: parquet magic (8B) + footer
# (~200B) + a row group with column chunks for the 10-col schema. The
# smallest viable file is well above 1KB; anything below is either a
# zero-byte truncate, a partial flush, or a non-parquet artefact.
DEFAULT_MIN_PARQUET_BYTES = 1024

# Schema produced by /equities/eod/ivs (verified empirically 2026-05-07).
EXPECTED_COLUMNS = (
    "record_no",
    "symbol",
    "exchange",
    "date",
    "period",
    "strike",
    "out-of-the-money %",
    "Call/Put",
    "IV",
    "delta",
)
DEDUP_KEYS = ("date", "period", "strike", "Call/Put")


def _existing_covers(path: Path, from_date: str, to_date: str) -> bool:
    """True iff the existing parquet's date span includes [from_date, to_date]."""
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path, columns=["date"])
    except Exception:
        return False
    if df.empty:
        return False
    dmin = pd.to_datetime(df["date"], errors="coerce").min()
    dmax = pd.to_datetime(df["date"], errors="coerce").max()
    if pd.isna(dmin) or pd.isna(dmax):
        return False
    return (
        dmin.date() <= pd.Timestamp(from_date).date()
        and dmax.date() >= pd.Timestamp(to_date).date()
    )


def pull_ivs_ticker(
    *,
    ticker: str,
    from_date: str,
    to_date: str,
    cache_dir: Path,
    api_key: str,
) -> str:
    """Pull IVS for one ticker; return outcome key.

    Outcomes (mirror ``backfill_ivolatility_pre_2018.py``):
    - ``fetched`` — new rows written
    - ``skipped_already_covered`` — existing parquet spans requested range
    - ``skipped_no_coverage`` — vendor returned empty (no data in window)
    - ``errors`` — fetch or write failed
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{ticker.upper()}.parquet"

    if _existing_covers(path, from_date, to_date):
        return "skipped_already_covered"

    try:
        new_df = fetch_calendar_endpoint(
            endpoint=IVS_ENDPOINT,
            params={"symbol": ticker, "from": from_date, "to": to_date},
            api_key=api_key,
        )
    except (CalendarFetchError, Exception) as exc:
        logger.warning("[%s] fetch failed: %s", ticker, exc)
        return "errors"

    if new_df is None or new_df.empty:
        return "skipped_no_coverage"

    try:
        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=list(DEDUP_KEYS), keep="last")
        else:
            combined = new_df
        combined = combined.sort_values(["date", "period", "strike", "Call/Put"]).reset_index(
            drop=True
        )
        combined.to_parquet(path, index=False)
        return "fetched"
    except Exception as exc:
        logger.warning("[%s] write failed: %s", ticker, exc)
        return "errors"


def pull_ivs_universe(
    *,
    tickers: list[str],
    from_date: str,
    to_date: str,
    cache_dir: Path,
    api_key: str,
    sleep_between: float = 0.2,
) -> dict[str, int]:
    """Drive ``pull_ivs_ticker`` over a list of tickers, aggregate counts."""
    counts = {
        "fetched": 0,
        "skipped_already_covered": 0,
        "skipped_no_coverage": 0,
        "errors": 0,
    }
    for ticker in tickers:
        outcome = pull_ivs_ticker(
            ticker=ticker,
            from_date=from_date,
            to_date=to_date,
            cache_dir=cache_dir,
            api_key=api_key,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
        if sleep_between > 0:
            time.sleep(sleep_between)
    return counts


def validate_cache(
    cache_dir: Path,
    *,
    min_size_bytes: int = DEFAULT_MIN_PARQUET_BYTES,
) -> dict[str, int]:
    """Sweep ``cache_dir`` for *.parquet files; delete any that are
    zero-byte, sub-threshold, or unreadable by pyarrow.

    Resume safety: ``_existing_covers`` checks ``dmin/dmax`` of the
    parquet's ``date`` column, which can be tricked by a SIGKILL or
    disk-full crash that leaves a half-written but pyarrow-readable
    file. Running this before a resume guarantees the next pull
    re-fetches anything that is structurally broken.

    Returns count dict with keys ``ok``, ``deleted_zero``,
    ``deleted_too_small``, ``deleted_corrupt``.
    """
    counts = {"ok": 0, "deleted_zero": 0, "deleted_too_small": 0, "deleted_corrupt": 0}
    if not cache_dir.is_dir():
        return counts

    for path in sorted(cache_dir.glob("*.parquet")):
        size = path.stat().st_size
        if size == 0:
            logger.info("validate_cache: deleting zero-byte file %s", path.name)
            path.unlink()
            counts["deleted_zero"] += 1
            continue
        if size < min_size_bytes:
            logger.info(
                "validate_cache: deleting %s (%d bytes < %d threshold)",
                path.name,
                size,
                min_size_bytes,
            )
            path.unlink()
            counts["deleted_too_small"] += 1
            continue
        try:
            pd.read_parquet(path, columns=["date"])
        except Exception as exc:
            logger.info("validate_cache: deleting unreadable parquet %s: %s", path.name, exc)
            path.unlink()
            counts["deleted_corrupt"] += 1
            continue
        counts["ok"] += 1
    return counts


def _load_tickers(path: Path) -> list[str]:
    """Read one ticker per line, skip blanks/comments."""
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    ap.add_argument("--tickers-file", type=Path, required=True)
    ap.add_argument("--from", dest="from_date", required=True)
    ap.add_argument("--to", dest="to_date", required=True)
    ap.add_argument(
        "--sleep-between",
        type=float,
        default=0.2,
        help="Seconds between ticker calls (matches retail rate limit).",
    )
    ap.add_argument(
        "--validate-cache",
        action="store_true",
        help="Before pulling, sweep cache_dir for zero-byte / sub-1KB / "
        "unreadable parquet files and delete them. Use on resume after "
        "a hard crash so idempotent skip cannot be tricked by a "
        "half-written file.",
    )
    args = ap.parse_args(argv)

    api_key = os.environ.get("IVOLATILITY_API_KEY", "")
    if not api_key:
        logger.error("IVOLATILITY_API_KEY not set in environment")
        return 1

    if args.validate_cache:
        validate_counts = validate_cache(args.cache_dir)
        logger.info("validate_cache pre-pull sweep: %s", validate_counts)

    tickers = _load_tickers(args.tickers_file)
    logger.info(
        "Universe: %d tickers; window=%s..%s; cache=%s",
        len(tickers),
        args.from_date,
        args.to_date,
        args.cache_dir,
    )

    counts = pull_ivs_universe(
        tickers=tickers,
        from_date=args.from_date,
        to_date=args.to_date,
        cache_dir=args.cache_dir,
        api_key=api_key,
        sleep_between=args.sleep_between,
    )
    logger.info("DONE. counts=%s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
