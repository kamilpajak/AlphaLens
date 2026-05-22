"""v10 — drawdown-control L4 overlay on FROZEN v9D options-implied base.

Pre-registered as ``v10_drawdown_overlay_on_v9D_options_2026_05_04`` in
signal class ``risk_management_overlay_2026_04_30`` (overlay-class test
#2 after vol-target). Per ADR 0007, primary success metric is
Sharpe-improvement, NOT Carhart αt.

Design:
- BASE = v9D cross-sectional residual scorer (FROZEN — same code path as
  ``scripts/experiment_v9_cross_sectional_residual.py``; this script does
  not touch the scorer module).
- OVERLAY = ``DrawdownControlOverlay`` (de-lever-only, cap=1.0, floor=0.0,
  step function on rolling-equity drawdown, T+1 execution).
- Sharpe-diff inference: Ledoit-Wolf-style paired circular block-bootstrap
  on (overlay_net − base_net), block_size=21d, n_bootstrap=10000.

Outputs:
- ``--out-json`` per-phase JSON capturing base + overlay raw return series
  (so a multi-phase pooler can run a single bootstrap on concatenated
  data without re-loading SMD cache).
- A canonical log line in the format the existing ``audit_multi_phase``
  regex parses (one per phase, BASE row), preserving the multi-phase
  driver's group-by-config aggregation.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import yaml
from alphalens_research.attribution.cost_model import CostModel
from alphalens_research.attribution.factor_analysis import run_regression
from alphalens_research.backtest.metrics import max_drawdown, sharpe
from alphalens_research.backtest.sharpe_inference import block_bootstrap_sharpe_diff
from alphalens_research.data.alt_data.ivolatility_smd_cache import load_cached_smd
from alphalens_research.data.factors import load_carhart_daily
from alphalens_research.overlays import (
    DrawdownControlConfig,
    DrawdownControlOverlay,
    apply_drawdown_control,
)
from alphalens_research.screeners.options_implied import (
    DEFAULT_HOLDING,
    build_feature_frame,
    load_delisting_events_index,
    score_cross_sectional_residual,
)
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SMD_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
ETFS = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD")
CARHART_COLS = ["Mkt-RF", "SMB", "HML", "Mom"]


def _pit_union(start_year: int = 2018) -> list[str]:
    union: set[str] = set()
    for p in sorted(PIT_DIR.glob("*.yaml")):
        try:
            snap_year = int(p.stem.split("-")[0])
        except ValueError:
            continue
        if snap_year < start_year:
            continue
        data = yaml.safe_load(p.read_text()) or {}
        for t in data.get("tickers", []):
            union.add(str(t).upper())
    union |= set(ETFS)
    return sorted(union)


_SMD_CACHE: dict[str, pd.DataFrame | None] = {}


def _smd_loader(ticker: str) -> pd.DataFrame | None:
    key = ticker.upper()
    if key not in _SMD_CACHE:
        _SMD_CACHE[key] = load_cached_smd(key, SMD_CACHE_DIR)
    return _SMD_CACHE[key]


def _benchmark_calendar(
    benchmark: str, start: date, end: date, stride: int, phase_offset: int
) -> list[date]:
    df = load_cached_smd(benchmark, SMD_CACHE_DIR)
    if df is None or df.empty:
        raise RuntimeError(f"benchmark {benchmark!r} not in smd cache {SMD_CACHE_DIR}")
    if "ivp30" in df.columns:
        df = df.loc[df["ivp30"].notna()]
    df = df.sort_values("tradeDate")
    dates = pd.to_datetime(df["tradeDate"])
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    sliced = sorted(set(dates.loc[mask].dt.date.tolist()))
    if not 0 <= phase_offset < stride:
        raise ValueError(f"phase_offset must satisfy 0 <= offset < {stride}")
    return sliced[phase_offset::stride]


def _portfolio_returns(
    feat_holdout: pd.DataFrame,
    scores: pd.Series,
    *,
    decile_pct: float = 0.1,
    delisting_events: dict | None = None,
) -> tuple[pd.Series, pd.DatetimeIndex, pd.Series]:
    """Long-only top-decile returns. Same code path as v9D `_portfolio_returns`
    but returns only the long leg + asof index + decile sizes (we don't need
    L/S spread for overlay-class success metric)."""
    holdout = feat_holdout.assign(_score=scores).dropna(subset=["_score"])
    asof_dates = sorted(holdout["asof"].unique())

    long_rets, indices, sizes = [], [], []
    for asof in asof_dates:
        slice_df = holdout.loc[holdout["asof"] == asof]
        n = len(slice_df)
        decile_size = max(1, round(n * decile_pct))
        if n < 2 * decile_size:
            continue

        ranked = slice_df.sort_values("_score", ascending=False)
        top = ranked.head(decile_size)["ticker"].tolist()

        from alphalens_research.screeners.options_implied.target import forward_raw_return

        top_rets = [
            forward_raw_return(
                _smd_loader, t, asof, holding_period=1, delisting_events=delisting_events
            )
            for t in top
        ]
        top_arr = np.array([r if r is not None else np.nan for r in top_rets], dtype=float)
        if np.all(np.isnan(top_arr)):
            continue
        long_rets.append(float(np.nanmean(top_arr)))
        indices.append(asof)
        sizes.append(decile_size)

    if not indices:
        empty = pd.Series(dtype=float)
        return empty, pd.DatetimeIndex([]), empty
    asof_idx = pd.DatetimeIndex(pd.to_datetime(indices))
    return (
        pd.Series(long_rets, index=asof_idx, name="long_return"),
        asof_idx,
        pd.Series(sizes, index=asof_idx, name="decile_size"),
    )


def _benchmark_holding_returns(asof_index: pd.DatetimeIndex, benchmark: str) -> pd.Series:
    from alphalens_research.screeners.options_implied.target import forward_raw_return

    rets = []
    for asof in asof_index:
        r = forward_raw_return(_smd_loader, benchmark, asof, holding_period=1)
        rets.append(np.nan if r is None else r)
    return pd.Series(rets, index=asof_index, dtype=float, name="benchmark_return")


def _stats(
    portfolio_returns: pd.Series,
    bench_returns: pd.Series,
    carhart: pd.DataFrame,
    *,
    rebalance_stride: int,
    cost_drag_per_period: float,
    label: str,
) -> dict:
    rets = portfolio_returns.dropna()
    if rets.empty:
        return {"n": 0, "label": label}

    rebalances_per_year = 252 / max(1, rebalance_stride)
    rets_net = rets - cost_drag_per_period
    sharpe_gross = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))
    sharpe_net = sharpe(rets_net.tolist(), periods_per_year=int(rebalances_per_year))

    res4 = run_regression(rets, carhart[[*CARHART_COLS, "RF"]], CARHART_COLS)

    bench_aligned = bench_returns.reindex(rets.index).dropna()
    excess_per_rebal = (rets.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann_gross = (
        float(excess_per_rebal * rebalances_per_year)
        if not math.isnan(excess_per_rebal)
        else float("nan")
    )
    drag_ann = cost_drag_per_period * rebalances_per_year

    cum = (1 + rets_net.fillna(0)).cumprod()
    mdd = float(max_drawdown(cum.tolist()))

    return {
        "label": label,
        "n": len(rets),
        "sharpe_gross": float(sharpe_gross),
        "sharpe_net": float(sharpe_net),
        "alpha_gross_4f": float(res4.alpha_annualized),
        "alpha_t_4f": float(res4.alpha_tstat),
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "excess_vs_bench_ann_gross": excess_ann_gross,
        "excess_vs_bench_ann_net": excess_ann_gross - drag_ann,
        "max_drawdown_net": mdd,
        "cost_drag_ann": drag_ann,
    }


def _evaluate_v10_gates(
    base_stats: dict,
    overlay_stats: dict,
    sharpe_diff,
    base_consistency_target_alpha_t: float,
    base_consistency_tolerance: float,
    weight_min: float,
    weight_max: float,
) -> dict:
    """Apply the six pre-registered gates to single-phase results.

    Phase-dispersion gate (G6) cannot be evaluated within a single phase;
    it lives in the multi-phase aggregator. G1..G5 are evaluable here.
    """
    g1_t_threshold = 2.5
    g1_p_threshold = 0.01
    g2_sharpe_improvement_threshold = 0.30
    g3_maxdd_reduction_relative = 0.7

    sharpe_improvement = overlay_stats.get("sharpe_net", 0.0) - base_stats.get("sharpe_net", 0.0)
    base_mdd = base_stats.get("max_drawdown_net", 0.0)
    overlay_mdd = overlay_stats.get("max_drawdown_net", 0.0)
    base_alpha_t = base_stats.get("alpha_t_4f", 0.0)

    g1 = sharpe_diff.t_stat >= g1_t_threshold and sharpe_diff.p_value_one_sided < g1_p_threshold
    g2 = sharpe_improvement >= g2_sharpe_improvement_threshold
    # MaxDD is reported as a negative drawdown magnitude (most negative = worst). Compare absolute values.
    g3 = abs(overlay_mdd) <= g3_maxdd_reduction_relative * abs(base_mdd) if base_mdd != 0 else False
    g4 = abs(base_alpha_t - base_consistency_target_alpha_t) <= base_consistency_tolerance
    g5 = (weight_min >= 0.0 - 1e-12) and (weight_max <= 1.0 + 1e-12)

    return {
        "g1_sharpe_diff_significance": {
            "rule": f"t≥{g1_t_threshold} AND p<{g1_p_threshold}",
            "t_stat": float(sharpe_diff.t_stat),
            "p_value_one_sided": float(sharpe_diff.p_value_one_sided),
            "pass": bool(g1),
        },
        "g2_sharpe_improvement_magnitude": {
            "rule": f"Δ Sharpe ≥ {g2_sharpe_improvement_threshold}",
            "sharpe_improvement": float(sharpe_improvement),
            "pass": bool(g2),
        },
        "g3_maxdd_reduction": {
            "rule": f"|MaxDD_overlay| ≤ {g3_maxdd_reduction_relative} × |MaxDD_base|",
            "base_max_drawdown": float(base_mdd),
            "overlay_max_drawdown": float(overlay_mdd),
            "ratio": float(abs(overlay_mdd) / abs(base_mdd)) if base_mdd != 0 else float("nan"),
            "pass": bool(g3),
        },
        "g4_base_consistency": {
            "rule": (
                f"|base αt − {base_consistency_target_alpha_t}| ≤ {base_consistency_tolerance}"
            ),
            "base_alpha_t_observed": float(base_alpha_t),
            "target_alpha_t": float(base_consistency_target_alpha_t),
            "tolerance": float(base_consistency_tolerance),
            "pass": bool(g4),
        },
        "g5_weight_bound_invariant": {
            "rule": "weight ∈ [0.0, 1.0] at every t",
            "weight_min": float(weight_min),
            "weight_max": float(weight_max),
            "pass": bool(g5),
        },
        "single_phase_all_pass": bool(g1 and g2 and g3 and g4 and g5),
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--holdout-start", type=date.fromisoformat, default=date(2024, 4, 30))
    ap.add_argument("--holdout-end", type=date.fromisoformat, default=date(2026, 4, 30))
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument("--holding", type=int, default=DEFAULT_HOLDING)
    ap.add_argument("--decile-pct", type=float, default=0.1)
    ap.add_argument("--benchmark", default="MDY")
    ap.add_argument("--adv-min-usd", type=float, default=2_000_000.0)
    ap.add_argument("--cost-bps-rt", type=float, default=30.0)

    ap.add_argument("--dd-light", type=float, default=0.05)
    ap.add_argument("--dd-heavy", type=float, default=0.10)
    ap.add_argument("--dd-half-weight", type=float, default=0.5)
    ap.add_argument("--dd-recovery-band", type=float, default=0.02)
    ap.add_argument(
        "--dd-peak-lookback",
        type=int,
        default=0,
        help="0 = expanding peak (since-inception max); >0 = rolling window in periods.",
    )

    ap.add_argument("--bootstrap-block-size", type=int, default=21)
    ap.add_argument("--bootstrap-n", type=int, default=10000)
    ap.add_argument("--bootstrap-seed", type=int, default=0)

    ap.add_argument(
        "--base-target-alpha-t",
        type=float,
        default=2.29,
        help="Pre-reg target αt for v9D base in this holdout (G4 gate target).",
    )
    ap.add_argument("--base-tolerance", type=float, default=0.5)

    ap.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v10_drawdown_overlay_holdout.md",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v10_drawdown_overlay_holdout.json",
    )
    ap.add_argument("--max-tickers", type=int, default=None)
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = _build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    universe = _pit_union()
    if args.max_tickers:
        universe = universe[: args.max_tickers]
    logger.info("Universe: %d tickers", len(universe))

    asofs = _benchmark_calendar(
        args.benchmark,
        args.holdout_start,
        args.holdout_end,
        args.rebalance_stride,
        args.phase_offset,
    )
    if not asofs:
        logger.error("Empty holdout calendar")
        return 1
    logger.info(
        "Calendar: %d asofs (stride=%d, phase=%d) %s..%s",
        len(asofs),
        args.rebalance_stride,
        args.phase_offset,
        asofs[0],
        asofs[-1],
    )

    ff_start = args.holdout_start - timedelta(days=400)
    carhart = load_carhart_daily(start=ff_start, end=args.holdout_end)

    t0 = time.time()
    asof_strs = [d.isoformat() for d in asofs]
    features = build_feature_frame(
        smd_loader=_smd_loader,
        universe=universe,
        asof_dates=asof_strs,
        adv_min_dollar=args.adv_min_usd,
    )
    logger.info(
        "Feature frame: %d rows × %d cols in %.1fs",
        len(features),
        len(features.columns),
        time.time() - t0,
    )
    if features.empty:
        logger.error("Empty feature frame — abort")
        return 1

    scores = score_cross_sectional_residual(features)
    n_scored = int(scores.notna().sum())
    coverage = n_scored / max(1, len(features))
    logger.info("Scored: %d / %d (%.1f%% coverage)", n_scored, len(features), coverage * 100)

    delisting_events = load_delisting_events_index(SURVIVORSHIP_PARQUET)
    long_rets, asof_idx, decile_sizes = _portfolio_returns(
        features,
        scores,
        decile_pct=args.decile_pct,
        delisting_events=delisting_events,
    )
    if long_rets.empty:
        logger.error("Empty holdout portfolio — abort")
        return 1
    bench_rets = _benchmark_holding_returns(asof_idx, args.benchmark)

    cost_long_only = CostModel.from_profile("long_only_30bps")
    drag_long_only = cost_long_only.annual_drag_bps / 10_000.0 / (252 / args.rebalance_stride)

    overlay_cfg = DrawdownControlConfig(
        light_dd=args.dd_light,
        heavy_dd=args.dd_heavy,
        half_weight=args.dd_half_weight,
        recovery_band_pct=args.dd_recovery_band,
        peak_lookback=args.dd_peak_lookback,
    )
    overlay = DrawdownControlOverlay(overlay_cfg)
    weights = overlay.scale_series(long_rets)
    overlay_rets = apply_drawdown_control(long_rets, overlay)

    base_stats = _stats(
        long_rets,
        bench_rets,
        carhart,
        rebalance_stride=args.rebalance_stride,
        cost_drag_per_period=drag_long_only,
        label="BASE v9D long-only top-decile",
    )
    overlay_stats = _stats(
        overlay_rets,
        bench_rets,
        carhart,
        rebalance_stride=args.rebalance_stride,
        cost_drag_per_period=drag_long_only,
        label="OVERLAY v10 drawdown-control",
    )

    rebalances_per_year = max(1, round(252 / args.rebalance_stride))
    base_net = (long_rets - drag_long_only).dropna()
    overlay_net = (overlay_rets - drag_long_only).dropna()
    aligned = pd.concat([base_net.rename("base"), overlay_net.rename("overlay")], axis=1).dropna()
    sharpe_diff = block_bootstrap_sharpe_diff(
        aligned["overlay"].to_numpy(),
        aligned["base"].to_numpy(),
        periods_per_year=rebalances_per_year,
        block_size=args.bootstrap_block_size,
        n_bootstrap=args.bootstrap_n,
        seed=args.bootstrap_seed,
    )

    gates = _evaluate_v10_gates(
        base_stats=base_stats,
        overlay_stats=overlay_stats,
        sharpe_diff=sharpe_diff,
        base_consistency_target_alpha_t=args.base_target_alpha_t,
        base_consistency_tolerance=args.base_tolerance,
        weight_min=float(weights.min()),
        weight_max=float(weights.max()),
    )

    logger.info(
        "BASE     | n=%d Sh_gross=%.2f Sh_net=%.2f αt=%.2f MDD=%.2f%%",
        base_stats["n"],
        base_stats.get("sharpe_gross", 0.0),
        base_stats.get("sharpe_net", 0.0),
        base_stats.get("alpha_t_4f", 0.0),
        base_stats.get("max_drawdown_net", 0.0) * 100,
    )
    logger.info(
        "OVERLAY  | n=%d Sh_gross=%.2f Sh_net=%.2f αt=%.2f MDD=%.2f%% w_min=%.2f w_max=%.2f w_mean=%.2f",
        overlay_stats["n"],
        overlay_stats.get("sharpe_gross", 0.0),
        overlay_stats.get("sharpe_net", 0.0),
        overlay_stats.get("alpha_t_4f", 0.0),
        overlay_stats.get("max_drawdown_net", 0.0) * 100,
        float(weights.min()),
        float(weights.max()),
        float(weights.mean()),
    )
    logger.info(
        "Δ Sharpe net=%+.2f | bootstrap t=%.2f p=%.4f CI=[%+.2f, %+.2f]",
        overlay_stats.get("sharpe_net", 0.0) - base_stats.get("sharpe_net", 0.0),
        sharpe_diff.t_stat,
        sharpe_diff.p_value_one_sided,
        sharpe_diff.ci_lower,
        sharpe_diff.ci_upper,
    )
    # Canonical log line consumed by audit_multi_phase regex (BASE row).
    label = f"HOLDOUT {args.holdout_start.year}-{args.holdout_end.year}"
    logger.warning(
        "%s | bench=%s ADV≥$%.0fM cost=%.0fbps RT | n=%d topN=%.1f turn=N/A | "
        "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
        "α 4F=%.1f%% t=%.2f",
        label,
        args.benchmark,
        args.adv_min_usd / 1e6,
        args.cost_bps_rt,
        base_stats["n"],
        float(decile_sizes.mean()),
        base_stats.get("sharpe_gross", 0.0),
        base_stats.get("sharpe_net", 0.0),
        base_stats.get("excess_vs_bench_ann_gross", 0.0) * 100,
        base_stats.get("excess_vs_bench_ann_net", 0.0) * 100,
        base_stats.get("alpha_gross_4f", 0.0) * 100,
        base_stats.get("alpha_t_4f", 0.0),
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "v10_drawdown_overlay_v1",
        "date": date.today().isoformat(),
        "preregistration_id": "v10_drawdown_overlay_on_v9D_options_2026_05_04",
        "single_phase_verdict": (
            "PASS" if gates["single_phase_all_pass"] else "FAIL (any single gate)"
        ),
        "config": {
            "holdout_window": (args.holdout_start.isoformat(), args.holdout_end.isoformat()),
            "stride_days": args.rebalance_stride,
            "phase_offset": args.phase_offset,
            "decile_pct": args.decile_pct,
            "benchmark": args.benchmark,
            "adv_min_usd": args.adv_min_usd,
            "cost_bps_rt": args.cost_bps_rt,
            "universe_size": len(universe),
            "overlay": {
                "light_dd": args.dd_light,
                "heavy_dd": args.dd_heavy,
                "half_weight": args.dd_half_weight,
                "recovery_band_pct": args.dd_recovery_band,
                "peak_lookback": args.dd_peak_lookback,
            },
            "bootstrap": {
                "block_size": args.bootstrap_block_size,
                "n_bootstrap": args.bootstrap_n,
                "seed": args.bootstrap_seed,
            },
        },
        "coverage_pct": coverage,
        "n_features_rows": len(features),
        "n_scored": n_scored,
        "base_stats": base_stats,
        "overlay_stats": overlay_stats,
        "sharpe_diff": {
            "sharpe_a_overlay": sharpe_diff.sharpe_a,
            "sharpe_b_base": sharpe_diff.sharpe_b,
            "sharpe_diff": sharpe_diff.sharpe_diff,
            "bootstrap_se": sharpe_diff.bootstrap_se,
            "t_stat": sharpe_diff.t_stat,
            "p_value_one_sided": sharpe_diff.p_value_one_sided,
            "ci_lower": sharpe_diff.ci_lower,
            "ci_upper": sharpe_diff.ci_upper,
            "n_bootstrap": sharpe_diff.n_bootstrap,
        },
        "gates": gates,
        "weights_summary": {
            "min": float(weights.min()),
            "max": float(weights.max()),
            "mean": float(weights.mean()),
            "pct_below_one": float((weights < 1.0).mean()),
            "pct_at_zero": float((weights == 0.0).mean()),
        },
        "raw_returns_for_pooling": {
            "asof": [str(t.date()) for t in aligned.index],
            "base_net": aligned["base"].tolist(),
            "overlay_net": aligned["overlay"].tolist(),
        },
    }
    args.out_json.write_text(json.dumps(payload, indent=2, default=str))
    args.out.write_text(
        f"# v10 drawdown-control overlay on v9D — {payload['single_phase_verdict']}\n\n"
        f"BASE   | Sharpe net={base_stats.get('sharpe_net', 0):.2f} αt={base_stats.get('alpha_t_4f', 0):+.2f} "
        f"MDD={base_stats.get('max_drawdown_net', 0) * 100:.1f}%\n"
        f"OVERLAY | Sharpe net={overlay_stats.get('sharpe_net', 0):.2f} αt={overlay_stats.get('alpha_t_4f', 0):+.2f} "
        f"MDD={overlay_stats.get('max_drawdown_net', 0) * 100:.1f}%\n"
        f"Δ Sharpe net = {overlay_stats.get('sharpe_net', 0) - base_stats.get('sharpe_net', 0):+.2f} | "
        f"bootstrap t={sharpe_diff.t_stat:.2f} p={sharpe_diff.p_value_one_sided:.4f}\n\n"
        f"Gates: {json.dumps({k: v.get('pass', None) if isinstance(v, dict) else v for k, v in gates.items()}, indent=2)}\n"
    )
    logger.info("→ %s\n→ %s", args.out_json, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
