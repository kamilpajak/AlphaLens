"""Quick status check for the v7 smd cache pull.

Reports cache count, ETA at current rate, vendor-CSV-bug recovery rate,
and which Tier 1+2 tickers are still missing.

Run:
    .venv/bin/python scripts/check_v7_pull_status.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yaml

CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
OPTIONABLE_PARQUET = (
    Path.home() / ".alphalens" / "survivorship" / "optionable_delisted_2018_2024.parquet"
)
PULL_LOG = Path("/tmp/v7_pull.log")

ETFS = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD")


def _pit_union() -> set[str]:
    union: set[str] = set()
    for p in sorted(PIT_DIR.glob("*.yaml")):
        try:
            year = int(p.stem.split("-")[0])
        except ValueError:
            continue
        if year < 2018:
            continue
        data = yaml.safe_load(p.read_text()) or {}
        for t in data.get("tickers", []):
            union.add(str(t).upper())
    return union | set(ETFS)


def _optionable_delisted() -> set[str]:
    if not OPTIONABLE_PARQUET.exists():
        return set()
    df = pd.read_parquet(OPTIONABLE_PARQUET)
    return {str(t).upper() for t in df.loc[df["optionable"] == True, "ticker"]}  # noqa: E712


def main() -> int:
    cached = {p.stem for p in CACHE_DIR.glob("*.parquet")}
    tier1 = _pit_union()
    tier2 = _optionable_delisted() - tier1
    full_target = tier1 | tier2

    cached_t1 = cached & tier1
    cached_t2 = cached & tier2
    missing_t1 = tier1 - cached
    missing_t2 = tier2 - cached
    extra = cached - full_target

    print("=" * 70)
    print(f"v7 smd cache status @ {CACHE_DIR}")
    print("=" * 70)
    print(f"Cached parquets:           {len(cached):>5}")
    print(
        f"  Tier 1 (PIT + ETFs):     {len(cached_t1):>5} / {len(tier1):>5}  "
        f"({100 * len(cached_t1) / len(tier1):.1f}%)"
    )
    print(
        f"  Tier 2 (optionable dl):  {len(cached_t2):>5} / {len(tier2):>5}  "
        f"({100 * len(cached_t2) / len(tier2):.1f}%)"
        if tier2
        else "  Tier 2: not loaded"
    )
    print(f"  Extra (outside targets): {len(extra):>5}")
    print(f"Missing Tier 1:            {len(missing_t1):>5}")
    print(f"Missing Tier 2:            {len(missing_t2):>5}")

    # Pull rate from log
    if PULL_LOG.exists():
        log_text = PULL_LOG.read_text()
        # Count vendor warnings (CSV parse retries)
        retry_warnings = log_text.count("vendor CSV parse error")
        # Recent pull rate: count [FILE MODE] Total time entries
        total_time_lines = re.findall(r"\[FILE MODE\] Total time: ([\d.]+)s", log_text)
        if total_time_lines:
            n_pulls = len(total_time_lines)
            # Rough: each pull = one ticker. Use first/last log timestamps for elapsed.
            first_ts_match = re.search(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", log_text)
            last_ts_match = re.search(
                r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\n*$", log_text[-2000:]
            )
            print()
            print("Pull log:")
            print(f"  Vendor CSV parse retries: {retry_warnings} (handled by robust_fetcher)")
            print(f"  Pulls completed (any tier): {n_pulls}")
            if first_ts_match and last_ts_match:
                from datetime import datetime

                t0 = datetime.strptime(first_ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                t1 = datetime.strptime(last_ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                elapsed_min = (t1 - t0).total_seconds() / 60
                rate_per_min = len(cached) / elapsed_min if elapsed_min else 0
                remaining = len(missing_t1) + len(missing_t2)
                eta_min = remaining / rate_per_min if rate_per_min else float("inf")
                print(f"  Elapsed: {elapsed_min:.1f} min ({elapsed_min / 60:.1f} h)")
                print(f"  Rate: {rate_per_min:.1f} cached/min")
                print(f"  ETA to full: {eta_min:.1f} min ({eta_min / 60:.1f} h)")

    print()
    print("Phase B readiness:")
    print(f"  ETFs cached:  {sorted(set(ETFS) & cached)}")
    if "MDY" in cached:
        print("  MDY (benchmark) cached ✓ — Phase B can run")
    else:
        print("  MDY MISSING — Phase B blocked until MDY pulled")
    if len(cached_t1) >= 1500:
        print("  Tier 1 ≥1500: ✓ ready for full Phase B")
    elif len(cached_t1) >= 500:
        print(f"  Tier 1 = {len(cached_t1)}: marginal Phase B (≥500, <1500)")
    else:
        print(f"  Tier 1 = {len(cached_t1)}: too thin for Phase B")

    return 0


if __name__ == "__main__":
    sys.exit(main())
