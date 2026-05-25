"""Phase A gate 2 — yfinance upgrades_downgrades survivorship probe.

Pre-registered as gate 2 of v10 (`docs/research/preregistration/
params_analyst_alt_data_v10_2026_05_01.json`). Auto-pivot trigger:
delisted/active event-rate ratio < 0.5 with z > 2 → ABORT Path γ,
pivot to Path β (long-only top-decile in alt_data_screener_search class).

Run:
    .venv/bin/python scripts/probe_yfinance_analyst_survivorship.py \
        --n-delisted 200 --n-active 200 --random-state 42

Output:
    docs/research/v10_phase_a_gate2_survivorship_probe.json
    stdout: PASS / FAIL summary with z-stat
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
import yfinance as yf

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
PIT_UNIVERSE_DIR = Path.home() / ".alphalens" / "pit_universe"

DELISTED_WINDOW_START = pd.Timestamp("2018-01-01")
DELISTED_WINDOW_END = pd.Timestamp("2024-04-30")
ACTIVE_PIT_WINDOW_START = pd.Timestamp("2023-01-01")
ACTIVE_PIT_WINDOW_END = pd.Timestamp("2024-04-30")
RATIO_FAIL_THRESHOLD = 0.5
Z_FAIL_THRESHOLD = 2.0


def _load_delisted_sample(n: int, random_state: int) -> list[str]:
    """Sample n delisted tickers from 2018-04 to 2024-04 window.

    Restricting to this window gives every ticker at least 1y of pre-delisting
    coverage opportunity that should have been captured during yfinance
    operational history.
    """
    df = pd.read_parquet(SURVIVORSHIP_PARQUET)
    mask = (df["delisted_date"] >= DELISTED_WINDOW_START) & (
        df["delisted_date"] <= DELISTED_WINDOW_END
    )
    pool = df.loc[mask, "ticker"].dropna().unique().tolist()
    rng = np.random.default_rng(random_state)
    rng.shuffle(pool)
    return pool[:n]


def _load_active_sample(n: int, random_state: int) -> list[str]:
    """Sample n active tickers from union of PIT universe yaml snapshots
    spanning ACTIVE_PIT_WINDOW (each file is an incremental month, so the
    full universe is the union across files in the window)."""
    union: set[str] = set()
    for path in sorted(PIT_UNIVERSE_DIR.glob("*.yaml")):
        try:
            snap = pd.Timestamp(path.stem + "-01")
        except ValueError:
            continue
        if not (ACTIVE_PIT_WINDOW_START <= snap <= ACTIVE_PIT_WINDOW_END):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for ticker in data.get("tickers", []):
            union.add(ticker)
    pool = sorted(union)
    rng = np.random.default_rng(random_state + 1)
    rng.shuffle(pool)
    return pool[:n]


def _probe_ticker_events(ticker: str) -> tuple[int, str]:
    """Return (event_count, status). status in {ok, empty, error}."""
    try:
        ud = yf.Ticker(ticker).upgrades_downgrades
        if ud is None or ud.empty:
            return 0, "empty"
        return len(ud), "ok"
    except Exception as exc:
        logger.debug("yfinance probe failed for %s: %s", ticker, exc, exc_info=True)
        return 0, f"error: {type(exc).__name__}"


def _bootstrap_ratio_z(
    delisted_counts: list[int],
    active_counts: list[int],
    n_boot: int = 5000,
    random_state: int = 42,
) -> tuple[float, float, float]:
    """Bootstrap z-stat on (mean_delisted / mean_active) ratio under H0=1.0.

    Returns (point_estimate, z_stat, p_value).
    """
    arr_d = np.array(delisted_counts, dtype=float)
    arr_a = np.array(active_counts, dtype=float)
    rng = np.random.default_rng(random_state)
    point = arr_d.mean() / max(arr_a.mean(), 1e-9)
    boot_ratios = np.empty(n_boot)
    n_d, n_a = len(arr_d), len(arr_a)
    for i in range(n_boot):
        d_resampled = rng.choice(arr_d, size=n_d, replace=True)
        a_resampled = rng.choice(arr_a, size=n_a, replace=True)
        boot_ratios[i] = d_resampled.mean() / max(a_resampled.mean(), 1e-9)
    se = boot_ratios.std()
    z_under_h0 = (1.0 - point) / max(se, 1e-9)
    p_value = float(np.mean(boot_ratios >= 1.0))
    return point, z_under_h0, p_value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-delisted", type=int, default=200)
    parser.add_argument("--n-active", type=int, default=200)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--rate-limit-sec", type=float, default=0.5)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs/research/v10_phase_a_gate2_survivorship_probe.json",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    delisted = _load_delisted_sample(args.n_delisted, args.random_state)
    active = _load_active_sample(args.n_active, args.random_state)
    logger.info(f"Sampled {len(delisted)} delisted, {len(active)} active.")

    delisted_counts: list[int] = []
    delisted_status_dist: dict[str, int] = {}
    active_counts: list[int] = []
    active_status_dist: dict[str, int] = {}

    t0 = time.time()
    for i, t in enumerate(delisted):
        n_events, status = _probe_ticker_events(t)
        delisted_counts.append(n_events)
        delisted_status_dist[status] = delisted_status_dist.get(status, 0) + 1
        if (i + 1) % 25 == 0:
            elapsed = time.time() - t0
            logger.info(
                f"delisted {i + 1}/{len(delisted)} elapsed={elapsed:.1f}s",
            )
        time.sleep(args.rate_limit_sec)

    t1 = time.time()
    for i, t in enumerate(active):
        n_events, status = _probe_ticker_events(t)
        active_counts.append(n_events)
        active_status_dist[status] = active_status_dist.get(status, 0) + 1
        if (i + 1) % 25 == 0:
            elapsed = time.time() - t1
            logger.info(
                f"active {i + 1}/{len(active)} elapsed={elapsed:.1f}s",
            )
        time.sleep(args.rate_limit_sec)

    arr_d = np.array(delisted_counts)
    arr_a = np.array(active_counts)
    mean_d, mean_a = float(arr_d.mean()), float(arr_a.mean())
    nonempty_d = float((arr_d > 0).mean())
    nonempty_a = float((arr_a > 0).mean())
    point_ratio, z_stat, p_value = _bootstrap_ratio_z(
        delisted_counts, active_counts, random_state=args.random_state
    )

    fail = (point_ratio < RATIO_FAIL_THRESHOLD) and (z_stat > Z_FAIL_THRESHOLD)
    verdict = "FAIL" if fail else "PASS"
    pivot = "AUTO_PIVOT_TO_PATH_BETA" if fail else "PROCEED_PATH_GAMMA"

    result = {
        "phase": "A",
        "gate": "2_survivorship_hard_block",
        "n_delisted_sampled": len(delisted),
        "n_active_sampled": len(active),
        "delisted_event_count_mean": mean_d,
        "active_event_count_mean": mean_a,
        "delisted_nonempty_rate": nonempty_d,
        "active_nonempty_rate": nonempty_a,
        "delisted_status_dist": delisted_status_dist,
        "active_status_dist": active_status_dist,
        "ratio_delisted_to_active_event_rate": point_ratio,
        "z_stat_under_h0_ratio_eq_1": z_stat,
        "p_value_one_sided_h1_ratio_lt_1": p_value,
        "fail_threshold_ratio_lt": RATIO_FAIL_THRESHOLD,
        "fail_threshold_z_gt": Z_FAIL_THRESHOLD,
        "verdict": verdict,
        "next_action": pivot,
        "delisted_window": [
            DELISTED_WINDOW_START.date().isoformat(),
            DELISTED_WINDOW_END.date().isoformat(),
        ],
        "active_pit_window": [
            ACTIVE_PIT_WINDOW_START.date().isoformat(),
            ACTIVE_PIT_WINDOW_END.date().isoformat(),
        ],
        "rate_limit_sec": args.rate_limit_sec,
        "random_state": args.random_state,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    print()
    print("=" * 60)
    print("PHASE A GATE 2 — yfinance upgrades_downgrades SURVIVORSHIP PROBE")
    print("=" * 60)
    print(f"Delisted sample (2018-2024 window):  n={len(delisted)}")
    print(f"  mean event count:           {mean_d:.2f}")
    print(f"  non-empty rate:             {nonempty_d:.1%}")
    print(f"Active sample (PIT 2024-01):  n={len(active)}")
    print(f"  mean event count:           {mean_a:.2f}")
    print(f"  non-empty rate:             {nonempty_a:.1%}")
    print()
    print(f"Ratio (delisted / active):    {point_ratio:.3f}")
    print(f"z-stat under H0=1:            {z_stat:.2f}")
    print(f"p-value (H1: ratio < 1):      {p_value:.4f}")
    print()
    print(f"FAIL threshold: ratio < {RATIO_FAIL_THRESHOLD} AND z > {Z_FAIL_THRESHOLD}")
    print(f"VERDICT: {verdict}")
    print(f"NEXT:    {pivot}")
    print(f"Output:  {args.out}")
    print("=" * 60)

    return 0 if not fail else 2  # exit 2 = pivot trigger


if __name__ == "__main__":
    sys.exit(main())
