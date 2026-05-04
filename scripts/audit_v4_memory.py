"""Memory audit for event_drift v4 breadth-audit pipeline.

Runs the v4 pipeline at multiple N values (default 50/100/200), recording
peak RSS (via resource.getrusage) and Python heap (via tracemalloc) after
each phase. Extrapolates linearly to the full S&P 1500 universe to confirm
whether the 2026-05-03 OOM crash is consistent with the 3x un-shared
facts-cache hypothesis (FosterSUEStore + SloanAccrualsStore +
AnnouncementDateProvider each holding their own copy of the same parsed
companyfacts JSON).

Phase semantics:
  - 00_universe_loaded: baseline after universe + history paths resolved
  - 01_setup_stores: stores instantiated (still empty caches)
  - 02_ann_idx: AnnouncementDateProvider warmed (1x facts cache)
  - 03_warm_sue_accr: FosterSUEStore + SloanAccrualsStore warmed (3x total)

If the hypothesis holds, RSS @ phase 03 should be ~3x the RSS delta from
01->02 (since phase 02 warms 1 cache, phase 03 warms 2 more on the same
CIKs).

Usage:
    .venv/bin/python scripts/audit_v4_memory.py --ns 50 100 200
    .venv/bin/python scripts/audit_v4_memory.py --ns 50 --abort-rss-mb 8000
"""

from __future__ import annotations

import argparse
import gc
import json
import resource
import sys
import time
import tracemalloc
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.experiment_event_drift_v4 import (
    PRICES_DIR,
    _build_announcement_index,
    _setup_stores,
    _sp1500_universe_for_window,
)

MB = 1024 * 1024


def rss_peak_mb() -> float:
    """Peak RSS in MB. macOS reports ru_maxrss in bytes; Linux in kilobytes."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / MB if sys.platform == "darwin" else raw / 1024


def snap(label: str, abort_rss_mb: float | None = None) -> dict:
    gc.collect()
    cur, peak = tracemalloc.get_traced_memory()
    rss = rss_peak_mb()
    print(
        f"  [{label}] tracemalloc cur={cur / MB:7.0f}MB peak={peak / MB:7.0f}MB | "
        f"rss_peak={rss:7.0f}MB",
        flush=True,
    )
    if abort_rss_mb is not None and rss >= abort_rss_mb:
        raise SystemExit(f"ABORT: rss_peak {rss:.0f}MB >= --abort-rss-mb {abort_rss_mb}MB")
    return {
        "label": label,
        "tracemalloc_cur_mb": cur / MB,
        "tracemalloc_peak_mb": peak / MB,
        "rss_peak_mb": rss,
    }


def run_for_n(n: int, start: date, end: date, abort_rss_mb: float | None) -> dict:
    print(f"\n=== N={n} tickers ===", flush=True)

    universe = _sp1500_universe_for_window(start, end)
    universe_with_history = [t for t in universe if (PRICES_DIR / f"{t}.parquet").exists()]
    universe_with_history = universe_with_history[:n]

    snapshots = [snap("00_universe_loaded", abort_rss_mb)]

    t0 = time.time()
    (cik_map, history, sue_store, accruals_store, announce_provider, calendar, sector_filter) = (
        _setup_stores(universe_with_history)
    )
    snapshots.append(snap(f"01_setup_stores_{time.time() - t0:.1f}s", abort_rss_mb))

    t1 = time.time()
    announcement_index = _build_announcement_index(announce_provider, universe_with_history)
    n_total = sum(len(v) for v in announcement_index.values())
    snapshots.append(snap(f"02_ann_idx_{n_total}anns_{time.time() - t1:.1f}s", abort_rss_mb))

    t2 = time.time()
    sue_calls = 0
    accr_calls = 0
    for ticker, anns in announcement_index.items():
        for ann in anns:
            sue_store.sue(ticker, ann.filed_date)
            accruals_store.accruals_ratio(ticker, ann.filed_date)
            sue_calls += 1
            accr_calls += 1
    snapshots.append(
        snap(
            f"03_warm_sue_accr_{sue_calls}calls_{time.time() - t2:.1f}s",
            abort_rss_mb,
        )
    )

    return {
        "n": n,
        "n_with_announcements": len(announcement_index),
        "n_total_announcements": n_total,
        "n_sue_calls": sue_calls,
        "snapshots": snapshots,
    }


def _delta_rss(snaps: list[dict], from_label_prefix: str, to_label_prefix: str) -> float:
    a = next(s for s in snaps if s["label"].startswith(from_label_prefix))
    b = next(s for s in snaps if s["label"].startswith(to_label_prefix))
    return b["rss_peak_mb"] - a["rss_peak_mb"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ns", type=int, nargs="+", default=[50, 100, 200])
    parser.add_argument("--start", default="2024-04-30")
    parser.add_argument("--end", default="2026-04-30")
    parser.add_argument(
        "--abort-rss-mb",
        type=float,
        default=12_000.0,
        help="Abort if RSS peak exceeds this (default 12 GB safety guard)",
    )
    parser.add_argument("--output", default="docs/research/v4_memory_audit.json")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    tracemalloc.start(1)
    results = []
    try:
        for n in args.ns:
            r = run_for_n(n, start, end, args.abort_rss_mb)
            results.append(r)
            # ru_maxrss is monotonic across the process — cannot reset.
            # tracemalloc peak we can reset to scope per-N python heap delta.
            tracemalloc.reset_peak()
    finally:
        tracemalloc.stop()

    print("\n=== Per-N summary (RSS deltas) ===", flush=True)
    print(
        f"{'N':>5} {'1cache(02-01)':>15} {'+2caches(03-02)':>17} "
        f"{'ratio':>7} {'rss_final_MB':>14} {'py_cur_MB':>12}"
    )
    summary_rows = []
    for r in results:
        s = r["snapshots"]
        d_one = _delta_rss(s, "01_", "02_")
        d_two = _delta_rss(s, "02_", "03_")
        ratio = (d_two / d_one) if d_one > 0 else float("nan")
        rss_final = s[-1]["rss_peak_mb"]
        py_cur = s[-1]["tracemalloc_cur_mb"]
        print(
            f"{r['n']:>5d} {d_one:>15.0f} {d_two:>17.0f} {ratio:>7.2f} "
            f"{rss_final:>14.0f} {py_cur:>12.0f}"
        )
        summary_rows.append(
            {
                "n": r["n"],
                "rss_delta_one_cache_mb": d_one,
                "rss_delta_two_caches_mb": d_two,
                "two_to_one_ratio": ratio,
                "rss_final_mb": rss_final,
                "py_heap_cur_mb": py_cur,
            }
        )

    if len(summary_rows) >= 2:
        # Linear extrapolation on last two points, on rss_final
        a, b = summary_rows[-2], summary_rows[-1]
        slope = (b["rss_final_mb"] - a["rss_final_mb"]) / (b["n"] - a["n"])
        intercept = a["rss_final_mb"] - slope * a["n"]
        est_1500 = intercept + slope * 1500
        est_1800 = intercept + slope * 1800
        print(
            f"\nLinear fit on (N, rss_final): slope={slope:.2f} MB/ticker, "
            f"intercept={intercept:.0f}MB"
        )
        print(f"  Extrapolated rss_final @ N=1500: {est_1500:.0f}MB ({est_1500 / 1024:.1f}GB)")
        print(f"  Extrapolated rss_final @ N=1800: {est_1800:.0f}MB ({est_1800 / 1024:.1f}GB)")
    else:
        slope = None
        est_1500 = None
        est_1800 = None

    out = {
        "params": {
            "ns": args.ns,
            "start": str(start),
            "end": str(end),
        },
        "runs": results,
        "summary": summary_rows,
        "extrapolation": {
            "slope_mb_per_ticker": slope,
            "est_rss_final_mb_at_n_1500": est_1500,
            "est_rss_final_mb_at_n_1800": est_1800,
        },
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
