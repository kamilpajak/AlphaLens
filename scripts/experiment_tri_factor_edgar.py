"""Tri-factor combo: mom + lowvol + ROE — EDGAR-backed extended-IS validation.

Score = z(mom_12_1m) - vol_weight * z(vol_60d) + roe_weight * z(roe_ttm)

  - mom_12_1m: Jegadeesh-Titman 12-1 month return
  - vol_60d:   60-day realised volatility (annualised stdev of daily log rets)
  - roe_ttm:   PIT TTM return on equity from SEC EDGAR companyfacts (parent
               attribution preferred; common-equity adjustment when preferred
               capital is present)

Originally validated on a 2-year IS (2021-2022) constrained by SimFin coverage
that begins 2020-Q2. That window contained the 2021 junk rally + 2022 bear
regime, both quality-hostile, so IS Sharpe was -0.36 vs OOS 1.04. This script
re-validates on an 8-year IS (2015-2022, EDGAR-backed) that spans multiple
regime cycles. Intent: rule out 2-year-OOS-luck before forward deployment.

Uses raw SEC EDGAR companyfacts cache: ~/.alphalens/companyfacts/{CIK}.json.
PIT correctness is enforced inside EdgarCompanyfactsROEStore (filed-date
filter + matched-pair concept hierarchy + Compustat TTM formula).
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

from alphalens.attribution.cost_model import RealisticCostModel
from alphalens.attribution.factor_analysis import run_regression
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.metrics import sharpe, turnover_pct
from alphalens.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import load_carhart_daily
from alphalens.data.fundamentals.edgar_companyfacts import EdgarCompanyfactsROEStore
from alphalens.data.store.history import HistoryStore

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_COMPANYFACTS_DIR = Path.home() / ".alphalens" / "companyfacts"
_TICKER_CIK_MAP_PATH = (
    Path(__file__).resolve().parent.parent
    / "alphalens"
    / "alt_data"
    / "data"
    / "ticker_cik_map.yaml"
)

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


def tri_factor_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:
    """Score = z(mom_12_1m) - vol_weight * z(vol_60d) + roe_weight * z(roe_ttm).

    Filtered by ADV; emits zero-row DataFrame when no eligible tickers.
    """
    config = dict(config or {})
    benchmark = config.get("benchmark")
    fundamentals = config["_fundamentals"]
    adv_min = float(config.get("_adv_min_usd", 0.0))
    adv_window = int(config.get("_adv_window", 60))
    vol_weight = float(config.get("_vol_weight", 1.0))
    vol_window = int(config.get("_vol_window", 60))
    roe_weight = float(config.get("_roe_weight", 0.5))

    dates = [df.index.max() for df in histories.values() if df is not None and not df.empty]
    if not dates:
        return pd.DataFrame(columns=["ticker", "score"])
    asof = max(dates).date()

    rows: list[dict] = []
    for ticker, df in histories.items():
        if ticker == benchmark or df is None or len(df) < 253:
            continue
        closes = df["close"].to_numpy(dtype=float)
        volumes = df["volume"].to_numpy(dtype=float)
        if closes[-1] <= 0 or closes[-253] <= 0 or closes[-22] <= 0:
            continue
        dollar_vol = closes[-adv_window:] * volumes[-adv_window:]
        adv = float(np.median(dollar_vol[dollar_vol > 0])) if (dollar_vol > 0).any() else 0.0
        if adv < adv_min:
            continue
        mom = closes[-22] / closes[-253] - 1.0
        rets = np.diff(np.log(closes[-vol_window - 1 :]))
        if len(rets) < vol_window // 2 or np.any(~np.isfinite(rets)):
            continue
        vol = float(np.std(rets, ddof=1) * np.sqrt(252))
        if not np.isfinite(vol) or vol <= 0:
            continue
        roe = fundamentals.roe_ttm(ticker, asof)
        if roe is None:
            continue
        rows.append({"ticker": ticker, "mom": float(mom), "vol": vol, "roe": float(roe)})

    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])

    df = pd.DataFrame(rows)
    for col in ("mom", "vol", "roe"):
        std = df[col].std(ddof=0)
        if std <= 0:
            df[f"z_{col}"] = 0.0
            continue
        z = (df[col] - df[col].mean()) / std
        df[f"z_{col}"] = z.clip(-3.0, 3.0)
    df["score"] = df["z_mom"] - vol_weight * df["z_vol"] + roe_weight * df["z_roe"]
    return df.sort_values("score", ascending=False).reset_index(drop=True)


tri_factor_adapter.MIN_BARS_REQUIRED = 253


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
        "r2_4f": float(res4.r_squared),
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
    ap.add_argument("--vol-weight", type=float, default=1.0)
    ap.add_argument(
        "--roe-weights",
        nargs="+",
        type=float,
        default=[0.5, 1.0],
        help="ROE z-score multipliers to test (default: 0.5 + 1.0 from prior tri_factor_combo doc)",
    )
    ap.add_argument(
        "--adv-thresholds",
        nargs="+",
        type=float,
        default=[5_000_000, 20_000_000],
        help="ADV thresholds in USD; default trims $1M to limit runtime",
    )
    ap.add_argument(
        "--cost-half-spreads",
        nargs="+",
        type=float,
        default=[5.0, 15.0],
    )
    ap.add_argument("--is-start", type=date.fromisoformat, default=date(2015, 1, 1))
    ap.add_argument("--is-end", type=date.fromisoformat, default=date(2022, 12, 31))
    ap.add_argument("--oos-start", type=date.fromisoformat, default=date(2023, 1, 1))
    ap.add_argument("--oos-end", type=date.fromisoformat, default=date(2026, 4, 22))
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/tri_factor_edgar_extended_is.md"),
    )
    ap.add_argument(
        "--lock-universe",
        action="store_true",
        help=(
            "Use the full --is-start..--oos-end PIT union as the screener "
            "universe for every period. Required for subsample stability "
            "checks so halves and full IS draw from the same ticker pool."
        ),
    )
    ap.add_argument(
        "--phase-offset",
        type=int,
        default=0,
        help=(
            "Sampling phase for the strided rebalance calendar. "
            "0..rebalance_stride-1; default 0. Required for honest subsample "
            "stability checks: independent halves with different start dates "
            "naturally end up on different phases, masking the same strategy "
            "as a 30-77pp/y phase-aliasing artifact (see "
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
    cik_map = TickerCikMap.load(_TICKER_CIK_MAP_PATH)
    fundamentals = EdgarCompanyfactsROEStore(_COMPANYFACTS_DIR, cik_map)

    periods = [
        (f"IS {args.is_start.year}-{args.is_end.year}", args.is_start, args.is_end),
        (f"OOS {args.oos_start.year}-{args.oos_end.year}", args.oos_start, args.oos_end),
    ]

    sections: list[str] = [
        f"# Tri-factor combo (mom + lowvol + ROE) — EDGAR-backed {args.is_start.year}-{args.oos_end.year}",
        "",
        "**RESEARCH ONLY.** Re-validation of `tri_factor_combo.md` (2026-04-29) on an",
        f"extended {args.is_end.year - args.is_start.year + 1}-year IS using SEC EDGAR",
        "companyfacts for the ROE component (SimFin's 2020-Q2 floor previously locked",
        "the IS to 2 years).",
        "",
        "- Score: z(mom_12_1m) − vol_w × z(vol_60d) + roe_w × z(roe_ttm)",
        f"- Top-N: {args.top_n}, holding-signal: {args.holding}d, stride: {args.rebalance_stride}",
        f"- Vol weight: {args.vol_weight} (fixed)",
        f"- ROE weights tested: {args.roe_weights}",
        f"- ADV thresholds: {[f'${t / 1e6:.0f}M' for t in args.adv_thresholds]}",
        "",
    ]

    all_rows: list[dict] = []
    for label, start, end in periods:
        universe = full_universe if args.lock_universe else load_pit_union(start, end)
        carhart = load_carhart_daily(start=start, end=end)
        bench_rets = benchmark_returns(history_store, args.benchmark, start, end)
        logger.info("=== %s | universe %d ===", label, len(universe))

        for roe_weight in args.roe_weights:
            for adv_min in args.adv_thresholds:
                logger.info(
                    "roe_weight=%.1f vol_weight=%.1f ADV ≥ $%.0fM",
                    roe_weight,
                    args.vol_weight,
                    adv_min / 1e6,
                )
                config = {
                    "benchmark": args.benchmark,
                    "_adv_min_usd": adv_min,
                    "_vol_weight": args.vol_weight,
                    "_roe_weight": roe_weight,
                    "_fundamentals": fundamentals,
                }
                engine = BacktestEngine(
                    history_store,
                    scorer=tri_factor_adapter,
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
                    stats = assess(report, carhart, args.rebalance_stride, cost_bps, bench_rets)
                    stats["period"] = label
                    stats["adv_min_m"] = adv_min / 1e6
                    stats["cost_bps"] = cost_bps
                    stats["roe_weight"] = roe_weight
                    all_rows.append(stats)
                    if stats.get("n", 0) > 0:
                        logger.info(
                            "%s | rw=%.1f vw=%.1f ADV≥$%.0fM cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
                            "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | α 4F=%.1f%% t=%.2f R²=%.3f",
                            label,
                            roe_weight,
                            args.vol_weight,
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
                            stats["r2_4f"],
                        )

    sections.append("## Results")
    sections.append("")
    sections.append(
        "| Period | roe_w | ADV | cost | mean topN | turn | Sharpe gross | Sharpe net | "
        "excess gross | excess net | α 4F | t (4F) | R² | β_MOM |"
    )
    sections.append("|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_rows:
        if r.get("n", 0) == 0:
            continue
        sections.append(
            "| {p} | {rw:.1f} | $${adv:.0f}M | {cb:.0f}bp | {tn:.1f} | {tr:.1f}% | "
            "{sg:.2f} | {sn:.2f} | {eg:+.1f}% | {en:+.1f}% | {a4:+.1f}% | {t4:+.2f} | {r2:.3f} | {bm:.2f} |".format(
                p=r["period"],
                rw=r["roe_weight"],
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
                r2=r["r2_4f"],
                bm=r["beta_mom"],
            ).replace("$$", "$")
        )

    sections.append("")
    sections.append("## Decision criteria")
    sections.append("")
    sections.append("Per `project_next_session_edgar_backfill.md`:")
    sections.append("")
    sections.append(
        "- **PASS**: per-subperiod tri-factor t > 2.0 in 2 of 2 IS halves "
        "(2015-2018 vs 2019-2022); R² stable. Run subsample stability check next."
    )
    sections.append(
        "- **MID**: one half t > 2.0, the other marginal — forward-walk with "
        "Sharpe ≥ 0.7 gate, regime-conditional sizing."
    )
    sections.append(
        "- **FAIL**: both halves t < 1.5 OR catastrophic in any half → fallback "
        "to mom+lowvol; document tri-factor as 2-year-OOS-luck."
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
