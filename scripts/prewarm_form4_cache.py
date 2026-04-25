"""Prewarm the Layer 2d Form 4 scorer disk cache.

Iterates every (ticker, asof) pair in the requested date range and triggers
``InsiderScorer.features_as_of`` so its per-(ticker,asof) disk cache under
``~/.alphalens/insider_form4/`` is fully populated. Existing cache entries
are skipped by the scorer itself, so the script is resumable — if it dies
mid-run, restart and it continues where it left off.

Designed to run 24/7 on a VPS while the local laptop does other work. Once
the cache is warm, a daily-rebalance backtest runs in ~1-2h instead of the
30h+ projected for cold-cache daily execution.

Transfer cache back with:
    rsync -az --info=progress2 vps:.alphalens/insider_form4/ ~/.alphalens/insider_form4/

SEC EDGAR requirements:
- ``SEC_EDGAR_USER_AGENT`` env var MUST contain a real contact (email or URL).
- Use a unique UA on the VPS if the local laptop is simultaneously hitting
  EDGAR; SEC rate-limits at 10 req/s per UA and combined load over the
  threshold gets throttled for everyone.

Usage (on VPS):
    export SEC_EDGAR_USER_AGENT="AlphaLens-Prewarm-VPS pajakkamil@gmail.com"
    nohup .venv/bin/python scripts/prewarm_form4_cache.py \\
        --start 2011-01-01 --end 2026-04-22 --stride 1 \\
        > logs/prewarm.log 2>&1 &

Split across hosts (optional speed-up):
    # VPS
    --start 2011-01-01 --end 2020-12-31
    # Local
    --start 2021-01-01 --end 2026-04-22
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yaml

from alphalens.alt_data.sec_edgar_client import SecEdgarClient, SecEdgarError
from alphalens.alt_data.ticker_cik_map import TickerCikMap
from alphalens.screeners.insider.scorer import InsiderScorer

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_CIK_MAP_PATH = Path("alphalens/alt_data/data/ticker_cik_map.yaml")
_CACHE_DIR = Path.home() / ".alphalens" / "insider_form4"


def load_pit_union(start: date, end: date) -> list[str]:
    """Union of all PIT-snapshot tickers for month-ends in [start, end]."""
    union: set[str] = set()
    for path in sorted(_PIT_DIR.glob("*.yaml")):
        snap_date = date.fromisoformat(path.stem + "-01")
        if not (start.replace(day=1) <= snap_date <= end):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for ticker in data.get("tickers", []):
            union.add(ticker)
    return sorted(union)


def build_calendar(start: date, end: date, stride: int) -> list[date]:
    """Trading-day calendar (NYSE weekdays) sampled every ``stride`` days."""
    bdays = pd.bdate_range(start, end)
    return [d.date() for d in bdays[:: max(1, stride)]]


def count_existing(tickers: list[str], asof: date) -> int:
    return sum(1 for t in tickers if (_CACHE_DIR / f"{t.upper()}_{asof.isoformat()}.json").exists())


def prewarm(
    tickers: list[str],
    calendar: list[date],
    user_agent: str,
    checkpoint_every: int,
) -> None:
    edgar = SecEdgarClient(user_agent=user_agent)
    cik_map = TickerCikMap.load(_CIK_MAP_PATH)
    scorer = InsiderScorer(
        edgar_client=edgar,
        ticker_cik_map=cik_map,
        cache_dir=_CACHE_DIR,
    )

    total_pairs = len(tickers) * len(calendar)
    logger.info(
        "prewarm start: %d tickers × %d dates = %d (ticker,asof) pairs; cache=%s",
        len(tickers),
        len(calendar),
        total_pairs,
        _CACHE_DIR,
    )

    run_start = time.monotonic()
    pairs_done = 0
    edgar_errors = 0
    date_stride_log = max(1, len(calendar) // 20)  # ~20 progress lines per run

    for day_idx, asof in enumerate(calendar):
        pre_existing = count_existing(tickers, asof)
        fetched_this_day = 0

        for ticker in tickers:
            try:
                scorer.features_as_of(ticker, asof)
            except SecEdgarError as exc:
                edgar_errors += 1
                logger.warning("edgar error %s@%s: %s", ticker, asof, exc)
            except Exception as exc:
                edgar_errors += 1
                logger.exception("unexpected error %s@%s: %s", ticker, asof, exc)
            pairs_done += 1

            if pairs_done % checkpoint_every == 0:
                elapsed = time.monotonic() - run_start
                rate = pairs_done / elapsed if elapsed > 0 else 0
                eta_s = (total_pairs - pairs_done) / rate if rate > 0 else 0
                logger.info(
                    "checkpoint: pairs=%d/%d (%.1f%%) rate=%.1f/s eta=%.1fh errs=%d",
                    pairs_done,
                    total_pairs,
                    100 * pairs_done / total_pairs,
                    rate,
                    eta_s / 3600,
                    edgar_errors,
                )

        post_existing = count_existing(tickers, asof)
        fetched_this_day = post_existing - pre_existing

        if (day_idx + 1) % date_stride_log == 0 or day_idx == len(calendar) - 1:
            logger.info(
                "day %d/%d (%s): cached=%d/%d fetched_this_run=%d errs_total=%d",
                day_idx + 1,
                len(calendar),
                asof,
                post_existing,
                len(tickers),
                fetched_this_day,
                edgar_errors,
            )

    total_elapsed = time.monotonic() - run_start
    logger.info(
        "prewarm done: pairs=%d elapsed=%.1fh errs=%d",
        pairs_done,
        total_elapsed / 3600,
        edgar_errors,
    )


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, default=date(2011, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument(
        "--stride",
        type=int,
        default=1,
        help="sample every Nth trading day (1=daily, 5=weekly)",
    )
    ap.add_argument(
        "--checkpoint-every",
        type=int,
        default=5000,
        help="log a rate/ETA line every N (ticker,asof) pairs",
    )
    return ap.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()

    ua = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        print("ERROR: SEC_EDGAR_USER_AGENT env var required", file=sys.stderr)
        return 2

    tickers = load_pit_union(args.start, args.end)
    if not tickers:
        logger.error("empty PIT universe for [%s, %s]; build snapshots first", args.start, args.end)
        return 3
    calendar = build_calendar(args.start, args.end, args.stride)
    if not calendar:
        logger.error("empty calendar for [%s, %s]", args.start, args.end)
        return 3

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prewarm(
        tickers=tickers,
        calendar=calendar,
        user_agent=ua,
        checkpoint_every=args.checkpoint_every,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
