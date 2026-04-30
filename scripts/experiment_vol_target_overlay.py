"""Vol-targeting overlay applied to the mom+lowvol BASE strategy.

Tests Moreira & Muir 2017 (JoF, "Volatility-Managed Portfolios"): scaling
gross exposure inversely with portfolio realised vol can improve risk-
adjusted returns even when the underlying factor exposure has weak alpha.

Design:
- BASE = ``momentum_lowvol_adapter`` from ``experiment_momentum_lowvol_combo``,
  scored exactly the same way (same z-mom − vol_weight × z-vol formulation
  at vol_weight=1.0). The overlay does not touch selection.
- Engine produces ``report.portfolio_returns`` per the standard contract.
- ``alphalens.overlays.apply_vol_target`` rescales those returns by a
  rolling-window realised-vol target (default: target=10% ann., lookback=5
  weekly periods ≈ 1 month, max_leverage=1.5). Causality: scale[t] uses
  returns[<t] only.
- **Dynamic cost accounting** — vol-targeting changes capital deployed
  AND introduces overlay turnover (|scale_t − scale_{t-1}|). Per-rebalance
  cost is NOT constant; we thread the scale series through the cost
  computation rather than re-using the constant-drag shortcut from
  ``experiment_momentum_lowvol_combo.assess``.

Pre-registered as ``vol_target_mom_lowvol_2026_04_30`` in fresh signal
class ``risk_management_overlay_2026_04_30`` (Bonferroni n=1, |t|≥1.96).

Known limitation (documented in ADR 0007): vol-scaling makes the strategy
beta time-varying. OLS Carhart-4F α t-stats reported here assume constant
betas; primary success-criterion is therefore Sharpe-improvement vs the
ungated BASE, which is robust to time-varying beta.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from alphalens.attribution.factor_analysis import run_regression
from alphalens.backtest.engine import BacktestEngine
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import load_carhart_daily
from alphalens.data.store.history import HistoryStore
from alphalens.overlays import VolTargeter, apply_vol_target
from alphalens.overlays.assess import compute_overlay_stats
from alphalens.screeners.momentum_lowvol import momentum_lowvol_adapter

# Helpers (universe loader, benchmark series, prices dir) still live in
# the BASE experiment script. The scorer itself is imported from the
# package above so the two scripts no longer need to import each other.
from scripts.experiment_momentum_lowvol_combo import (
    _PRICES_DIR,
    benchmark_returns,
    load_pit_union,
)

logger = logging.getLogger(__name__)

_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]


def _excess_ann_from_per_rebal(portfolio_rets: pd.Series, bench_rets: pd.Series) -> float:
    bench_aligned = bench_rets.reindex(portfolio_rets.index).dropna()
    excess = (portfolio_rets.reindex(bench_aligned.index) - bench_aligned).mean()
    if pd.isna(excess):
        return float("nan")
    return float(excess * 252)


def assess_overlay(
    report,
    vol_targeter: VolTargeter,
    factors: pd.DataFrame,
    rebalance_stride: int,
    cost_half_spread_bps: float,
    bench_rets: pd.Series,
) -> dict:
    """Apply vol-target overlay + dynamic cost accounting + Carhart 4F.

    Delegates the canonical Sharpe-improvement computation to
    `alphalens.overlays.assess.compute_overlay_stats` (true
    per-period turnover from snapshots, plus side-by-side BASE Sharpe).
    Carhart-4F α t-stat is reported as an *upper-bound* indicator only —
    OLS assumes constant betas, but vol-scaling introduces time-varying
    betas (ADR 0007 limitation). Primary success metric is
    ``sharpe_improvement_net``.
    """
    raw_rets = report.portfolio_returns
    if raw_rets.empty:
        return {"n": 0}

    rebalances_per_year = max(1, int(round(252 / max(1, rebalance_stride))))

    snapshots = [list(r.top_n_tickers) for r in report.rebalance_results]
    overlay_stats = compute_overlay_stats(
        raw_returns=raw_rets,
        targeter=vol_targeter,
        top_n_snapshots=snapshots,
        cost_half_spread_bps=cost_half_spread_bps,
        periods_per_year=rebalances_per_year,
    )

    # Carhart attribution kept for legacy comparability with prior
    # screener experiments. Distorted under time-varying betas; treat as
    # upper-bound, not the success gate.
    gross_scaled = apply_vol_target(raw_rets, vol_targeter)
    res4 = run_regression(gross_scaled, factors[[*_CARHART_FACTORS, "RF"]], _CARHART_FACTORS)

    # Excess-vs-benchmark (gross & net) is computed here because it
    # depends on the benchmark series the script pulled. Net uses the
    # canonical scaled-net series for parity with the audit JSON.
    scales = vol_targeter.scale_series(raw_rets)
    snapshots_idx = list(raw_rets.index)
    base_turnover_series = pd.Series(
        [
            (
                len(set(snapshots[i - 1]) - set(snapshots[i])) / max(len(snapshots[i - 1]), 1)
                if i > 0 and i - 1 < len(snapshots) and i < len(snapshots)
                else 0.0
            )
            for i in range(len(snapshots_idx))
        ],
        index=snapshots_idx,
        name="base_turnover",
    )
    scale_changes = scales.diff().abs().fillna(0.0)
    cost_per_rebal = (base_turnover_series * scales + scale_changes) * (
        cost_half_spread_bps / 10_000.0
    )
    net_scaled = gross_scaled - cost_per_rebal

    excess_gross_ann = _excess_ann_from_per_rebal(gross_scaled, bench_rets)
    excess_net_ann = _excess_ann_from_per_rebal(net_scaled, bench_rets)

    return {
        **overlay_stats,
        "alpha_gross_4f": float(res4.alpha_annualized),
        "t_4f": float(res4.alpha_tstat),
        "beta_mom": float(res4.betas.get("Mom", 0.0)),
        "excess_gross_ann": excess_gross_ann,
        "excess_net_ann": excess_net_ann,
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument(
        "--phase-offset",
        type=int,
        default=0,
        help="Phase offset for strided rebalance calendar; 0..rebalance_stride-1.",
    )
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--vol-weight", type=float, default=1.0)
    ap.add_argument(
        "--adv-thresholds",
        nargs="+",
        type=float,
        default=[5_000_000.0],
    )
    ap.add_argument(
        "--cost-half-spreads",
        nargs="+",
        type=float,
        default=[5.0],
    )
    ap.add_argument(
        "--target-vol",
        type=float,
        default=0.10,
        help="Annualised target portfolio vol. Default 0.10 = 10%%.",
    )
    ap.add_argument(
        "--vol-lookback",
        type=int,
        default=5,
        help=(
            "Lookback for realised-vol estimate, in REBALANCE periods "
            "(NOT trading days). Default 5 ≈ 1 month at stride=5 weekly "
            "(parity with Moreira-Muir 2017)."
        ),
    )
    ap.add_argument(
        "--max-leverage",
        type=float,
        default=1.5,
        help="Cap on the vol-target multiplier. Default 1.5 (M-M parity).",
    )
    ap.add_argument("--out", type=Path, default=Path("docs/research/vol_target_overlay.md"))
    ap.add_argument("--is-start", type=date.fromisoformat, default=date(2011, 1, 1))
    ap.add_argument("--is-end", type=date.fromisoformat, default=date(2022, 12, 31))
    ap.add_argument("--oos-start", type=date.fromisoformat, default=date(2023, 1, 1))
    ap.add_argument("--oos-end", type=date.fromisoformat, default=date(2026, 4, 22))
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

    rebalances_per_year = max(1, int(round(252 / args.rebalance_stride)))

    periods = [
        (f"IS {args.is_start.year}-{args.is_end.year}", args.is_start, args.is_end),
        (f"OOS {args.oos_start.year}-{args.oos_end.year}", args.oos_start, args.oos_end),
    ]

    sections: list[str] = [
        "# Vol-target overlay on mom+lowvol BASE — Moreira-Muir 2017",
        "",
        "**RESEARCH ONLY.** Pre-registered hypothesis "
        "`vol_target_mom_lowvol_2026_04_30` (signal class "
        "`risk_management_overlay_2026_04_30`, Bonferroni n=1, |t|≥1.96).",
        "",
        f"- BASE: mom+lowvol combo (vol_weight={args.vol_weight}), top-{args.top_n}, stride {args.rebalance_stride}",
        f"- Vol target: {args.target_vol:.2f} ann, lookback {args.vol_lookback} rebalances, max_leverage {args.max_leverage}",
        "- Cost model: dynamic per-rebalance "
        "(`turnover_t = base_turnover · scale_t + |scale_t − scale_{t-1}|`)",
        "",
    ]

    all_rows: list[dict] = []
    for label, start, end in periods:
        universe = load_pit_union(start, end)
        carhart = load_carhart_daily(start=start, end=end)
        bench_rets = benchmark_returns(history_store, args.benchmark, start, end)
        logger.info("=== %s | universe %d ===", label, len(universe))

        for adv_min in args.adv_thresholds:
            for cost_bps in args.cost_half_spreads:
                logger.info(
                    "ADV ≥ $%.0fM cost=%.0fbps target_vol=%.2f lookback=%d max_lev=%.2f",
                    adv_min / 1e6,
                    cost_bps,
                    args.target_vol,
                    args.vol_lookback,
                    args.max_leverage,
                )
                config = {
                    "benchmark": args.benchmark,
                    "_adv_min_usd": adv_min,
                    "_vol_weight": args.vol_weight,
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

                vol_targeter = VolTargeter(
                    target_vol=args.target_vol,
                    lookback=args.vol_lookback,
                    periods_per_year=rebalances_per_year,
                    max_leverage=args.max_leverage,
                )
                stats = assess_overlay(
                    report,
                    vol_targeter,
                    carhart,
                    args.rebalance_stride,
                    cost_bps,
                    bench_rets,
                )
                stats["period"] = label
                stats["adv_min_m"] = adv_min / 1e6
                stats["cost_bps"] = cost_bps
                all_rows.append(stats)

                if stats.get("n", 0) > 0:
                    logger.info(
                        "%s | ADV≥$%.0fM cost=%.0fbps | n=%d scale mean=%.2f (min=%.2f max=%.2f) | "
                        "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
                        "α 4F=%.1f%% t=%.2f β_MOM=%.2f | "
                        "BASE Sh net=%.2f → Δ Sh net=%+.2f",
                        label,
                        adv_min / 1e6,
                        cost_bps,
                        stats["n"],
                        stats["mean_scale"],
                        stats["min_scale"],
                        stats["max_scale"],
                        stats["sharpe_scaled_gross"],
                        stats["sharpe_scaled_net"],
                        stats["excess_gross_ann"] * 100,
                        stats["excess_net_ann"] * 100,
                        stats["alpha_gross_4f"] * 100,
                        stats["t_4f"],
                        stats["beta_mom"],
                        stats["sharpe_unscaled_net"],
                        stats["sharpe_improvement_net"],
                    )

    sections.append("## Results — gross / net (vol-targeted, dynamic cost)")
    sections.append("")
    sections.append(
        "| Period | ADV | cost | n | scale (mean / min / max) | Sharpe gross | Sharpe net | "
        "BASE Sh net | Δ Sh net | excess gross | excess net | α 4F | t (4F) | β_MOM |"
    )
    sections.append("|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_rows:
        if r.get("n", 0) == 0:
            continue
        sections.append(
            "| {p} | ${a:.0f}M | {c:.0f}bp | {n} | {ms:.2f} / {mn:.2f} / {mx:.2f} | "
            "{sg:.2f} | {sn:.2f} | {bsn:.2f} | {dsn:+.2f} | {eg:+.1f}% | {en:+.1f}% | "
            "{ag:+.1f}% | {t:+.2f} | {bm:+.2f} |".format(
                p=r["period"],
                a=r["adv_min_m"],
                c=r["cost_bps"],
                n=r["n"],
                ms=r["mean_scale"],
                mn=r["min_scale"],
                mx=r["max_scale"],
                sg=r["sharpe_scaled_gross"],
                sn=r["sharpe_scaled_net"],
                bsn=r["sharpe_unscaled_net"],
                dsn=r["sharpe_improvement_net"],
                eg=r["excess_gross_ann"] * 100,
                en=r["excess_net_ann"] * 100,
                ag=r["alpha_gross_4f"] * 100,
                t=r["t_4f"],
                bm=r["beta_mom"],
            )
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
