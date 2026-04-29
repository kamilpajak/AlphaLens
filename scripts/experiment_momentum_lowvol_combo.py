"""Momentum + low-vol combo — quality proxy via realized volatility.

Frazzini-Pedersen "Betting Against Beta" + Asness-Frazzini-Pedersen "Quality
minus Junk" thesis: low-volatility (low-beta) stocks deliver higher
risk-adjusted returns than high-vol counterparts. Combining momentum with
a low-vol filter should hedge against momentum crashes (where junk-momentum
names — low-quality, high-vol — collapse hardest).

Score = z(mom_12_1m) − vol_weight × z(vol_60d).
Higher score = strong 12-1m winner AND low realized volatility.

Same harness as `experiment_constrained_momentum.py`: PIT R2000-like
universe, ADV ≥ threshold, weekly stride, 5/15 bps cost stress.

Test goal: does the vol filter rescue momentum from its 2023-2026 OOS
catastrophe (Sharpe -0.21 to -0.67 across all ADV thresholds)? If yes,
low-vol momentum is a candidate strategy.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import yaml

from alphalens.alt_data.yfinance_cache import load_cached_histories
from alphalens.backtest.cost_model import RealisticCostModel
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.factor_analysis import run_regression
from alphalens.backtest.factors import load_carhart_daily
from alphalens.backtest.history_store import HistoryStore
from alphalens.backtest.metrics import sharpe, turnover_pct

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_STR_PATH = Path.home() / ".alphalens" / "factors" / "str_daily.csv"

_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]


def load_pit_union(start: date, end: date) -> list[str]:
    union: set[str] = set()
    for path in sorted(_PIT_DIR.glob("*.yaml")):
        snap_date = date.fromisoformat(path.stem + "-01")
        if not (start.replace(day=1) <= snap_date <= end):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for ticker in data.get("tickers", []):
            union.add(ticker)
    return sorted(union)


def momentum_lowvol_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:
    """Score = z(mom_12_1m) − vol_weight × z(vol_60d), filtered by ADV."""
    config = dict(config or {})
    benchmark = config.get("benchmark")
    adv_min = float(config.get("_adv_min_usd", 0.0))
    adv_window = int(config.get("_adv_window", 60))
    vol_weight = float(config.get("_vol_weight", 1.0))
    vol_window = int(config.get("_vol_window", 60))

    rows: list[dict] = []
    for ticker, df in histories.items():
        if ticker == benchmark or df is None or len(df) < 253:
            continue
        closes = df["close"].to_numpy(dtype=float)
        volumes = df["volume"].to_numpy(dtype=float)
        if closes[-1] <= 0 or closes[-253] <= 0 or closes[-22] <= 0:
            continue
        # ADV filter
        dollar_vol = closes[-adv_window:] * volumes[-adv_window:]
        adv = float(np.median(dollar_vol[dollar_vol > 0])) if (dollar_vol > 0).any() else 0.0
        if adv < adv_min:
            continue
        # 12-1m momentum
        mom = closes[-22] / closes[-253] - 1.0
        # 60d realized vol (annualized std of daily log returns)
        rets = np.diff(np.log(closes[-vol_window - 1 :]))
        if len(rets) < vol_window // 2 or np.any(~np.isfinite(rets)):
            continue
        vol = float(np.std(rets, ddof=1) * np.sqrt(252))
        if not np.isfinite(vol) or vol <= 0:
            continue
        rows.append({"ticker": ticker, "mom": float(mom), "vol": vol})

    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])

    df = pd.DataFrame(rows)
    for col in ("mom", "vol"):
        std = df[col].std(ddof=0)
        if std <= 0:
            df[f"z_{col}"] = 0.0
            continue
        z = (df[col] - df[col].mean()) / std
        df[f"z_{col}"] = z.clip(-3.0, 3.0)
    df["score"] = df["z_mom"] - vol_weight * df["z_vol"]
    return df.sort_values("score", ascending=False).reset_index(drop=True)


momentum_lowvol_adapter.MIN_BARS_REQUIRED = 253


def benchmark_returns(
    history_store: HistoryStore, benchmark: str, start: date, end: date
) -> pd.Series:
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def assess(report, factors, rebalance_stride, cost_bps, bench_rets) -> dict:
    rets = report.portfolio_returns
    if rets.empty:
        return {"n": 0}
    rebalances_per_year = 252 / max(1, rebalance_stride)
    sharpe_gross = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))
    avg_turnover = turnover_pct(r.top_n_tickers for r in report.rebalance_results)

    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_per_rebal_bps = cost_model.primary_period_drag_bps(cost_bps, avg_turnover)
    drag_ann = drag_per_rebal_bps * rebalances_per_year / 10_000.0
    drag_per_rebal = drag_per_rebal_bps / 10_000.0
    rets_net = rets - drag_per_rebal
    sharpe_net = sharpe(rets_net.tolist(), periods_per_year=int(rebalances_per_year))

    res4 = run_regression(rets, factors[[*_CARHART_FACTORS, "RF"]], _CARHART_FACTORS)

    bench_aligned = bench_rets.reindex(rets.index).dropna()
    excess_per_rebal = (rets.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann = float(excess_per_rebal * 252) if not np.isnan(excess_per_rebal) else float("nan")

    mean_top_n = float(
        sum(len(r.top_n_tickers) for r in report.rebalance_results)
        / max(1, len(report.rebalance_results))
    )
    return {
        "n": len(rets),
        "mean_top_n": mean_top_n,
        "turnover_per_rebal": avg_turnover,
        "sharpe_gross": sharpe_gross,
        "sharpe_net": sharpe_net,
        "alpha_gross_4f": float(res4.alpha_annualized),
        "t_4f": float(res4.alpha_tstat),
        "beta_mom": float(res4.betas.get("Mom", 0.0)),
        "beta_smb": float(res4.betas.get("SMB", 0.0)),
        "beta_hml": float(res4.betas.get("HML", 0.0)),
        "cost_drag_ann": drag_ann,
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "excess_vs_bench_ann": excess_ann,
        "excess_vs_bench_net": excess_ann - drag_ann,
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--vol-weights", nargs="+", type=float, default=[0.5, 1.0, 2.0])
    ap.add_argument(
        "--adv-thresholds",
        nargs="+",
        type=float,
        default=[1_000_000, 5_000_000, 20_000_000],
    )
    ap.add_argument(
        "--cost-half-spreads",
        nargs="+",
        type=float,
        default=[5.0, 15.0],
    )
    ap.add_argument("--out", type=Path, default=Path("docs/research/momentum_lowvol_combo.md"))
    ap.add_argument("--is-start", type=date.fromisoformat, default=date(2011, 1, 1))
    ap.add_argument("--is-end", type=date.fromisoformat, default=date(2022, 12, 31))
    ap.add_argument("--oos-start", type=date.fromisoformat, default=date(2023, 1, 1))
    ap.add_argument("--oos-end", type=date.fromisoformat, default=date(2026, 4, 22))
    ap.add_argument(
        "--lock-universe",
        action="store_true",
        help=(
            "Use the full --is-start..--oos-end PIT union for every period. "
            "Required for subsample stability checks so halves and full IS "
            "draw from the same ticker pool."
        ),
    )
    ap.add_argument(
        "--phase-offset",
        type=int,
        default=0,
        help=(
            "Sampling phase for the strided rebalance calendar. "
            "0..rebalance_stride-1; default 0. Required for honest subsample "
            "stability checks (see "
            "docs/research/methodology_audit_2026_04_29.md)."
        ),
    )
    return ap


def main() -> int:
    ap = _build_parser()
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    full_universe = load_pit_union(args.is_start, args.oos_end)
    histories = load_cached_histories([*full_universe, args.benchmark], _PRICES_DIR)
    history_store = HistoryStore(histories)

    periods = [
        (f"IS {args.is_start.year}-{args.is_end.year}", args.is_start, args.is_end),
        (f"OOS {args.oos_start.year}-{args.oos_end.year}", args.oos_start, args.oos_end),
    ]

    sections: list[str] = [
        "# Momentum + low-vol combo — Asness-Frazzini-Pedersen quality proxy",
        "",
        "**RESEARCH ONLY.** Pure 12-1m momentum failed catastrophically OOS",
        "(`momentum_constrained.md`: -47% to -89% excess vs SPY at all ADV thresholds).",
        "Hypothesis: low-vol filter (Frazzini-Pedersen 2014 BAB / Asness et al QMJ) hedges",
        'against momentum crashes by filtering out high-vol "junk momentum" names that',
        "collapse hardest in regime shifts.",
        "",
        "- Score: z(mom_12_1m) − vol_weight × z(vol_60d) (per-rebalance cross-sectional z)",
        f"- Top-N: {args.top_n}, holding-signal: {args.holding}d, stride: {args.rebalance_stride}",
        f"- Vol weights tested: {args.vol_weights} (0.5 = mostly momentum, 2.0 = mostly low-vol)",
        f"- ADV thresholds: {[f'${t / 1e6:.0f}M' for t in args.adv_thresholds]}",
        "",
    ]

    all_rows: list[dict] = []
    for label, start, end in periods:
        universe = full_universe if args.lock_universe else load_pit_union(start, end)
        carhart = load_carhart_daily(start=start, end=end)
        bench_rets = benchmark_returns(history_store, args.benchmark, start, end)
        logger.info("=== %s | universe %d ===", label, len(universe))

        for vol_weight in args.vol_weights:
            for adv_min in args.adv_thresholds:
                logger.info("vol_weight=%.1f ADV ≥ $%.0fM", vol_weight, adv_min / 1e6)
                config = {
                    "benchmark": args.benchmark,
                    "_adv_min_usd": adv_min,
                    "_vol_weight": vol_weight,
                }
                engine = BacktestEngine(
                    history_store,
                    scorer=momentum_lowvol_adapter,
                    scorer_config=config,
                    holding_period=args.holding,
                    top_n=args.top_n,
                    benchmark=args.benchmark,
                    screener_tickers=universe,
                    weighting="linear",
                    rebalance_stride=args.rebalance_stride,
                    phase_offset=args.phase_offset,
                )
                report = engine.run(start, end)
                for cost_bps in args.cost_half_spreads:
                    if cost_bps != args.cost_half_spreads[0]:
                        # Skip 15bps to halve output volume; still computed in IS but
                        # primary metric we focus on is 5bps net for retail-realistic cost.
                        pass
                    stats = assess(report, carhart, args.rebalance_stride, cost_bps, bench_rets)
                    stats["period"] = label
                    stats["adv_min_m"] = adv_min / 1e6
                    stats["cost_bps"] = cost_bps
                    stats["vol_weight"] = vol_weight
                    all_rows.append(stats)
                    if stats.get("n", 0) > 0:
                        logger.info(
                            "%s | vw=%.1f ADV≥$%.0fM cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
                            "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | α 4F=%.1f%% t=%.2f",
                            label,
                            vol_weight,
                            adv_min / 1e6,
                            cost_bps,
                            stats["n"],
                            stats["mean_top_n"],
                            stats["turnover_per_rebal"] * 100,
                            stats["sharpe_gross"],
                            stats["sharpe_net"],
                            stats["excess_vs_bench_ann"] * 100,
                            stats["excess_vs_bench_net"] * 100,
                            stats["alpha_gross_4f"] * 100,
                            stats["t_4f"],
                        )

    sections.append("## Results")
    sections.append("")
    sections.append(
        "| Period | vol_w | ADV | cost | mean topN | turn | Sharpe gross | Sharpe net | "
        "excess gross | excess net | α 4F | t (4F) | β_MOM |"
    )
    sections.append("|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_rows:
        if r.get("n", 0) == 0:
            continue
        sections.append(
            "| {p} | {vw:.1f} | $${adv:.0f}M | {cb:.0f}bp | {tn:.1f} | {tr:.1f}% | "
            "{sg:.2f} | {sn:.2f} | {eg:+.1f}% | {en:+.1f}% | {a4:+.1f}% | {t4:+.2f} | {bm:.2f} |".format(
                p=r["period"],
                vw=r["vol_weight"],
                adv=r["adv_min_m"],
                cb=r["cost_bps"],
                tn=r["mean_top_n"],
                tr=r["turnover_per_rebal"] * 100,
                sg=r["sharpe_gross"],
                sn=r["sharpe_net"],
                eg=r["excess_vs_bench_ann"] * 100,
                en=r["excess_vs_bench_net"] * 100,
                a4=r["alpha_gross_4f"] * 100,
                t4=r["t_4f"],
                bm=r["beta_mom"],
            ).replace("$$", "$")
        )

    sections.append("")
    sections.append("## Decision criteria")
    sections.append("")
    sections.append(
        "- **CANDIDATE**: OOS net Sharpe ≥ 0.4 AND OOS excess vs SPY ≥ 0%/y at ADV ≥ $5M with 5bps cost."
    )
    sections.append(
        "- **CLOSED**: OOS net excess vs SPY < 0 across all (vol_w, ADV) configurations at 5bps cost."
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
