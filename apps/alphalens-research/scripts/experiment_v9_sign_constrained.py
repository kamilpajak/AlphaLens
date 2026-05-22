"""v9 holdout reveal — sign-constrained Lasso (Xing prior enforced).

Pre-registered as `v9_sign_constrained_options_implied_2026_05_03` per
`docs/research/preregistration/params_v9_sign_constrained_options_implied_2026_05_03.json`.

Built after v7 (Lasso, αt +2.60) and v8 (model-free −ivp30, αt +2.18) both
FAIL'd. v9 combines v7's Lasso magnitude lift with v8's sign safety:
mechanically forces options-feature coefs ≤ 0 (Xing prior), lets equity
controls fit freely. v7's L/S diagnostic αt = −3.25 implied bottom-decile
αt ≈ +5.34 if the direction had been correct — v9 tests whether the
sign-constrained fit recovers some of that lift.

Pipeline mirrors v7 with two changes:
- Model swap: `fit_sign_constrained_lasso` instead of `fit_global_lasso`
- Sign-alignment auto_pivot logic dropped (sign mechanically guaranteed)

Audit-multi-phase compatible: emits a single WARNING line matching
`audit_multi_phase.py:_RESULT_LINE`.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import yaml
from alphalens_research.attribution.cost_model import CostModel
from alphalens_research.attribution.factor_analysis import run_regression
from alphalens_research.backtest.metrics import max_drawdown, sharpe
from alphalens_research.data.alt_data.ivolatility_smd_cache import load_cached_smd
from alphalens_research.data.factors import load_carhart_daily
from alphalens_research.screeners.options_implied import (
    DEFAULT_HOLDING,
    FEATURE_NAMES,
    OPTIONS_FEATURES,
    aligned_train,
    build_feature_frame,
    build_target_frame,
    fit_sign_constrained_lasso,
    load_delisting_events_index,
    multicollinearity_drop_recommendation,
    predict_scores,
    split_train_holdout,
    validate_phase_a_gates,
)
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SMD_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
ETFS = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD")

CARHART_COLS = ["Mkt-RF", "SMB", "HML", "Mom"]


# ---------------------------------------------------------------------------
# Universe + smd loader (verbatim from v7/v8)


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
    """Memoized loader; see v7 driver for I/O rationale."""
    key = ticker.upper()
    if key not in _SMD_CACHE:
        _SMD_CACHE[key] = load_cached_smd(key, SMD_CACHE_DIR)
    return _SMD_CACHE[key]


# ---------------------------------------------------------------------------
# Calendar


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


# ---------------------------------------------------------------------------
# Decile selection + portfolio returns (1d-fwd to match Carhart cadence)


def _portfolio_returns(
    feat_holdout: pd.DataFrame,
    scores: pd.Series,
    *,
    decile_pct: float = 0.1,
    delisting_events: dict | None = None,
) -> tuple[pd.Series, pd.Series, pd.DatetimeIndex, pd.Series]:
    holdout = feat_holdout.assign(_score=scores).dropna(subset=["_score"])
    asof_dates = sorted(holdout["asof"].unique())

    long_rets, short_rets, indices, sizes = [], [], [], []
    for asof in asof_dates:
        slice_df = holdout.loc[holdout["asof"] == asof]
        n = len(slice_df)
        decile_size = max(1, round(n * decile_pct))
        if n < 2 * decile_size:
            continue

        ranked = slice_df.sort_values("_score", ascending=False)
        top = ranked.head(decile_size)["ticker"].tolist()
        bottom = ranked.tail(decile_size)["ticker"].tolist()

        from alphalens_research.screeners.options_implied.target import forward_raw_return

        top_rets = [
            forward_raw_return(
                _smd_loader,
                t,
                asof,
                holding_period=1,
                delisting_events=delisting_events,
            )
            for t in top
        ]
        bot_rets = [
            forward_raw_return(
                _smd_loader,
                t,
                asof,
                holding_period=1,
                delisting_events=delisting_events,
            )
            for t in bottom
        ]
        top_arr = np.array([r if r is not None else np.nan for r in top_rets], dtype=float)
        bot_arr = np.array([r if r is not None else np.nan for r in bot_rets], dtype=float)
        if np.all(np.isnan(top_arr)) or np.all(np.isnan(bot_arr)):
            continue
        long_rets.append(float(np.nanmean(top_arr)))
        short_rets.append(float(np.nanmean(bot_arr)))
        indices.append(asof)
        sizes.append(decile_size)

    if not indices:
        empty = pd.Series(dtype=float)
        return empty, empty, pd.DatetimeIndex([]), empty
    asof_idx = pd.DatetimeIndex(pd.to_datetime(indices))
    return (
        pd.Series(long_rets, index=asof_idx, name="long_return"),
        pd.Series(short_rets, index=asof_idx, name="short_return"),
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


# ---------------------------------------------------------------------------
# Carhart-4F + Sharpe + MDD


def _assess(
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


# ---------------------------------------------------------------------------
# Verdict


def _verdict(
    primary_stats: Mapping,
    *,
    coverage_pct: float,
    bonferroni_t: float = 3.13,
    coverage_min: float = 0.70,
    n_nonzero_options: int = 0,
) -> str:
    """Pre-reg single-bar PASS rule for v9. Threshold tightened to n=17.

    PASS: αt ≥ 3.13 program-Bonferroni n=17
          AND ivp30 coverage ≥ 70%
          AND ≥1 nonzero coef on options features (selection-mechanism gate)
    FAIL: any gate misses
    """
    t = primary_stats.get("alpha_t_4f", 0.0)
    if coverage_pct < coverage_min:
        return f"FAIL (ivp30 coverage {coverage_pct * 100:.1f}% < {coverage_min * 100:.0f}%)"
    if n_nonzero_options == 0:
        return (
            "FAIL (selection-mechanism artifact: 0/4 options coefs survived sign-constrained fit)"
        )
    if abs(t) >= bonferroni_t:
        return f"PASS single-phase (αt={t:+.2f} ≥ {bonferroni_t}); pending multi-phase audit"
    return f"FAIL (αt={t:+.2f} < {bonferroni_t} program-Bonferroni n=17)"


# ---------------------------------------------------------------------------
# CLI


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--train-start", type=date.fromisoformat, default=date(2018, 4, 30))
    ap.add_argument("--holdout-start", type=date.fromisoformat, default=date(2024, 4, 30))
    ap.add_argument("--holdout-end", type=date.fromisoformat, default=date(2026, 4, 30))
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument("--holding", type=int, default=DEFAULT_HOLDING)
    ap.add_argument("--decile-pct", type=float, default=0.1)
    ap.add_argument("--benchmark", default="MDY")
    ap.add_argument("--adv-min-usd", type=float, default=2_000_000.0)
    ap.add_argument("--cost-bps-rt", type=float, default=30.0, help="Long-only RT cost")
    ap.add_argument("--winsorize-pct", type=float, default=0.995)
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v9_sign_constrained_holdout.md",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v9_sign_constrained_holdout.json",
    )
    ap.add_argument("--max-tickers", type=int, default=None, help="Cap universe for testing")
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = _build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # 1. Universe + calendar
    universe = _pit_union()
    if args.max_tickers:
        universe = universe[: args.max_tickers]
    logger.info("Universe: %d tickers", len(universe))

    asofs = _benchmark_calendar(
        args.benchmark,
        args.train_start,
        args.holdout_end,
        args.rebalance_stride,
        args.phase_offset,
    )
    logger.info(
        "Calendar: %d asofs (stride=%d, phase=%d) %s..%s",
        len(asofs),
        args.rebalance_stride,
        args.phase_offset,
        asofs[0],
        asofs[-1],
    )

    # 2. Carhart factors
    ff_start = args.train_start - timedelta(days=400)
    carhart = load_carhart_daily(start=ff_start, end=args.holdout_end)
    logger.info("Carhart: %d rows", len(carhart))

    # 3. Feature frame
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

    # 4. Phase A gates on TRAIN only
    train_features, holdout_features = split_train_holdout(features, args.holdout_start)
    gates = validate_phase_a_gates(train_features)
    logger.info(
        "Phase A train: coverage=%.1f%%, max |corr|=%.4f%s",
        gates["coverage_pct"] * 100,
        gates["max_abs_corr"],
        f" → drop pair {gates['offending_pair']}" if gates["offending_pair"] else "",
    )
    feat_cols = list(FEATURE_NAMES)
    if not gates["multicollinearity_pass"] and gates["offending_pair"]:
        try:
            drop = multicollinearity_drop_recommendation(offending_pair=gates["offending_pair"])
            feat_cols = [c for c in feat_cols if c != drop]
            logger.warning(
                "Phase A multicollinearity gate tripped — dropping %s; reduced stack: %s",
                drop,
                feat_cols,
            )
        except ValueError as e:
            logger.error("Cannot auto-remediate: %s", e)
            return 2

    # 5. Targets
    delisting_events = load_delisting_events_index(SURVIVORSHIP_PARQUET)
    logger.info("Loaded %d delisting events for terminal-return rule", len(delisting_events))
    winsorize_pct = None if args.winsorize_pct >= 1.0 else args.winsorize_pct
    targets = build_target_frame(
        features,
        smd_loader=_smd_loader,
        holding_period=args.holding,
        delisting_events=delisting_events,
        winsorize_right_tail_pct=winsorize_pct,
    )
    logger.info("Target winsorize_right_tail_pct=%s", winsorize_pct)
    logger.info("Targets: %d rows, %d non-NaN", len(targets), int(targets["target"].notna().sum()))

    # 6. Strict-temporal split
    targ_train, _ = split_train_holdout(targets, args.holdout_start)
    train_X_raw, train_y = aligned_train(train_features, targ_train)
    logger.info("Train aligned: %d rows", len(train_X_raw))
    if train_X_raw.empty:
        logger.error("Empty train — abort")
        return 1

    # 7. Fit SIGN-CONSTRAINED Lasso on train
    fit = fit_sign_constrained_lasso(train_X_raw, train_y, feature_names=feat_cols)
    logger.info(
        "Sign-constrained Lasso: α=%.4g, n_train=%d, n_nonzero=%d/%d (options nonzero=%d/%d), CV-MSE=%.4g",
        fit.chosen_alpha,
        fit.n_train_obs,
        fit.n_nonzero_coefs,
        len(fit.feature_names),
        fit.n_nonzero_options,
        len([c for c in feat_cols if c in OPTIONS_FEATURES]),
        fit.cv_mean_mse,
    )
    logger.info(
        "Coefs (standardized): %s",
        dict(zip(fit.feature_names, [round(c, 4) for c in fit.coefficients], strict=False)),
    )

    if fit.all_options_zeroed:
        logger.error("Pre-reg gate FIRED: ALL 4 options features zeroed by sign-constrained fit")

    # 8. Predict on holdout
    scores = predict_scores(fit, holdout_features)
    n_scored = int(scores.notna().sum())
    coverage = n_scored / max(1, len(holdout_features))
    logger.info(
        "Holdout scored: %d / %d (%.1f%% coverage)", n_scored, len(holdout_features), coverage * 100
    )

    # 9. Decile portfolios
    long_rets, short_rets, asof_idx, decile_sizes = _portfolio_returns(
        holdout_features,
        scores,
        decile_pct=args.decile_pct,
        delisting_events=delisting_events,
    )
    if long_rets.empty:
        logger.error("Empty holdout portfolio — abort")
        return 1
    logger.info(
        "Holdout: %d rebalances, mean decile size=%.1f",
        len(long_rets),
        float(decile_sizes.mean()),
    )

    # 10. Benchmark + Carhart attribution
    bench_rets = _benchmark_holding_returns(asof_idx, args.benchmark)

    cost_long_only = CostModel.from_profile("long_only_30bps")
    drag_long_only = cost_long_only.annual_drag_bps / 10_000.0 / (252 / args.rebalance_stride)
    drag_ls = (cost_long_only.annual_drag_bps * 2) / 10_000.0 / (252 / args.rebalance_stride)

    primary_stats = _assess(
        long_rets,
        bench_rets,
        carhart,
        rebalance_stride=args.rebalance_stride,
        cost_drag_per_period=drag_long_only,
        label="LONG-only top decile (sign-constrained Lasso)",
    )
    ls_rets = long_rets - short_rets
    ls_stats = _assess(
        ls_rets,
        bench_rets,
        carhart,
        rebalance_stride=args.rebalance_stride,
        cost_drag_per_period=drag_ls,
        label="L/S decile spread (diagnostic)",
    )

    verdict = _verdict(
        primary_stats,
        coverage_pct=coverage,
        n_nonzero_options=fit.n_nonzero_options,
    )

    logger.info(
        "PRIMARY  | n=%d Sh_gross=%.2f Sh_net=%.2f α_4F=%.2f%% αt=%.2f excess_net=%.2f%%",
        primary_stats["n"],
        primary_stats.get("sharpe_gross", 0.0),
        primary_stats.get("sharpe_net", 0.0),
        primary_stats.get("alpha_gross_4f", 0.0) * 100,
        primary_stats.get("alpha_t_4f", 0.0),
        primary_stats.get("excess_vs_bench_ann_net", 0.0) * 100,
    )
    label = f"HOLDOUT {args.holdout_start.year}-{args.holdout_end.year}"
    logger.warning(
        "%s | bench=%s ADV≥$%.0fM cost=%.0fbps RT | n=%d topN=%.1f turn=N/A | "
        "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
        "α 4F=%.1f%% t=%.2f",
        label,
        args.benchmark,
        args.adv_min_usd / 1e6,
        args.cost_bps_rt,
        primary_stats["n"],
        float(decile_sizes.mean()),
        primary_stats.get("sharpe_gross", 0.0),
        primary_stats.get("sharpe_net", 0.0),
        primary_stats.get("excess_vs_bench_ann_gross", 0.0) * 100,
        primary_stats.get("excess_vs_bench_ann_net", 0.0) * 100,
        primary_stats.get("alpha_gross_4f", 0.0) * 100,
        primary_stats.get("alpha_t_4f", 0.0),
    )
    logger.info(
        "L/S diag | n=%d Sh_gross=%.2f Sh_net=%.2f α_4F=%.2f%% αt=%.2f",
        ls_stats["n"],
        ls_stats.get("sharpe_gross", 0.0),
        ls_stats.get("sharpe_net", 0.0),
        ls_stats.get("alpha_gross_4f", 0.0) * 100,
        ls_stats.get("alpha_t_4f", 0.0),
    )
    logger.info("VERDICT: %s", verdict)

    # 11. Persist
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "v9_sign_constrained_v1",
        "date": date.today().isoformat(),
        "verdict": verdict,
        "config": {
            "train_window": (args.train_start.isoformat(), args.holdout_start.isoformat()),
            "holdout_window": (args.holdout_start.isoformat(), args.holdout_end.isoformat()),
            "stride_days": args.rebalance_stride,
            "phase_offset": args.phase_offset,
            "holding_period_days": args.holding,
            "decile_pct": args.decile_pct,
            "benchmark": args.benchmark,
            "adv_min_usd": args.adv_min_usd,
            "cost_bps_rt": args.cost_bps_rt,
            "winsorize_pct": winsorize_pct,
            "universe_size": len(universe),
        },
        "phase_a_gates": gates,
        "lasso_fit": {
            "feature_names": list(fit.feature_names),
            "coefficients": [float(c) for c in fit.coefficients],
            "intercept": fit.intercept,
            "chosen_alpha": fit.chosen_alpha,
            "cv_mean_mse": fit.cv_mean_mse,
            "n_train_obs": fit.n_train_obs,
            "n_nonzero_coefs": fit.n_nonzero_coefs,
            "n_nonzero_options": fit.n_nonzero_options,
        },
        "primary_stats": primary_stats,
        "ls_diagnostic_stats": ls_stats,
        "feat_cols_used": feat_cols,
        "coverage_pct": coverage,
    }
    args.out_json.write_text(json.dumps(payload, indent=2, default=str))
    _write_md_report(args.out, payload)
    logger.info("→ %s\n→ %s", args.out_json, args.out)
    return 0


def _write_md_report(out_path: Path, payload: dict) -> None:
    p = payload["primary_stats"]
    ls = payload["ls_diagnostic_stats"]
    fit = payload["lasso_fit"]
    coefs_pairs = list(zip(fit["feature_names"], fit["coefficients"], strict=False))
    lines = [
        f"# v9 sign-constrained holdout reveal — {payload['verdict']}",
        "",
        f"**Date:** {payload['date']}",
        "**Pre-reg:** v9_sign_constrained_options_implied_2026_05_03",
        "**Model:** Lasso with mechanically-enforced `coef_options ≤ 0` (Xing prior)",
        "",
        "## Headline (PRIMARY = LONG TOP decile by sign-constrained Lasso)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| n holdout rebalances | {p.get('n', 0)} |",
        f"| Sharpe (gross) | {p.get('sharpe_gross', 0):.2f} |",
        f"| Sharpe (net 30bps RT) | {p.get('sharpe_net', 0):.2f} |",
        f"| Carhart-4F α (gross, ann) | {p.get('alpha_gross_4f', 0) * 100:+.2f}% |",
        f"| Carhart-4F α (net, ann) | {p.get('alpha_net_4f', 0) * 100:+.2f}% |",
        f"| α t-stat (HAC=5) | **{p.get('alpha_t_4f', 0):+.2f}** |",
        f"| Excess vs MDY (gross, ann) | {p.get('excess_vs_bench_ann_gross', 0) * 100:+.2f}% |",
        f"| Excess vs MDY (net, ann) | {p.get('excess_vs_bench_ann_net', 0) * 100:+.2f}% |",
        f"| Max drawdown (net cum) | {p.get('max_drawdown_net', 0) * 100:+.2f}% |",
        "",
        "## L/S diagnostic (top − bottom decile, NOT primary verdict)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Sharpe (gross) | {ls.get('sharpe_gross', 0):.2f} |",
        f"| Sharpe (net 60bps RT) | {ls.get('sharpe_net', 0):.2f} |",
        f"| Carhart-4F α (gross, ann) | {ls.get('alpha_gross_4f', 0) * 100:+.2f}% |",
        f"| α t-stat (HAC=5) | {ls.get('alpha_t_4f', 0):+.2f} |",
        "",
        "## Sign-constrained Lasso fit (standardized features, train period)",
        "",
        f"- α (penalty): {fit['chosen_alpha']:.4g}",
        f"- n_train: {fit['n_train_obs']}, CV-MSE: {fit['cv_mean_mse']:.4g}",
        f"- nonzero coefs: {fit['n_nonzero_coefs']} / {len(fit['feature_names'])}"
        f" (options-feature subset: {fit['n_nonzero_options']} / 4)",
        "",
        "| Feature | Coef (standardized) |",
        "| --- | ---: |",
    ]
    for name, c in coefs_pairs:
        lines.append(f"| {name} | {c:+.4g} |")
    lines += [
        "",
        "## Coverage",
        "",
        f"- Holdout-scored / total holdout rows: {payload['coverage_pct'] * 100:.1f}%",
        "",
        "## Pre-reg discipline",
        "",
        "- Sign constraint MECHANICALLY enforced — `coef_options ≤ 0` cannot violate Xing prior.",
        "- Equity controls free-sign (encoded via positive/negative-pair augmentation).",
        "- ONE-shot holdout, no peek-and-tune.",
        "- Carhart-4F (HAC=5) attribution post-hoc.",
        "- L/S diagnostic reported as power-loss check, NOT additional Bonferroni test.",
        "- Threshold |αt| ≥ 3.13 program-Bonferroni n=17 (one-up from v8's n=16).",
        "- Selection-mechanism gate: ≥1 nonzero options coef required for non-degenerate fit.",
    ]
    out_path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
