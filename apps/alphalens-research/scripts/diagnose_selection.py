#!/usr/bin/env python
"""Fixed-horizon CAR (selection) + Kaplan-Meier survival-fill (entry) diagnostic.

Read-only, research-side. Reads the same three ~/.alphalens parquet stores as
diagnose_nofill.py. Selection = daily market-adjusted BHAR over fixed k-session
windows from the event (complete-window-only) with bootstrap CIs; entry =
time-to-touch-E1 survival with right-censoring at the entry TTL. Telemetry-only.

    .venv/bin/python apps/alphalens-research/scripts/diagnose_selection.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
)
from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS
from alphalens_research.diagnostics import anchor as anchor_mod
from alphalens_research.diagnostics import edge_stores, fill_survival, fixed_horizon, nofill

_SPY = "SPY"
_FILLED = {"OPEN", "PARTIAL_TP_OPEN", "TP_FULL", "SL_HIT"}


def _close(snapshot: dict | None, ticker: str) -> float | None:
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


def _low(snapshot: dict | None, ticker: str) -> float | None:
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        return float(bar["l"])
    except (KeyError, TypeError, ValueError):
        return None


def _open(snapshot: dict | None, ticker: str) -> float | None:
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        o = float(bar["o"])
    except (KeyError, TypeError, ValueError):
        return None
    return o if o > 0.0 else None


def _e1(setup: dict | None) -> float | None:
    if not setup or setup.get("status") != "OK":
        return None
    tiers = setup.get("entry_tiers") or []
    if not tiers:
        return None
    try:
        return float(tiers[0]["limit"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ladders-dir", type=Path, default=edge_stores.HOME / "population_ladders")
    ap.add_argument("--briefs-dir", type=Path, default=edge_stores.HOME / "thematic_briefs")
    ap.add_argument("--grouped-root", type=Path, default=rs_history.DEFAULT_RS_HISTORY_ROOT)
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    ap.add_argument("--ttl", type=int, default=DEFAULT_ORDER_TTL_DAYS)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--anchor",
        choices=anchor_mod.ANCHOR_MODES,
        default=anchor_mod.ANCHOR_PRIOR_CLOSE,
        help="CAR anchor: prior_close (legacy) or arrival_vwap (price an arrival entry pays)",
    )
    ap.add_argument(
        "--out", type=Path, default=edge_stores.HOME / "diagnostics" / "selection.parquet"
    )
    args = ap.parse_args()

    outcomes = edge_stores.load_store(args.ladders_dir)
    if outcomes.empty or "plannable" not in outcomes.columns:
        print("no plannable population-ladder outcomes at", args.ladders_dir)
        return
    setups = edge_stores.setup_index(args.briefs_dir)
    grouped = edge_stores.GroupedDailyCache(args.grouped_root)
    newest = edge_stores.newest_session(args.grouped_root)
    if newest is None:
        print("empty grouped-daily store at", args.grouped_root)
        return

    plannable = outcomes[outcomes["plannable"] == True].copy()  # noqa: E712

    # Per-event CAR at each k (complete-window-only) + fill duration/censoring.
    records: list[dict] = []
    for _, row in plannable.iterrows():
        brief_date = row["brief_date"]
        ticker = str(row["ticker"]).upper()
        classification = str(row.get("ladder_classification") or "")
        arrival = session_on_or_after(brief_date, args.exchange)
        anchor_session = previous_trading_day(arrival, args.exchange)
        prior_close_stock = _close(grouped.get(anchor_session), ticker)
        prior_close_spy = _close(grouped.get(anchor_session), _SPY)
        ref_close = row.get("reference_close")
        arrival_vwap_stock = (
            float(ref_close) if ref_close is not None and not pd.isna(ref_close) else None
        )
        arrival_open_spy = _open(grouped.get(arrival), _SPY)
        a_stock, a_spy = anchor_mod.event_anchor(
            args.anchor,
            prior_close_stock=prior_close_stock,
            prior_close_spy=prior_close_spy,
            arrival_vwap_stock=arrival_vwap_stock,
            arrival_open_spy=arrival_open_spy,
        )

        rec: dict = {"brief_date": brief_date, "ticker": ticker, "classification": classification}
        for k in fixed_horizon.K_WINDOWS:
            horizon = advance_trading_sessions(arrival, k - 1, args.exchange)
            if horizon > newest:
                rec[f"car_{k}"] = None  # window not elapsed
                continue
            rec[f"car_{k}"] = fixed_horizon.car_for_event(
                stock_anchor=a_stock,
                stock_horizon=_close(grouped.get(horizon), ticker),
                spy_anchor=a_spy,
                spy_horizon=_close(grouped.get(horizon), _SPY),
            )

        # Survival: first session in [arrival, arrival+ttl) whose low touches E1.
        e1 = _e1(setups.get((brief_date, ticker)))
        duration: int | None = None
        event = 0
        if e1 is not None and e1 > 0.0:
            incomplete = False
            for i in range(args.ttl):
                s = advance_trading_sessions(arrival, i, args.exchange)
                if s > newest:
                    incomplete = True
                    break
                low = _low(grouped.get(s), ticker)
                if low is None:
                    incomplete = True
                    break
                if low <= e1 * (1.0 + nofill.TOUCH_EPS):
                    duration, event = i + 1, 1
                    break
            if duration is None and not incomplete:
                duration, event = args.ttl, 0  # right-censored at TTL
        rec["fill_duration"] = duration
        rec["fill_event"] = event
        records.append(rec)

    table = pd.DataFrame.from_records(records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)
    print(f"plannable: {len(plannable)}; wrote {args.out} rows: {len(table)}")

    # ---- Selection: per-k CAR with bootstrap CI (all / filled / unfilled) ----
    print(
        f"\nfixed-horizon CAR (market-adjusted BHAR vs SPY, anchor={args.anchor}), "
        "bootstrap 90% CI:"
    )
    for k in fixed_horizon.K_WINDOWS:
        col = table.get(f"car_{k}", None)
        if col is None:
            continue
        complete = table[col.notna()]
        groups = {
            "all": complete,
            "filled": complete[complete["classification"].isin(_FILLED)],
            "unfilled": complete[complete["classification"] == "NO_FILL"],
        }
        print(f"  k={k}:")
        for name, sub in groups.items():
            lo, mean, hi = fixed_horizon.bootstrap_ci(sub[f"car_{k}"].tolist(), seed=args.seed)
            warn = "  [low-N]" if len(sub) < fixed_horizon.LOW_N_WARN else ""
            ms = f"{mean:+.4f}" if mean is not None else "n/a"
            cis = f"[{lo:+.4f}, {hi:+.4f}]" if lo is not None else ""
            print(f"    {name:9} n={len(sub):3} mean={ms} {cis}{warn}")

    # ---- Entry: fill-rate + Kaplan-Meier survival ----
    fillable = table[table["fill_duration"].notna()]
    n_total = len(fillable)
    n_touched = int((fillable["fill_event"] == 1).sum())
    lo, rate, hi = fill_survival.fill_rate_ci(n_touched, n_total, seed=args.seed)
    if rate is not None:
        warn = "  [low-N]" if n_total < fixed_horizon.LOW_N_WARN else ""
        print(
            f"\nfill-rate (touch E1 within TTL={args.ttl}): {n_touched}/{n_total} "
            f"= {rate:.3f}  90% CI [{lo:.3f}, {hi:.3f}]{warn}"
        )
        durations = [int(d) for d in fillable["fill_duration"].tolist()]
        events = [int(e) for e in fillable["fill_event"].tolist()]
        print("Kaplan-Meier S(t) = P(not yet filled by session t):")
        for t, s in fill_survival.kaplan_meier(durations, events):
            print(f"  t={t:2}  S={s:.3f}")
    else:
        print("\nno fillable rows with a complete entry window yet")


if __name__ == "__main__":
    main()
