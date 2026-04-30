"""Layer 2c (Lean) re-validation under unified gate regime.

Real-world test of the kill_verdict_checklist.md framework: runs lean rule-based
scorer through 6 of 7 unified gates (sanity 4-gate is N/A — Layer 2c is a
ranking screener, not a rotation overlay).

Per ADR-0005 (closed-layers as anti-pattern catalog) the layer remains ARCHIVED
regardless of outcome — this script is research replay, not a re-opening
signal. The point is to pin a paper trail to every applicable gate so future
maintainers can see the verdict came from a uniform pipeline, not an ad-hoc
single observation.

Outputs:
    docs/backtest/layer2c_revalidation.md            — main verdict report
    docs/backtest/layer2c_revalidation_walkforward.csv — per-window pass/fail
    docs/backtest/layer2c_revalidation_bootstrap.csv  — bootstrap distribution
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from alphalens.archive.screeners.lean.config import (  # noqa: E402
    BENCHMARKS,
    DATA_DIR,
    LEAN_DEFAULTS,
)
from alphalens.archive.screeners.lean.lean_csv_loader import load_lean_histories  # noqa: E402
from alphalens.archive.screeners.lean.lean_project.scorer import rank_universe  # noqa: E402
from alphalens.archive.screeners.lean.universe import all_tickers  # noqa: E402
from alphalens.backtest.cost_model import cost_sensitivity_table  # noqa: E402
from alphalens.backtest.engine import BacktestEngine  # noqa: E402
from alphalens.backtest.factor_analysis import (  # noqa: E402
    bootstrap_carhart_alpha_ci,
    run_carhart_attribution,
)
from alphalens.backtest.factors import load_carhart_daily  # noqa: E402
from alphalens.backtest.history_store import HistoryStore  # noqa: E402
from alphalens.backtest.metrics import sharpe  # noqa: E402
from alphalens.backtest.multiple_testing import (  # noqa: E402
    apply_bonferroni,
    bonferroni_critical_tstat,
)
from alphalens.backtest.survivorship_pit import (  # noqa: E402
    compute_selection_bias,
    load_delisting_events,
    picks_from_report,
)
from alphalens.backtest.walk_forward import run_walk_forward  # noqa: E402

IS_START = date(2021, 4, 19)
IS_END = date(2024, 4, 19)
OOS_START = date(2024, 4, 22)
OOS_END = date(2026, 4, 17)
TOP_N = 5
HOLDING = 5
BENCHMARK = "SPY"

REPORT_PATH = REPO_ROOT / "docs/backtest/layer2c_revalidation.md"
WF_CSV_PATH = REPO_ROOT / "docs/backtest/layer2c_revalidation_walkforward.csv"
DELISTED_YAML = REPO_ROOT / "alphalens/screeners/lean/lean_project/delisted_universe.yaml"


def run_phase(
    label: str,
    store: HistoryStore,
    tickers: list[str],
    start: date,
    end: date,
    carhart: pd.DataFrame,
):
    print(f"  Running {label} {start} → {end}…")
    engine = BacktestEngine(
        store,
        scorer=rank_universe,
        scorer_config=dict(LEAN_DEFAULTS),
        holding_period=HOLDING,
        top_n=TOP_N,
        benchmark=BENCHMARK,
        screener_tickers=tickers,
        weighting="linear",
    )
    result = engine.run(start=start, end=end)
    if not result.rebalance_results:
        raise RuntimeError(f"{label}: backtest produced no daily snapshots")
    print(f"    {len(result.rebalance_results)} snapshots, {len(result.portfolio_returns)} returns")

    sharpe_g = sharpe(result.portfolio_returns.tolist())
    car_specs = run_carhart_attribution(result.portfolio_returns, carhart)
    car = next((s for s in car_specs if s.spec_name == "Carhart-4F"), None)
    return engine, result, sharpe_g, car


def fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%" if x is not None and not np.isnan(x) else "n/a"


def fmt_t(x: float | None) -> str:
    return f"{x:+.2f}" if x is not None and not np.isnan(x) else "n/a"


def main() -> None:
    print("Layer 2c (Lean) re-validation — unified gate regime")
    print(f"  IS:  {IS_START} → {IS_END}  (~3 trading years)")
    print(f"  OOS: {OOS_START} → {OOS_END}  (~2 trading years)")
    print("  (window matches Lean CSV cache extent — original archive verdict was 5y)")

    tickers = all_tickers()
    print(f"\nLean universe: {len(tickers)} tickers")

    print("Loading OHLCV…")
    histories = load_lean_histories(DATA_DIR, tickers + list(BENCHMARKS))
    store = HistoryStore(histories)
    print(f"  {len(store.tickers())} tickers loaded")

    print("Loading Carhart-4F factors…")
    carhart = load_carhart_daily(start=IS_START, end=OOS_END)

    print("\n=== IS + OOS backtest phases ===")
    _, is_result, is_sharpe, is_car = run_phase("IS", store, tickers, IS_START, IS_END, carhart)
    _, oos_result, oos_sharpe, oos_car = run_phase(
        "OOS", store, tickers, OOS_START, OOS_END, carhart
    )

    print("\n=== Gate 4: Bonferroni (n_tests=2: IS + OOS) ===")
    crit = bonferroni_critical_tstat(2)
    bonf = apply_bonferroni(
        {"H_IS": is_car.alpha_tstat, "H_OOS": oos_car.alpha_tstat},
        n_tests=2,
    )
    print(f"  critical |t| @ α=0.05, n=2: {crit:.3f}")
    for k, v in bonf.items():
        print(
            f"  {k}: t={(is_car if k == 'H_IS' else oos_car).alpha_tstat:+.2f} → {'PASS' if v else 'FAIL'}"
        )

    print("\n=== Gate 5: Cost drag (tiered AQR) ===")
    cost_is_df = cost_sensitivity_table(is_result.portfolio_returns.tolist())
    cost_oos_df = cost_sensitivity_table(oos_result.portfolio_returns.tolist())
    print("  IS  table:")
    print(cost_is_df.to_string(index=False))
    print("  OOS table:")
    print(cost_oos_df.to_string(index=False))

    print("\n=== Gate 3: Walk-forward C1-C5 (IS phase, 252d windows) ===")
    bench_close = store.full(BENCHMARK)["close"]
    wf = run_walk_forward(is_result, bench_close, carhart, window_days=252, step_days=21)
    print(f"  windows: {len(wf.window_results)}, verdict: {wf.verdict.overall}")
    for cn in ("c1", "c2", "c3", "c4", "c5"):
        passed = getattr(wf.verdict, f"{cn}_pass")
        reason = wf.verdict.reasons.get(cn.upper(), "")
        print(f"  {cn.upper()}: {passed}  — {reason}")

    print("\n=== Gate 6: Bootstrap CI on annualized Carhart α (10k iter, block n^(1/3)) ===")
    bs_lower, bs_upper = bootstrap_carhart_alpha_ci(
        oos_result.portfolio_returns, carhart, iterations=10_000
    )
    bs_pass = (bs_lower > 0.0) or (bs_upper < 0.0)
    print(
        f"  OOS Carhart α 95% CI (annualized): [{bs_lower * 100:+.2f}%, {bs_upper * 100:+.2f}%] → "
        f"{'PASS (excludes 0)' if bs_pass else 'FAIL (includes 0)'}"
    )

    print("\n=== Gate 7: Survivorship — delisting selection bias ===")
    print("  Loading delisting events…")
    events = load_delisting_events(yaml_path=DELISTED_YAML)
    print(f"    {len(events)} delisting events loaded")
    print("  C2 — delisting selection bias (Fisher exact)…")
    picks = picks_from_report(oos_result)
    bias_results = compute_selection_bias(picks, events, universe_tickers=tickers)
    for b in bias_results:
        print(
            f"    window={b.window_days:3d}d: "
            f"pick_rate={b.pick_delisting_rate:.4f}, "
            f"univ_rate={b.universe_delisting_rate:.4f}, "
            f"lift={b.lift_ratio:.2f}, "
            f"fisher p={b.fisher_p:.4f}"
        )

    print("\n=== Writing report ===")
    write_report(
        REPORT_PATH,
        is_sharpe=is_sharpe,
        is_car=is_car,
        oos_sharpe=oos_sharpe,
        oos_car=oos_car,
        bonf=bonf,
        crit=crit,
        cost_is_df=cost_is_df,
        cost_oos_df=cost_oos_df,
        wf=wf,
        bs_lower=bs_lower,
        bs_upper=bs_upper,
        bs_pass=bs_pass,
        bias_results=bias_results,
        n_tickers=len(tickers),
    )
    print(f"  → {REPORT_PATH.relative_to(REPO_ROOT)}")

    WF_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(w) for w in wf.window_results]).to_csv(WF_CSV_PATH, index=False)
    print(f"  → {WF_CSV_PATH.relative_to(REPO_ROOT)}")


def write_report(
    path: Path,
    *,
    is_sharpe: float,
    is_car,
    oos_sharpe: float,
    oos_car,
    bonf: dict[str, bool],
    crit: float,
    cost_is_df: pd.DataFrame,
    cost_oos_df: pd.DataFrame,
    wf,
    bs_lower: float,
    bs_upper: float,
    bs_pass: bool,
    bias_results: list,
    n_tickers: int,
) -> None:
    lines: list[str] = []
    lines.append("# Layer 2c (Lean) — re-validation under unified gate regime")
    lines.append("")
    lines.append("Generated by `scripts/layer2c_revalidation.py`. Per ADR-0005 the layer")
    lines.append("remains `ARCHIVED` regardless of this re-run; the purpose is to attach")
    lines.append("a uniform 6/7-gate paper trail to the existing kill verdict so future")
    lines.append("readers can compare it against Layer 2b/2d (rigorous) vs 2f/2g")
    lines.append("(minimal) on the same axis.")
    lines.append("")
    lines.append(f"- Universe: {n_tickers} tickers (Lean curated small/mid-cap)")
    lines.append(f"- IS:  {IS_START} → {IS_END}")
    lines.append(f"- OOS: {OOS_START} → {OOS_END}")
    lines.append(f"- Top-{TOP_N}, holding={HOLDING}d, weighting=linear, benchmark={BENCHMARK}")
    lines.append("")

    lines.append("## Headline")
    lines.append("")
    lines.append(f"- IS  Sharpe (gross) = {is_sharpe:+.3f}")
    lines.append(f"- OOS Sharpe (gross) = {oos_sharpe:+.3f}")
    lines.append(
        f"- IS  Carhart-4F α = {fmt_pct(is_car.alpha_annualized)} ann, "
        f"t = {fmt_t(is_car.alpha_tstat)} (HAC), R² = {is_car.r_squared:.3f}"
    )
    lines.append(
        f"- OOS Carhart-4F α = {fmt_pct(oos_car.alpha_annualized)} ann, "
        f"t = {fmt_t(oos_car.alpha_tstat)} (HAC), R² = {oos_car.r_squared:.3f}"
    )
    lines.append("")

    lines.append("## Gate 1: Carhart-4F + HAC")
    lines.append("")
    lines.append("Fitted via `alphalens.backtest.factor_analysis.run_carhart_attribution`")
    lines.append("with Newey-West HAC (lag = `int(4·(n/100)^(2/9))`).")
    lines.append("")
    lines.append("| Phase | α annualized | t-stat (HAC) | R² | Verdict (|t|>2.0) |")
    lines.append("|-------|--------------|--------------|-----|--------------------|")
    lines.append(
        f"| IS  | {fmt_pct(is_car.alpha_annualized)} | "
        f"{fmt_t(is_car.alpha_tstat)} | {is_car.r_squared:.3f} | "
        f"{'PASS' if abs(is_car.alpha_tstat) > 2.0 and is_car.alpha_tstat > 0 else 'FAIL'} |"
    )
    lines.append(
        f"| OOS | {fmt_pct(oos_car.alpha_annualized)} | "
        f"{fmt_t(oos_car.alpha_tstat)} | {oos_car.r_squared:.3f} | "
        f"{'PASS' if abs(oos_car.alpha_tstat) > 2.0 and oos_car.alpha_tstat > 0 else 'FAIL'} |"
    )
    lines.append("")

    lines.append("## Gate 2: 4-gate sanity (`alphalens.archive.rotation.sanity_checks`)")
    lines.append("")
    lines.append("**N/A** — Layer 2c is a rule-based ranking screener (lean technicals).")
    lines.append("It has no passive benchmark to overlay against, so")
    lines.append("`passive_correlation`, `rolling_sharpe_stability`,")
    lines.append("`per_regime_vs_passive`, and `overlay_alpha` are not applicable.")
    lines.append("Per `kill_verdict_checklist.md` 'where applicable' clause.")
    lines.append("")

    lines.append("## Gate 3: Walk-forward C1-C5 (252-day windows, 21-day stride)")
    lines.append("")
    lines.append(f"- Total windows: {len(wf.window_results)}")
    lines.append(f"- Baseline Sharpe (full range): {wf.baseline_sharpe:+.3f}")
    lines.append(f"- Baseline Carhart α t-stat: {fmt_t(wf.baseline_alpha_tstat)}")
    lines.append(f"- Overall verdict: **{wf.verdict.overall}**")
    lines.append("")
    lines.append("| Gate | Pass | Detail |")
    lines.append("|------|------|--------|")
    for cn in ("c1", "c2", "c3", "c4", "c5"):
        passed = getattr(wf.verdict, f"{cn}_pass")
        reason = wf.verdict.reasons.get(cn.upper(), "")
        lines.append(f"| {cn.upper()} | {passed} | {reason} |")
    lines.append("")

    lines.append("## Gate 4: Multiple-testing correction (Bonferroni, n=2)")
    lines.append("")
    lines.append("Pre-committed n_tests = 2 (IS Carhart α + OOS Carhart α as the")
    lines.append(f"two decision-critical hypotheses). Critical |t| at α=0.05/2: **{crit:.3f}**.")
    lines.append("")
    lines.append("| Hypothesis | t-stat | Survives Bonferroni |")
    lines.append("|------------|--------|---------------------|")
    lines.append(f"| H_IS  Carhart α | {fmt_t(is_car.alpha_tstat)} | {bonf['H_IS']} |")
    lines.append(f"| H_OOS Carhart α | {fmt_t(oos_car.alpha_tstat)} | {bonf['H_OOS']} |")
    lines.append("")

    lines.append("## Gate 5: Realistic cost drag (tiered AQR)")
    lines.append("")
    lines.append("`cost_sensitivity_table` across gross / 75 bps / 100 bps / 150 bps")
    lines.append("annual drag profiles (turnover-scaled).")
    lines.append("")
    lines.append("### IS phase")
    lines.append("")
    lines.append("```")
    lines.append(cost_is_df.to_string(index=False))
    lines.append("```")
    lines.append("")
    lines.append("### OOS phase")
    lines.append("")
    lines.append("```")
    lines.append(cost_oos_df.to_string(index=False))
    lines.append("```")
    lines.append("")

    lines.append("## Gate 6: Bootstrap CI on annualized Carhart-4F α")
    lines.append("")
    lines.append("Moving-block bootstrap (Hall-Horowitz 1995, block = n^(1/3)) on the")
    lines.append("OLS-fitted Carhart-4F α intercept, 10k iterations. Canonical Gate 6")
    lines.append("statistic per `docs/research/kill_verdict_checklist.md` —")
    lines.append("`alphalens.backtest.factor_analysis.bootstrap_carhart_alpha_ci`.")
    lines.append("")
    lines.append(f"- OOS 95% CI (annualized): **[{bs_lower * 100:+.2f}%, {bs_upper * 100:+.2f}%]**")
    lines.append(
        f"- Verdict: **{'PASS' if bs_pass else 'FAIL'}** "
        f"({'CI excludes 0' if bs_pass else 'CI includes 0 — α not significant'})"
    )
    lines.append("")

    lines.append("## Gate 7: Survivorship / PIT — delisting selection bias")
    lines.append("")
    lines.append("Reduced from full C1+C2+C3 protocol: this run reports the C2")
    lines.append("delisting-bias check only (Fisher exact on top-N picks vs universe).")
    lines.append("Cohort split (C1) and mid-holding wipeout audit (C3) skipped because")
    lines.append("Layer 2c is a rule-based screener and the additional 4 backtest runs")
    lines.append("yielded no new evidence vs the existing FF3 IS-only verdict per")
    lines.append("`docs/research/paradigm_failures_postmortem.md`.")
    lines.append("")
    lines.append("### Delisting selection bias (Fisher exact)")
    lines.append("")
    lines.append("| Window | n picks | pick rate | universe rate | lift | Fisher p |")
    lines.append("|--------|---------|-----------|---------------|------|----------|")
    for b in bias_results:
        lines.append(
            f"| {b.window_days}d | {b.n_picks} | "
            f"{b.pick_delisting_rate:.4f} | {b.universe_delisting_rate:.4f} | "
            f"{b.lift_ratio:.2f} | {b.fisher_p:.4f} |"
        )
    lines.append("")
    lines.append("Interpretation: Fisher p < 0.05 with lift > 1.0 would indicate the")
    lines.append("scorer systematically picks names about to delist (selection bias).")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    lines.append("Layer 2c was originally archived 2026-04-19 on IS-only FF3 α t=0.14")
    lines.append("(Sharpe 0.25 net). This re-run inserts results for the 6 applicable")
    lines.append("gates so the kill verdict carries uniform pipeline backing.")
    lines.append("")
    lines.append("Per ADR-0005 the layer remains `ARCHIVED` independently of this run's")
    lines.append("outcome — the project pivoted away from active alpha generation 2026-04-25.")
    lines.append("Companion file paths are linked from")
    lines.append("`alphalens/screeners/lean/__init__.py::__closed_evidence__`.")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
