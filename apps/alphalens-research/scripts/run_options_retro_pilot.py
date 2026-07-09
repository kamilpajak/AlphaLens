#!/usr/bin/env python
"""Options retro exploratory pilot — ledger look ``options_retro_pilot_2026_07``.

Reconstructs the v9D options-implied stack (ivx30, term slope, hv20, ivp30)
from the immutable iVolatility smd deep cache as-of each banked brief date,
joins it to matured EDGE market-excess k=10 outcomes, and runs EXACTLY the
4-test family pinned in ``docs/research/options_retro_firstlook_design_
2026_07_09.md`` §6 (changing any spec re-opens the memo). Primary inference:
restricted wild cluster bootstrap over brief-day clusters; CR2 alongside.

Read-only research script; artifacts land under
``~/.alphalens/options_retro_pilot_2026_07/``.

    .venv/bin/python apps/alphalens-research/scripts/run_options_retro_pilot.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.data.alt_data.ivolatility_smd_cache import load_cached_smd
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
)
from alphalens_research.diagnostics import edge_stores, fixed_horizon
from alphalens_research.diagnostics.options_retro import (
    OPTIONS_RETRO_VERSION,
    cluster_ols,
    smd_features_asof,
    ticker_episode_dedup,
    vif_table,
    wild_cluster_bootstrap_p,
)

_SPY = "SPY"
_K = 10
_CONTROL_ATR = "technical_atr_pct"
_CONTROLS_FULL = (_CONTROL_ATR, "log10_mcap", "earnings_within_30d")
_CONTROLS_VRP = ("log10_mcap", "earnings_within_30d")  # NO ATR in the VRP test (memo §6)
_COVERAGE_HALT = 0.70
_BONFERRONI_ALPHA = 0.05 / 4.0

# The pinned family — memo §6, verbatim. (feature read, extra regressors, controls)
_TESTS = (
    ("ivx30_level", "ivx30", (), _CONTROLS_FULL),
    ("term_slope", "ivx180_minus_ivx30", (), _CONTROLS_FULL),
    ("vrp_decomposed", "ivx30", ("hv20",), _CONTROLS_VRP),
    ("ivp30", "ivp30", (), _CONTROLS_FULL),
)
_NEEDED_COLUMNS = ("ivx30", "ivx180_minus_ivx30", "hv20", "ivp30", *_CONTROLS_FULL)
_VIF_COLUMNS = ["ivx30", "ivp30", "ivx180_minus_ivx30", _CONTROL_ATR, "log10_mcap"]


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


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _earnings_within_30d(next_earnings: object, brief_date: dt.date) -> float:
    """1.0 if next_earnings_date falls within [brief_date, brief_date+30d]; missing -> 0."""
    if next_earnings is None or (isinstance(next_earnings, float) and math.isnan(next_earnings)):
        return 0.0
    try:
        d = dt.date.fromisoformat(str(next_earnings)[:10])
    except ValueError:
        return 0.0
    return 1.0 if brief_date <= d <= brief_date + dt.timedelta(days=30) else 0.0


def _build_panel(args: argparse.Namespace) -> pd.DataFrame:
    """Matured plannable pairs with car_10, briefs controls, and smd features."""
    outcomes = edge_stores.load_store(args.ladders_dir)
    if outcomes.empty or "plannable" not in outcomes.columns:
        raise SystemExit(f"no plannable population-ladder outcomes at {args.ladders_dir}")
    briefs = edge_stores.load_store(args.briefs_dir)
    grouped = edge_stores.GroupedDailyCache(args.grouped_root)
    newest = edge_stores.newest_session(args.grouped_root)
    if newest is None:
        raise SystemExit(f"empty grouped-daily store at {args.grouped_root}")

    briefs = briefs.assign(ticker=briefs["ticker"].astype(str).str.upper())
    brief_cols = ["technical_atr_pct", "market_cap", "next_earnings_date"]
    briefs_ix = briefs.set_index(["brief_date", "ticker"])[
        [c for c in brief_cols if c in briefs.columns]
    ]

    plannable = outcomes[outcomes["plannable"] == True].copy()  # noqa: E712
    smd_cache: dict[str, pd.DataFrame | None] = {}
    records: list[dict] = []
    for _, row in plannable.iterrows():
        brief_date = row["brief_date"]
        ticker = str(row["ticker"]).upper()
        arrival = session_on_or_after(brief_date, args.exchange)
        horizon = advance_trading_sessions(arrival, _K - 1, args.exchange)
        if horizon > newest:
            continue  # not matured
        anchor_session = previous_trading_day(arrival, args.exchange)
        car = fixed_horizon.car_for_event(
            stock_anchor=_close(grouped.get(anchor_session), ticker),
            stock_horizon=_close(grouped.get(horizon), ticker),
            spy_anchor=_close(grouped.get(anchor_session), _SPY),
            spy_horizon=_close(grouped.get(horizon), _SPY),
        )
        if car is None:
            continue

        rec: dict = {
            "brief_date": brief_date,
            "ticker": ticker,
            "car_10": car,
            "ladder_classification": str(row.get("ladder_classification") or ""),
            "realized_r": _num(row.get("realized_r")),
        }
        try:
            bc = briefs_ix.loc[(brief_date, ticker)]
            if isinstance(bc, pd.DataFrame):
                bc = bc.iloc[0]
        except KeyError:
            bc = None
        atr = _num(bc.get("technical_atr_pct")) if bc is not None else None
        mcap = _num(bc.get("market_cap")) if bc is not None else None
        rec[_CONTROL_ATR] = atr
        rec["log10_mcap"] = math.log10(mcap) if mcap is not None and mcap > 0 else None
        rec["earnings_within_30d"] = _earnings_within_30d(
            bc.get("next_earnings_date") if bc is not None else None, brief_date
        )

        if ticker not in smd_cache:
            smd_cache[ticker] = load_cached_smd(ticker, args.smd_cache)
        feats = smd_features_asof(smd_cache[ticker], brief_date)
        rec.update(feats or dict.fromkeys(("ivx30", "ivx180_minus_ivx30", "hv20", "ivp30")))
        records.append(rec)
    return pd.DataFrame.from_records(records)


def _run_family(frame: pd.DataFrame, y_col: str, *, n_boot: int, seed: int) -> list[dict]:
    """The pinned 4 tests on ``frame`` (already deduped, complete cases)."""
    results = []
    for name, feature, extras, controls in _TESTS:
        cols = [feature, *extras, *controls]
        sub = frame.dropna(subset=[y_col, *cols])
        y = sub[y_col].to_numpy(dtype=float)
        X = np.column_stack([np.ones(len(sub)), sub[cols].to_numpy(dtype=float)])
        clusters = sub["brief_date"].astype(str).to_numpy()
        res = cluster_ols(y, X, clusters)
        p_wcb = wild_cluster_bootstrap_p(y, X, clusters, coef_idx=1, n_boot=n_boot, seed=seed)
        feature_sd = float(sub[feature].std(ddof=1))
        results.append(
            {
                "test": name,
                "read_coefficient": feature,
                "n": len(sub),
                "n_clusters": res.n_clusters,
                "beta": float(res.beta[1]),
                "beta_per_1sd": float(res.beta[1] * feature_sd),
                "se_cr1": float(res.se_cr1[1]),
                "se_cr2": float(res.se_cr2[1]),
                "t_cr2": float(res.t_cr2[1]),
                "p_cr2": float(res.p_cr2[1]),
                "p_wild_cluster_bootstrap": p_wcb,
                "bonferroni_survivor": bool(p_wcb < _BONFERRONI_ALPHA),
            }
        )
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ladders-dir", type=Path, default=edge_stores.HOME / "population_ladders")
    ap.add_argument("--briefs-dir", type=Path, default=edge_stores.HOME / "thematic_briefs")
    ap.add_argument("--grouped-root", type=Path, default=rs_history.DEFAULT_RS_HISTORY_ROOT)
    ap.add_argument(
        "--smd-cache",
        type=Path,
        default=Path.home() / ".alphalens/ivolatility_smd_retro_2026_07_deep",
    )
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    ap.add_argument("--n-boot", type=int, default=9999)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--out-dir", type=Path, default=edge_stores.HOME / "options_retro_pilot_2026_07"
    )
    args = ap.parse_args()

    panel = _build_panel(args)
    if panel.empty:
        raise SystemExit("no matured pairs — nothing to analyse")

    covered = panel.dropna(subset=list(_NEEDED_COLUMNS))
    coverage = len(covered) / len(panel)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(args.out_dir / "panel.parquet", index=False)

    summary: dict = {
        "version": OPTIONS_RETRO_VERSION,
        "ledger_look": "options_retro_pilot_2026_07",
        "matured_pairs": len(panel),
        "covered_pairs": len(covered),
        "coverage": round(coverage, 4),
        "coverage_halt_threshold": _COVERAGE_HALT,
        "brief_dates": [str(panel["brief_date"].min()), str(panel["brief_date"].max())],
    }
    print(
        f"matured pairs: {len(panel)} | covered (all 4 tests computable): {len(covered)}"
        f" ({coverage:.1%}) | window {summary['brief_dates'][0]} .. {summary['brief_dates'][1]}"
    )
    if coverage < _COVERAGE_HALT:
        summary["verdict"] = "HALT_COVERAGE"
        (args.out_dir / "results.json").write_text(json.dumps(summary, indent=2))
        print(f"HALT: coverage {coverage:.1%} < {_COVERAGE_HALT:.0%} — no verdicts (memo §8)")
        return

    deduped = ticker_episode_dedup(covered, exchange=args.exchange)
    summary["episodes_after_dedup"] = len(deduped)
    summary["vif"] = {k: round(v, 2) for k, v in vif_table(deduped, _VIF_COLUMNS).items()}
    summary["tests_primary_car10"] = _run_family(
        deduped, "car_10", n_boot=args.n_boot, seed=args.seed
    )

    # Sub-window stability split (hint, not a test — memo §6).
    dates = deduped["brief_date"].sort_values()
    mid = dates.iloc[len(dates) // 2]
    halves = {}
    for label, half in (
        ("first_half", deduped[deduped["brief_date"] < mid]),
        ("second_half", deduped[deduped["brief_date"] >= mid]),
    ):
        halves[label] = [
            {k: r[k] for k in ("test", "n", "beta", "t_cr2")}
            for r in _run_family(half, "car_10", n_boot=999, seed=args.seed)
        ]
    summary["stability_split"] = {"split_date": str(mid), **halves}

    # Terminal realized_r — DESCRIPTIVE ONLY (memo §5/§6): reported, never tested.
    terminal = deduped[deduped["realized_r"].notna()]
    summary["descriptive_terminal_realized_r"] = {
        "n": len(terminal),
        "note": "fill-dependent subset; selection into fills confounds — no p-values, no verdicts",
        "tests": [
            {k: r[k] for k in ("test", "n", "beta", "se_cr2")}
            for r in _run_family(terminal, "realized_r", n_boot=999, seed=args.seed)
        ]
        if len(terminal) >= 30
        else [],
    }

    (args.out_dir / "results.json").write_text(json.dumps(summary, indent=2))
    print(f"episodes after dedup: {len(deduped)} | VIF: {summary['vif']}")
    for r in summary["tests_primary_car10"]:
        print(
            f"  {r['test']:>15}: beta={r['beta']:+.4f} (per 1sd {r['beta_per_1sd']:+.4f}) "
            f"t_cr2={r['t_cr2']:+.2f} p_wcb={r['p_wild_cluster_bootstrap']:.4f} "
            f"{'** SURVIVOR (Bonferroni/4)' if r['bonferroni_survivor'] else ''}"
        )
    print(f"wrote {args.out_dir / 'results.json'}")


if __name__ == "__main__":
    main()
