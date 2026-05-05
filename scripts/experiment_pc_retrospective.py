"""P/C abnormal volume — retrospective pre-2018 fresh-OOS test (one cell per call).

Pre-registered as ``pc_abnormal_volume_retrospective_pre_2018_2026_05_05`` in
signal class ``options_volume_search_2026_05_05``. Locks frozen scorer
(``score_pc_abnormal_residual``), two universe variants (U1/U2), three
sub-periods, five phase offsets → 30 cells total.

Mirrors ``scripts/experiment_v9d_retrospective_pre_2018.py`` driver structure;
only feature space differs (P/C abnormal volume vs IV cross-sectional residual).

Per-cell output: same JSON layout as v9D retrospective for aggregator reuse.
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
from dotenv import load_dotenv

from alphalens.attribution.cost_model import CostModel
from alphalens.attribution.factor_analysis import run_regression
from alphalens.backtest.metrics import max_drawdown, sharpe
from alphalens.data.alt_data.ivolatility_smd_cache import load_cached_smd
from alphalens.data.factors import load_carhart_daily
from alphalens.paper_trade.universe_loaders import (
    pit_union_from_ivol_cache,
    pit_union_legacy,
)
from alphalens.screeners.options_implied import load_delisting_events_index
from alphalens.screeners.options_implied.target import forward_raw_return
from alphalens.screeners.options_volume import (
    build_feature_frame,
    score_pc_abnormal_residual,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SMD_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
RETRO_SURVIVORSHIP_PARQUET = (
    Path.home() / ".alphalens" / "survivorship" / "delisting_events_2008_2018.parquet"
)
ETFS = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD")
CARHART_COLS = ["Mkt-RF", "SMB", "HML", "Mom"]

SUB_PERIODS: dict[str, tuple[date, date]] = {
    "GFC_recovery": (date(2008, 4, 30), date(2011, 12, 31)),
    "mid_cycle_eu_debt": (date(2012, 1, 1), date(2014, 12, 31)),
    "late_cycle_china_shock": (date(2015, 1, 1), date(2018, 4, 29)),
}


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
    if "close" in df.columns:
        df = df.loc[df["close"].notna()]
    df = df.sort_values("tradeDate")
    dates = pd.to_datetime(df["tradeDate"])
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    sliced = sorted(set(dates.loc[mask].dt.date.tolist()))
    if not 0 <= phase_offset < stride:
        raise ValueError(f"phase_offset must satisfy 0 <= offset < {stride}")
    return sliced[phase_offset::stride]


def _load_universe(variant: str, asof_dates: list[date]) -> list[str]:
    if variant == "U1":
        return pit_union_legacy(start_year=2008, extra_etfs=ETFS)
    if variant == "U2":
        seen: set[str] = set()
        for asof in asof_dates:
            seen.update(pit_union_from_ivol_cache(asof, extra_etfs=ETFS))
        return sorted(seen)
    raise ValueError(f"Unknown universe variant {variant!r} (expected U1/U2)")


def _portfolio_returns(
    feat_holdout: pd.DataFrame,
    scores: pd.Series,
    *,
    decile_pct: float = 0.1,
    holding_period: int = 1,
    delisting_events: dict | None = None,
) -> tuple[pd.Series, pd.DatetimeIndex, pd.Series, list[set]]:
    holdout = feat_holdout.assign(_score=scores).dropna(subset=["_score"])
    asof_dates = sorted(holdout["asof"].unique())

    long_rets, indices, sizes, holdings = [], [], [], []
    for asof in asof_dates:
        slice_df = holdout.loc[holdout["asof"] == asof]
        n = len(slice_df)
        decile_size = max(1, round(n * decile_pct))
        if n < 2 * decile_size:
            continue
        ranked = slice_df.sort_values("_score", ascending=False)
        top = ranked.head(decile_size)["ticker"].tolist()

        top_rets = [
            forward_raw_return(
                _smd_loader,
                t,
                asof,
                holding_period=holding_period,
                delisting_events=delisting_events,
            )
            for t in top
        ]
        top_arr = np.array([r if r is not None else np.nan for r in top_rets], dtype=float)
        if np.all(np.isnan(top_arr)):
            continue
        long_rets.append(float(np.nanmean(top_arr)))
        indices.append(asof)
        sizes.append(decile_size)
        holdings.append(set(top))

    if not indices:
        empty = pd.Series(dtype=float)
        return empty, pd.DatetimeIndex([]), empty, []
    asof_idx = pd.DatetimeIndex(pd.to_datetime(indices))
    return (
        pd.Series(long_rets, index=asof_idx, name="long_return"),
        asof_idx,
        pd.Series(sizes, index=asof_idx, name="decile_size"),
        holdings,
    )


def _benchmark_holding_returns(
    asof_index: pd.DatetimeIndex, benchmark: str, holding_period: int = 1
) -> pd.Series:
    rets = []
    for asof in asof_index:
        r = forward_raw_return(_smd_loader, benchmark, asof, holding_period=holding_period)
        rets.append(np.nan if r is None else r)
    return pd.Series(rets, index=asof_index, dtype=float, name="benchmark_return")


def _turnover(holdings_history: list[set]) -> float:
    if len(holdings_history) <= 1:
        return float("nan")
    total = 0.0
    for prev, curr in zip(holdings_history, holdings_history[1:]):
        if not curr:
            continue
        total += len(curr - prev) / len(curr)
    return total / max(1, len(holdings_history) - 1)


def _stats(
    portfolio_returns: pd.Series,
    bench_returns: pd.Series,
    carhart: pd.DataFrame,
    *,
    rebalance_stride: int,
    cost_drag_per_period: float,
    holdings_history: list[set],
    label: str,
) -> dict:
    rets = portfolio_returns.dropna()
    if rets.empty:
        return {"n": 0, "label": label}

    rebalances_per_year = 252 / max(1, rebalance_stride)
    rets_net = rets - cost_drag_per_period
    sharpe_gross = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))
    sharpe_net = sharpe(rets_net.tolist(), periods_per_year=int(rebalances_per_year))

    res4 = run_regression(
        rets,
        carhart[[*CARHART_COLS, "RF"]],
        CARHART_COLS,
        periods_per_year=int(rebalances_per_year),
    )

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
        "alpha_se_4f": (
            float(res4.alpha_annualized) / float(res4.alpha_tstat)
            if abs(float(res4.alpha_tstat)) > 1e-9
            else float("nan")
        ),
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "excess_vs_bench_ann_gross": excess_ann_gross,
        "excess_vs_bench_ann_net": excess_ann_gross - drag_ann,
        "max_drawdown_net": mdd,
        "cost_drag_ann": drag_ann,
        "turnover_per_rebal": _turnover(holdings_history),
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--universe", choices=["U1", "U2"], required=True)
    ap.add_argument("--sub-period", choices=list(SUB_PERIODS.keys()), required=True)
    ap.add_argument("--phase-offset", type=int, required=True, help="0..4 inclusive")
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--holding", type=int, default=1)
    ap.add_argument("--decile-pct", type=float, default=0.1)
    ap.add_argument("--benchmark", default="MDY")
    ap.add_argument("--adv-min-usd", type=float, default=2_000_000.0)
    ap.add_argument("--cost-bps-rt", type=float, default=30.0)
    ap.add_argument("--max-tickers", type=int, default=None)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "pc_abnormal_retrospective_pre_2018",
    )
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = _build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if not 0 <= args.phase_offset < args.rebalance_stride:
        logger.error(
            "phase_offset (%d) must satisfy 0 <= offset < stride (%d)",
            args.phase_offset,
            args.rebalance_stride,
        )
        return 2
    sub_start, sub_end = SUB_PERIODS[args.sub_period]

    asofs = _benchmark_calendar(
        args.benchmark, sub_start, sub_end, args.rebalance_stride, args.phase_offset
    )
    if not asofs:
        logger.error("Empty calendar for %s phase=%d", args.sub_period, args.phase_offset)
        return 1
    logger.info(
        "Cell %s × %s × p%d: %d asofs %s..%s",
        args.universe,
        args.sub_period,
        args.phase_offset,
        len(asofs),
        asofs[0],
        asofs[-1],
    )

    universe = _load_universe(args.universe, asofs)
    if args.max_tickers:
        universe = universe[: args.max_tickers]
    logger.info("Universe %s: %d tickers", args.universe, len(universe))

    ff_start = sub_start - timedelta(days=400)
    carhart = load_carhart_daily(start=ff_start, end=sub_end)

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

    scores = score_pc_abnormal_residual(features)
    n_scored = int(scores.notna().sum())
    coverage = n_scored / max(1, len(features))
    logger.info("Scored: %d / %d (%.1f%%)", n_scored, len(features), coverage * 100)

    delisting_events = (
        load_delisting_events_index(RETRO_SURVIVORSHIP_PARQUET)
        if RETRO_SURVIVORSHIP_PARQUET.exists()
        else {}
    )
    logger.info("Delisting events for terminal-return patch: %d", len(delisting_events))

    long_rets, asof_idx, decile_sizes, holdings_history = _portfolio_returns(
        features,
        scores,
        decile_pct=args.decile_pct,
        holding_period=args.holding,
        delisting_events=delisting_events,
    )
    if long_rets.empty:
        logger.error("Empty portfolio returns — abort")
        return 1
    bench_rets = _benchmark_holding_returns(asof_idx, args.benchmark, args.holding)

    cost_long_only = CostModel.from_profile("long_only_30bps")
    drag_per_period = cost_long_only.annual_drag_bps / 10_000.0 / (252 / args.rebalance_stride)

    stats = _stats(
        long_rets,
        bench_rets,
        carhart,
        rebalance_stride=args.rebalance_stride,
        cost_drag_per_period=drag_per_period,
        holdings_history=holdings_history,
        label=f"pc_abnormal {args.universe} {args.sub_period} p{args.phase_offset}",
    )

    out_path = args.out_dir / f"{args.universe}_{args.sub_period}_p{args.phase_offset}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cell": {
            "universe": args.universe,
            "sub_period": args.sub_period,
            "phase_offset": args.phase_offset,
        },
        "config": {
            "rebalance_stride": args.rebalance_stride,
            "holding_period": args.holding,
            "decile_pct": args.decile_pct,
            "benchmark": args.benchmark,
            "adv_min_usd": args.adv_min_usd,
            "cost_bps_rt": args.cost_bps_rt,
            "sub_period_start": sub_start.isoformat(),
            "sub_period_end": sub_end.isoformat(),
            "n_tickers_universe": len(universe),
            "n_asofs": len(asofs),
            "n_features_rows": len(features),
            "n_scored": n_scored,
            "coverage_pct": coverage,
            "n_delisting_events_loaded": len(delisting_events),
            "scorer": "pc_abnormal_volume.score_pc_abnormal_residual",
            "pre_reg_sha256": "1debf1cc0ae8644d53955e7406007248e0052ab12559cff3f55fde688dbc8922",
        },
        "stats": stats,
        "raw_returns_for_pooling": {
            "asof": [d.date().isoformat() for d in asof_idx],
            "long_net": (long_rets - drag_per_period).tolist(),
            "long_gross": long_rets.tolist(),
            "benchmark": bench_rets.tolist(),
        },
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Wrote %s", out_path)
    logger.info(
        "[CELL %s|%s|p%d] αt=%.2f sharpe_net=%.2f mdd=%.2f%% turnover=%.0f%% n=%d",
        args.universe,
        args.sub_period,
        args.phase_offset,
        stats.get("alpha_t_4f", float("nan")),
        stats.get("sharpe_net", float("nan")),
        stats.get("max_drawdown_net", 0.0) * 100,
        stats.get("turnover_per_rebal", 0.0) * 100,
        stats.get("n", 0),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
