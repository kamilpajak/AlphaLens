"""What-if study: TRAILING entry (bounce-off-low buy-stop) vs HARDCODED limit entry.

ENGINEERING DECISION SUPPORT ONLY — another cut of already-used data, NOT a
pre-registered strategy test. Direction-level answer per cohort.

Design (binding, operator-approved 2026-08-12):
* Universe: every population-ladder candidate with a plannable brief_trade_setup
  and a cached minute-bar path (the monitor's own cache — NO Polygon refetch).
* Per candidate x entry tier (E1/E2/E3): first touch of the tier limit within
  the entry window (entry_expiry_ms from the same _engine_cutoffs the monitor
  uses = 7 trading days GTD).
* Variant A (baseline): entry at the tier limit at touch time.
  Slippage-adverse A: fill requires trading THROUGH the limit (low <= L - tick);
  fill time = first through-bar; price still L.
* Variant B(d): after the touch, trail the running LOW; enter at the first bar
  whose high reaches run_low*(1+d) (stop-buy). Trigger level uses lows up to the
  PREVIOUS bar (conservative: the stop rests at the level derived from
  already-seen lows). Gap-open above the level fills at the open. Same-bar
  bounce inside the touch bar is NOT triggered (minute-bar intra-bar order
  unknown; conservative against B's fill rate). No trigger inside the entry
  window => tier MISSED. Slippage-adverse B: +1 tick on the fill price.
* Exits: ONE shared implementation for both variants —
  alphalens_pipeline.feedback.ladder_replay._replay_synthetic_fill (the repo's
  synthetic-fill exit walk: TP tranches / disaster stop / TIME_STOP at
  position_expiry_ms, SL-first ambiguity). The walk's weighted exit mark
  M = entry + r*(entry - stop) is entry-price-invariant for a fixed fill bar,
  so slippage/denominator views are derived analytically from one walk.
* R views: own-denominator r = (M - e)/(e - stop); fixed-denominator
  r_fixed = (M - e)/(L_A - stop) (A's risk unit) so the two are never conflated.
* Cohorts: fill day of the A-touch (day-1 vs day-2+), tier depth, all.
* Guards: no outcome-based selection; every exclusion counted; N<15 flagged.

Read-only against ~/.alphalens stores; intermediates go to /tmp.

Usage (VPS):
    /home/jacoren/AlphaLens/.venv/bin/python /tmp/whatif_trailing_entry.py \
        [--store ~/.alphalens/population_ladders] [--briefs ~/.alphalens/thematic_briefs]
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_pipeline.feedback.ladder_replay import (
    _replay_synthetic_fill,
    parse_ladder,
    replay_ladder,
)
from alphalens_pipeline.feedback.population_ladder_monitor import (
    _engine_cutoffs,
    _filter_bars_to_rth,
    _read_cached_bars,
)
from alphalens_pipeline.paper.brief_loader import load_brief
from alphalens_pipeline.paper.calendar import trading_days_elapsed

TICK = 0.01
D_GRID = (0.005, 0.01, 0.015, 0.02, 0.03)
IMPLAUSIBLE = 0.60  # mirror bar_window.IMPLAUSIBLE_RETURN_THRESHOLD (raw split guard)
EXCHANGE = "XNYS"
MIN_TOUCHES = 50


def _ts_date(ts_ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).date()


def exit_mark_for_fill(
    setup: dict,
    bars: list[dict],
    fill_price: float,
    fill_ts: int,
    stop: float,
    position_expiry_ms: int,
) -> tuple[float | None, str, bool]:
    """One shared exit walk (repo engine). Returns (M, classification, horizon_open).

    M = weighted exit mark per share = entry + r*(entry-stop); entry-invariant for
    a fixed fill bar, so callers re-denominate freely.
    """
    out = _replay_synthetic_fill(
        setup,
        bars,
        fill_price=fill_price,
        fill_ts_ms=fill_ts,
        own_stop=stop,
        position_expiry_ms=position_expiry_ms,
    )
    if out.status != "OK" or out.realized_r is None or out.blended_entry is None:
        return None, out.status + "/" + out.classification, False
    m = out.blended_entry + out.realized_r * (out.blended_entry - stop)
    return m, out.classification, out.horizon_open


def first_touch(bars: list[dict], limit: float, entry_expiry_ms: int) -> int | None:
    """Index of the first bar (ts < expiry) with low <= limit; None if never."""
    for i, b in enumerate(bars):
        if int(b["t"]) >= entry_expiry_ms:
            return None
        if float(b["l"]) <= limit:
            return i
    return None


def trail_trigger(
    bars: list[dict], touch_idx: int, d: float, entry_expiry_ms: int
) -> tuple[int, float] | None:
    """(bar_idx, fill_price) of the trailed buy-stop, or None (MISSED).

    Arms at the touch bar; run_low seeds from the touch bar's low. From the next
    bar onward: check trigger vs the level from PREVIOUS bars' lows first, then
    update run_low. Gap-open above the level fills at the open.
    """
    run_low = float(bars[touch_idx]["l"])
    for i in range(touch_idx + 1, len(bars)):
        b = bars[i]
        if int(b["t"]) >= entry_expiry_ms:
            return None
        level = run_low * (1.0 + d)
        o, h, lo = float(b["o"]), float(b["h"]), float(b["l"])
        if o >= level:
            return i, o
        if h >= level:
            return i, level
        run_low = min(run_low, lo)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=str(Path.home() / ".alphalens" / "population_ladders"))
    ap.add_argument("--briefs", default=str(Path.home() / ".alphalens" / "thematic_briefs"))
    ap.add_argument("--out", default="/tmp/whatif_trailing_entry_records.parquet")
    args = ap.parse_args()
    store = Path(args.store)
    briefs_dir = Path(args.briefs)

    dates = sorted(dt.date.fromisoformat(p.stem) for p in store.glob("????-??-??.parquet"))
    cov = {
        "dates_total": len(dates),
        "dates_no_brief": 0,
        "candidates": 0,
        "not_plannable": 0,
        "no_bar_cache": 0,
        "empty_after_rth": 0,
        "entry_window_truncated": 0,
        "tier_bad_geometry": 0,
        "tier_no_touch": 0,
        "tier_touches": 0,
        "implausible_dropped": 0,
    }
    records: list[dict] = []
    recon: list[dict] = []

    for date in dates:
        try:
            candidates = load_brief(date, briefs_dir)
        except Exception:
            cov["dates_no_brief"] += 1
            continue
        # stored baseline outcomes for reconciliation
        stored = pd.read_parquet(store / f"{date.isoformat()}.parquet")
        stored_by_ticker = {str(r["ticker"]).upper(): r for _, r in stored.iterrows()}

        for c in candidates:
            if not c.verified or c.trade_setup is None:
                continue  # mirrors the monitor's population predicate
            ladder = parse_ladder(c.trade_setup)
            cov["candidates"] += 1
            if not ladder.ok:
                cov["not_plannable"] += 1
                continue
            setup = c.trade_setup
            cut = _engine_cutoffs(date, setup, EXCHANGE)
            (arrival, _ee_sess, pos_sess, _et, _pt, entry_expiry_ms, position_expiry_ms) = cut
            raw = _read_cached_bars(store, c.ticker, arrival)
            if not raw:
                cov["no_bar_cache"] += 1
                continue
            bars = _filter_bars_to_rth(raw, arrival, pos_sess, EXCHANGE)
            bars = sorted(bars, key=lambda b: int(b["t"]))
            if not bars:
                cov["empty_after_rth"] += 1
                continue
            if int(bars[-1]["t"]) < entry_expiry_ms:
                cov["entry_window_truncated"] += 1  # processed anyway; touch may predate
            stop = ladder.disaster_stop
            assert stop is not None

            # ---- reconciliation: full as-specified ladder replay vs stored row
            srow = stored_by_ticker.get(c.ticker.upper())
            if srow is not None and bool(srow.get("terminal")) and pd.notna(srow.get("realized_r")):
                full = replay_ladder(
                    setup,
                    bars,
                    entry_expiry_ms=entry_expiry_ms,
                    position_expiry_ms=position_expiry_ms,
                )
                recon.append(
                    {
                        "date": date,
                        "ticker": c.ticker,
                        "stored_r": float(srow["realized_r"]),
                        "replay_r": full.realized_r,
                        "stored_cls": srow.get("ladder_classification"),
                        "replay_cls": full.classification,
                    }
                )

            # ---- per-tier study
            for tier_i, lvl in enumerate(ladder.entries):
                tier = f"E{tier_i + 1}"
                limit = lvl.price
                if not math.isfinite(limit) or limit <= stop:
                    cov["tier_bad_geometry"] += 1
                    continue
                t_idx = first_touch(bars, limit, entry_expiry_ms)
                if t_idx is None:
                    cov["tier_no_touch"] += 1
                    continue
                cov["tier_touches"] += 1
                touch_ts = int(bars[t_idx]["t"])
                day_n = trading_days_elapsed(arrival, _ts_date(touch_ts), EXCHANGE)
                day_cohort = "day1" if day_n == 0 else "day2plus"
                risk_a = limit - stop
                base = {
                    "date": date,
                    "ticker": c.ticker,
                    "tier": tier,
                    "day_cohort": day_cohort,
                    "limit": limit,
                    "stop": stop,
                }

                def emit(
                    variant: str,
                    d: float | None,
                    slip: str,
                    filled: bool,
                    entry: float | None,
                    fill_idx: int | None,
                ) -> None:
                    rec = {
                        **base,
                        "variant": variant,
                        "d": d,
                        "slip": slip,
                        "filled": filled,
                        "entry": entry,
                        "r_own": None,
                        "r_fixed": None,
                        "concession_pct": None,
                        "concession_r": None,
                        "cls": None,
                        "open": False,
                    }
                    if filled:
                        assert entry is not None and fill_idx is not None
                        m, cls, hopen = exit_mark_for_fill(
                            setup, bars, entry, int(bars[fill_idx]["t"]), stop, position_expiry_ms
                        )
                        if m is None or abs(m / entry - 1.0) > IMPLAUSIBLE:
                            cov["implausible_dropped"] += 1
                            return
                        if entry - stop <= 0:
                            cov["tier_bad_geometry"] += 1
                            return
                        rec.update(
                            {
                                "r_own": (m - entry) / (entry - stop),
                                "r_fixed": (m - entry) / risk_a,
                                "concession_pct": (entry - limit) / limit,
                                "concession_r": (entry - limit) / risk_a,
                                "cls": cls,
                                "open": hopen,
                            }
                        )
                    records.append(rec)

                # Variant A, no slippage: fill at limit at touch
                emit("A", None, "none", True, limit, t_idx)
                # Variant A, adverse: needs trade-through (low <= L - tick)
                thr_idx = first_touch(bars, limit - TICK, entry_expiry_ms)
                if thr_idx is not None:
                    emit("A", None, "adverse", True, limit, thr_idx)
                else:
                    emit("A", None, "adverse", False, None, None)
                # Variant B(d) x slippage
                for d in D_GRID:
                    trg = trail_trigger(bars, t_idx, d, entry_expiry_ms)
                    if trg is None:
                        emit("B", d, "none", False, None, None)
                        emit("B", d, "adverse", False, None, None)
                    else:
                        fi, fp = trg
                        emit("B", d, "none", True, fp, fi)
                        emit("B", d, "adverse", True, fp + TICK, fi)

    if cov["tier_touches"] < MIN_TOUCHES:
        print("## COVERAGE PROBLEM — study aborted")
        print(f"Only {cov['tier_touches']} tier touches (<{MIN_TOUCHES}). Coverage: {cov}")
        return 1

    rf = pd.DataFrame(records)
    rf.to_parquet(args.out)
    rc = pd.DataFrame(recon)

    # ------------------------------------------------------------------ report
    print("# What-if: trailing entry (B) vs hardcoded limit entry (A)")
    print()
    print(
        "**Engineering decision support — a re-cut of already-used data, NOT a "
        "pre-registered strategy test.** Direction-level only."
    )
    print()
    print("## Data coverage")
    for k, v in cov.items():
        print(f"- {k}: {v}")
    n_open = int(rf.loc[rf["filled"] == True, "open"].sum())  # noqa: E712
    print(f"- tier-entries still horizon-open at path end (marked at last close): {n_open}")
    print()

    def agg(sub: pd.DataFrame, universe_n: int) -> dict:
        f = sub[sub["filled"] == True]  # noqa: E712
        n_fill = len(f)
        out = {
            "N_universe": universe_n,
            "N_fill": n_fill,
            "fill_rate": n_fill / universe_n if universe_n else np.nan,
        }
        if n_fill:
            out.update(
                {
                    "meanR_own": f["r_own"].mean(),
                    "medR_own": f["r_own"].median(),
                    "meanR_fix": f["r_fixed"].mean(),
                    "medR_fix": f["r_fixed"].median(),
                    "win_rate": (f["r_own"] > 0).mean(),
                    # policy view: a missed tier = 0R (cash), fixed denominator
                    "meanR_policy_fix": f["r_fixed"].sum() / universe_n,
                    "conc_pct": f["concession_pct"].mean(),
                    "conc_R": f["concession_r"].mean(),
                }
            )
        else:
            out.update(
                dict.fromkeys(
                    [
                        "meanR_own",
                        "medR_own",
                        "meanR_fix",
                        "medR_fix",
                        "win_rate",
                        "meanR_policy_fix",
                        "conc_pct",
                        "conc_R",
                    ],
                    np.nan,
                )
            )
        return out

    cohorts: dict[str, pd.DataFrame] = {
        "ALL": rf,
        "day-1 touch": rf[rf["day_cohort"] == "day1"],
        "day-2+ touch": rf[rf["day_cohort"] == "day2plus"],
        "E1": rf[rf["tier"] == "E1"],
        "E2": rf[rf["tier"] == "E2"],
        "E3": rf[rf["tier"] == "E3"],
    }
    cols = [
        "config",
        "N_universe",
        "N_fill",
        "fill_rate",
        "meanR_own",
        "medR_own",
        "meanR_fix",
        "medR_fix",
        "win_rate",
        "meanR_policy_fix",
        "conc_pct",
        "conc_R",
    ]
    for name, sub in cohorts.items():
        # universe = distinct tier-touch events in this cohort (same for A and B)
        uni = len(sub[(sub["variant"] == "A") & (sub["slip"] == "none")])
        flag = "  **[N<15 — anecdotal]**" if uni < 15 else ""
        print(f"## Cohort: {name} (tier-touch universe N={uni}){flag}")
        rows = []
        for slip in ("none", "adverse"):
            a = sub[(sub["variant"] == "A") & (sub["slip"] == slip)]
            rows.append({"config": f"A slip={slip}", **agg(a, uni)})
        for d in D_GRID:
            for slip in ("none", "adverse"):
                b = sub[(sub["variant"] == "B") & (sub["d"] == d) & (sub["slip"] == slip)]
                rows.append({"config": f"B d={d:.1%} slip={slip}", **agg(b, uni)})
        tbl = pd.DataFrame(rows)[cols]
        for c in tbl.columns[1:]:
            tbl[c] = tbl[c].map(
                lambda x: (
                    f"{x:.3f}"
                    if isinstance(x, float) and not math.isnan(x)
                    else ("" if isinstance(x, float) else x)
                )
            )
        print(tbl.to_markdown(index=False))
        print()

    print("## Reconciliation vs RECORDED parquet outcomes (terminal rows)")
    if len(rc):
        rc2 = rc.dropna(subset=["replay_r"])
        match = (rc["stored_cls"] == rc["replay_cls"]).mean()
        print(f"- rows compared: {len(rc)} (both-numeric: {len(rc2)})")
        print(f"- classification match rate: {match:.1%}")
        if len(rc2) > 2:
            corr = np.corrcoef(rc2["stored_r"], rc2["replay_r"])[0, 1]
            mad = (rc2["stored_r"] - rc2["replay_r"]).abs().mean()
            big = (rc2["stored_r"] - rc2["replay_r"]).abs() > 0.05
            print(
                f"- Pearson r: {corr:.4f}; mean |diff|: {mad:.4f}; rows |diff|>0.05: {int(big.sum())}"
            )
            if big.any():
                print(rc2[big].head(15).to_markdown(index=False))
    else:
        print("- no terminal stored rows matched (unexpected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
