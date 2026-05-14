"""Daily AV EARNINGS backfill — VPS cron job.

Walks the S&P 500 PIT union (or sp1500 if requested) and prefetches one AV
EARNINGS payload per ticker into ``~/.alphalens/av_cache/earnings_<T>.json``.
Free-tier quota (25 calls/day) is the binding constraint: each daily run
consumes up to 25 fresh tickers before AV signals rate-limit, then exits
cleanly. ~21 days calendar for a 503-name S&P 500 union; ~80 days for the
~2000-name SP1500 union.

The cache is per-ticker JSON with atomic writes (see ``av_earnings_client``)
so this script is fully resumable across crashes / quota windows / VPS
reboots — re-running picks up only uncached tickers.

Designed to be general-purpose: any future paradigm that needs AV EARNINGS
data reads from the same cache. The cache lives under ``~/.alphalens/`` so
it survives ``git`` operations and can be Nextcloud-synced (optionally via
``--rclone-remote``) to a workstation for local development.

Usage (VPS cron, daily 00:05 UTC = ~5 min after AV quota reset)::

    5 0 * * * cd /home/op/AlphaLens && \
        .venv/bin/python -m scripts.av_earnings_daily_backfill \
            --universe sp500_union \
            --rclone-remote nextcloud:alphalens/av_cache \
            >> /var/log/av_backfill.log 2>&1

The ``--rclone-remote`` is optional; omit for VPS-only caching, add when
sync to a workstation is needed for downstream consumption.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from alphalens.data.alt_data.av_earnings_client import (  # noqa: E402
    AVRateLimitError,
    fetch_earnings_batch,
)
from alphalens.data.universes.sp1500_pit import (  # noqa: E402
    load_sp500_pit_union,
    load_sp1500_pit_union,
)

logger = logging.getLogger(__name__)

_UNIVERSES = {
    "sp500_union": load_sp500_pit_union,
    "sp1500_union": load_sp1500_pit_union,
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument(
        "--universe",
        choices=sorted(_UNIVERSES),
        default="sp500_union",
        help="Which ticker universe to backfill (default: sp500_union).",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".alphalens" / "av_cache",
        help="Per-ticker JSON cache directory.",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=REPO_ROOT / "data",
        help="Repo data root (contains sp500_pit/, sp400_pit/, sp600_pit/).",
    )
    p.add_argument(
        "--throttle-seconds",
        type=float,
        default=1.5,
        help="Sleep between successful AV calls (default 1.5s, empirically safe).",
    )
    p.add_argument(
        "--rclone-remote",
        default=None,
        help="Optional rclone destination, e.g. 'nextcloud:alphalens/av_cache'. "
        "When set, runs `rclone copy <cache-dir> <remote>` after the batch.",
    )
    p.add_argument(
        "--rclone-bin",
        default="rclone",
        help="Path to rclone binary (override if not on $PATH).",
    )
    return p.parse_args(argv)


def _select_universe(args: argparse.Namespace) -> list[str]:
    loader = _UNIVERSES[args.universe]
    if args.universe == "sp500_union":
        return loader(data_dir=args.data_root / "sp500_pit")
    return loader(data_root=args.data_root)


def _nextcloud_sync(cache_dir: Path, remote: str, rclone_bin: str) -> None:
    """Push cache directory to rclone remote (e.g. Nextcloud)."""
    cmd = [rclone_bin, "copy", str(cache_dir), remote]
    logger.info("rclone sync: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args(argv)

    tickers = _select_universe(args)
    logger.info(
        "starting AV EARNINGS backfill: universe=%s n_tickers=%d cache=%s",
        args.universe,
        len(tickers),
        args.cache_dir,
    )

    try:
        statuses = fetch_earnings_batch(
            tickers,
            args.cache_dir,
            throttle_seconds=args.throttle_seconds,
        )
    except AVRateLimitError as exc:
        # Persistent rate-limit (retry exhausted) — exit clean so the cron job
        # is not treated as a failure. Tomorrow's quota window will pick up
        # where this run left off.
        logger.warning("AV rate-limit persisted past retry, exiting clean: %s", exc)
        return 0

    counts = Counter(statuses.values())
    logger.info(
        "batch complete: cached=%d fetched=%d failed=%d (total=%d)",
        counts.get("cached", 0),
        counts.get("fetched", 0),
        counts.get("failed", 0),
        sum(counts.values()),
    )

    if args.rclone_remote:
        _nextcloud_sync(args.cache_dir, args.rclone_remote, args.rclone_bin)

    return 0


if __name__ == "__main__":
    sys.exit(main())
