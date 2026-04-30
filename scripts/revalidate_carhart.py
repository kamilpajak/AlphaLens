"""Re-validate Layer 2b themed screener (momentum scorer) under the new Carhart-4F + HAC factor pipeline.

Reference: project_mvp1_backtest_findings memo records Sharpe 1.61 net + FF3 alpha
t-stat 2.78 on top-5 × linear weighting over 2021-04 → 2026-04.

This script runs the same config through the upgraded attribution pipeline:
  CAPM → FF3 → Carhart-4F, all with Newey-West HAC standard errors.
The Carhart row answers: does alpha survive once UMD (momentum factor) is in the RHS?
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.archive.screeners.lean.config import DATA_DIR
from alphalens.archive.screeners.lean.lean_csv_loader import load_lean_histories
from alphalens.archive.screeners.themed.backtest_adapter import momentum_scorer_adapter
from alphalens.archive.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
from alphalens.archive.screeners.themed.universe import flatten_universe
from alphalens.attribution.factor_analysis import (
    format_attribution_table,
    run_carhart_attribution,
    run_regression,
)
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.metrics import rank_ic_tstat, sharpe
from alphalens.data.factors import load_carhart_daily, load_industry12_daily
from alphalens.data.store.history import HistoryStore

START = date(2021, 4, 19)
END = date(2026, 4, 17)
TOP_N = 5
HOLDING = 5
BENCHMARK = "SPY"


def main() -> None:
    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    themes_map = flatten_universe(universe)
    curated = sorted(themes_map.keys())
    print(f"Curated universe: {len(curated)} tickers")

    print("Loading Lean CSV histories…")
    histories = load_lean_histories(DATA_DIR, [*curated, BENCHMARK])
    store = HistoryStore(histories)
    print(f"  loaded {len(store.tickers())} tickers")

    cfg = dict(THEMED_DEFAULTS)
    cfg["benchmark"] = BENCHMARK

    engine = BacktestEngine(
        store,
        scorer=momentum_scorer_adapter,
        scorer_config=cfg,
        holding_period=HOLDING,
        top_n=TOP_N,
        benchmark=BENCHMARK,
        screener_tickers=curated,
        weighting="linear",
    )
    engine.MIN_BARS_REQUIRED = 252

    print(f"Running backtest {START} → {END} (top-{TOP_N}, linear weights)…")
    report = engine.run(start=START, end=END)
    returns = report.portfolio_returns
    ic = report.ic_series
    print(f"  {len(report.rebalance_results)} daily snapshots, {len(returns)} return observations")

    sharpe_gross = sharpe(returns.tolist())
    mean_ic = float(ic.mean()) if len(ic) else float("nan")
    ic_t = rank_ic_tstat(ic.tolist())
    ann_return = float(returns.mean() * 252 * 100)

    print("")
    print("=== HEADLINE ===")
    print(f"  sharpe_gross      = {sharpe_gross:+.3f}")
    print(f"  mean_ic           = {mean_ic:+.4f}  (t={ic_t:+.2f})")
    print(f"  ann_return_gross  = {ann_return:+.2f}%")
    print("")

    print("=== FACTOR ATTRIBUTION — Carhart (HAC) ===")
    carhart_factors = load_carhart_daily(start=START, end=END)
    attrib = run_carhart_attribution(returns, carhart_factors)
    print(format_attribution_table(attrib))
    print("")

    # Industry-adjusted robustness: add 12 FF industry excess returns as
    # independent regressors. Zen-identified concern: 113-ticker themed universe
    # has sector tilt that FF factors don't absorb. If Carhart alpha collapses
    # after industry controls → strategy is sector timing, not stock selection.
    print("=== INDUSTRY-ADJUSTED ROBUSTNESS (Carhart-4F + 12 FF industries, excess returns) ===")
    industries = load_industry12_daily(start=START, end=END)
    # Convert industries to excess returns over RF (match Mkt-RF convention).
    industries_excess = industries.sub(carhart_factors["RF"], axis=0)
    industries_excess.columns = [f"ind_{c}" for c in industries.columns]
    full_factors = carhart_factors.join(industries_excess, how="inner")

    carhart_ind = run_regression(
        returns,
        full_factors,
        factor_columns=[
            "Mkt-RF",
            "SMB",
            "HML",
            "Mom",
            *industries_excess.columns,
        ],
        spec_name="Carhart-4F + 12 Industries",
    )
    print(format_attribution_table([carhart_ind]))
    print("")
    print("  Non-trivial industry betas (|β| > 0.05):")
    ind_betas = {k: v for k, v in carhart_ind.betas.items() if k.startswith("ind_")}
    sorted_ind = sorted(ind_betas.items(), key=lambda kv: -abs(kv[1]))
    for name, beta in sorted_ind:
        if abs(beta) > 0.05:
            print(f"    {name:<14} β = {beta:+.3f}")
    print("")

    by_spec = {r.spec_name: r for r in attrib}
    carhart = by_spec["Carhart-4F"]
    ff3 = by_spec["FF3"]
    print("=== DECISION ===")
    print(f"  FF3 alpha t-stat              = {ff3.alpha_tstat:+.2f}")
    print(f"  Carhart-4F alpha t-stat       = {carhart.alpha_tstat:+.2f}  (with Mom factor)")
    print(f"  Carhart + 12 Ind alpha t-stat = {carhart_ind.alpha_tstat:+.2f}  (+ sector controls)")
    print(
        f"  FF3 → Carhart → +Industries   = {ff3.alpha_annualized * 100:+.2f}% → "
        f"{carhart.alpha_annualized * 100:+.2f}% → {carhart_ind.alpha_annualized * 100:+.2f}% ann"
    )
    print("")
    if carhart_ind.alpha_tstat > 2.0:
        verdict = "STRONG — alpha survives both Carhart AND industry controls. Edge is stock-specific, not sector timing or momentum factor."
    elif carhart_ind.alpha_tstat > 1.5:
        verdict = "WEAK-BUT-ALIVE — alpha survives weakly; sector tilts explain part of the edge."
    elif carhart_ind.alpha_tstat > 0.0:
        verdict = "DEAD-UNDER-SECTOR-CONTROLS — alpha evaporates once you control for industry exposures. Strategy is sector timing."
    else:
        verdict = "NEGATIVE — strategy underperforms after all controls."
    print(f"  Verdict: {verdict}")


if __name__ == "__main__":
    main()
