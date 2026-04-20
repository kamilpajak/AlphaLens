"""Pull tick-level trades for the Layer 2b universe (issue #3 Phase 4a).

Populates `~/.alphalens/tick_samples/{TICKER}/{YYYY-MM-DD}.parquet` with 5
most recent trading days for every ticker in `universe.yaml`. Idempotent:
re-running skips days already on disk.

Usage:
    .venv/bin/python scripts/pull_tick_sample.py            # last 5 weekdays
    .venv/bin/python scripts/pull_tick_sample.py --days 10  # last 10 weekdays
    .venv/bin/python scripts/pull_tick_sample.py --force    # ignore cache
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.screeners.lean.polygon_client import PolygonClient  # noqa: E402
from alphalens.screeners.themed.config import UNIVERSE_PATH  # noqa: E402
from alphalens.screeners.themed.universe import flatten_universe  # noqa: E402
from alphalens.tick_data import TickLoader  # noqa: E402

import yaml  # noqa: E402


CACHE_DIR = Path.home() / ".alphalens" / "tick_samples"


def _last_n_weekdays(n: int, end: date | None = None) -> list[date]:
    if end is None:
        end = date.today() - timedelta(days=1)
    days: list[date] = []
    cursor = end
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=5, help="Number of recent weekdays to pull")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if parquet exists")
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=500,
        help="Requests per minute (Starter is unlimited; 500 rpm is conservative)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print("ERROR: POLYGON_API_KEY not set (check .env)", file=sys.stderr)
        return 2

    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    tickers = sorted(flatten_universe(universe).keys())
    days = _last_n_weekdays(args.days)

    print(f"Universe: {len(tickers)} tickers")
    print(f"Days: {days[0].isoformat()} .. {days[-1].isoformat()} ({len(days)} weekdays)")
    print(f"Cache: {CACHE_DIR}")
    print(f"Force refresh: {args.force}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = PolygonClient(api_key=api_key, rate_limit_per_min=args.rate_limit)
    loader = TickLoader(client=client, cache_dir=CACHE_DIR)

    start_t = time.monotonic()
    fetched = 0
    skipped = 0
    total = len(tickers) * len(days)
    for i, ticker in enumerate(tickers, start=1):
        print(f"[{i}/{len(tickers)}] {ticker}", flush=True)
        for d in days:
            path = loader.path_for(ticker, d)
            if path.exists() and not args.force:
                skipped += 1
                continue
            try:
                loader.fetch_day(ticker, d, force=args.force)
                fetched += 1
            except Exception as exc:
                print(f"  FAILED {ticker} {d}: {exc}")
        elapsed = time.monotonic() - start_t
        rate = (fetched + skipped) / max(elapsed, 1.0)
        remaining = (total - fetched - skipped) / max(rate, 0.01)
        print(
            f"  fetched={fetched} skipped={skipped} "
            f"elapsed={elapsed:.0f}s eta={remaining:.0f}s"
        )

    print(f"\nDone. Fetched {fetched}, skipped {skipped} (cached).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
