"""v9 SECONDARY — cross-sectional residual scorer holdout reveal.

Pre-committed in `params_v9_sign_constrained_options_implied_2026_05_03.json` as
the SECONDARY hypothesis. Run only if PRIMARY (sign-constrained Lasso) FAILs.
Pre-commitment encoded in pre-reg JSON before any v9 run.

Per asof: residualize `−ivp30` against (`reversal_1m, momentum_6m, rv_30d`)
cross-sectionally; sort by residual; long top-decile EW.

Pipeline mirrors v8 (model-free deterministic) with scoring swap.
Bonferroni threshold: |t|≥3.20 (n=18, one-up from v9 primary's n=17).
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
from dotenv import load_dotenv

from alphalens.attribution.cost_model import CostModel
from alphalens.attribution.factor_analysis import run_regression
from alphalens.backtest.metrics import max_drawdown, sharpe
from alphalens.data.alt_data.ivolatility_smd_cache import load_cached_smd
from alphalens.data.factors import load_carhart_daily
from alphalens.screeners.options_implied import (
    DEFAULT_HOLDING,
    build_feature_frame,
    load_delisting_events_index,
    score_cross_sectional_residual,
)

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

        from alphalens.screeners.options_implied.target import forward_raw_return

        top_rets = [
            forward_raw_return(
                _smd_loader, t, asof, holding_period=1, delisting_events=delisting_events
            )
            for t in top
        ]
        bot_rets = [
            forward_raw_return(
                _smd_loader, t, asof, holding_period=1, delisting_events=delisting_events
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
    from alphalens.screeners.options_implied.target import forward_raw_return

    rets = []
    for asof in asof_index:
        r = forward_raw_return(_smd_loader, benchmark, asof, holding_period=1)
        rets.append(np.nan if r is None else r)
    return pd.Series(rets, index=asof_index, dtype=float, name="benchmark_return")


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


def _verdict(primary_stats: Mapping, *, coverage_pct: float, bonferroni_t: float = 3.20) -> str:
    """Pre-reg single-bar PASS rule for v9 SECONDARY (n=18). No selection-mechanism gate
    needed — cross-sectional residual scorer is non-degenerate by construction."""
    t = primary_stats.get("alpha_t_4f", 0.0)
    if coverage_pct < 0.70:
        return f"FAIL (coverage {coverage_pct * 100:.1f}% < 70%)"
    if abs(t) >= bonferroni_t:
        return f"PASS single-phase (αt={t:+.2f} ≥ {bonferroni_t}); pending multi-phase audit"
    return f"FAIL (αt={t:+.2f} < {bonferroni_t} program-Bonferroni n=18)"


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
    ap.add_argument("--train-start", type=date.fromisoformat, default=date(2018, 4, 30))
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v9_cross_sectional_residual_holdout.md",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v9_cross_sectional_residual_holdout.json",
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
    logger.info("Carhart: %d rows", len(carhart))

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
    logger.info(
        "Scored: %d / %d non-NaN (%.1f%% coverage)",
        n_scored,
        len(features),
        coverage * 100,
    )

    delisting_events = load_delisting_events_index(SURVIVORSHIP_PARQUET)
    logger.info("Loaded %d delisting events for terminal-return rule", len(delisting_events))

    long_rets, short_rets, asof_idx, decile_sizes = _portfolio_returns(
        features,
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
        label="LONG-only top decile (cross-sectional residual scorer)",
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

    verdict = _verdict(primary_stats, coverage_pct=coverage)

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

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "v9_cross_sectional_residual_v1",
        "date": date.today().isoformat(),
        "verdict": verdict,
        "config": {
            "holdout_window": (args.holdout_start.isoformat(), args.holdout_end.isoformat()),
            "stride_days": args.rebalance_stride,
            "phase_offset": args.phase_offset,
            "decile_pct": args.decile_pct,
            "benchmark": args.benchmark,
            "adv_min_usd": args.adv_min_usd,
            "cost_bps_rt": args.cost_bps_rt,
            "universe_size": len(universe),
            "scoring": "OLS residual: -ivp30 ~ reversal_1m + momentum_6m + rv_30d + intercept (per asof)",
        },
        "coverage_pct": coverage,
        "n_features_rows": len(features),
        "n_scored": n_scored,
        "primary_stats": primary_stats,
        "ls_diagnostic_stats": ls_stats,
    }
    args.out_json.write_text(json.dumps(payload, indent=2, default=str))
    args.out.write_text(
        f"# v9 cross-sectional residual holdout — {payload['verdict']}\n\n"
        f"αt=**{primary_stats.get('alpha_t_4f', 0):+.2f}** (HAC=5)\n"
        f"Sharpe net={primary_stats.get('sharpe_net', 0):.2f}\n"
        f"L/S αt={ls_stats.get('alpha_t_4f', 0):+.2f}\n"
        f"Coverage: {coverage * 100:.1f}%\n"
    )
    logger.info("→ %s\n→ %s", args.out_json, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
