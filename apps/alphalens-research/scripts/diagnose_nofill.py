#!/usr/bin/env python
"""Diagnose WHY EDGE outcomes end as NO_FILL (read-only, research-side).

Reads three parquet stores under ~/.alphalens (population_ladders, thematic_briefs,
grouped_daily_history), reconstructs each NO_FILL outcome's entry-window price path,
classifies the cause, and prints population aggregates + writes a tidy table.

Run on the VPS (where the stores live) or against rsync'd copies:
    .venv/bin/python apps/alphalens-research/scripts/diagnose_nofill.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
from collections import Counter
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper import brief_loader
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    session_on_or_after,
)
from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS
from alphalens_research.diagnostics import nofill

_HOME = Path.home() / ".alphalens"
_TAIL_SESSIONS = 10  # post-TTL window for TOUCHED_AFTER_TTL detection


def _tiers_and_stop(setup: dict | None) -> tuple[list[float], float | None]:
    """E1..E3 (descending) + disaster_stop from a decoded brief_trade_setup."""
    if not setup or setup.get("status") != "OK":
        return [], None
    raw = setup.get("entry_tiers") or []
    tiers: list[float] = []
    for t in raw:
        try:
            tiers.append(float(t["limit"]))
        except (KeyError, TypeError, ValueError):
            continue
    stop = setup.get("disaster_stop")
    try:
        stop_f = float(stop) if stop is not None else None
    except (TypeError, ValueError):
        stop_f = None
    return tiers, stop_f


def _load_store(store_dir: Path) -> pd.DataFrame:
    frames = []
    for path in sorted(store_dir.glob("*.parquet")):
        try:
            d = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        df = pd.read_parquet(path)
        df["brief_date"] = d
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _setup_index(briefs_dir: Path) -> dict[tuple[dt.date, str], dict]:
    out: dict[tuple[dt.date, str], dict] = {}
    briefs = _load_store(briefs_dir)
    if briefs.empty:
        return out
    for _, row in briefs.iterrows():
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        raw = row.get("brief_trade_setup")
        setup = brief_loader._coerce_trade_setup(raw)
        if setup is not None:
            out[(row["brief_date"], ticker)] = setup
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ladders-dir", type=Path, default=_HOME / "population_ladders")
    ap.add_argument("--briefs-dir", type=Path, default=_HOME / "thematic_briefs")
    ap.add_argument("--grouped-root", type=Path, default=rs_history.DEFAULT_RS_HISTORY_ROOT)
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    ap.add_argument("--ttl", type=int, default=DEFAULT_ORDER_TTL_DAYS)
    ap.add_argument("--out", type=Path, default=_HOME / "diagnostics" / "nofill.parquet")
    args = ap.parse_args()

    outcomes = _load_store(args.ladders_dir)
    if outcomes.empty:
        print("no population-ladder outcomes found at", args.ladders_dir)
        return
    setups = _setup_index(args.briefs_dir)

    # Population mix over ALL rows (NO_FILL classification needs no maturity).
    mix = Counter(str(c or "") for c in outcomes.get("ladder_classification", []))
    print("classification mix (all rows):", dict(mix))

    nofill_rows = outcomes[outcomes["ladder_classification"] == "NO_FILL"].copy()
    print(f"NO_FILL rows: {len(nofill_rows)} / {len(outcomes)} total")

    grouped_cache: dict[dt.date, dict | None] = {}

    def grouped(session: dt.date) -> dict | None:
        if session not in grouped_cache:
            grouped_cache[session] = rs_history.read_grouped_day(args.grouped_root, session)
        return grouped_cache[session]

    records: list[dict] = []
    for _, row in nofill_rows.iterrows():
        brief_date = row["brief_date"]
        ticker = str(row["ticker"]).upper()
        tiers, stop = _tiers_and_stop(setups.get((brief_date, ticker)))

        arrival = session_on_or_after(brief_date, args.exchange)
        window_sessions = [
            advance_trading_sessions(arrival, i, args.exchange) for i in range(args.ttl)
        ]
        tail_sessions = [
            advance_trading_sessions(arrival, args.ttl + j, args.exchange)
            for j in range(_TAIL_SESSIONS)
        ]
        grouped_by_session: dict[dt.date, dict | None] = {
            s: grouped(s) for s in (*window_sessions, *tail_sessions)
        }

        r = nofill.analyze_outcome_row(
            ticker=ticker,
            tiers=tiers,
            stop=stop,
            reference_close=_as_float(row.get("reference_close")),
            window_sessions=window_sessions,
            tail_sessions=tail_sessions,
            grouped_by_session=grouped_by_session,
        )
        records.append(
            {
                "brief_date": brief_date,
                "ticker": ticker,
                "cause": r.cause,
                "e1": r.e1,
                "stop": r.stop,
                "min_low_in_window": r.min_low_in_window,
                "gap_to_e1": r.gap_to_e1,
                "days_to_first_touch": r.days_to_first_touch,
                "arrival_drift": r.arrival_drift,
                "window_complete": r.window_complete,
                "market_excess_return": _as_float(row.get("market_excess_return")),
                "terminal": bool(row.get("terminal", False)),
                "ladder_config_version": str(row.get("ladder_config_version", "")),
            }
        )

    table = pd.DataFrame.from_records(records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)
    print("wrote", args.out, "rows:", len(table))

    if table.empty:
        print("no NO_FILL rows to classify")
        return

    print("\ncause distribution (NO_FILL):", dict(Counter(table["cause"])))

    # The lynchpin: NO_FILL cause x sign(market_excess) over MATURED rows only.
    matured = table[table["terminal"] & table["market_excess_return"].notna()]
    if not matured.empty:
        matured = matured.assign(excess_sign=matured["market_excess_return"].apply(_sign))
        print("\nmatured NO_FILL  cause x sign(market_excess):")
        print(pd.crosstab(matured["cause"], matured["excess_sign"]))
    else:
        print("\nno matured NO_FILL rows with market_excess yet")


def _as_float(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f  # drop NaN


def _sign(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "zero")


if __name__ == "__main__":
    main()
