"""What-if study: TRAILING take-profit tranches vs FIXED TP targets.

ENGINEERING DECISION SUPPORT ONLY — another cut of already-used data, NOT a
pre-registered strategy test. Direction-level answer per cohort. Exit-side twin
of ``whatif_trailing_entry.py`` (2026-08-12), trailing direction flipped.

Design (binding, architect-approved 2026-08-18):
* Universe: every population-ladder candidate with a plannable brief_trade_setup,
  TP tranches, and a cached minute-bar path (the monitor's own cache — NO
  Polygon refetch).
* ENTRY side identical in both variants: per candidate x entry tier (E1/E2/E3),
  a synthetic fill at the tier limit at its first touch inside the entry window
  (entry_expiry_ms from the same _engine_cutoffs the monitor uses). The study
  varies ONLY the exit of each TP tranche.
* Variant A (baseline): the exit walk exactly as the repo does it today —
  alphalens_pipeline.feedback.ladder_replay._replay_synthetic_fill (fixed TP
  targets, disaster stop, SL-first ambiguity, TIME_STOP at position_expiry_ms).
* Variant B(d), per touched TP tranche: at the tranche target's FIRST touch, do
  NOT sell — arm a trailing sell for THAT tranche. Track the running HIGH from
  the touch bar onward; the tranche exits at the first later bar whose LOW
  reaches run_high*(1-d), at price run_high_prev*(1-d) (the trigger level is
  derived from highs up to the PREVIOUS bar — a resting stop can only know
  already-seen highs). A gap-open BELOW the level exits at the open. A tranche
  that trails past the NEXT target keeps trailing; the next tranche's own touch
  still arms its own independent trail.
* CONSERVATISM DOCTRINE (mirror of the entry study, direction flipped): every
  ambiguous minute-bar choice cuts AGAINST variant B —
  - same-bar retrace inside the touch bar (low <= high*(1-d), the broadest
    plausible trigger) DOES fire, at the WORST plausible fill: the trail is
    assumed armed at the touch itself (run_high == target) and the retrace to
    have happened right after, so the tranche exits at
    max(bar_low, target*(1-d)) — up to d BELOW the target — capped at the
    target so a gap-up bar (low > target) can never credit B above A's fill;
  - SL-first: a bar that pierces the disaster stop exits every remaining
    tranche (pending AND trailing) at the STOP, even when a trail level far
    above it was also crossable that bar (mirrors the repo TIE_BREAK_SL_FIRST;
    the harshest choice for B — auditable via the stop-path share);
  - the adverse-slippage view charges B +1 tick against EVERY trail-triggered
    sell (trail / gap-open / same-bar) while A stays un-penalised
    (resting-limit sells at the target keep the repo's touch-fill assumption).
* The disaster stop and TIME_STOP remain live for the un-exited remainder: a
  trailing-armed tranche that never retraces d% exits at the walk's time-stop
  mark (expiry-bar close) or at the stop if price falls there first; these
  paths are counted separately (they are where B can LOSE big). Bars exhausted
  before both -> remainder marked at last close (horizon-open), same as A.
* R views: own-denominator r = (M - e)/(e - stop) AND fixed-denominator
  r_fixed = (M - e)/(L_A - stop). Because the entry side is SHARED (e == L_A),
  the two coincide by construction here — both are still computed through the
  two separate formulas and dumped, so the discipline is auditable (a nonzero
  |r_own - r_fixed| would flag a re-denomination bug).
* Built-in parity guard: variant A is ALSO re-derived through this script's own
  tranche walk in fixed mode and compared against the repo engine's mark per
  touch (max |diff| reported) — proving the B walk shares the repo's exit
  semantics except for the trailing rule itself.
* d grid: 0.5% / 1% / 1.5% / 2% / 3%, plus ONE ATR-scaled config
  d_i = 0.25 * atr / entry (capped at 5%) when the setup carries a usable atr
  (no new plumbing; touches without atr are counted and skipped for that
  config only).
* Variant family "last-only" ("last:<config>", one twin per config above):
  ONLY the tranche whose target is the DEEPEST TP level arms a trail on ITS
  touch; every other tranche exits at its fixed target exactly as in A
  (identical per-tranche behavior and contribution to the weighted mark M).
  The trailing tranche obeys the SAME conservatism doctrine. Ladders with a
  single TP level degenerate to the all-tranche variant for that touch.
* Guards: no outcome-based selection; every exclusion counted (incl. the
  monitor population predicate); N<15 flagged; IMPLAUSIBLE split guard (0.60,
  mirror of the monitor's) applied at the TOUCH level, symmetric across
  variants: an implausible A drops the touch, and an implausible B under ANY
  config also drops the touch across ALL configs (per-config dropping would be
  outcome-based selection and would drift config Ns apart) — dropped B
  identities are printed for hand adjudication (split artifact vs genuine
  runner).
* Cohorts: day of the entry fill (day-1 vs day-2+), tier depth, ALL.

Read-only against ~/.alphalens stores; outputs go to /tmp.

Usage (VPS):
    /home/jacoren/AlphaLens/.venv/bin/python /tmp/whatif_trailing_tp.py \
        [--store ~/.alphalens/population_ladders] \
        [--briefs ~/.alphalens/thematic_briefs] [--limit N]
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from dataclasses import dataclass
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
ATR_K = 0.25  # ATR-scaled config: d_i = ATR_K * atr / entry
ATR_D_CAP = 0.05  # cap on the effective ATR-scaled d (entry-study addendum guard)
IMPLAUSIBLE = 0.60  # mirror bar_window.IMPLAUSIBLE_RETURN_THRESHOLD (raw split guard)
EXCHANGE = "XNYS"
MIN_TOUCHES = 50
PARITY_TOL = 1e-6

# Tranche path labels. "*_untouched" = the tranche's target was never touched, so
# it behaved identically under A and B (exits with the remainder).
TRAIL_PATHS = ("trail", "gap_open", "same_bar_target")
ARMED_PATHS = (*TRAIL_PATHS, "stop", "time_stop", "open_mark")


def _ts_date(ts_ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).date()


def first_touch(bars: list[dict], limit: float, entry_expiry_ms: int) -> int | None:
    """Index of the first bar (ts < expiry) with low <= limit; None if never."""
    for i, b in enumerate(bars):
        if int(b["t"]) >= entry_expiry_ms:
            return None
        if float(b["l"]) <= limit:
            return i
    return None


@dataclass
class _Tranche:
    tp_id: str
    target: float
    share: float
    trail_arm: bool = True  # False (last-only family, non-deepest) -> fixed exit as in A
    state: str = "PENDING"  # PENDING -> TRAILING -> EXITED
    run_high: float = math.nan  # running high through the PREVIOUS bar once TRAILING
    exit_price: float = math.nan
    path: str = ""
    trail_sell: bool = False  # B-specific trail-triggered sell (pays the adverse tick)

    def exit(self, price: float, path: str, *, trail_sell: bool = False) -> None:
        self.exit_price = price
        self.path = path
        self.trail_sell = trail_sell
        self.state = "EXITED"


@dataclass(frozen=True)
class _WalkResult:
    m_none: float  # weighted exit mark per share, no slippage
    m_adverse: float  # B's trail-triggered sells pay +1 tick against
    paths: tuple[str, ...]  # per tranche, ladder order
    horizon_open: bool


def tranche_specs(ladder) -> list[tuple[str, float, float]]:
    """(tp_id, target, share) per tranche; shares mirror _realized_r_with_frac at
    filled_frac == 1 (tranche_pct normalised by the sum; equal-weight fallback)."""
    tps = ladder.tps
    wsum = sum(t.weight for t in tps)
    if wsum > 0:
        return [(t.level_id, t.price, t.weight / wsum) for t in tps]
    return [(t.level_id, t.price, 1.0 / len(tps)) for t in tps]


def _step_trailing(live: list[_Tranche], o: float, lo: float, h: float, d: float) -> None:
    """Trail checks for tranches armed on a PRIOR bar: trigger vs the level from
    highs through the previous bar first, THEN update the running high."""
    for t in live:
        if t.state != "TRAILING":
            continue
        level = t.run_high * (1.0 - d)
        if o <= level:
            t.exit(o, "gap_open", trail_sell=True)  # gap-open below the level
        elif lo <= level:
            t.exit(level, "trail", trail_sell=True)
        else:
            t.run_high = max(t.run_high, h)


def _step_touches(trs: list[_Tranche], lo: float, h: float, d: float | None) -> None:
    """First touch of a pending tranche's target: fixed mode sells at the target;
    trailing mode arms the trail (or exits on a same-bar retrace at the worst
    plausible trail fill — conservative against B). A tranche with trail_arm
    False (last-only family, non-deepest) always takes the fixed exit."""
    for t in trs:
        if t.state != "PENDING" or h < t.target:
            continue
        if d is None or not t.trail_arm:
            t.exit(t.target, "target")
        elif lo <= h * (1.0 - d):
            # Worst plausible fill: the trail armed at the touch itself
            # (run_high == target) and the retrace happened right after, so the
            # tranche exits at max(bar_low, target*(1-d)) — up to d BELOW the
            # target. Capped at the target: a gap-up bar (lo > target) must not
            # credit B above A's fill. Cannot print below the stop — a bar with
            # lo <= stop already exited SL-first before touches were stepped.
            fill = min(max(lo, t.target * (1.0 - d)), t.target)
            t.exit(fill, "same_bar_target", trail_sell=True)
        else:
            t.state = "TRAILING"
            t.run_high = h


def exit_walk(
    specs: list[tuple[str, float, float]],
    bars: list[dict],
    stop: float,
    position_expiry_ms: int | None,
    d: float | None,
    *,
    last_only: bool = False,
) -> _WalkResult:
    """Per-tranche exit walk over post-fill bars.

    d=None -> FIXED exits at the target (repo-parity mode, must reproduce
    _replay_synthetic_fill's mark); d>0 -> trailing variant B. last_only=True
    arms the trail ONLY for the tranche(s) at the DEEPEST target — every other
    tranche exits fixed at its target, per-tranche identical to A. Bar order
    mirrors the repo _LadderWalk.step: SL first, then exits/touches, time-stop
    LAST (a real exit on the cutoff bar wins over the synthetic time-stop).
    """
    deepest = max(p for _tp_id, p, _s in specs) if last_only else None
    trs = [
        _Tranche(tp_id=i, target=p, share=s, trail_arm=deepest is None or p >= deepest)
        for i, p, s in specs
    ]
    last_close: float | None = None
    for b in bars:
        ts = int(b["t"])
        o, h, lo, cl = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        last_close = cl
        live = [t for t in trs if t.state != "EXITED"]
        if not live:
            break
        if lo <= stop:  # SL-first on any ambiguity (mirror TIE_BREAK_SL_FIRST)
            for t in live:
                t.exit(stop, "stop" if t.state == "TRAILING" else "stop_untouched")
            break
        if d is not None:
            _step_trailing(live, o, lo, h, d)
        _step_touches(trs, lo, h, d)
        if position_expiry_ms is not None and ts >= position_expiry_ms:
            for t in trs:
                if t.state != "EXITED":
                    t.exit(cl, "time_stop" if t.state == "TRAILING" else "time_stop_untouched")
            break
    horizon_open = False
    for t in trs:  # bars exhausted before exit: mark remainder at last close (repo)
        if t.state != "EXITED":
            assert last_close is not None
            t.exit(last_close, "open_mark" if t.state == "TRAILING" else "open_mark_untouched")
            horizon_open = True
    m_none = sum(t.share * t.exit_price for t in trs)
    m_adv = sum(t.share * (t.exit_price - (TICK if t.trail_sell else 0.0)) for t in trs)
    return _WalkResult(m_none, m_adv, tuple(t.path for t in trs), horizon_open)


def path_counts(paths: tuple[str, ...]) -> dict[str, int]:
    return {
        "n_tp": len(paths),
        "n_armed": sum(p in ARMED_PATHS for p in paths),
        "n_trail": sum(p in ("trail", "gap_open") for p in paths),
        "n_same_bar": sum(p == "same_bar_target" for p in paths),
        "n_stop_armed": sum(p == "stop" for p in paths),
        "n_timestop_armed": sum(p == "time_stop" for p in paths),
        "n_open_armed": sum(p == "open_mark" for p in paths),
    }


def build_configs(ladder) -> list[tuple[str, float | None]]:
    """(config_label, d) pairs; the ATR config maps to None when atr is unusable."""
    configs: list[tuple[str, float | None]] = [(f"d={d:.1%}", d) for d in D_GRID]
    atr = ladder.atr
    configs.append(("atr_k0.25", atr if atr is not None and math.isfinite(atr) else None))
    return configs


def atr_d_for(limit: float, atr: float | None) -> float | None:
    if atr is None or not math.isfinite(atr) or atr <= 0 or limit <= 0:
        return None
    return min(ATR_K * atr / limit, ATR_D_CAP)


def study_touch(
    setup: dict,
    ladder,
    bars: list[dict],
    t_idx: int,
    limit: float,
    stop: float,
    position_expiry_ms: int,
    base: dict,
    cov: dict,
    parity: dict,
    dropped_b: list[dict],
) -> list[dict]:
    """All per-config records for one tier touch (shared synthetic entry)."""
    touch_ts = int(bars[t_idx]["t"])
    post_fill = [b for b in bars if int(b["t"]) >= touch_ts]

    # ---- Variant A: the repo exit walk, exactly as production runs it today.
    out = _replay_synthetic_fill(
        setup,
        bars,
        fill_price=limit,
        fill_ts_ms=touch_ts,
        own_stop=stop,
        position_expiry_ms=position_expiry_ms,
    )
    if out.status != "OK" or out.realized_r is None or out.blended_entry is None:
        cov["a_walk_failed"] += 1
        return []
    m_a = out.blended_entry + out.realized_r * (out.blended_entry - stop)
    if abs(m_a / limit - 1.0) > IMPLAUSIBLE:
        cov["implausible_a_dropped"] += 1
        return []
    risk_a = limit - stop
    r_a = (m_a - limit) / risk_a

    specs = tranche_specs(ladder)

    # ---- Parity guard: fixed-mode own walk must reproduce the repo mark.
    par = exit_walk(specs, post_fill, stop, position_expiry_ms, d=None)
    diff = abs(par.m_none - m_a)
    parity["checked"] += 1
    parity["max_diff"] = max(parity["max_diff"], diff)
    if diff > PARITY_TOL:
        parity["mismatch"] += 1

    # ---- Variant B per config. The implausible guard is applied at the TOUCH
    # level, symmetric with the A-side guard: dropping per-config on B's own
    # realized outcome would be outcome-based selection (it would censor
    # exactly the big runners where trailing wins) and would drift config Ns
    # apart. Instead: walk ALL configs first; if ANY config's mark is
    # implausible, drop the touch across ALL configs and record its identity
    # for the report (hand adjudication: split artifact vs genuine runner).
    walks: list[tuple[str, float, _WalkResult]] = []
    base_cfgs = build_configs(ladder)
    # Each base config gets a "last:<config>" twin (last-only family): same d,
    # trail armed only for the deepest-target tranche. The B-implausible guard
    # below spans BOTH families (a config is a config).
    for last_only in (False, True):
        for base_label, d_raw in base_cfgs:
            label = f"last:{base_label}" if last_only else base_label
            if base_label.startswith("atr"):
                d = atr_d_for(limit, d_raw)
                if d is None:
                    if not last_only:  # count the TOUCH once, not once per family
                        cov["atr_missing_touches"] += 1
                    continue
            else:
                d = d_raw
            assert d is not None
            walk = exit_walk(specs, post_fill, stop, position_expiry_ms, d=d, last_only=last_only)
            walks.append((label, d, walk))
    bad = [
        (label, res.m_none / limit - 1.0)
        for label, _d, res in walks
        if abs(res.m_none / limit - 1.0) > IMPLAUSIBLE
    ]
    if bad:
        cov["implausible_b_touch_dropped"] += 1
        dropped_b.append(
            {**base, "configs": "; ".join(f"{lbl} ret={ret:+.1%}" for lbl, ret in bad)}
        )
        return []

    records: list[dict] = []
    for label, d, res in walks:
        r_b_none = (res.m_none - limit) / risk_a
        r_b_adv = (res.m_adverse - limit) / risk_a
        records.append(
            {
                **base,
                "config": label,
                "d_eff": d,
                "r_a": r_a,
                "r_b_none": r_b_none,
                "r_b_adv": r_b_adv,
                # own-denominator (entry - stop) and fixed-denominator (A's risk
                # unit): identical here BY CONSTRUCTION (shared entry) — both
                # computed via their own formulas so the discipline is auditable.
                "d_own_none": (res.m_none - limit) / (limit - stop) - r_a,
                "d_own_adv": (res.m_adverse - limit) / (limit - stop) - r_a,
                "d_fix_none": r_b_none - r_a,
                "d_fix_adv": r_b_adv - r_a,
                "a_cls": out.classification,
                "a_open": out.horizon_open,
                "b_open": res.horizon_open,
                **path_counts(res.paths),
            }
        )
    return records


def process_candidate(
    c,
    date: dt.date,
    store: Path,
    cov: dict,
    parity: dict,
    records: list[dict],
    recon: list[dict],
    stored_by_ticker: dict,
    dropped_b: list[dict],
) -> None:
    ladder = parse_ladder(c.trade_setup)
    cov["candidates"] += 1
    if not ladder.ok:
        cov["not_plannable"] += 1
        return
    if not ladder.tps:
        # No TP tranches -> nothing to trail; A == B identically. Structural
        # exclusion (counted), NOT outcome-based.
        cov["no_tp_tranches"] += 1
        return
    setup = c.trade_setup
    cut = _engine_cutoffs(date, setup, EXCHANGE)
    (arrival, _ee_sess, pos_sess, _et, _pt, entry_expiry_ms, position_expiry_ms) = cut
    raw = _read_cached_bars(store, c.ticker, arrival)
    if not raw:
        cov["no_bar_cache"] += 1
        return
    bars = _filter_bars_to_rth(raw, arrival, pos_sess, EXCHANGE)
    bars = sorted(bars, key=lambda b: int(b["t"]))
    if not bars:
        cov["empty_after_rth"] += 1
        return
    if int(bars[-1]["t"]) < entry_expiry_ms:
        cov["entry_window_truncated"] += 1  # processed anyway; touch may predate
    stop = ladder.disaster_stop
    assert stop is not None

    # ---- reconciliation: full as-specified ladder replay vs stored row
    srow = stored_by_ticker.get(c.ticker.upper())
    if srow is not None and bool(srow.get("terminal")) and pd.notna(srow.get("realized_r")):
        full = replay_ladder(
            setup, bars, entry_expiry_ms=entry_expiry_ms, position_expiry_ms=position_expiry_ms
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

    # ---- per-tier study (entry shared across variants; exits vary)
    for tier_i, lvl in enumerate(ladder.entries):
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
        base = {
            "date": date,
            "ticker": c.ticker,
            "tier": f"E{tier_i + 1}",
            "day_cohort": "day1" if day_n == 0 else "day2plus",
            "limit": limit,
            "stop": stop,
        }
        records.extend(
            study_touch(
                setup,
                ladder,
                bars,
                t_idx,
                limit,
                stop,
                position_expiry_ms,
                base,
                cov,
                parity,
                dropped_b,
            )
        )


def agg_row(sub: pd.DataFrame) -> dict:
    n = len(sub)
    out: dict = {"N": n}
    if not n:
        return out
    armed = int(sub["n_armed"].sum())
    out.update(
        {
            "meanR_A": sub["r_a"].mean(),
            "meanR_B_none": sub["r_b_none"].mean(),
            "meanR_B_adv": sub["r_b_adv"].mean(),
            "mean_dR_own_none": sub["d_own_none"].mean(),
            "mean_dR_own_adv": sub["d_own_adv"].mean(),
            "mean_dR_fix_none": sub["d_fix_none"].mean(),
            "mean_dR_fix_adv": sub["d_fix_adv"].mean(),
            "med_dR_none": sub["d_own_none"].median(),
            "pct_B_wins": (sub["d_own_none"] > 0).mean(),
            "pct_ties": (sub["d_own_none"] == 0).mean(),
            "armed_tranches": armed,
            "pct_trail": sub["n_trail"].sum() / armed if armed else np.nan,
            "pct_same_bar": sub["n_same_bar"].sum() / armed if armed else np.nan,
            "pct_stop": sub["n_stop_armed"].sum() / armed if armed else np.nan,
            "pct_timestop": sub["n_timestop_armed"].sum() / armed if armed else np.nan,
            "pct_open": sub["n_open_armed"].sum() / armed if armed else np.nan,
        }
    )
    return out


_BASE_CONFIG_ORDER = [f"d={d:.1%}" for d in D_GRID] + ["atr_k0.25"]
CONFIG_ORDER = _BASE_CONFIG_ORDER + [f"last:{c}" for c in _BASE_CONFIG_ORDER]
TABLE_COLS = [
    "config",
    "N",
    "meanR_A",
    "meanR_B_none",
    "meanR_B_adv",
    "mean_dR_own_none",
    "mean_dR_own_adv",
    "mean_dR_fix_none",
    "mean_dR_fix_adv",
    "med_dR_none",
    "pct_B_wins",
    "pct_ties",
    "armed_tranches",
    "pct_trail",
    "pct_same_bar",
    "pct_stop",
    "pct_timestop",
    "pct_open",
]


def _render_table(tbl: pd.DataFrame) -> str:
    """Markdown when tabulate is available; plain to_string fallback otherwise."""
    try:
        return tbl.to_markdown(index=False)
    except ImportError:
        return tbl.to_string(index=False)


def _fmt_num_cols(tbl: pd.DataFrame, skip: int) -> pd.DataFrame:
    """Format float cells to 3dp in place (NaN -> blank), skipping label columns."""
    for c in tbl.columns[skip:]:
        tbl[c] = tbl[c].map(
            lambda x: (
                f"{x:.3f}"
                if isinstance(x, float) and not math.isnan(x)
                else ("" if isinstance(x, float) else x)
            )
        )
    return tbl


def print_report(
    rf: pd.DataFrame,
    rc: pd.DataFrame,
    cov: dict,
    parity: dict,
    tsv: str,
    dropped_b: list[dict],
) -> None:
    print("# What-if: trailing TP tranches (B) vs fixed TP targets (A)")
    print()
    print(
        "**Engineering decision support — a re-cut of already-used data, NOT a "
        "pre-registered strategy test.** Direction-level only. Entry side is "
        "SHARED (fill at tier limit at touch), so own- and fixed-denominator "
        "deltas coincide by construction; both are computed independently as an "
        "internal check."
    )
    print()
    print("## Data coverage")
    for k, v in cov.items():
        print(f"- {k}: {v}")
    n_b_open = int(rf["b_open"].sum())
    print(f"- B tier-entries horizon-open at path end (marked at last close): {n_b_open}")
    print()
    if dropped_b:
        print("## B-implausible touches DROPPED across all configs — adjudicate by hand")
        print("(guard is symmetric with the A-side touch drop; listed so genuine >60%")
        print("runners can be told apart from split artifacts)")
        for row in dropped_b:
            print(f"- {row['date']} {row['ticker']} {row['tier']}: {row['configs']}")
        print()
    print("## Parity guard (fixed-mode own walk vs repo _replay_synthetic_fill)")
    print(
        f"- touches checked: {parity['checked']}; max |M_walk - M_repo|: "
        f"{parity['max_diff']:.2e}; mismatches > {PARITY_TOL:.0e}: {parity['mismatch']}"
    )
    if parity["mismatch"]:
        print("- **WARNING: the B walk does NOT share the repo exit semantics — fix first.**")
    denom_gap = (rf["d_own_none"] - rf["d_fix_none"]).abs().max()
    print(f"- max |dR_own - dR_fixed| (must be ~0, shared entry): {denom_gap:.2e}")
    print()

    cohorts: dict[str, pd.DataFrame] = {
        "ALL": rf,
        "day-1 fill": rf[rf["day_cohort"] == "day1"],
        "day-2+ fill": rf[rf["day_cohort"] == "day2plus"],
        "E1": rf[rf["tier"] == "E1"],
        "E2": rf[rf["tier"] == "E2"],
        "E3": rf[rf["tier"] == "E3"],
    }
    tsv_rows: list[dict] = []
    for name, sub in cohorts.items():
        uni = sub[["date", "ticker", "tier"]].drop_duplicates().shape[0]
        flag = "  **[N<15 — anecdotal]**" if uni < 15 else ""
        print(f"## Cohort: {name} (tier-touch universe N={uni}){flag}")
        rows = []
        for label in CONFIG_ORDER:
            row = {"config": label, **agg_row(sub[sub["config"] == label])}
            rows.append(row)
            tsv_rows.append({"cohort": name, **row})
        tbl = _fmt_num_cols(pd.DataFrame(rows).reindex(columns=TABLE_COLS), skip=1)
        print(_render_table(tbl))
        med_atr_d = sub.loc[sub["config"] == "atr_k0.25", "d_eff"].median()
        if not math.isnan(med_atr_d):
            print(f"(atr_k0.25 median effective d: {med_atr_d:.2%})")
        print()

    pd.DataFrame(tsv_rows).to_csv(tsv, sep="\t", index=False)
    print(f"Aggregate table dumped to {tsv}")
    print()

    print("## Path-class cut: groupby(a_cls, config) — decision-relevant view")
    print("(a_cls is the BASELINE A walk's classification, shared by both variants")
    print("of a touch — conditioning on it is NOT outcome-based selection on B)")
    cls_rows: list[dict] = []
    for a_cls in sorted(rf["a_cls"].dropna().unique()):
        sub_cls = rf[rf["a_cls"] == a_cls]
        for label in CONFIG_ORDER:
            sub = sub_cls[sub_cls["config"] == label]
            row: dict = {"a_cls": a_cls, "config": label, "N": len(sub)}
            if len(sub):
                row.update(
                    {
                        "meanR_A": sub["r_a"].mean(),
                        "mean_dR_own_none": sub["d_own_none"].mean(),
                        "median_dR": sub["d_own_none"].median(),
                        "pct_B_wins": (sub["d_own_none"] > 0).mean(),
                    }
                )
            cls_rows.append(row)
    cls_cols = ["a_cls", "config", "N", "meanR_A", "mean_dR_own_none", "median_dR", "pct_B_wins"]
    cls_tbl = _fmt_num_cols(pd.DataFrame(cls_rows).reindex(columns=cls_cols), skip=2)
    print(_render_table(cls_tbl))
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
                print(_render_table(rc2[big].head(15)))
    else:
        print("- no terminal stored rows matched (unexpected)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=str(Path.home() / ".alphalens" / "population_ladders"))
    ap.add_argument("--briefs", default=str(Path.home() / ".alphalens" / "thematic_briefs"))
    ap.add_argument("--out", default="/tmp/whatif_trailing_tp_records.parquet")
    ap.add_argument("--tsv", default="/tmp/whatif_trailing_tp_results.tsv")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap plannable candidates processed (smoke / correctness pass)",
    )
    args = ap.parse_args()
    store = Path(args.store)
    briefs_dir = Path(args.briefs)

    dates = sorted(dt.date.fromisoformat(p.stem) for p in store.glob("????-??-??.parquet"))
    cov = {
        "dates_total": len(dates),
        "dates_no_brief": 0,
        "not_in_population": 0,
        "candidates": 0,
        "not_plannable": 0,
        "no_tp_tranches": 0,
        "no_bar_cache": 0,
        "empty_after_rth": 0,
        "entry_window_truncated": 0,
        "tier_bad_geometry": 0,
        "tier_no_touch": 0,
        "tier_touches": 0,
        "a_walk_failed": 0,
        "implausible_a_dropped": 0,
        "implausible_b_touch_dropped": 0,
        "atr_missing_touches": 0,
    }
    parity = {"checked": 0, "mismatch": 0, "max_diff": 0.0}
    records: list[dict] = []
    recon: list[dict] = []
    dropped_b: list[dict] = []
    processed = 0

    for date in dates:
        if args.limit is not None and processed >= args.limit:
            break
        try:
            candidates = load_brief(date, briefs_dir)
        except Exception:
            cov["dates_no_brief"] += 1
            continue
        stored = pd.read_parquet(store / f"{date.isoformat()}.parquet")
        stored_by_ticker = {str(r["ticker"]).upper(): r for _, r in stored.iterrows()}
        for c in candidates:
            if args.limit is not None and processed >= args.limit:
                break
            if not c.verified or c.trade_setup is None:
                cov["not_in_population"] += 1  # mirrors the monitor's population predicate
                continue
            process_candidate(
                c, date, store, cov, parity, records, recon, stored_by_ticker, dropped_b
            )
            processed += 1

    if cov["tier_touches"] < MIN_TOUCHES and args.limit is None:
        print("## COVERAGE PROBLEM — study aborted")
        print(f"Only {cov['tier_touches']} tier touches (<{MIN_TOUCHES}). Coverage: {cov}")
        return 1
    if not records:
        print("## No records produced — nothing to report. Coverage:", cov)
        return 1

    rf = pd.DataFrame(records)
    rf.to_parquet(args.out)
    rc = pd.DataFrame(recon)
    print_report(rf, rc, cov, parity, args.tsv, dropped_b)
    print(f"\nRecords parquet: {args.out} ({len(rf)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
