"""Pure-contrarian + ADV liquidity filter + transaction cost stress.

Layer 2d work (`docs/research/layer2d_definitive_synthesis.md`) established
that pure-contrarian on the unrestricted PIT universe shows OOS α ~150%/y but
implied vol ~180%/y — likely tail-rebound artifacts of un-tradeable names.
This script tests whether α survives realistic constraints:

  1. ADV ≥ threshold (median 60d dollar volume) — liquidity filter
  2. Per-rebalance cost drag (5 bps half-spread primary, 15 bps stress)
  3. Per-subperiod PIT universe (no non-contemporaneous ticker leakage)

Decision criteria for "candidate strategy":
  - OOS net Sharpe ≥ 0.5 at $5M ADV + 5bps cost
  - OOS net Carhart α t-stat ≥ 1.0
  - α stable across ADV thresholds (not collapsing as we get more liquid)
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

from alphalens.backtest.cost_model import RealisticCostModel
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.factor_analysis import run_regression
from alphalens.backtest.metrics import sharpe, turnover_pct
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import load_carhart_daily
from alphalens.data.store.history import HistoryStore

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


def constrained_contrarian_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:
    """Score = -ret_60d + bounce_weight * ret_5d, filtered by 60d-median dollar ADV >= adv_min_usd."""
    config = dict(config or {})
    benchmark = config.get("benchmark")
    bounce_weight = float(config.get("_bounce_weight", 0.5))
    adv_min = float(config.get("_adv_min_usd", 0.0))
    adv_window = int(config.get("_adv_window", 60))

    rows: list[dict] = []
    for ticker, df in histories.items():
        if ticker == benchmark or df is None or len(df) < max(65, adv_window + 1):
            continue
        closes = df["close"].to_numpy(dtype=float)
        volumes = df["volume"].to_numpy(dtype=float)
        if closes[-1] <= 0 or closes[-61] <= 0 or closes[-6] <= 0:
            continue
        dollar_vol = closes[-adv_window:] * volumes[-adv_window:]
        adv = float(np.median(dollar_vol[dollar_vol > 0])) if (dollar_vol > 0).any() else 0.0
        if adv < adv_min:
            continue
        ret_60d = closes[-1] / closes[-61] - 1.0
        ret_5d = closes[-1] / closes[-6] - 1.0
        score = -ret_60d + bounce_weight * ret_5d
        rows.append({"ticker": ticker, "score": float(score), "adv_usd": adv})
    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])
    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


constrained_contrarian_adapter.MIN_BARS_REQUIRED = 65


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


def run_one_backtest(
    history_store: HistoryStore,
    universe: list[str],
    benchmark: str,
    start: date,
    end: date,
    adv_min: float,
    bounce_weight: float,
    top_n: int,
    holding: int,
    rebalance_stride: int,
    phase_offset: int = 0,
):
    config = {
        "benchmark": benchmark,
        "_adv_min_usd": adv_min,
        "_bounce_weight": bounce_weight,
    }
    engine = BacktestEngine(
        history_store,
        scorer=constrained_contrarian_adapter,
        scorer_config=config,
        holding_period=holding,
        top_n=top_n,
        benchmark=benchmark,
        screener_tickers=universe,
        weighting="linear",
        rebalance_stride=rebalance_stride,
        phase_offset=phase_offset,
    )
    return engine.run(start, end)


def assess(
    report,
    factors_5: pd.DataFrame,
    rebalance_stride: int,
    cost_half_spread_bps: float,
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

    res4 = run_regression(rets, factors_5[[*_CARHART_FACTORS, "RF"]], _CARHART_FACTORS)
    res5 = run_regression(rets, factors_5, _CARHART_PLUS_STR)

    mean_top_n = float(
        sum(len(r.top_n_tickers) for r in report.rebalance_results)
        / max(1, len(report.rebalance_results))
    )
    return {
        "n": len(rets),
        "mean_top_n": mean_top_n,
        "turnover_per_rebal": avg_turnover,
        "sharpe_gross": sharpe_gross,
        "alpha_gross_4f": float(res4.alpha_annualized),
        "t_4f": float(res4.alpha_tstat),
        "r2_4f": float(res4.r_squared),
        "alpha_gross_5f": float(res5.alpha_annualized),
        "t_5f": float(res5.alpha_tstat),
        "r2_5f": float(res5.r_squared),
        "beta_str": float(res5.betas.get("STR", 0.0)),
        "cost_drag_ann": drag_ann,
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "alpha_net_5f": float(res5.alpha_annualized) - drag_ann,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument(
        "--phase-offset",
        type=int,
        default=0,
        help="Phase offset for strided rebalance calendar; 0..rebalance_stride-1. "
        "Required for honest multi-phase audit (see audit_multi_phase.py).",
    )
    ap.add_argument("--bounce-weight", type=float, default=0.5)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument(
        "--adv-thresholds",
        nargs="+",
        type=float,
        default=[0, 1_000_000, 5_000_000, 20_000_000, 100_000_000],
        help="Median 60d dollar-volume floor in USD",
    )
    ap.add_argument(
        "--cost-half-spreads",
        nargs="+",
        type=float,
        default=[5.0, 15.0],
        help="Half-spread bps for cost stress",
    )
    ap.add_argument("--out", type=Path, default=Path("docs/research/contrarian_constrained.md"))
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
        "# Constrained pure-contrarian — ADV liquidity floor + transaction cost stress",
        "",
        "**RESEARCH ONLY.** Tests whether the small-cap contrarian premium documented",
        "in `docs/research/layer2d_definitive_synthesis.md` survives realistic deployment",
        "constraints. Score: −60d_return + 0.5 × 5d_return; portfolio: top-15 by score from",
        "PIT universe filtered to 60d-median dollar ADV ≥ threshold. Each rebalance",
        "(weekly, stride=5) computes ADV per ticker live; non-PIT names excluded.",
        "",
        f"- Top-N: {args.top_n}, holding-signal: {args.holding}d, stride: {args.rebalance_stride}",
        "- Cost model: RealisticCostModel(adverse=5bps), drag = round_trip × turnover × stride/year",
        f"- ADV thresholds tested: {[f'${t / 1e6:.0f}M' for t in args.adv_thresholds]}",
        "",
    ]

    all_rows: list[dict] = []
    for label, start, end in periods:
        universe = load_pit_union(start, end)
        carhart = load_carhart_daily(start=start, end=end)
        str_factor = load_str_factor(start=start, end=end)
        factors_5 = merge_factors(carhart, str_factor)
        logger.info("=== %s | universe %d ===", label, len(universe))

        for adv_min in args.adv_thresholds:
            logger.info("ADV ≥ $%.0fM", adv_min / 1e6)
            report = run_one_backtest(
                history_store=history_store,
                universe=universe,
                benchmark=args.benchmark,
                start=start,
                end=end,
                adv_min=adv_min,
                bounce_weight=args.bounce_weight,
                top_n=args.top_n,
                holding=args.holding,
                rebalance_stride=args.rebalance_stride,
                phase_offset=args.phase_offset,
            )
            for cost_bps in args.cost_half_spreads:
                stats = assess(report, factors_5, args.rebalance_stride, cost_bps)
                stats["period"] = label
                stats["adv_min_m"] = adv_min / 1e6
                stats["cost_bps"] = cost_bps
                all_rows.append(stats)
                if stats.get("n", 0) > 0:
                    logger.info(
                        "%s | ADV≥$%.0fM cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | Sharpe %.2f | "
                        "α 4F gross=%.1f%% net=%.1f%% t=%.2f | β_STR=%.2f",
                        label,
                        adv_min / 1e6,
                        cost_bps,
                        stats["n"],
                        stats["mean_top_n"],
                        stats["turnover_per_rebal"] * 100,
                        stats["sharpe_gross"],
                        stats["alpha_gross_4f"] * 100,
                        stats["alpha_net_4f"] * 100,
                        stats["t_4f"],
                        stats["beta_str"],
                    )

    # Per-period table
    sections.append("## Results — gross / net (cost-stressed)")
    sections.append("")
    sections.append(
        "| Period | ADV floor | cost (bps half-spread) | mean top-N | turnover/rebal | Sharpe gross | "
        "α 4F gross | t (4F) | drag/y | α 4F net | α 5F net | β_STR |"
    )
    sections.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_rows:
        if r.get("n", 0) == 0:
            sections.append(
                f"| {r['period']} | $%.0fM | %.0fbps | – | – | – | – | – | – | – | – | – |"
                % (r["adv_min_m"], r["cost_bps"])
            )
            continue
        sections.append(
            "| {p} | $${adv:.0f}M | {cb:.0f} | {tn:.1f} | {tr:.1f}% | {sh:.2f} | "
            "{ag:.1f}% | {t4:.2f} | {dr:.2f}% | {an4:.1f}% | {an5:.1f}% | {bs:.2f} |".format(
                p=r["period"],
                adv=r["adv_min_m"],
                cb=r["cost_bps"],
                tn=r["mean_top_n"],
                tr=r["turnover_per_rebal"] * 100,
                sh=r["sharpe_gross"],
                ag=r["alpha_gross_4f"] * 100,
                t4=r["t_4f"],
                dr=r["cost_drag_ann"] * 100,
                an4=r["alpha_net_4f"] * 100,
                an5=r["alpha_net_5f"] * 100,
                bs=r["beta_str"],
            ).replace("$$", "$")
        )

    sections.append("")
    sections.append("## Decision criteria")
    sections.append("")
    sections.append(
        "- **CANDIDATE for Phase-3 validation**: OOS net 4F α t-stat ≥ 1.0 AND OOS Sharpe ≥ 0.5 "
        "AND α stable across ADV ≥ $5M, $20M (sign of robustness)."
    )
    sections.append(
        "- **CONTRARIAN ANGLE CLOSED for retail capital**: OOS net α drops below 5%/y at any "
        "of ADV ≥ $5M with 15bps cost stress."
    )
    sections.append(
        "- **MID — needs more research**: Sharpe between 0.3-0.5; possibly investable "
        "with refined sizing/holding period (next steps: longer holding, weighting variants)."
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
