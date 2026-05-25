"""Resumable iVolatility smd cache pull for v7 universe.

Tier 1 (priority): PIT yaml union (~1627) + ETFs (~8) = ~1635 tickers.
Tier 2 (survivorship): optionable_delisted_2018_2024.parquet (~1482) — runs
when Tier 1 finishes if time/quota permits.

Range: 2018-04-30 → 2026-04-30 (full v7 train + holdout window).

Cache layout: ~/.alphalens/ivolatility_smd/{TICKER}.parquet (one parquet
per ticker, all 150 vendor columns verbatim). Resumable — existing
parquets are skipped on rerun.

Run (foreground):
    ALPHALENS_IVOL_API_KEY=... .venv/bin/python scripts/pull_v7_smd_universe.py

Run (background):
    nohup .venv/bin/python scripts/pull_v7_smd_universe.py > /tmp/v7_pull.log 2>&1 &
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

import ivolatility as ivol
import pandas as pd
import yaml
from alphalens_pipeline.data.alt_data.ivolatility_smd_cache import download_and_cache

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SMD_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
OPTIONABLE_PARQUET = (
    Path.home() / ".alphalens" / "survivorship" / "optionable_delisted_2018_2024.parquet"
)

ETFS = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD")


def _pit_union(start_year: int = 2018) -> set[str]:
    """Union of all PIT yaml ticker lists for snapshots ≥ start_year-01."""
    union: set[str] = set()
    for p in sorted(PIT_DIR.glob("*.yaml")):
        try:
            snap_year = int(p.stem.split("-")[0])
        except ValueError:
            continue
        if snap_year < start_year:
            continue
        data = yaml.safe_load(p.read_text()) or {}
        for t in data.get("tickers", []):
            union.add(str(t).upper())
    return union


def _optionable_delisted() -> set[str]:
    """Tickers from optionable_delisted_2018_2024.parquet with optionable=True."""
    if not OPTIONABLE_PARQUET.exists():
        logger.warning("Optionable parquet missing: %s", OPTIONABLE_PARQUET)
        return set()
    df = pd.read_parquet(OPTIONABLE_PARQUET)
    sub = df[df.get("optionable") == True]  # noqa: E712
    return {str(t).upper() for t in sub["ticker"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=["1", "2", "all"],
        default="1",
        help="Tier 1 = PIT-active + ETFs; Tier 2 = optionable delisted; all = both",
    )
    parser.add_argument("--smd-start", type=date.fromisoformat, default=date(2018, 4, 30))
    parser.add_argument("--smd-end", type=date.fromisoformat, default=date(2026, 4, 30))
    parser.add_argument("--sleep", type=float, default=0.3)
    parser.add_argument("--limit", type=int, default=None, help="Cap pull count for testing")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    api_key = os.environ.get("ALPHALENS_IVOL_API_KEY") or os.environ.get("IVOL_API_KEY")
    if not api_key:
        logger.error("ALPHALENS_IVOL_API_KEY env var not set")
        return 2

    ivol.setLoginParams(apiKey=api_key)
    ivol.setDelayBetweenRequests(args.sleep)

    SMD_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    tickers: list[str] = []
    if args.tier in ("1", "all"):
        pit = _pit_union()
        tier1 = sorted(pit | set(ETFS))
        logger.info("Tier 1: %d tickers (PIT %d + ETFs %d)", len(tier1), len(pit), len(ETFS))
        tickers.extend(tier1)
    if args.tier in ("2", "all"):
        opt = _optionable_delisted()
        tier2 = sorted(opt - set(tickers))
        logger.info("Tier 2: %d new tickers (optionable delisted)", len(tier2))
        tickers.extend(tier2)

    if args.limit:
        tickers = tickers[: args.limit]

    logger.info("Total to pull: %d tickers (cache: %s)", len(tickers), SMD_CACHE_DIR)
    logger.info("Already cached: %d parquets", len(list(SMD_CACHE_DIR.glob("*.parquet"))))

    t0 = time.time()
    n_new = download_and_cache(
        tickers=tickers,
        start=args.smd_start,
        end=args.smd_end,
        cache_dir=SMD_CACHE_DIR,
        sleep_between=args.sleep,
    )
    elapsed = time.time() - t0
    logger.info(
        "Pull complete: %d new parquets in %.0fs (%.1f min). Total cached: %d",
        n_new,
        elapsed,
        elapsed / 60,
        len(list(SMD_CACHE_DIR.glob("*.parquet"))),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
