"""Long-short mom+lowvol — market-neutral spread strategy.

Hypothesis: the long-only mom+lowvol combo lost 11%/y in IS 2017-2022 because
mid-cap-tilted top-15 underperformed mega-cap SPY benchmark. A long-short
spread would have hedged that benchmark drift. Asness-Frazzini-Pedersen 2018
documented MOM-FACTOR (long-short) Sharpe ~0.6-0.8 historically.

Implementation: at each rebalance, score full PIT universe (filtered by ADV).
- LONG = equal-weight top-15 by score
- SHORT = equal-weight bottom-15 by score
- Daily return = mean(long_fwd_1d) − mean(short_fwd_1d)
- Compare both vs SPY (long-only) and zero (long-short, market-neutral)

Cost: long-short doubles transaction cost (you trade both legs).
RealisticCostModel adverse_selection_bps=5 + 5bps half-spread per leg.
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
import yaml

from alphalens.alt_data.yfinance_cache import load_cached_histories
from alphalens.backtest.cost_model import RealisticCostModel
from alphalens.backtest.factor_analysis import run_regression
from alphalens.backtest.factors import load_carhart_daily
from alphalens.backtest.history_store import HistoryStore
from alphalens.backtest.metrics import sharpe

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"

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


def score_universe(
    history_store: HistoryStore,
    universe: list[str],
    asof: date,
    *,
    adv_min: float,
    vol_weight: float,
    adv_window: int = 60,
    vol_window: int = 60,
) -> pd.DataFrame:
    rows: list[dict] = []
    for ticker in universe:
        df = history_store.truncate_to(ticker, asof)
        if len(df) < 253:
            continue
        closes = df["close"].to_numpy(dtype=float)
        volumes = df["volume"].to_numpy(dtype=float)
        if closes[-1] <= 0 or closes[-253] <= 0 or closes[-22] <= 0:
            continue
        dollar_vol = closes[-adv_window:] * volumes[-adv_window:]
        if not (dollar_vol > 0).any():
            continue
        adv = float(np.median(dollar_vol[dollar_vol > 0]))
        if adv < adv_min:
            continue
        mom = closes[-22] / closes[-253] - 1.0
        rets = np.diff(np.log(closes[-vol_window - 1 :]))
        if len(rets) < vol_window // 2 or np.any(~np.isfinite(rets)):
            continue
        vol = float(np.std(rets, ddof=1) * np.sqrt(252))
        if not np.isfinite(vol) or vol <= 0:
            continue
        # 1-day forward return
        fwd = history_store.forward_return(ticker, asof, 1)
        if fwd is None:
            continue
        rows.append({"ticker": ticker, "mom": mom, "vol": vol, "fwd_1d": fwd})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in ("mom", "vol"):
        std = df[col].std(ddof=0)
        df[f"z_{col}"] = (df[col] - df[col].mean()) / std if std > 0 else 0.0
        df[f"z_{col}"] = df[f"z_{col}"].clip(-3.0, 3.0)
    df["score"] = df["z_mom"] - vol_weight * df["z_vol"]
    return df


def run_longshort(
    history_store: HistoryStore,
    universe: list[str],
    benchmark: str,
    start: date,
    end: date,
    *,
    top_n: int,
    adv_min: float,
    vol_weight: float,
    rebalance_stride: int,
) -> pd.DataFrame:
    calendar = HistoryStore.benchmark_calendar(history_store, benchmark, start, end)
    calendar = calendar[::rebalance_stride]

    rows: list[dict] = []
    for i, ts in enumerate(calendar):
        day = ts.date()
        scored = score_universe(
            history_store, universe, day, adv_min=adv_min, vol_weight=vol_weight
        )
        if len(scored) < 2 * top_n:
            continue
        scored = scored.sort_values("score", ascending=False).reset_index(drop=True)
        long_leg = scored.head(top_n)
        short_leg = scored.tail(top_n)
        long_ret = float(long_leg["fwd_1d"].mean())
        short_ret = float(short_leg["fwd_1d"].mean())
        rows.append(
            {
                "date": ts,
                "long_ret": long_ret,
                "short_ret": short_ret,
                "ls_ret": long_ret - short_ret,
                "n_scored": len(scored),
                "long_tickers": long_leg["ticker"].tolist(),
                "short_tickers": short_leg["ticker"].tolist(),
            }
        )
        if (i + 1) % 60 == 0 or i == len(calendar) - 1:
            logger.info(
                "  %s | %d/%d | scored=%d long=%.2f%% short=%.2f%% LS=%.2f%%",
                day,
                i + 1,
                len(calendar),
                len(scored),
                long_ret * 100,
                short_ret * 100,
                (long_ret - short_ret) * 100,
            )
    return pd.DataFrame(rows)


def benchmark_returns(history_store, benchmark, start, end):
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def turnover(positions: list[list[str]]) -> float:
    if len(positions) < 2:
        return 0.0
    churns = []
    for prev, curr in zip(positions[:-1], positions[1:]):
        prev_set, curr_set = set(prev), set(curr)
        churn = len(curr_set - prev_set) / max(1, len(curr_set))
        churns.append(churn)
    return float(np.mean(churns))


def assess_period(
    df: pd.DataFrame,
    factors: pd.DataFrame,
    bench_rets: pd.Series,
    rebalance_stride: int,
    cost_half_spread_bps: float,
    label: str,
) -> dict:
    if df.empty:
        return {"label": label, "n": 0}
    rebalances_per_year = 252 / max(1, rebalance_stride)
    idx = pd.DatetimeIndex(df["date"])

    long_series = pd.Series(df["long_ret"].to_numpy(), index=idx, name="long")
    short_series = pd.Series(df["short_ret"].to_numpy(), index=idx, name="short")
    ls_series = pd.Series(df["ls_ret"].to_numpy(), index=idx, name="ls")

    cm = RealisticCostModel(adverse_selection_bps=5.0)
    long_turn = turnover(df["long_tickers"].tolist())
    short_turn = turnover(df["short_tickers"].tolist())
    long_drag_per = cm.primary_period_drag_bps(cost_half_spread_bps, long_turn) / 10_000.0
    short_drag_per = cm.primary_period_drag_bps(cost_half_spread_bps, short_turn) / 10_000.0
    ls_drag_per = long_drag_per + short_drag_per  # both legs trade

    sharpe_long_gross = sharpe(long_series.tolist(), periods_per_year=int(rebalances_per_year))
    sharpe_long_net = sharpe(
        (long_series - long_drag_per).tolist(), periods_per_year=int(rebalances_per_year)
    )
    sharpe_ls_gross = sharpe(ls_series.tolist(), periods_per_year=int(rebalances_per_year))
    sharpe_ls_net = sharpe(
        (ls_series - ls_drag_per).tolist(), periods_per_year=int(rebalances_per_year)
    )

    bench_aligned = bench_rets.reindex(idx).dropna()
    long_excess_per = (long_series.reindex(bench_aligned.index) - bench_aligned).mean()
    long_excess_ann = float(long_excess_per * 252)

    ls_long_drag_ann = long_drag_per * rebalances_per_year
    ls_drag_ann = ls_drag_per * rebalances_per_year

    # Carhart on LS series (no benchmark subtraction; LS is already excess)
    res_ls = run_regression(ls_series, factors, _CARHART_FACTORS, subtract_rf=False)
    res_long = run_regression(long_series, factors, _CARHART_FACTORS, subtract_rf=True)

    return {
        "label": label,
        "n": len(df),
        "long_turn": long_turn,
        "short_turn": short_turn,
        "long_excess_gross": long_excess_ann,
        "long_excess_net": long_excess_ann - ls_long_drag_ann,
        "long_sharpe_gross": sharpe_long_gross,
        "long_sharpe_net": sharpe_long_net,
        "long_alpha_4f": float(res_long.alpha_annualized),
        "long_t_4f": float(res_long.alpha_tstat),
        "long_beta_mom": float(res_long.betas.get("Mom", 0.0)),
        "ls_mean_ann": float(ls_series.mean() * 252),
        "ls_sharpe_gross": sharpe_ls_gross,
        "ls_sharpe_net": sharpe_ls_net,
        "ls_drag_ann": ls_drag_ann,
        "ls_alpha_4f": float(res_ls.alpha_annualized),
        "ls_t_4f": float(res_ls.alpha_tstat),
        "ls_beta_mkt": float(res_ls.betas.get("Mkt-RF", 0.0)),
        "ls_beta_mom": float(res_ls.betas.get("Mom", 0.0)),
        "ls_r2": float(res_ls.r_squared),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--vol-weight", type=float, default=1.0)
    ap.add_argument("--adv-min", type=float, default=5_000_000)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--out", type=Path, default=Path("docs/research/longshort_mom_lowvol.md"))
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    full_universe = load_pit_union(date(2011, 1, 1), date(2026, 4, 22))
    histories = load_cached_histories([*full_universe, args.benchmark], _PRICES_DIR)
    history_store = HistoryStore(histories)

    periods = [
        ("IS_2011_2016", date(2011, 1, 1), date(2016, 12, 31)),
        ("IS_2017_2022", date(2017, 1, 1), date(2022, 12, 31)),
        ("OOS_2023_2026", date(2023, 1, 1), date(2026, 4, 22)),
    ]

    sections = [
        "# Long-short mom+lowvol — market-neutral spread test",
        "",
        "**RESEARCH ONLY.** Hypothesis: long-only mom+lowvol lost 11%/y in 2017-2022 because",
        "mid-cap-tilted top-15 underperformed mega-cap SPY benchmark. Long-short spread",
        "(top-15 minus bottom-15) hedges benchmark drift; if positive consistently",
        "across all 3 periods, regime-risk reduced.",
        "",
        f"- Top-N: {args.top_n}, vol_weight: {args.vol_weight}, ADV ≥ ${args.adv_min / 1e6:.0f}M",
        f"- Rebalance stride: {args.rebalance_stride}; cost: {args.cost_bps}bp half-spread per leg",
        "- Universe: per-subperiod PIT R2000-like",
        "",
    ]

    all_stats = []
    for label, start, end in periods:
        universe = load_pit_union(start, end)
        carhart = load_carhart_daily(start=start, end=end)
        carhart.index = pd.DatetimeIndex(carhart.index).tz_localize(None)
        bench = benchmark_returns(history_store, args.benchmark, start, end)

        logger.info("=== %s ===", label)
        df = run_longshort(
            history_store=history_store,
            universe=universe,
            benchmark=args.benchmark,
            start=start,
            end=end,
            top_n=args.top_n,
            adv_min=args.adv_min,
            vol_weight=args.vol_weight,
            rebalance_stride=args.rebalance_stride,
        )
        if df.empty:
            logger.warning("%s empty", label)
            continue
        stats = assess_period(df, carhart, bench, args.rebalance_stride, args.cost_bps, label)
        all_stats.append(stats)
        logger.info(
            "%s | n=%d | LONG: gross excess=%.1f%% Sharpe=%.2f→%.2f α t=%.2f | "
            "LS: mean=%.1f%% Sharpe=%.2f→%.2f α=%.1f%% t=%.2f β_MKT=%.2f β_MOM=%.2f",
            label,
            stats["n"],
            stats["long_excess_gross"] * 100,
            stats["long_sharpe_gross"],
            stats["long_sharpe_net"],
            stats["long_t_4f"],
            stats["ls_mean_ann"] * 100,
            stats["ls_sharpe_gross"],
            stats["ls_sharpe_net"],
            stats["ls_alpha_4f"] * 100,
            stats["ls_t_4f"],
            stats["ls_beta_mkt"],
            stats["ls_beta_mom"],
        )

    sections.append("## Long-only (top-15) — for reference vs synthesis report")
    sections.append("")
    sections.append(
        "| Period | N | turn | excess gross | excess net | Sharpe gross | Sharpe net | α 4F | t (4F) | β_MOM |"
    )
    sections.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in all_stats:
        sections.append(
            f"| {s['label']} | {s['n']} | {s['long_turn'] * 100:.1f}% | "
            f"{s['long_excess_gross'] * 100:+.1f}% | {s['long_excess_net'] * 100:+.1f}% | "
            f"{s['long_sharpe_gross']:+.2f} | {s['long_sharpe_net']:+.2f} | "
            f"{s['long_alpha_4f'] * 100:+.1f}% | {s['long_t_4f']:+.2f} | "
            f"{s['long_beta_mom']:.2f} |"
        )

    sections.append("")
    sections.append("## Long-short spread (top-15 minus bottom-15) — primary test")
    sections.append("")
    sections.append(
        "| Period | N | mean ann | Sharpe gross | Sharpe net | drag/y | α 4F | t (4F) | β_MKT | β_MOM | R² |"
    )
    sections.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in all_stats:
        sections.append(
            f"| {s['label']} | {s['n']} | {s['ls_mean_ann'] * 100:+.1f}% | "
            f"{s['ls_sharpe_gross']:+.2f} | {s['ls_sharpe_net']:+.2f} | "
            f"{s['ls_drag_ann'] * 100:.1f}% | "
            f"{s['ls_alpha_4f'] * 100:+.1f}% | {s['ls_t_4f']:+.2f} | "
            f"{s['ls_beta_mkt']:+.2f} | {s['ls_beta_mom']:+.2f} | {s['ls_r2']:.3f} |"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
