"""idiosyncratic_momentum_2026_05_14_v1 — Blitz residual-momentum, S&P 1500 PIT.

Pre-reg context:
- Class: price_factor_search_2026_04_29 (n=4 prior FAIL → n=5 with IM).
  Strict class-internal critical |t| = 2.57 at α=0.05.
- Design memo: docs/research/idiosyncratic_momentum_v1_design_2026_05_14.md
- Bonferroni: project doctrine 3.5 binds (k=15).
- Data: yfinance OHLCV cache + Fama-French daily factors (no new vendor).
- Universe: S&P 1500 PIT union via ``load_sp1500_pit_union`` (~2000 names).
- Rebalance: monthly stride=21 trading days, holding=21; top-decile equal-weight.
- Cost stress grid: {0, 5, 10, 15, 25} bps half-spread. G4 reads ``t_net_4f``
  (second regression on net daily) — ev_fcff_yield H1 fix pattern.
- §5.1 mandatory diagnostics (BAB-confound):
    1. realised β_market (Mkt-RF only) — flag if < 0.8
    2. FF5+UMD attenuation vs Carhart-4F α — flag if > 30%
    3. Sharpe gross/net (raw-momentum comparison deferred to post-verdict
       diagnostic doc; not on the audit's PASS/FAIL critical path)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from alphalens_pipeline.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens_pipeline.data.factors import (  # noqa: E402
    load_carhart_daily,
    load_ff5_daily,
    load_umd_daily,
)
from alphalens_pipeline.data.store.history import HistoryStore  # noqa: E402
from alphalens_pipeline.data.universes.sp1500_pit import load_sp1500_pit_union  # noqa: E402
from alphalens_research.attribution.cost_model import RealisticCostModel  # noqa: E402
from alphalens_research.attribution.factor_analysis import run_regression  # noqa: E402
from alphalens_research.backtest.daily_continuous_returns import (  # noqa: E402
    daily_continuous_returns,
)
from alphalens_research.backtest.engine import BacktestEngine  # noqa: E402
from alphalens_research.backtest.metrics import sharpe, turnover_pct  # noqa: E402
from alphalens_research.screeners.idiosyncratic_momentum.adapter import (  # noqa: E402
    IdiosyncraticMomentumScorer,
    ff3_monthly_from_carhart_daily,
)
from alphalens_research.screeners.idiosyncratic_momentum.scorer import (  # noqa: E402
    monthly_returns_from_daily,
)

logger = logging.getLogger(__name__)

# Pre-reg locks per memo §7.
_REBALANCE_STRIDE_LOCK = 21  # monthly cadence
_HOLDING_LOCK = 21
_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]
_FF5_UMD_FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]
_HAC_MAXLAGS_LOCK = 21  # 1 month, matches monthly rebalance cycle (memo §5)
_BONFERRONI_CRITICAL_T_CLASS_INTERNAL = 2.57  # strict n=5 at α=0.05
_BONFERRONI_PROJECT_THRESHOLD = 3.5  # memo §8 doctrine
_BETA_MARKET_FLAG_THRESHOLD = 0.8  # memo §5.1.1
_ATTENUATION_FLAG_THRESHOLD = 0.30  # memo §5.1.2

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_HYPER_TURNOVER_FLAG_THRESHOLD = 0.80  # memo §4 + pre-reg turnover_logging_mandate


class _RawMonthlyMomentumScorer:
    """Diagnostic comparator — raw 12-1 cumulative monthly return.

    §5.1.3 mandates a Sharpe-vs-raw-momentum comparison. To keep
    universe-eligibility apples-to-apples with the IM scorer, this
    comparator imposes the SAME ``MIN_BARS_REQUIRED=800`` + price-floor
    + monthly cadence. The only difference is the score: raw cumulative
    residual-of-FF3-skipped monthly return over [t-12, t-2], NOT
    standardised by σ_36 and NOT residualised.
    """

    MIN_BARS_REQUIRED = 900  # match IM scorer post-zen H1 follow-up

    def __init__(
        self,
        *,
        price_floor: float = 5.0,
        formation_lookback: int = 12,
        skip: int = 2,
    ) -> None:
        self._price_floor = price_floor
        self._formation_lookback = formation_lookback
        self._skip = skip

    def __call__(self, histories, config=None) -> pd.DataFrame:
        cfg = dict(config or {})
        benchmark = cfg.get("benchmark")
        scores: dict[str, float] = {}
        for ticker, df in histories.items():
            if ticker == benchmark or df is None or df.empty:
                continue
            close = df["close"]
            if float(close.iloc[-1]) < self._price_floor:
                continue
            monthly = monthly_returns_from_daily(close)
            if len(monthly) < self._formation_lookback:
                continue
            tail = monthly.iloc[
                -self._formation_lookback : (-(self._skip - 1) if self._skip > 1 else None)
            ]
            if tail.empty:
                continue
            # Cumulative compound return over formation window.
            cum = float((1.0 + tail).prod() - 1.0)
            if not np.isfinite(cum):
                continue
            scores[ticker] = cum

        if not scores:
            return pd.DataFrame(columns=["ticker", "score"])
        out = pd.DataFrame({"ticker": list(scores.keys()), "score": list(scores.values())})
        return out.sort_values("score", ascending=False).reset_index(drop=True)


def benchmark_returns(
    history_store: HistoryStore, benchmark: str, start: date, end: date
) -> pd.Series:
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def _load_ff5_umd_daily(start: date, end: date) -> pd.DataFrame:
    """Merge FF5 + UMD into a single daily DataFrame for §5.1.2 diagnostic."""
    ff5 = load_ff5_daily(start=start, end=end)
    umd = load_umd_daily(start=start, end=end)
    merged = ff5[["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]].join(umd, how="inner")
    return merged


def assess(
    report,
    factors: pd.DataFrame,
    ff5_umd: pd.DataFrame,
    rebalance_stride: int,
    cost_bps: float,
    bench_rets_daily: pd.Series,
    *,
    history_store: HistoryStore,
    benchmark: str,
    end_date: date,
) -> dict:
    """Daily-cadence Carhart-4F + §5.1 BAB diagnostics — mirrors ev_fcff_yield."""
    rets_daily = daily_continuous_returns(
        report.rebalance_results,
        history_store,
        calendar_ticker=benchmark,
        end_date=end_date,
    )
    if rets_daily.empty:
        return {"n": 0}

    avg_turnover = turnover_pct(r.top_n_tickers for r in report.rebalance_results)
    rebalances_per_year = 252 / max(1, rebalance_stride)
    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_per_rebal_bps = cost_model.primary_period_drag_bps(cost_bps, avg_turnover)
    drag_ann = drag_per_rebal_bps * rebalances_per_year / 10_000.0
    drag_per_day = drag_ann / 252.0
    rets_net_daily = rets_daily - drag_per_day

    sharpe_gross = sharpe(rets_daily.tolist(), periods_per_year=252)
    sharpe_net = sharpe(rets_net_daily.tolist(), periods_per_year=252)

    # Primary Carhart-4F gross — emits the orchestrator-readable α token.
    res4 = run_regression(
        rets_daily,
        factors[[*_CARHART_FACTORS, "RF"]],
        _CARHART_FACTORS,
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=252,
    )
    # G4 net-cost regression — t-stat must move with cost_bps (H1 fix).
    res4_net = run_regression(
        rets_net_daily,
        factors[[*_CARHART_FACTORS, "RF"]],
        _CARHART_FACTORS,
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=252,
    )

    # §5.1.1 realised β_market — single-factor (Mkt-RF) regression.
    res_capm = run_regression(
        rets_daily,
        factors[["Mkt-RF", "RF"]],
        ["Mkt-RF"],
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=252,
    )
    beta_market = float(res_capm.betas.get("Mkt-RF", float("nan")))
    bab_beta_flag = (
        bool(np.isfinite(beta_market) and beta_market < _BETA_MARKET_FLAG_THRESHOLD)
        if np.isfinite(beta_market)
        else False
    )

    # §5.1.2 FF5+UMD attenuation check.
    res_ff5_umd = run_regression(
        rets_daily,
        ff5_umd[[*_FF5_UMD_FACTORS, "RF"]],
        _FF5_UMD_FACTORS,
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=252,
    )
    alpha_ann_carhart = float(res4.alpha_annualized)
    alpha_ann_ff5_umd = float(res_ff5_umd.alpha_annualized)
    attenuation = (
        (alpha_ann_carhart - alpha_ann_ff5_umd) / alpha_ann_carhart
        if alpha_ann_carhart != 0
        else float("nan")
    )
    bab_attenuation_flag = bool(
        np.isfinite(attenuation) and attenuation > _ATTENUATION_FLAG_THRESHOLD
    )

    bench_aligned = bench_rets_daily.reindex(rets_daily.index).dropna()
    excess_per_day = (rets_daily.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann = float(excess_per_day * 252) if not np.isnan(excess_per_day) else float("nan")

    mean_top_n = float(
        sum(len(r.top_n_tickers) for r in report.rebalance_results)
        / max(1, len(report.rebalance_results))
    )
    return {
        "n": len(rets_daily),
        "mean_top_n": mean_top_n,
        "turnover_per_rebal": avg_turnover,
        "sharpe_gross": sharpe_gross,
        "sharpe_net": sharpe_net,
        "alpha_gross_4f": alpha_ann_carhart,
        "t_4f": float(res4.alpha_tstat),
        "beta_mkt_capm": beta_market,
        "beta_smb": float(res4.betas.get("SMB", 0.0)),
        "beta_hml": float(res4.betas.get("HML", 0.0)),
        "beta_mom": float(res4.betas.get("Mom", 0.0)),
        "alpha_ff5_umd": alpha_ann_ff5_umd,
        "t_ff5_umd": float(res_ff5_umd.alpha_tstat),
        "attenuation_carhart_to_ff5_umd": attenuation,
        "bab_beta_flag": bab_beta_flag,
        "bab_attenuation_flag": bab_attenuation_flag,
        "cost_drag_ann": drag_ann,
        "alpha_net_4f": float(res4_net.alpha_annualized),
        "t_net_4f": float(res4_net.alpha_tstat),
        "excess_vs_bench_ann": excess_ann,
        "excess_vs_bench_net": excess_ann - drag_ann,
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--top-n",
        type=int,
        default=200,
        help="Top-decile portfolio size. ~200 = 10%% of effective S&P 1500 universe.",
    )
    ap.add_argument("--holding", type=int, default=_HOLDING_LOCK)
    ap.add_argument(
        "--rebalance-stride",
        type=int,
        default=_REBALANCE_STRIDE_LOCK,
        help="Locked at 21 trading days (monthly) per memo §7. Override raises pre-reg violation.",
    )
    ap.add_argument("--benchmark", default="IWM")
    ap.add_argument(
        "--cost-half-spreads",
        nargs="+",
        type=float,
        default=[0.0, 5.0, 10.0, 15.0, 25.0],
        help="Cost stress grid per memo §7. G4 gate: αt @ 15bps must be ≥ 2.0.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/idiosyncratic_momentum_audit_run.json"),
        help="Canonical JSON output consumed by phase_robust_backtesting audit driver.",
    )
    ap.add_argument("--is-start", type=date.fromisoformat, default=date(2010, 1, 1))
    ap.add_argument("--is-end", type=date.fromisoformat, default=date(2017, 12, 31))
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument(
        "--universe-mode",
        choices=["SP1500"],
        default="SP1500",
        help="S&P 1500 PIT union (per pre-reg). Sole accepted value.",
    )
    ap.add_argument(
        "--universe-size-cap",
        type=int,
        default=None,
        help="Optional ticker cap for smoke runs.",
    )
    ap.add_argument(
        "--skip-precheck",
        action="store_true",
        help="No-op placeholder — idiosyncratic_momentum has no IS precheck. "
        "Accepted for smoke harness compatibility.",
    )
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Pre-reg locks per memo §7.
    if args.rebalance_stride != _REBALANCE_STRIDE_LOCK:
        sys.stderr.write(
            f"PRE-REG VIOLATION: --rebalance-stride={args.rebalance_stride} "
            f"overrides locked memo §7 value {_REBALANCE_STRIDE_LOCK}.\n"
        )
        return 9
    if args.holding != _HOLDING_LOCK:
        sys.stderr.write(
            f"PRE-REG VIOLATION: --holding={args.holding} overrides locked "
            f"memo §7 value {_HOLDING_LOCK}.\n"
        )
        return 9

    logger.info(
        "experiment idiosyncratic_momentum | universe=%s | %s..%s | phase_offset=%d",
        args.universe_mode,
        args.is_start,
        args.is_end,
        args.phase_offset,
    )

    universe = load_sp1500_pit_union()
    if not universe:
        sys.stderr.write("ERROR: empty S&P 1500 PIT union.\n")
        return 3
    if args.universe_size_cap is not None:
        universe = universe[: args.universe_size_cap]
        logger.info("Universe capped to %d tickers (smoke mode)", len(universe))
    logger.info("Universe: %d tickers", len(universe))

    logger.info(
        "Loading yfinance histories for %d tickers + %s benchmark…",
        len(universe),
        args.benchmark,
    )
    histories = load_cached_histories([*universe, args.benchmark], _PRICES_DIR)
    if args.benchmark not in histories or histories[args.benchmark].empty:
        sys.stderr.write(f"ERROR: benchmark {args.benchmark} OHLCV missing from {_PRICES_DIR}.\n")
        return 4

    history_store = HistoryStore(histories)

    # Load factor data spanning from 3 years before IS start so 36-month
    # residualisation has full warm-up.
    factor_load_start = date(args.is_start.year - 4, 1, 1)
    carhart = load_carhart_daily(start=factor_load_start, end=args.is_end)
    ff5_umd = _load_ff5_umd_daily(args.is_start, args.is_end)
    ff3_monthly = ff3_monthly_from_carhart_daily(carhart)
    rf_monthly = ff3_monthly["RF"] if "RF" in ff3_monthly.columns else None
    if rf_monthly is None:
        # Fallback: derive monthly RF from daily carhart directly.
        rf_monthly = (1.0 + carhart["RF"]).resample("ME").prod() - 1.0
    ff3_monthly_features = ff3_monthly[["Mkt-RF", "SMB", "HML"]]

    scorer = IdiosyncraticMomentumScorer(ff3_monthly_features, rf_monthly)
    engine = BacktestEngine(
        history_store=history_store,
        scorer=scorer,
        scorer_config={"benchmark": args.benchmark},
        holding_period=args.holding,
        top_n=args.top_n,
        benchmark=args.benchmark,
        screener_tickers=universe,
        weighting="equal",
        rebalance_stride=args.rebalance_stride,
        phase_offset=args.phase_offset,
    )
    logger.info(
        "Engine: stride=%d holding=%d top_n=%d phase_offset=%d benchmark=%s",
        args.rebalance_stride,
        args.holding,
        args.top_n,
        args.phase_offset,
        args.benchmark,
    )

    report = engine.run(args.is_start, args.is_end)
    logger.info("Engine completed: %d rebalances", len(report.rebalance_results))

    # §5.1.3 raw-momentum comparator pass — same universe + MIN_BARS=800 +
    # cadence, scored by raw 12-1 cumulative return instead of FF3
    # residualised momentum. Sharpe-improvement of IM over raw is the
    # Blitz primary claim (anti-pattern documentation per memo §5.1).
    logger.info("Running §5.1.3 raw-momentum comparator pass…")
    raw_scorer = _RawMonthlyMomentumScorer()
    raw_engine = BacktestEngine(
        history_store=history_store,
        scorer=raw_scorer,
        scorer_config={"benchmark": args.benchmark},
        holding_period=args.holding,
        top_n=args.top_n,
        benchmark=args.benchmark,
        screener_tickers=universe,
        weighting="equal",
        rebalance_stride=args.rebalance_stride,
        phase_offset=args.phase_offset,
    )
    raw_report = raw_engine.run(args.is_start, args.is_end)
    raw_rets_daily = daily_continuous_returns(
        raw_report.rebalance_results,
        history_store,
        calendar_ticker=args.benchmark,
        end_date=args.is_end,
    )
    if not raw_rets_daily.empty:
        sharpe_raw_gross = sharpe(raw_rets_daily.tolist(), periods_per_year=252)
    else:
        sharpe_raw_gross = float("nan")
    logger.info(
        "Raw-momentum comparator: %d rebalances, Sharpe gross=%.3f",
        len(raw_report.rebalance_results),
        sharpe_raw_gross,
    )

    # Reload carhart aligned to IS window for assess() — engine warm-up
    # factor span was extended, but Carhart attribution operates on the IS
    # window only.
    carhart_is = load_carhart_daily(start=args.is_start, end=args.is_end)
    bench_rets = benchmark_returns(history_store, args.benchmark, args.is_start, args.is_end)

    all_rows: list[dict] = []
    for cost_bps in args.cost_half_spreads:
        stats = assess(
            report,
            carhart_is,
            ff5_umd,
            args.rebalance_stride,
            cost_bps,
            bench_rets,
            history_store=history_store,
            benchmark=args.benchmark,
            end_date=args.is_end,
        )
        stats["cost_bps"] = cost_bps
        all_rows.append(stats)
        if stats.get("n", 0) > 0:
            logger.info(
                "cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
                "Sh gross=%.2f net=%.2f | β_mkt=%.2f | "
                "α 4F=%.1f%% t=%.2f | α-net 4F=%.1f%% t-net=%.2f | "
                "α FF5+UMD=%.1f%% atten=%.0f%%",
                cost_bps,
                stats["n"],
                stats["mean_top_n"],
                stats["turnover_per_rebal"] * 100,
                stats["sharpe_gross"],
                stats["sharpe_net"],
                stats["beta_mkt_capm"],
                stats["alpha_gross_4f"] * 100,
                stats["t_4f"],
                stats["alpha_net_4f"] * 100,
                stats["t_net_4f"],
                stats["alpha_ff5_umd"] * 100,
                (stats["attenuation_carhart_to_ff5_umd"] * 100)
                if np.isfinite(stats["attenuation_carhart_to_ff5_umd"])
                else float("nan"),
            )

    baseline = next(
        (r for r in all_rows if math.isclose(r["cost_bps"], 5.0, abs_tol=1e-5)),
        all_rows[0] if all_rows else None,
    )
    stress_15bps = next(
        (r for r in all_rows if math.isclose(r["cost_bps"], 15.0, abs_tol=1e-5)),
        None,
    )
    gate_summary: dict = {}
    if baseline and baseline.get("n", 0) > 0:
        net_t = baseline["t_4f"]
        gate_summary["G1_alpha_t_baseline"] = {
            "value": net_t,
            "class_internal_threshold": _BONFERRONI_CRITICAL_T_CLASS_INTERNAL,
            "project_threshold": _BONFERRONI_PROJECT_THRESHOLD,
            "passed_project": net_t >= _BONFERRONI_PROJECT_THRESHOLD,
        }
    if stress_15bps and stress_15bps.get("n", 0) > 0:
        gate_summary["G4_alpha_t_at_15bps"] = {
            "value": stress_15bps["t_net_4f"],
            "threshold": 2.0,
            "passed": stress_15bps["t_net_4f"] >= 2.0,
        }
    # §5.1 diagnostic flags piggy-back on the baseline row so the
    # orchestrator JSON has a single canonical location.
    if baseline and baseline.get("n", 0) > 0:
        gate_summary["bab_diagnostics"] = {
            "beta_market": baseline["beta_mkt_capm"],
            "beta_market_flag": baseline["bab_beta_flag"],
            "ff5_umd_attenuation": baseline["attenuation_carhart_to_ff5_umd"],
            "attenuation_flag": baseline["bab_attenuation_flag"],
        }
        # Pre-reg turnover_logging_mandate: flag mean_monthly_turnover > 80%.
        # rebalance_stride=21 makes turnover_per_rebal numerically equal to
        # mean monthly turnover.
        mean_monthly_turnover = float(baseline["turnover_per_rebal"])
        gate_summary["hyper_turnover_flag"] = {
            "mean_monthly_turnover": mean_monthly_turnover,
            "threshold": _HYPER_TURNOVER_FLAG_THRESHOLD,
            "material_finding": mean_monthly_turnover > _HYPER_TURNOVER_FLAG_THRESHOLD,
        }
        # §5.1.3 raw-momentum comparison — anti-pattern documentation per
        # Blitz primary claim. Not a PASS gate; logged for postmortem.
        gate_summary["raw_momentum_diagnostic"] = {
            "sharpe_im_gross": baseline["sharpe_gross"],
            "sharpe_raw_gross": sharpe_raw_gross,
            "sharpe_improvement": baseline["sharpe_gross"] - sharpe_raw_gross,
        }

    payload = {
        "strategy": "idiosyncratic_momentum",
        "ledger_id": "idiosyncratic_momentum_2026_05_14_v1",
        "signal_class": "price_factor_search_2026_04_29",
        "design_memo": "docs/research/idiosyncratic_momentum_v1_design_2026_05_14.md",
        "is_start": args.is_start.isoformat(),
        "is_end": args.is_end.isoformat(),
        "phase_offset": args.phase_offset,
        "universe_size": len(universe),
        "rebalance_stride": args.rebalance_stride,
        "holding": args.holding,
        "top_n": args.top_n,
        "benchmark": args.benchmark,
        "cost_grid_results": [
            {
                k: (v if not isinstance(v, (np.floating, np.integer)) else float(v))
                for k, v in r.items()
            }
            for r in all_rows
        ],
        "gate_summary": gate_summary,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    logger.info("Wrote audit output to %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
