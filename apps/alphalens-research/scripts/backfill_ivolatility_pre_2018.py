"""Defensive backfill of pre-2018 iVolatility SMD data into the local cache.

Existing cache covers 2018-04-30 → present. This script extends each
existing ticker parquet back to 2007-01-01 (earliest viable iVol coverage
per probe — ivp30 starts ~2007-01-02 for large/mid-cap, partial earlier).

Purpose: defensive — if the iVolatility subscription expires, we keep
whatever historical depth we paid for. Also enables future retrospective
replication tests on a fresh pre-2018 OOS window without burning the
2024-04-30 → 2026-04-30 holdout further.

Operational notes:
- Wall time estimate: ~7s per ticker × 3099 tickers ≈ 6h.
- API uses async file-mode (request queued → polled → file download).
- No bulk discount; each call returns up to ~11 years of data per ticker.
- Idempotent: tickers with min_date <= 2007-01-01 are skipped
  ("already_covered"). Re-running is safe.
- Rate limit: 0.2s between calls (matches v9D scoring convention).
- Output: incremental writes to ~/.alphalens/ivolatility_smd/{TICKER}.parquet

Logs progress to stdout every 50 tickers. Reads IVOLATILITY_API_KEY from
environment (call from a wrapper that has set it).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ivolatility as ivol
from alphalens_research.paper_trade.scorer_v9d import (
    DEFAULT_SMD_CACHE_DIR,
    backfill_smd_history,
    pit_union,
)

TARGET_START = date(2007, 1, 1)
PROGRESS_INTERVAL = 50


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    api_key = os.environ.get("IVOLATILITY_API_KEY", "")
    if not api_key:
        logger.error("IVOLATILITY_API_KEY not set in environment")
        return 1

    ivol.setLoginParams(apiKey=api_key)
    ivol.setDelayBetweenRequests(0.2)

    universe = pit_union()
    logger.info("Universe: %d tickers; target_start=%s", len(universe), TARGET_START)
    logger.info("Cache dir: %s", DEFAULT_SMD_CACHE_DIR)

    t_start = time.time()
    aggregate = {
        "backfilled": 0,
        "skipped_already_covered": 0,
        "skipped_no_coverage": 0,
        "skipped_missing": 0,
        "errors": 0,
    }

    # Process in batches of PROGRESS_INTERVAL to log progress.
    for i in range(0, len(universe), PROGRESS_INTERVAL):
        batch = universe[i : i + PROGRESS_INTERVAL]
        batch_counts = backfill_smd_history(
            batch,
            target_start=TARGET_START,
            cache_dir=DEFAULT_SMD_CACHE_DIR,
            sleep_between=0.2,
        )
        for k, v in batch_counts.items():
            aggregate[k] += v
        elapsed = time.time() - t_start
        n_done = i + len(batch)
        eta_total = elapsed * len(universe) / max(n_done, 1)
        eta_remaining = eta_total - elapsed
        logger.info(
            "Batch %d/%d done. Aggregate: %s. Elapsed %.1fmin, ETA remaining %.1fmin.",
            n_done,
            len(universe),
            aggregate,
            elapsed / 60,
            eta_remaining / 60,
        )

    logger.info(
        "DONE. Final aggregate: %s. Total wall: %.1fmin", aggregate, (time.time() - t_start) / 60
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
