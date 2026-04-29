"""Pure 12-1m Jegadeesh-Titman momentum + ADV liquidity filter + cost stress.

Direct counterpart to `experiment_constrained_contrarian.py`. After
contrarian was closed for retail capital deployment (OOS net α < 0 at ADV ≥
$5M), this script asks the orthogonal question: does pure 12-1 month
momentum (Jegadeesh-Titman 1993) survive realistic constraints on the same
PIT universe?

Score: ret(t-21d → t-252d), i.e. cumulative log-return from 12 months ago
to 1 month ago. Skipping the last month is the canonical Jegadeesh-Titman
specification (avoids 1-month reversal contamination, which we saw
operating in the contrarian work).

A momentum strategy will load heavily on Carhart's MOM factor by
construction; Carhart α is expected near zero. The economically meaningful
metric is *net Sharpe* and *total excess return vs benchmark* after costs.
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
_CARHART_PLUS_STR = [*_CARHART_FACTORS, "STR"]


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


def constrained_momentum_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:
    """Score = close[-21] / close[-252] - 1 (12-1m return), filtered by ADV."""
    config = dict(config or {})
    benchmark = config.get("benchmark")
    adv_min = float(config.get("_adv_min_usd", 0.0))
    adv_window = int(config.get("_adv_window", 60))
    formation_start = int(config.get("_formation_start", 252))
    formation_end = int(config.get("_formation_end", 21))

    rows: list[dict] = []
    for ticker, df in histories.items():
        if ticker == benchmark or df is None or len(df) < formation_start + 1:
            continue
        closes = df["close"].to_numpy(dtype=float)
        volumes = df["volume"].to_numpy(dtype=float)
        if closes[-1] <= 0 or closes[-formation_start - 1] <= 0 or closes[-formation_end - 1] <= 0:
            continue
        dollar_vol = closes[-adv_window:] * volumes[-adv_window:]
        adv = float(np.median(dollar_vol[dollar_vol > 0])) if (dollar_vol > 0).any() else 0.0
        if adv < adv_min:
            continue
        # 12-1m return: from 12 months ago to 1 month ago
        score = closes[-formation_end - 1] / closes[-formation_start - 1] - 1.0
        rows.append({"ticker": ticker, "score": float(score), "adv_usd": adv})
    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])
    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


constrained_momentum_adapter.MIN_BARS_REQUIRED = 253


def load_str_factor(start: date, end: date) -> pd.Series:
    df = pd.read_csv(_STR_PATH, parse_dates=["date"], index_col="date")
    s = df["STR"]
    s.index = pd.DatetimeIndex(s.index).tz_localize(None)
    return s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]


def merge_factors(carhart: pd.DataFrame, str_factor: pd.Series) -> pd.DataFrame:
    merged = carhart.copy()
    merged.index = pd.DatetimeIndex(merged.index).tz_localize(None)
    merged["STR"] = str_factor.reindex(merged.index)
    return merged.dropna(subset=["STR"])


def benchmark_returns(
    history_store: HistoryStore, benchmark: str, start: date, end: date
) -> pd.Series:
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def assess(
    report,
    factors_5: pd.DataFrame,
    rebalance_stride: int,
    cost_half_spread_bps: float,
    benchmark_rets: pd.Series,
) -> dict:
    rets = report.portfolio_returns
    if rets.empty:
        return {"n": 0}
    rebalances_per_year = 252 / max(1, rebalance_stride)
    sharpe_gross = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))
    avg_turnover = turnover_pct(r.top_n_tickers for r in report.rebalance_results)

    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_per_rebal_bps = cost_model.primary_period_drag_bps(cost_half_spread_bps, avg_turnover)
    drag_ann = drag_per_rebal_bps * rebalances_per_year / 10_000.0

    # Sharpe net of cost drag (subtract drag from each rebalance return)
    drag_per_rebal = drag_per_rebal_bps / 10_000.0
    rets_net = rets - drag_per_rebal
    sharpe_net = sharpe(rets_net.tolist(), periods_per_year=int(rebalances_per_year))

    res4 = run_regression(rets, factors_5[[*_CARHART_FACTORS, "RF"]], _CARHART_FACTORS)
    res5 = run_regression(rets, factors_5, _CARHART_PLUS_STR)

    bench_aligned = benchmark_rets.reindex(rets.index).dropna()
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
        "r2_4f": float(res4.r_squared),
        "alpha_gross_5f": float(res5.alpha_annualized),
        "t_5f": float(res5.alpha_tstat),
        "beta_mom": float(res4.betas.get("Mom", 0.0)),
        "beta_str": float(res5.betas.get("STR", 0.0)),
        "cost_drag_ann": drag_ann,
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "excess_vs_bench_ann": excess_ann,
        "excess_vs_bench_net": excess_ann - drag_ann,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument(
        "--adv-thresholds",
        nargs="+",
        type=float,
        default=[0, 1_000_000, 5_000_000, 20_000_000, 100_000_000],
    )
    ap.add_argument(
        "--cost-half-spreads",
        nargs="+",
        type=float,
        default=[5.0, 15.0],
    )
    ap.add_argument("--out", type=Path, default=Path("docs/research/momentum_constrained.md"))
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    full_universe = load_pit_union(date(2011, 1, 1), date(2026, 4, 22))
    histories = load_cached_histories([*full_universe, args.benchmark], _PRICES_DIR)
    history_store = HistoryStore(histories)

    periods = [
        ("Full IS 2011-2022", date(2011, 1, 1), date(2022, 12, 31)),
        ("OOS 2023-2026", date(2023, 1, 1), date(2026, 4, 22)),
    ]

    sections: list[str] = [
        "# Constrained 12-1m momentum — ADV liquidity floor + transaction cost stress",
        "",
        "**RESEARCH ONLY.** Counterpart to `contrarian_constrained.md`. After the contrarian",
        "angle was closed for retail capital deployment, this script tests whether pure",
        "12-1 month Jegadeesh-Titman momentum survives the same ADV + cost framework.",
        "",
        f"- Top-N: {args.top_n}, holding-signal: {args.holding}d, stride: {args.rebalance_stride}",
        "- Score: close[t−21] / close[t−252] − 1 (cumulative return from 12 months ago to 1 month ago)",
        "- Cost model: RealisticCostModel(adverse=5bps); Sharpe net subtracts drag from each rebalance return",
        f"- ADV thresholds: {[f'${t / 1e6:.0f}M' for t in args.adv_thresholds]}",
        "",
        "*Note*: a momentum strategy loads heavily on Carhart's MOM factor; Carhart-4F α is",
        "expected near zero by construction. The economically meaningful metrics are net",
        "Sharpe and excess return vs SPY benchmark.",
        "",
    ]

    all_rows: list[dict] = []
    for label, start, end in periods:
        universe = load_pit_union(start, end)
        carhart = load_carhart_daily(start=start, end=end)
        str_factor = load_str_factor(start=start, end=end)
        factors_5 = merge_factors(carhart, str_factor)
        bench_rets = benchmark_returns(history_store, args.benchmark, start, end)
        logger.info(
            "=== %s | universe %d | bench rets %d ===", label, len(universe), len(bench_rets)
        )

        for adv_min in args.adv_thresholds:
            logger.info("ADV ≥ $%.0fM", adv_min / 1e6)
            config = {
                "benchmark": args.benchmark,
                "_adv_min_usd": adv_min,
            }
            engine = BacktestEngine(
                history_store,
                scorer=constrained_momentum_adapter,
                scorer_config=config,
                holding_period=args.holding,
                top_n=args.top_n,
                benchmark=args.benchmark,
                screener_tickers=universe,
                weighting="linear",
                rebalance_stride=args.rebalance_stride,
            )
            report = engine.run(start, end)
            for cost_bps in args.cost_half_spreads:
                stats = assess(report, factors_5, args.rebalance_stride, cost_bps, bench_rets)
                stats["period"] = label
                stats["adv_min_m"] = adv_min / 1e6
                stats["cost_bps"] = cost_bps
                all_rows.append(stats)
                if stats.get("n", 0) > 0:
                    logger.info(
                        "%s | ADV≥$%.0fM cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
                        "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
                        "α 4F=%.1f%% t=%.2f β_MOM=%.2f",
                        label,
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
                        stats["beta_mom"],
                    )

    sections.append("## Results — gross / net (cost-stressed)")
    sections.append("")
    sections.append(
        "| Period | ADV floor | cost | mean topN | turn | Sharpe gross | Sharpe net | "
        "excess gross | excess net | α 4F | t (4F) | β_MOM | β_STR |"
    )
    sections.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_rows:
        if r.get("n", 0) == 0:
            continue
        sections.append(
            "| {p} | $${adv:.0f}M | {cb:.0f}bp | {tn:.1f} | {tr:.1f}% | "
            "{sg:.2f} | {sn:.2f} | {eg:+.1f}% | {en:+.1f}% | {a4:+.1f}% | {t4:+.2f} | {bm:.2f} | {bs:.2f} |".format(
                p=r["period"],
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
                bs=r["beta_str"],
            ).replace("$$", "$")
        )

    sections.append("")
    sections.append("## Decision criteria")
    sections.append("")
    sections.append(
        "- **CANDIDATE**: OOS net Sharpe ≥ 0.5 AND OOS excess vs benchmark net ≥ 5%/y "
        "AND ADV-stable across $5M+, $20M+."
    )
    sections.append(
        "- **MOMENTUM ANGLE CLOSED for retail**: OOS net excess vs benchmark < 0 at ADV ≥ $5M with 15bps."
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
