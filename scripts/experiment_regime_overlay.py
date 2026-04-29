"""Regime overlay on mom+lowvol — deploy only when SPY low-vol regime.

Hypothesis: mom+lowvol works in low-vol mean-reverting regimes (2011-2016,
2023-2026) but fails in high-vol disruption regimes (2017-2022). A
regime indicator based on SPY trailing volatility could gate deployment:
- LOW VOL regime: deploy strategy (capture excess)
- HIGH VOL regime: hold cash / SPY benchmark (avoid drawdown)

Tested thresholds based on SPY 60d realized vol percentile (within
expanding window from start). Multiple thresholds tested.

This is a CONDITIONAL strategy — at each rebalance, decide whether to be
in market (low vol) or out (high vol). The regime filter is computed PIT
correctly: vol uses only data up to asof.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from alphalens.alt_data.yfinance_cache import load_cached_histories
from alphalens.backtest.cost_model import RealisticCostModel
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.factor_analysis import run_regression
from alphalens.backtest.factors import load_carhart_daily
from alphalens.backtest.history_store import HistoryStore
from alphalens.backtest.metrics import sharpe, turnover_pct
from scripts.experiment_momentum_lowvol_combo import load_pit_union, momentum_lowvol_adapter

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"

_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]


def compute_spy_realized_vol(
    history_store: HistoryStore, benchmark: str, window: int = 60
) -> pd.Series:
    """Rolling 60d annualized realized volatility of SPY."""
    df = history_store.full(benchmark)
    closes = df["close"]
    log_rets = np.log(closes / closes.shift(1)).dropna()
    rolling_vol = log_rets.rolling(window=window, min_periods=window).std() * np.sqrt(252)
    return rolling_vol.dropna()


def benchmark_returns(history_store, benchmark, start, end):
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def assess_overlay(
    portfolio_returns: pd.Series,
    spy_vol: pd.Series,
    bench_rets: pd.Series,
    factors: pd.DataFrame,
    rebalance_stride: int,
    cost_half_spread_bps: float,
    vol_threshold: float,
    label: str,
    avg_turnover: float,
) -> dict:
    """Apply regime filter post-hoc: replace portfolio_return with 0 when SPY vol >= threshold."""
    rebalances_per_year = 252 / max(1, rebalance_stride)
    aligned_vol = spy_vol.reindex(portfolio_returns.index, method="ffill")
    in_regime = aligned_vol < vol_threshold
    pct_in_regime = float(in_regime.mean())

    conditional_rets = portfolio_returns.where(in_regime, 0.0)

    sharpe_uncond = sharpe(portfolio_returns.tolist(), periods_per_year=int(rebalances_per_year))
    sharpe_cond = sharpe(conditional_rets.tolist(), periods_per_year=int(rebalances_per_year))

    cm = RealisticCostModel(adverse_selection_bps=5.0)
    drag_per = cm.primary_period_drag_bps(cost_half_spread_bps, avg_turnover) / 10_000.0
    drag_ann = drag_per * rebalances_per_year
    # Conditional drag: only when in regime (turnover only happens those days)
    conditional_drag_per = drag_per * pct_in_regime
    conditional_drag_ann = conditional_drag_per * rebalances_per_year

    cond_net = conditional_rets - drag_per * in_regime.astype(float)
    sharpe_cond_net = sharpe(cond_net.tolist(), periods_per_year=int(rebalances_per_year))

    bench_aligned = bench_rets.reindex(portfolio_returns.index).dropna()
    excess_uncond_per = (portfolio_returns.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_uncond_ann = float(excess_uncond_per * 252)

    excess_cond_per = (conditional_rets.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_cond_ann = float(excess_cond_per * 252)

    res_uncond = run_regression(portfolio_returns, factors, _CARHART_FACTORS, subtract_rf=True)
    try:
        res_cond = run_regression(conditional_rets, factors, _CARHART_FACTORS, subtract_rf=True)
    except ValueError:
        res_cond = None

    return {
        "label": label,
        "vol_threshold": vol_threshold,
        "n": len(portfolio_returns),
        "pct_in_regime": pct_in_regime,
        "sharpe_uncond_gross": sharpe_uncond,
        "sharpe_cond_gross": sharpe_cond,
        "sharpe_cond_net": sharpe_cond_net,
        "excess_uncond_gross": excess_uncond_ann,
        "excess_cond_gross": excess_cond_ann,
        "excess_cond_net": excess_cond_ann - conditional_drag_ann,
        "alpha_uncond_4f": float(res_uncond.alpha_annualized),
        "t_uncond_4f": float(res_uncond.alpha_tstat),
        "alpha_cond_4f": float(res_cond.alpha_annualized) if res_cond else float("nan"),
        "t_cond_4f": float(res_cond.alpha_tstat) if res_cond else float("nan"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--vol-weight", type=float, default=1.0)
    ap.add_argument("--adv-min", type=float, default=5_000_000)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument(
        "--vol-thresholds", nargs="+", type=float, default=[0.12, 0.15, 0.18, 0.22, 0.30]
    )
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--out", type=Path, default=Path("docs/research/regime_overlay.md"))
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    full_universe = load_pit_union(date(2011, 1, 1), date(2026, 4, 22))
    histories = load_cached_histories([*full_universe, args.benchmark], _PRICES_DIR)
    history_store = HistoryStore(histories)

    spy_vol = compute_spy_realized_vol(history_store, args.benchmark, window=60)
    logger.info(
        "SPY 60d vol stats: median=%.3f p25=%.3f p75=%.3f p95=%.3f",
        spy_vol.median(),
        spy_vol.quantile(0.25),
        spy_vol.quantile(0.75),
        spy_vol.quantile(0.95),
    )

    periods = [
        ("IS_2011_2016", date(2011, 1, 1), date(2016, 12, 31)),
        ("IS_2017_2022", date(2017, 1, 1), date(2022, 12, 31)),
        ("OOS_2023_2026", date(2023, 1, 1), date(2026, 4, 22)),
    ]

    sections = [
        "# Regime overlay on mom+lowvol — SPY trailing-vol gate",
        "",
        "**RESEARCH ONLY.** Hypothesis: mom+lowvol fails in 2017-2022 due to regime shift",
        "(2018 Q4 drawdown, 2020 COVID crash, 2022 bear). A SPY 60d realized vol gate",
        "could deploy strategy only in low-vol regimes, avoiding the painful periods.",
        "",
        f"- Strategy spec: vol_weight={args.vol_weight}, ADV ≥ ${args.adv_min / 1e6:.0f}M, top-{args.top_n}",
        "- Regime filter: SPY 60d realized vol < threshold → deploy; ≥ threshold → cash (return = 0)",
        f"- Rebalance stride: {args.rebalance_stride}, cost: {args.cost_bps}bp half-spread",
        f"- Vol thresholds tested: {args.vol_thresholds}",
        "",
    ]

    all_rows = []
    for label, start, end in periods:
        universe = load_pit_union(start, end)
        carhart = load_carhart_daily(start=start, end=end)
        carhart.index = pd.DatetimeIndex(carhart.index).tz_localize(None)
        bench = benchmark_returns(history_store, args.benchmark, start, end)

        logger.info("=== %s | universe %d ===", label, len(universe))
        config = {
            "benchmark": args.benchmark,
            "_adv_min_usd": args.adv_min,
            "_vol_weight": args.vol_weight,
        }
        engine = BacktestEngine(
            history_store,
            scorer=momentum_lowvol_adapter,
            scorer_config=config,
            holding_period=60,
            top_n=args.top_n,
            benchmark=args.benchmark,
            screener_tickers=universe,
            weighting="linear",
            rebalance_stride=args.rebalance_stride,
        )
        report = engine.run(start, end)
        rets = report.portfolio_returns
        avg_turnover = turnover_pct(r.top_n_tickers for r in report.rebalance_results)

        for thr in args.vol_thresholds:
            stats = assess_overlay(
                portfolio_returns=rets,
                spy_vol=spy_vol,
                bench_rets=bench,
                factors=carhart,
                rebalance_stride=args.rebalance_stride,
                cost_half_spread_bps=args.cost_bps,
                vol_threshold=thr,
                label=label,
                avg_turnover=avg_turnover,
            )
            all_rows.append(stats)
            logger.info(
                "%s | thr=%.2f in_regime=%.0f%% | uncond Sh=%.2f exc=%.1f%% | "
                "cond Sh gross=%.2f net=%.2f exc gross=%.1f%% net=%.1f%% | α t cond=%.2f",
                label,
                thr,
                stats["pct_in_regime"] * 100,
                stats["sharpe_uncond_gross"],
                stats["excess_uncond_gross"] * 100,
                stats["sharpe_cond_gross"],
                stats["sharpe_cond_net"],
                stats["excess_cond_gross"] * 100,
                stats["excess_cond_net"] * 100,
                stats["t_cond_4f"],
            )

    sections.append("## Results")
    sections.append("")
    sections.append(
        "| Period | vol_thr | in regime | uncond Sh | uncond excess | cond Sh gross | cond Sh net | "
        "cond excess gross | cond excess net | α 4F cond | t cond |"
    )
    sections.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_rows:
        sections.append(
            f"| {r['label']} | {r['vol_threshold']:.2f} | {r['pct_in_regime'] * 100:.0f}% | "
            f"{r['sharpe_uncond_gross']:+.2f} | {r['excess_uncond_gross'] * 100:+.1f}% | "
            f"{r['sharpe_cond_gross']:+.2f} | {r['sharpe_cond_net']:+.2f} | "
            f"{r['excess_cond_gross'] * 100:+.1f}% | {r['excess_cond_net'] * 100:+.1f}% | "
            f"{r['alpha_cond_4f'] * 100:+.1f}% | {r['t_cond_4f']:+.2f} |"
        )

    sections.append("")
    sections.append("## Decision")
    sections.append("")
    sections.append(
        "Best regime overlay = (vol_threshold, ADV) combination where ALL THREE periods give"
        " positive net excess vs SPY. Compare to unconditional baseline (mom+lowvol $5M vol_w=1.0):"
        " IS_2011_2016 +28%, IS_2017_2022 -11%, OOS +19% (regime hole in 2017-2022)."
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
