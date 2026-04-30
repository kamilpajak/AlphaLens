"""Phase 3b full backtest runbook for Layer 2d insider screener.

Inputs (assumed already built by earlier phases):
- ``~/.alphalens/pit_universe/{YYYY-MM}.yaml`` — PIT universe snapshots (Phase 2.5)
- ``~/.alphalens/prices/{TICKER}.parquet`` — yfinance OHLCV cache (Phase 2.5)
- ``~/.alphalens/insider_form4/{TICKER}_{ASOF}.json`` — scorer cache (Phase 3a)
- ``alphalens/alt_data/data/ticker_cik_map.yaml`` — SEC ticker map (Phase 2)
- Ken French factor CSVs under ``~/.alphalens/factors/`` (one-time manual refresh)

Outputs:
- ``docs/backtest/layer2d_insider_{split}.{md,csv}``  (one run per --split)
- ``docs/research/layer2d_validation_final.md``      (final Phase 3b.5 doc;
  written by user after reviewing all splits — this script only feeds data)

Rebalance cadence (``--rebalance-stride``):
- ``1`` (daily, default) — matches original Phase 3b design. Requires warm
  EDGAR cache or the per-(ticker,asof) fetches push runtime past 24h on
  12-year × 1400-ticker sweeps. Prewarm with ``scripts/prewarm_form4_cache.py``
  before running daily.
- ``5`` (weekly) — explicit opt-in when cache is cold; N=604 in-sample / 160 OOS,
  2.24× larger HAC SE per Perplexity R10 review. Cost model + Sharpe annualize
  at ``252/stride`` rebalances per year automatically.
- ``21`` (monthly) — not standard; use only for ad-hoc smoke tests.

Usage:
    # Daily rebalance (requires warm cache):
    .venv/bin/python scripts/run_layer2d_backtest.py \\
        --split insample --start 2011-01-01 --end 2022-12-31 --top-n 15
    # Weekly rebalance (cold cache, ~6h runtime per split):
    .venv/bin/python scripts/run_layer2d_backtest.py \\
        --split oos --start 2023-01-01 --end 2026-04-22 --top-n 15 \\
        --rebalance-stride 5

GATE 1 YELLOW constraint: default top_n=15 (not 30) per plan file §GATE
decision rules, acknowledging signal scarcity (~16 clusters/mo observed
2026-04-22) may underpower a wider top-30.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yaml

from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter
from alphalens.archive.screeners.insider.scorer import InsiderScorer
from alphalens.backtest.cost_model import RealisticCostModel
from alphalens.backtest.decision_matrix import evaluate_exit_criteria
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.factor_analysis import (
    bootstrap_carhart_alpha_ci,
    run_carhart_attribution,
    run_ff5_umd_attribution,
    run_q4_attribution,
    run_regression,
)
from alphalens.backtest.metrics import sharpe, sharpe_autocorr_adjusted, turnover_pct
from alphalens.backtest.regime import classify_regime
from alphalens.data.alt_data.sec_edgar_client import SecEdgarClient
from alphalens.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import (
    load_carhart_daily,
    load_ff5_umd_daily,
    load_q4_daily,
)
from alphalens.data.store.history import HistoryStore

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_CACHE_DIR = Path.home() / ".alphalens" / "insider_form4"
_CIK_MAP_PATH = Path("alphalens/alt_data/data/ticker_cik_map.yaml")


_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]
_BOOTSTRAP_ITERATIONS = 10_000
# Primary spread assumption per design doc §5 R8 — 5 bps half-spread on R2000 retail.
_PRIMARY_HALF_SPREAD_BPS = 5.0
# k=0.15 stress: Almgren-Chriss requires per-trade ADV/size which the daily
# engine does not retain. We proxy k=0.15 with a 3× primary half-spread (15 bps)
# — matches the doubling-to-tripling cost-regime discussed in §5 R5/R8 for
# adverse execution on thin names.
_STRESS_HALF_SPREAD_BPS = 15.0
_ADVERSE_SELECTION_BPS = 5.0
_REGIME_MIN_OBS = 30


def compute_regime_alpha_tstats(
    portfolio_returns: pd.Series,
    benchmark_close: pd.Series,
    carhart_factors: pd.DataFrame,
) -> dict[str, float]:
    """Per-regime Carhart-4F alpha t-stats (HAC cov).

    Regimes defined by 60-day trailing benchmark return (classify_regime):
    bull (>+5%), bear (<-5%), flat (between). Each regime subset is re-regressed
    against the full Carhart factor set. Requires >=_REGIME_MIN_OBS observations
    per regime to produce a valid t-stat; otherwise returns 0.0 (neutral).
    """
    labels = classify_regime(benchmark_close)
    out: dict[str, float] = {}
    for regime in ("bull", "bear", "flat"):
        regime_dates = labels[labels == regime].index
        port_slice = portfolio_returns.reindex(regime_dates).dropna()
        if len(port_slice) < _REGIME_MIN_OBS:
            logger.warning(
                "regime %s: %d obs < %d min → t-stat set to 0.0",
                regime,
                len(port_slice),
                _REGIME_MIN_OBS,
            )
            out[regime] = 0.0
            continue
        try:
            result = run_regression(
                port_slice,
                carhart_factors,
                _CARHART_FACTORS,
                spec_name=f"Carhart-{regime}",
            )
            out[regime] = result.alpha_tstat
            logger.info(
                "regime %s: n=%d alpha_ann=%.2f%% t=%.2f",
                regime,
                result.n_observations,
                result.alpha_annualized * 100,
                result.alpha_tstat,
            )
        except ValueError as exc:
            logger.warning("regime %s regression failed: %s", regime, exc)
            out[regime] = 0.0
    return out


def compute_net_alpha(
    gross_alpha_annualized: float,
    avg_rebal_turnover: float,
    rebalances_per_year: float,
) -> tuple[float, float, float, float]:
    """Primary + stress net alphas derived from per-rebalance turnover × round-trip spread.

    ``avg_rebal_turnover`` is the mean fraction of top-N that churns between
    consecutive rebalance snapshots (daily if stride=1, weekly if stride=5).
    ``rebalances_per_year`` must match the sampling cadence: 252 daily, 52
    weekly, 12 monthly. Otherwise drag is mis-scaled.

    Returns (net_primary, net_stress, primary_drag_ann, stress_drag_ann).
    """
    cost_model = RealisticCostModel(adverse_selection_bps=_ADVERSE_SELECTION_BPS)
    primary_rt_bps = cost_model.primary_round_trip_bps(_PRIMARY_HALF_SPREAD_BPS)
    stress_rt_bps = cost_model.primary_round_trip_bps(_STRESS_HALF_SPREAD_BPS)

    primary_drag_ann = primary_rt_bps * avg_rebal_turnover * rebalances_per_year / 10_000.0
    stress_drag_ann = stress_rt_bps * avg_rebal_turnover * rebalances_per_year / 10_000.0

    return (
        gross_alpha_annualized - primary_drag_ann,
        gross_alpha_annualized - stress_drag_ann,
        primary_drag_ann,
        stress_drag_ann,
    )


def load_pit_union(start: date, end: date) -> list[str]:
    """Union of all PIT-snapshot tickers for month-ends in [start, end].

    Phase 3b uses the superset because per-day PIT filtering would
    require adapter surgery; the scorer itself is already PIT-safe
    (features_as_of filters filing_date ≤ asof), so tickers outside
    the concurrent PIT universe simply score None and get excluded.
    """
    union: set[str] = set()
    for path in sorted(_PIT_DIR.glob("*.yaml")):
        snap_date = date.fromisoformat(path.stem + "-01")
        if not (start.replace(day=1) <= snap_date <= end):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for ticker in data.get("tickers", []):
            union.add(ticker)
    return sorted(union)


def run_split(
    split: str,
    start: date,
    end: date,
    top_n: int,
    user_agent: str,
    benchmark: str,
    holding: int,
    rebalance_stride: int,
) -> None:
    logger.info("Phase 3b.3 backtest: split=%s %s..%s top_n=%d", split, start, end, top_n)

    universe = load_pit_union(start, end)
    logger.info("PIT union universe: %d tickers", len(universe))
    if not universe:
        logger.error("empty universe; build PIT snapshots first (Phase 2.5)")
        sys.exit(2)

    tickers_with_bench = [*universe, benchmark]
    histories = load_cached_histories(tickers_with_bench, _PRICES_DIR)
    if benchmark not in histories:
        logger.error("benchmark %s missing from cache; add to yfinance cache", benchmark)
        sys.exit(2)
    logger.info("loaded %d histories (need %d)", len(histories), len(tickers_with_bench))
    store = HistoryStore(histories)

    cik_map = TickerCikMap.load(_CIK_MAP_PATH)
    insider_store = InsiderScorer(
        edgar_client=SecEdgarClient(user_agent=user_agent),
        ticker_cik_map=cik_map,
        cache_dir=_CACHE_DIR,
    )

    engine = BacktestEngine(
        store,
        scorer=insider_scorer_adapter,
        scorer_config={"benchmark": benchmark, "_insider_store": insider_store},
        holding_period=holding,
        top_n=top_n,
        benchmark=benchmark,
        screener_tickers=universe,
        weighting="linear",
        rebalance_stride=rebalance_stride,
    )

    report = engine.run(start, end)
    logger.info(
        "backtest done; %d daily snapshots, mean IC=%.4f",
        len(report.rebalance_results),
        report.ic_series.mean() if len(report.ic_series) else 0.0,
    )

    portfolio_returns = report.portfolio_returns
    if portfolio_returns.empty:
        logger.error("no portfolio returns; scorer likely returned nothing")
        sys.exit(3)

    # Factor attributions
    carhart_factors = load_carhart_daily(start=start, end=end)
    carhart = run_carhart_attribution(portfolio_returns, carhart_factors)[-1]
    ff5_umd = run_ff5_umd_attribution(portfolio_returns, load_ff5_umd_daily(start=start, end=end))
    try:
        q4 = run_q4_attribution(portfolio_returns, load_q4_daily(start=start, end=end))
    except Exception as exc:
        logger.warning("Q4 attribution skipped: %s", exc)
        q4 = None

    benchmark_close = store.truncate_to(benchmark, end)["close"]
    regime_alpha_tstats = compute_regime_alpha_tstats(
        portfolio_returns, benchmark_close, carhart_factors
    )

    logger.info("running bootstrap CI on Carhart alpha (iter=%d)…", _BOOTSTRAP_ITERATIONS)
    ci_low_ann, ci_high_ann = bootstrap_carhart_alpha_ci(portfolio_returns, carhart_factors)
    bootstrap_ci_excludes_zero = (ci_low_ann > 0) or (ci_high_ann < 0)
    logger.info(
        "bootstrap 95%% CI (annualized): [%.2f%%, %.2f%%] → excludes_zero=%s",
        ci_low_ann * 100,
        ci_high_ann * 100,
        bootstrap_ci_excludes_zero,
    )

    avg_rebal_turnover = turnover_pct(r.top_n_tickers for r in report.rebalance_results)
    rebalances_per_year = 252 / max(1, engine.rebalance_stride)
    net_alpha_primary, net_alpha_stress, primary_drag, stress_drag = compute_net_alpha(
        carhart.alpha_annualized, avg_rebal_turnover, rebalances_per_year
    )
    logger.info(
        "turnover/rebal=%.1f%% rebal/y=%.0f | primary drag=%.2f%%/y (net α=%.2f%%) | stress drag=%.2f%%/y (net α=%.2f%%)",
        avg_rebal_turnover * 100,
        rebalances_per_year,
        primary_drag * 100,
        net_alpha_primary * 100,
        stress_drag * 100,
        net_alpha_stress * 100,
    )

    sharpe_net = sharpe(portfolio_returns.tolist(), periods_per_year=int(rebalances_per_year))
    sharpe_net_adj = sharpe_autocorr_adjusted(
        portfolio_returns.tolist(), periods_per_year=int(rebalances_per_year)
    )
    logger.info(
        "Sharpe (naive sqrt-k)=%.2f | Sharpe (autocorr-adj, Perplexity R11)=%.2f",
        sharpe_net,
        sharpe_net_adj,
    )
    decision = evaluate_exit_criteria(
        carhart=carhart,
        ff5_umd=ff5_umd,
        q4=q4,
        net_alpha_primary=net_alpha_primary,
        net_alpha_stress_k15=net_alpha_stress,
        bootstrap_95ci_excludes_zero=bootstrap_ci_excludes_zero,
        sharpe_net=sharpe_net,
        regime_alpha_tstats=regime_alpha_tstats,
        n_tests=2,
    )

    # Report
    out_dir = Path("docs/backtest")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"layer2d_insider_{split}.md"
    csv_path = out_dir / f"layer2d_insider_{split}.csv"

    md_lines = [
        f"# Layer 2d insider backtest — {split} ({start} to {end})",
        "",
        f"- Universe (PIT union): {len(universe)} tickers",
        f"- Holding period: {holding} days",
        f"- Top-N: {top_n}",
        f"- Benchmark: {benchmark}",
        f"- Rebalance stride: {engine.rebalance_stride} trading day(s) ({rebalances_per_year:.0f}/y)",
        f"- Avg per-rebalance turnover: {avg_rebal_turnover * 100:.1f}%",
        f"- Sharpe (naive sqrt-k, rebal cadence): {sharpe_net:.2f}",
        f"- Sharpe (autocorr-adjusted, Perplexity R11): {sharpe_net_adj:.2f}",
        "",
        "## Factor attribution",
        "",
        "| Spec | α (ann) | α t-stat | R² | n |",
        "|---|---:|---:|---:|---:|",
        f"| Carhart-4F | {carhart.alpha_annualized * 100:.2f}% | {carhart.alpha_tstat:.2f} | {carhart.r_squared:.3f} | {carhart.n_observations} |",
        f"| FF5+UMD    | {ff5_umd.alpha_annualized * 100:.2f}% | {ff5_umd.alpha_tstat:.2f} | {ff5_umd.r_squared:.3f} | {ff5_umd.n_observations} |",
    ]
    if q4 is not None:
        md_lines.append(
            f"| Q4         | {q4.alpha_annualized * 100:.2f}% | {q4.alpha_tstat:.2f} | {q4.r_squared:.3f} | {q4.n_observations} |"
        )

    md_lines += [
        "",
        "## Cost sensitivity",
        "",
        f"- Primary (half-spread {_PRIMARY_HALF_SPREAD_BPS:.0f} bps): drag = {primary_drag * 100:.2f}%/y → **net α = {net_alpha_primary * 100:.2f}%**",
        f"- Stress (half-spread {_STRESS_HALF_SPREAD_BPS:.0f} bps, ~k=0.15 proxy): drag = {stress_drag * 100:.2f}%/y → **net α = {net_alpha_stress * 100:.2f}%**",
        "",
        "## Bootstrap CI (Carhart α, annualized)",
        "",
        f"- Block-bootstrap 95% CI: [{ci_low_ann * 100:.2f}%, {ci_high_ann * 100:.2f}%]",
        f"- Excludes zero: **{bootstrap_ci_excludes_zero}** ({_BOOTSTRAP_ITERATIONS} iters, block n^(1/3))",
        "",
        "## Regime breakdown (Carhart α t-stat, HAC)",
        "",
        "| Regime | α t-stat |",
        "|---|---:|",
    ]
    for regime in ("bull", "bear", "flat"):
        md_lines.append(f"| {regime} | {regime_alpha_tstats.get(regime, 0.0):.2f} |")

    md_lines += [
        "",
        "## Decision matrix",
        "",
        f"**Verdict: {decision.verdict}**",
        "",
        "| Gate | Pass |",
        "|---|:---:|",
    ]
    for name, ok in decision.gates.items():
        md_lines.append(f"| {name} | {'✓' if ok else '✗'} |")
    if decision.failing_gates:
        md_lines += ["", "### Failing gates", ""]
        for g in decision.failing_gates:
            md_lines.append(f"- {g}")
    if decision.notes:
        md_lines += ["", "### Notes", ""]
        for n in decision.notes:
            md_lines.append(f"- {n}")

    md_path.write_text("\n".join(md_lines) + "\n")
    logger.info("wrote markdown → %s", md_path)

    # Daily CSV
    portfolio_returns.to_csv(csv_path, header=["portfolio_return"])
    logger.info("wrote CSV → %s", csv_path)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["insample", "oos", "full"], default="full")
    ap.add_argument("--start", type=date.fromisoformat, default=date(2009, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--top-n", type=int, default=15)  # YELLOW constraint per GATE 1
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--benchmark", default="SPY")
    # stride=1 → daily (original Phase 3b design); 5 → weekly; 21 → monthly.
    # Daily default matches design doc — when cache is cold, pass --rebalance-stride 5
    # explicitly and cite the Perplexity R10 caveats in post-mortem.
    ap.add_argument("--rebalance-stride", type=int, default=1)
    return ap.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()
    ua = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        print("ERROR: SEC_EDGAR_USER_AGENT env var required", file=sys.stderr)
        return 2

    run_split(
        split=args.split,
        start=args.start,
        end=args.end,
        top_n=args.top_n,
        user_agent=ua,
        benchmark=args.benchmark,
        holding=args.holding,
        rebalance_stride=args.rebalance_stride,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
