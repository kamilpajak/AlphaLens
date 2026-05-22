"""ev_fcff_yield_2026_05_12_v1 — EV/FCFF-Yield value screener on R2000 ex-fin.

Pre-reg context:
- Class: fundamental_value_dcf_2026_05_12 (LOCKED 2026-05-12, first test in class).
- Design memo: docs/research/ev_fcff_yield_v1_design_2026_05_12.md
- Bonferroni: class-internal n=1 (|t|>=1.96), project-imposed conservative
  threshold |t|>=3.5 per memo §8.
- Data: SimFin Start tier ($25/mo paid 2026-05-12), 10y depth from 2016,
  PIT via PUBLISH_DATE column.
- Universe: R2000 active (IWM current snapshot) ∩ SimFin us-income-annual
  (excludes banks + insurance per Option D 2026-05-12). Forward-looking
  universe known limitation per memo §2.
- Rebalance: quarterly (stride locked at 63 trading days ≈ Feb/May/Aug/Nov
  filing-aligned). Long-only top-decile equal-weight, no sector neutralization.
- Cost stress grid: --cost-half-spreads default mirrors memo §6 grid
  {0, 5, 10, 15, 25} bps. G4 gate: net αt at 15bps must remain ≥ 2.0.
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
from alphalens_research.attribution.cost_model import RealisticCostModel  # noqa: E402
from alphalens_research.attribution.factor_analysis import run_regression  # noqa: E402
from alphalens_research.backtest.daily_continuous_returns import (  # noqa: E402
    daily_continuous_returns,
)
from alphalens_research.backtest.engine import BacktestEngine  # noqa: E402
from alphalens_research.backtest.metrics import sharpe, turnover_pct  # noqa: E402
from alphalens_research.data.alt_data.russell_universe import load_iwm_current  # noqa: E402
from alphalens_research.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens_research.data.factors import load_carhart_daily  # noqa: E402
from alphalens_research.data.store.edgar_fundamentals import EdgarFundamentalsStore  # noqa: E402
from alphalens_research.data.store.history import HistoryStore  # noqa: E402
from alphalens_research.screeners.ev_fcff_yield.adapter import EvFcffYieldScorer  # noqa: E402

logger = logging.getLogger(__name__)

# Per pre-reg memo §5.
_REBALANCE_STRIDE_LOCK = 63  # quarterly ≈ Feb/May/Aug/Nov filing cadence
_HOLDING_LOCK = 63
_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]
_HAC_MAXLAGS_LOCK = 126  # daily, 6 months — matches quarterly signal window
_BONFERRONI_CRITICAL_T_CLASS_INTERNAL = 1.96
_BONFERRONI_PROJECT_THRESHOLD = 3.5  # memo §8 conservative
_IWM_SNAPSHOT = REPO_ROOT / "alphalens_research" / "data" / "alt_data" / "data" / "iwm_current.yaml"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"


def _load_universe_ex_financials(
    store: EdgarFundamentalsStore,
) -> list[str]:
    """R2000 active ∩ EDGAR cache, with banks + insurance explicitly excluded.

    Prior to PR #161, this intersected with SimFin's ``us-income-annual``
    dataset which structurally excluded financials (banks/insurance lived
    in separate SimFin datasets). EDGAR companyfacts covers every filer
    including financials, so we restore the original semantics by
    filtering on SEC SIC division metadata via
    ``alphalens_research.data.fundamentals.sic_index`` (re-exported through
    ``alphalens_research/thematic/screening/sector_peers.py``). SEC Division H is
    "Finance, Insurance and Real Estate" — match the substring "Finance".
    Required: EV / FCFF math is mathematically broken for banks (debt is
    raw material, OCF is dominated by loan originations), so leaking them
    in would corrupt the paradigm-13 replay verdicts.

    Universe is forward-looking (current IWM snapshot, not PIT) per
    design memo §2 known-limitation.
    """
    from alphalens_research.thematic.screening import sector_peers

    iwm_tickers = set(load_iwm_current(_IWM_SNAPSHOT))
    edgar_tickers = set(store.universe())
    universe: list[str] = []
    excluded_financials = 0
    for ticker in sorted(iwm_tickers & edgar_tickers):
        ind_id = sector_peers.get_industry_id(ticker)
        if ind_id is not None:
            _, sector = sector_peers.industry_label(ind_id)
            if sector and "Finance" in sector:
                excluded_financials += 1
                continue
        universe.append(ticker)
    logger.info(
        "R2000 ex-fin universe: %d tickers (IWM=%d ∩ EDGAR=%d, %d financials dropped)",
        len(universe),
        len(iwm_tickers),
        len(edgar_tickers),
        excluded_financials,
    )
    return universe


def benchmark_returns(
    history_store: HistoryStore, benchmark: str, start: date, end: date
) -> pd.Series:
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def assess(
    report,
    factors: pd.DataFrame,
    rebalance_stride: int,
    cost_bps: float,
    bench_rets_daily: pd.Series,
    *,
    history_store: HistoryStore,
    benchmark: str,
    end_date: date,
) -> dict:
    """Daily-cadence Carhart attribution — mirrors compound driver lock."""
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

    res4 = run_regression(
        rets_daily,
        factors[[*_CARHART_FACTORS, "RF"]],
        _CARHART_FACTORS,
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=252,
    )
    # H1 (issue #105): second regression on net daily so the t-stat is
    # actually sensitive to cost_bps. Pre-fix the scalar α_gross − drag_ann
    # post-hoc adjustment left t_4f cost-invariant, making G4 cost-stress a
    # structural no-op duplicate of G1. G4 must consume t_net_4f.
    res4_net = run_regression(
        rets_net_daily,
        factors[[*_CARHART_FACTORS, "RF"]],
        _CARHART_FACTORS,
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=252,
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
        "alpha_gross_4f": float(res4.alpha_annualized),
        "t_4f": float(res4.alpha_tstat),
        "beta_smb": float(res4.betas.get("SMB", 0.0)),
        "beta_hml": float(res4.betas.get("HML", 0.0)),
        "beta_mom": float(res4.betas.get("Mom", 0.0)),
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
        default=150,
        help="Top-decile portfolio size. ~150 = 10%% of effective R2000 ex-fin universe.",
    )
    ap.add_argument("--holding", type=int, default=_HOLDING_LOCK)
    ap.add_argument(
        "--rebalance-stride",
        type=int,
        default=_REBALANCE_STRIDE_LOCK,
        help="Locked at 63 trading days (quarterly) per memo §5. Override raises pre-reg violation.",
    )
    ap.add_argument("--benchmark", default="IWM")
    ap.add_argument(
        "--cost-half-spreads",
        nargs="+",
        type=float,
        default=[0.0, 5.0, 10.0, 15.0, 25.0],
        help="Cost stress grid per memo §6. G4 gate: αt @ 15bps must be ≥ 2.0.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/ev_fcff_yield_audit_run.json"),
        help="Canonical JSON output path consumed by phase_robust_backtesting audit driver.",
    )
    ap.add_argument("--is-start", type=date.fromisoformat, default=date(2016, 8, 31))
    ap.add_argument("--is-end", type=date.fromisoformat, default=date(2019, 8, 31))
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument(
        "--universe-mode",
        choices=["R2000"],
        default="R2000",
        help="R2000 ex-financials (per pre-reg). Sole accepted value.",
    )
    ap.add_argument(
        "--universe-size-cap",
        type=int,
        default=None,
        help="Optional ticker cap for smoke runs. Smoke profile uses 200.",
    )
    ap.add_argument(
        "--skip-precheck",
        action="store_true",
        help="No-op placeholder — ev_fcff_yield has no IS precheck. Accepted for smoke harness compatibility.",
    )
    ap.add_argument(
        "--no-prices",
        action="store_true",
        help="Smoke optimization — skip ~435MB SimFin daily shareprices load and use yfinance-derived market caps via separate path. Tests only; full audit requires SimFin prices.",
    )
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Pre-reg lock per memo §5 quarterly cadence.
    if args.rebalance_stride != _REBALANCE_STRIDE_LOCK:
        sys.stderr.write(
            f"PRE-REG VIOLATION: --rebalance-stride={args.rebalance_stride} "
            f"overrides locked memo §5 value {_REBALANCE_STRIDE_LOCK}. "
            "Quarterly cadence is the cost-drag defense from the adversarial review. "
            "Remove the override to proceed.\n"
        )
        return 9
    if args.holding != _HOLDING_LOCK:
        sys.stderr.write(
            f"PRE-REG VIOLATION: --holding={args.holding} overrides locked "
            f"memo §5 value {_HOLDING_LOCK}.\n"
        )
        return 9

    logger.info(
        "experiment ev_fcff_yield | universe=%s | %s..%s | phase_offset=%d",
        args.universe_mode,
        args.is_start,
        args.is_end,
        args.phase_offset,
    )

    store = EdgarFundamentalsStore(with_prices=not args.no_prices)
    logger.info("EdgarFundamentalsStore ready (SEC XBRL companyfacts parquets).")

    universe = _load_universe_ex_financials(store)
    if not universe:
        sys.stderr.write("ERROR: empty universe after R2000 ∩ SimFin ex-fin filter.\n")
        return 3
    if args.universe_size_cap is not None:
        universe = universe[: args.universe_size_cap]
        logger.info("Universe capped to %d tickers (smoke mode)", len(universe))

    logger.info(
        "Loading yfinance histories for %d tickers + %s benchmark…", len(universe), args.benchmark
    )
    histories = load_cached_histories([*universe, args.benchmark], _PRICES_DIR)
    if args.benchmark not in histories or histories[args.benchmark].empty:
        sys.stderr.write(f"ERROR: benchmark {args.benchmark} OHLCV missing from {_PRICES_DIR}.\n")
        return 4

    history_store = HistoryStore(histories)

    scorer = EvFcffYieldScorer(store)
    engine = BacktestEngine(
        history_store=history_store,
        scorer=scorer,
        scorer_config={},
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

    carhart = load_carhart_daily(start=args.is_start, end=args.is_end)
    bench_rets = benchmark_returns(history_store, args.benchmark, args.is_start, args.is_end)

    all_rows: list[dict] = []
    for cost_bps in args.cost_half_spreads:
        stats = assess(
            report,
            carhart,
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
                "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
                "α 4F=%.1f%% t=%.2f | α-net 4F=%.1f%% t-net=%.2f",
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
                stats["alpha_net_4f"] * 100,
                stats["t_net_4f"],
            )

    # Pre-reg gate evaluation (against baseline cost row, conventionally 5bps).
    # `math.isclose` rather than `==` so a future caller passing a computed
    # cost grid (linspace/arange) still matches the canonical 5/15 bps rows.
    # Issue #105 L2.
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
        # G4 reads t_net_4f (regression on net daily) — t_4f (gross) is
        # cost-invariant by construction and would duplicate G1. Issue #105 H1.
        gate_summary["G4_alpha_t_at_15bps"] = {
            "value": stress_15bps["t_net_4f"],
            "threshold": 2.0,
            "passed": stress_15bps["t_net_4f"] >= 2.0,
        }

    payload = {
        "strategy": "ev_fcff_yield",
        "ledger_id": "ev_fcff_yield_2026_05_12_v1",
        "signal_class": "fundamental_value_dcf_2026_05_12",
        "design_memo": "docs/research/ev_fcff_yield_v1_design_2026_05_12.md",
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
