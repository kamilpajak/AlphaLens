#!/usr/bin/env python
"""Entry-arm Faza-0 offline counterfactual comparison.

Replays the 5-arm entry grid (baseline / narrow_tiers / single_at_close /
market_at_arrival / vwap_arrival) over every plannable population-ladder event
whose minute-bar cache is present, and reports the per-arm cost-adjusted
market-excess on the COMMON-SUPPORT subset (events where every arm is
evaluable).

COMMON SUPPORT is the headline because the 5 arms have different fill rates:
baseline (a dip-buy touch arm) fills only when price dips to the limit,
while market_at_arrival and vwap_arrival always fill.  Computing the per-arm
mean over events where arm A filled but arm B did not confounds fill-conditional
reward with cash reward (−benchmark).  The common-support subset keeps only
events where ALL arms produced a non-None result, giving an apples-to-apples
comparison of fill-conditional reward under equal-fill-rate conditions.

PRE/POST-HAIRCUT NOTE
---------------------
replay_entry_grid returns ALREADY cost-adjusted (post-haircut) rewards.
The resting-limit touch arms (baseline, narrow_tiers, single_at_close) carry
0 bps haircut (resting limits pay no spread), so pre-haircut equals post-haircut
for those arms.  The always-fill arms (market_at_arrival, vwap_arrival) carry a
half-spread + market-impact haircut that is baked into the returned value.
There is no public ``no_haircut`` toggle on replay_entry_grid, and adding one
would require modifying the substrate (out of Faza-0 scope).  This script
therefore prints post-haircut results only and documents the choice:
  - Touch arms: post-haircut == pre-haircut (0 bps).
  - Always-fill arms: post-haircut only; the haircut is typically 5-15 bps
    depending on market cap.  A pre/post split can be added in Faza-1 by
    exposing a haircut toggle in replay_entry_grid.

Usage:
    .venv/bin/python apps/alphalens-research/scripts/diagnose_entry_grid.py
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.feedback.ladder_replay import ENTRY_GRID_ARMS, replay_entry_grid
from alphalens_pipeline.feedback.population_ladder_monitor import (
    _read_cached_bars,
    _rth_window_utc,
)
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
    session_open_utc,
)
from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS, TIME_STOP_DAYS
from alphalens_research.diagnostics import edge_stores, fixed_horizon


def _close(snapshot: dict | None, ticker: str) -> float | None:
    """Extract close price for ``ticker`` from a grouped-daily snapshot dict."""
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        c = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        return None
    return c if c > 0.0 else None


def _common_support(rows: list[dict[str, float | None]]) -> list[dict]:
    """Keep only events where all 5 ENTRY_GRID_ARMS are non-None.

    This produces the equal-fill-rate common-support subset required for a fair
    arm comparison: on this subset every arm produced a fill-conditional reward
    (no arm received a cash substitute), so per-arm means reflect only the
    fill-conditional return, not the mix of fill-conditional + cash returns.

    Parameters
    ----------
    rows:
        List of per-event dicts (at minimum one key per arm in ENTRY_GRID_ARMS).

    Returns
    -------
    The subset of ``rows`` where every arm in ENTRY_GRID_ARMS is non-None.
    """
    return [row for row in rows if all(row.get(arm) is not None for arm in ENTRY_GRID_ARMS)]


def _market_cap_index(briefs: pd.DataFrame) -> dict[tuple, float | None]:
    """Build a ``(brief_date, TICKER) -> market_cap`` lookup from the briefs store.

    Parameters
    ----------
    briefs:
        DataFrame with at minimum ``brief_date``, ``ticker``, and ``mcap``
        columns.  Missing / NaN mcap values map to ``None``.  numpy.float64
        values are coerced to plain Python float so the haircut's
        isinstance(float) guard passes correctly.

    Returns
    -------
    Dict keyed ``(brief_date, TICKER_UPPER) -> float | None``.
    """
    if briefs.empty:
        return {}
    if "mcap" not in briefs.columns:
        return {}

    out: dict[tuple, float | None] = {}
    for _, row in briefs.iterrows():
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        brief_date = row["brief_date"]
        raw = row.get("mcap")
        if raw is None or (isinstance(raw, float) and math.isnan(raw)):
            out[(brief_date, ticker)] = None
        else:
            # Guard pandas-wrapped numpy scalars (e.g. np.float64 NaN).
            try:
                val = float(raw)
            except (TypeError, ValueError):
                out[(brief_date, ticker)] = None
                continue
            if math.isnan(val):
                out[(brief_date, ticker)] = None
            else:
                # Coerce to a plain Python float (not numpy.float64) so the
                # haircut model's isinstance guard works correctly.
                out[(brief_date, ticker)] = float(val)
    return out


_SPY = "SPY"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ladders-dir",
        type=Path,
        default=edge_stores.HOME / "population_ladders",
        help="Directory containing the population-ladder per-date parquets.",
    )
    ap.add_argument(
        "--briefs-dir",
        type=Path,
        default=edge_stores.HOME / "thematic_briefs",
        help="Directory containing the thematic-brief per-date parquets.",
    )
    ap.add_argument(
        "--grouped-root",
        type=Path,
        default=rs_history.DEFAULT_RS_HISTORY_ROOT,
        help="Root of the split-adjusted grouped-daily history store.",
    )
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    ap.add_argument(
        "--ttl",
        type=int,
        default=DEFAULT_ORDER_TTL_DAYS,
        help="Entry TTL in trading sessions (default: %(default)s).",
    )
    ap.add_argument(
        "--k",
        type=int,
        default=10,
        help="Benchmark window in trading sessions (default: %(default)s).",
    )
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--out",
        type=Path,
        default=edge_stores.HOME / "diagnostics" / "entry_grid.parquet",
        help="Output diagnostics parquet path.",
    )
    args = ap.parse_args()

    # --- Load stores ---
    outcomes = edge_stores.load_store(args.ladders_dir)
    if outcomes.empty or "plannable" not in outcomes.columns:
        print("no plannable population-ladder outcomes at", args.ladders_dir)
        return

    briefs_df = edge_stores.load_store(args.briefs_dir)
    mcap_idx = _market_cap_index(briefs_df) if not briefs_df.empty else {}

    setups = edge_stores.setup_index(args.briefs_dir)
    grouped = edge_stores.GroupedDailyCache(args.grouped_root)
    newest = edge_stores.newest_session(args.grouped_root)
    if newest is None:
        print("empty grouped-daily store at", args.grouped_root)
        return

    plannable = outcomes[outcomes["plannable"] == True].copy()  # noqa: E712

    # Counters for the coverage report.
    n_plannable = len(plannable)
    n_missing_bars = 0
    n_unevaluable = 0  # benchmark window not elapsed or SPY data absent

    rows: list[dict] = []

    for _, outcome_row in plannable.iterrows():
        brief_date = outcome_row["brief_date"]
        ticker = str(outcome_row["ticker"]).upper()

        setup = setups.get((brief_date, ticker))
        if setup is None:
            n_missing_bars += 1
            continue

        arrival = session_on_or_after(brief_date, args.exchange)

        # --- Derive arrival_open_ms / arrival_close_ms ---
        # Reuse _rth_window_utc (the same helper the monitor uses for the
        # arrival-window haircut model's first_rth_bar filter).
        arrival_open_ms, arrival_close_ms = _rth_window_utc(arrival, args.exchange)

        # --- Entry + position expiry in epoch ms (mirrors _engine_cutoffs) ---
        entry_ttl = int(setup.get("order_ttl_days") or args.ttl)
        entry_expiry_session = advance_trading_sessions(arrival, entry_ttl, args.exchange)
        position_expiry_session = advance_trading_sessions(arrival, TIME_STOP_DAYS, args.exchange)
        entry_expiry_ms = int(
            session_open_utc(entry_expiry_session, args.exchange).timestamp() * 1000
        )
        position_expiry_ms = int(
            session_open_utc(position_expiry_session, args.exchange).timestamp() * 1000
        )

        # --- Read cached minute bars ---
        bars = _read_cached_bars(args.ladders_dir, ticker, arrival)
        if not bars:
            n_missing_bars += 1
            continue

        # --- Compute benchmark_window_return from grouped-daily SPY ---
        # SPY anchor: close of the session before arrival (same anchor as CAR).
        anchor_session = previous_trading_day(arrival, args.exchange)
        spy_anchor = _close(grouped.get(anchor_session), _SPY)

        # SPY horizon: close at k sessions after arrival.
        horizon = advance_trading_sessions(arrival, args.k - 1, args.exchange)
        if horizon > newest:
            n_unevaluable += 1
            continue  # window not elapsed yet

        spy_horizon = _close(grouped.get(horizon), _SPY)
        if spy_anchor is None or spy_horizon is None:
            n_unevaluable += 1
            continue

        benchmark_window_return = spy_horizon / spy_anchor - 1.0

        # --- Market cap for haircut model ---
        market_cap = mcap_idx.get((brief_date, ticker))

        # --- Replay all 5 arms ---
        arm_rewards = replay_entry_grid(
            setup,
            bars,
            arrival_open_ms=arrival_open_ms,
            arrival_close_ms=arrival_close_ms,
            benchmark_window_return=benchmark_window_return,
            market_cap=market_cap,
            entry_expiry_ms=entry_expiry_ms,
            position_expiry_ms=position_expiry_ms,
        )

        rec = {"brief_date": brief_date, "ticker": ticker, **arm_rewards}
        rows.append(rec)

    n_full_coverage = len(rows)
    common = _common_support(rows)
    n_common = len(common)
    n_dropped_cs = n_full_coverage - n_common

    # --- Write diagnostics parquet ---
    table = pd.DataFrame.from_records(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)

    print(
        f"plannable: {n_plannable}  |  "
        f"missing-bars/setup: {n_missing_bars}  |  "
        f"unevaluable (window not elapsed / SPY absent): {n_unevaluable}  |  "
        f"full-coverage N: {n_full_coverage}  |  "
        f"common-support N: {n_common}  |  "
        f"dropped (not all arms evaluable): {n_dropped_cs}"
    )
    print(f"wrote diagnostics parquet: {args.out}")
    print()

    # --- Per-arm common-support report ---
    print(f"entry-arm comparison (k={args.k}, post-haircut, common-support N={n_common}):")
    print(
        "  [note] touch arms (baseline/narrow_tiers/single_at_close) carry 0-bps haircut "
        "(post = pre); always-fill arms carry half-spread+impact (Faza-1 can split pre/post)."
    )
    if not common:
        print("  no common-support events yet.")
        return

    # Group rewards by arrival session for day-block bootstrap.
    for arm in ENTRY_GRID_ARMS:
        # Build {arrival_session: [rewards...]} from common subset.
        by_day: dict = {}
        for rec in common:
            val = rec.get(arm)
            bd = rec["brief_date"]
            session = session_on_or_after(bd, args.exchange)
            by_day.setdefault(session, []).append(val)

        lo, mean, hi = fixed_horizon.day_block_bootstrap_ci(by_day, seed=args.seed)
        warn = "  [low-N]" if n_common < fixed_horizon.LOW_N_WARN else ""
        if mean is not None:
            ms = f"{mean:+.4f}"
            cis = f"[{lo:+.4f}, {hi:+.4f}]" if lo is not None and hi is not None else ""
        else:
            ms = "n/a"
            cis = ""
        print(f"  {arm:20s}  mean={ms}  90% CI {cis}{warn}")


if __name__ == "__main__":
    main()
