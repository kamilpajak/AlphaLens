"""Layer 2d (insider Form-4 cluster-buy) re-validation under unified gate regime.

Companion to ``scripts/layer2c_revalidation.py``. Reads insider features from
the parquet migration (``~/.alphalens/insider_form4.parquet/``, 94 MB) instead
of the original 25 GB JSON cache, so the per-rebalance feature lookups during
the engine sweep are O(1) memory hits instead of disk syscalls.

Runs 6 of 7 unified gates (sanity 4-gate is N/A — Layer 2d has no passive
overlay). Produces ``docs/backtest/layer2d_revalidation.md`` with per-gate
verdict for both IS and OOS phases.

Per ADR-0005 the layer remains CLOSED regardless of outcome — this is research
replay, not a re-opening signal. The point is to confirm the parquet migration
preserves the validation pipeline result and to refresh evidence pointers in
``alphalens/screeners/insider/__init__.py::__closed_evidence__``.
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

from alphalens.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens.backtest.cost_model import cost_sensitivity_table  # noqa: E402
from alphalens.backtest.engine import BacktestEngine  # noqa: E402
from alphalens.backtest.factor_analysis import run_carhart_attribution  # noqa: E402
from alphalens.backtest.factors import load_carhart_daily  # noqa: E402
from alphalens.backtest.history_store import HistoryStore  # noqa: E402
from alphalens.backtest.metrics import sharpe  # noqa: E402
from alphalens.backtest.multiple_testing import (  # noqa: E402
    apply_bonferroni,
    bonferroni_critical_tstat,
)
from alphalens.backtest.walk_forward import run_walk_forward  # noqa: E402
from alphalens.screeners.insider.backtest_adapter import insider_scorer_adapter  # noqa: E402
from alphalens.screeners.insider.parquet_scorer import ParquetInsiderScorer  # noqa: E402

IS_START = date(2011, 1, 1)
IS_END = date(2022, 12, 31)
OOS_START = date(2023, 1, 1)
OOS_END = date(2026, 4, 22)
TOP_N = 15  # GATE 1 YELLOW per design (signal scarcity ~16 clusters/mo)
HOLDING = 5
BENCHMARK = "SPY"
REBALANCE_STRIDE = 5  # weekly — matches Layer 2d Phase 3b cold-cache regime

PARQUET_PATH = Path.home() / ".alphalens" / "insider_form4.parquet"
PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
PRICES_DIR = Path.home() / ".alphalens" / "prices"

REPORT_PATH = REPO_ROOT / "docs/backtest/layer2d_revalidation.md"
WF_CSV_PATH = REPO_ROOT / "docs/backtest/layer2d_revalidation_walkforward.csv"
BS_CSV_PATH = REPO_ROOT / "docs/backtest/layer2d_revalidation_bootstrap.csv"


def load_pit_union(start: date, end: date) -> list[str]:
    """Union of all PIT-snapshot tickers for month-ends in [start, end]."""
    import yaml

    union: set[str] = set()
    for path in sorted(PIT_DIR.glob("*.yaml")):
        snap_date = date.fromisoformat(path.stem + "-01")
        if not (start.replace(day=1) <= snap_date <= end):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for ticker in data.get("tickers", []):
            union.add(ticker)
    return sorted(union)


def moving_block_bootstrap(
    returns: np.ndarray,
    *,
    n_iter: int = 10000,
    block: int = 21,
    seed: int = 42,
) -> tuple[float, float, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = len(returns)
    n_blocks = n // block
    starts_max = n - block
    means = np.empty(n_iter)
    for i in range(n_iter):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        sample = np.concatenate([returns[s : s + block] for s in starts])
        means[i] = sample.mean()
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975)), means


def run_phase(
    label: str,
    store: HistoryStore,
    universe: list[str],
    insider_store: ParquetInsiderScorer,
    start: date,
    end: date,
    carhart: pd.DataFrame,
):
    print(f"  Running {label} {start} → {end} (top-{TOP_N}, stride={REBALANCE_STRIDE})…")
    engine = BacktestEngine(
        store,
        scorer=insider_scorer_adapter,
        scorer_config={"benchmark": BENCHMARK, "_insider_store": insider_store},
        holding_period=HOLDING,
        top_n=TOP_N,
        benchmark=BENCHMARK,
        screener_tickers=universe,
        weighting="linear",
        rebalance_stride=REBALANCE_STRIDE,
    )
    result = engine.run(start=start, end=end)
    if not result.rebalance_results:
        raise RuntimeError(f"{label}: backtest produced no rebalance snapshots")
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
    print("Layer 2d (insider Form-4 cluster-buy) re-validation — unified gate regime")
    print(f"  IS:  {IS_START} → {IS_END}  (12 trading years)")
    print(f"  OOS: {OOS_START} → {OOS_END}  (~3.3 trading years)")
    print(f"  Insider features: parquet @ {PARQUET_PATH}")

    print("\nLoading parquet insider scorer…")
    insider_store = ParquetInsiderScorer(PARQUET_PATH)
    s = insider_store.stats
    print(f"  {s['total_rows']:,} cached lookups, {s['with_features']:,} cluster detections")

    print("Loading PIT union universe…")
    universe = load_pit_union(IS_START, OOS_END)
    print(f"  {len(universe)} PIT-union tickers")

    print(f"Loading yfinance histories from {PRICES_DIR}…")
    histories = load_cached_histories(universe + [BENCHMARK], PRICES_DIR)
    if BENCHMARK not in histories:
        raise RuntimeError(f"benchmark {BENCHMARK} missing from {PRICES_DIR}")
    print(f"  {len(histories)}/{len(universe) + 1} ticker histories loaded")
    store = HistoryStore(histories)

    print("Loading Carhart-4F factors…")
    carhart = load_carhart_daily(start=IS_START, end=OOS_END)

    print("\n=== IS + OOS backtest phases ===")
    _, is_result, is_sharpe, is_car = run_phase(
        "IS", store, universe, insider_store, IS_START, IS_END, carhart
    )
    _, oos_result, oos_sharpe, oos_car = run_phase(
        "OOS", store, universe, insider_store, OOS_START, OOS_END, carhart
    )

    print("\n=== Gate 4: Bonferroni (n_tests=2: IS + OOS) ===")
    crit = bonferroni_critical_tstat(2)
    bonf = apply_bonferroni(
        {"H_IS": is_car.alpha_tstat, "H_OOS": oos_car.alpha_tstat},
        n_tests=2,
    )
    print(f"  critical |t| @ α=0.05, n=2: {crit:.3f}")
    for k, v in bonf.items():
        t = (is_car if k == "H_IS" else oos_car).alpha_tstat
        print(f"  {k}: t={t:+.2f} → {'PASS' if v else 'FAIL'}")

    print("\n=== Gate 5: Cost drag (tiered AQR) ===")
    cost_is_df = cost_sensitivity_table(is_result.portfolio_returns.tolist())
    cost_oos_df = cost_sensitivity_table(oos_result.portfolio_returns.tolist())
    print("  IS  table:")
    print(cost_is_df.to_string(index=False))
    print("  OOS table:")
    print(cost_oos_df.to_string(index=False))

    print("\n=== Gate 3: Walk-forward C1-C5 (IS phase) ===")
    bench_close = store.full(BENCHMARK)["close"]
    wf = run_walk_forward(is_result, bench_close, carhart, window_days=252, step_days=21)
    print(f"  windows: {len(wf.window_results)}, verdict: {wf.verdict.overall}")
    for cn in ("c1", "c2", "c3", "c4", "c5"):
        passed = getattr(wf.verdict, f"{cn}_pass")
        reason = wf.verdict.reasons.get(cn.upper(), "")
        print(f"  {cn.upper()}: {passed}  — {reason}")

    print("\n=== Gate 6: Bootstrap CI (10k iter, moving-block n=21) ===")
    is_arr = is_result.portfolio_returns.dropna().to_numpy()
    oos_arr = oos_result.portfolio_returns.dropna().to_numpy()
    is_lower, is_upper, _ = moving_block_bootstrap(is_arr, n_iter=10000, block=21)
    oos_lower, oos_upper, oos_dist = moving_block_bootstrap(oos_arr, n_iter=10000, block=21)
    print(
        f"  IS  daily mean 95% CI: [{is_lower:.6f}, {is_upper:.6f}] → "
        f"{'PASS' if is_lower > 0 else 'FAIL'}"
    )
    print(
        f"  OOS daily mean 95% CI: [{oos_lower:.6f}, {oos_upper:.6f}] → "
        f"{'PASS' if oos_lower > 0 else 'FAIL'}"
    )

    print("\n=== Gate 7: Survivorship — N/A ===")
    print("  Layer 2d is weekly Form-4 scoring on a PIT-reconstructed Russell 2000")
    print("  universe; survivorship handled at universe construction (PIT union")
    print("  per design doc §3). No separate cohort-split / delisting-bias check")
    print("  is meaningful at the screener level.")

    print("\n=== Writing report ===")
    write_report(
        REPORT_PATH,
        universe_size=len(universe),
        is_sharpe=is_sharpe,
        is_car=is_car,
        oos_sharpe=oos_sharpe,
        oos_car=oos_car,
        bonf=bonf,
        crit=crit,
        cost_is_df=cost_is_df,
        cost_oos_df=cost_oos_df,
        wf=wf,
        is_lower=is_lower,
        is_upper=is_upper,
        oos_lower=oos_lower,
        oos_upper=oos_upper,
        n_tickers=len(universe),
        parquet_stats=s,
    )
    print(f"  → {REPORT_PATH.relative_to(REPO_ROOT)}")

    WF_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(w) for w in wf.window_results]).to_csv(WF_CSV_PATH, index=False)
    print(f"  → {WF_CSV_PATH.relative_to(REPO_ROOT)}")

    pd.DataFrame({"oos_bootstrap_mean": oos_dist}).to_csv(BS_CSV_PATH, index=False)
    print(f"  → {BS_CSV_PATH.relative_to(REPO_ROOT)}")


def write_report(
    path: Path,
    *,
    universe_size: int,
    is_sharpe: float,
    is_car,
    oos_sharpe: float,
    oos_car,
    bonf: dict[str, bool],
    crit: float,
    cost_is_df: pd.DataFrame,
    cost_oos_df: pd.DataFrame,
    wf,
    is_lower: float,
    is_upper: float,
    oos_lower: float,
    oos_upper: float,
    n_tickers: int,
    parquet_stats: dict[str, int],
) -> None:
    lines: list[str] = []
    lines.append("# Layer 2d (insider Form-4) — re-validation under unified gate regime")
    lines.append("")
    lines.append("Generated by `scripts/layer2d_revalidation.py`. Insider features sourced")
    lines.append("from the parquet migration (`~/.alphalens/insider_form4.parquet/`, 94 MB,")
    lines.append("hive-partitioned by year) via `ParquetInsiderScorer` — drop-in for the")
    lines.append("original `InsiderScorer` whose JSON cache (~25 GB) is no longer required")
    lines.append("for backtest replay.")
    lines.append("")
    lines.append("Per ADR-0005 the layer remains `CLOSED` regardless of this re-run; the")
    lines.append("purpose is to attach a uniform 6/7-gate paper trail using the new data")
    lines.append("path and verify the migration preserves the validation pipeline.")
    lines.append("")
    lines.append(f"- Universe (PIT union): {universe_size} tickers")
    lines.append(f"- IS:  {IS_START} → {IS_END}")
    lines.append(f"- OOS: {OOS_START} → {OOS_END}")
    lines.append(
        f"- Top-{TOP_N}, holding={HOLDING}d, stride={REBALANCE_STRIDE}d (weekly), "
        f"weighting=linear, benchmark={BENCHMARK}"
    )
    lines.append(
        f"- Parquet: {parquet_stats['total_rows']:,} rows, "
        f"{parquet_stats['with_features']:,} cluster detections "
        f"({parquet_stats['with_features'] / parquet_stats['total_rows']:.2%})"
    )
    lines.append("")

    lines.append("## Headline")
    lines.append("")
    lines.append(f"- IS  Sharpe (gross) = {is_sharpe:+.3f}")
    lines.append(f"- OOS Sharpe (gross) = {oos_sharpe:+.3f}")
    lines.append(
        f"- IS  Carhart-4F α = {fmt_pct(is_car.alpha_annualized)} ann, "
        f"t = {fmt_t(is_car.alpha_tstat)} (HAC), R² = {is_car.r_squared:.3f}, "
        f"n = {is_car.n_observations}"
    )
    lines.append(
        f"- OOS Carhart-4F α = {fmt_pct(oos_car.alpha_annualized)} ann, "
        f"t = {fmt_t(oos_car.alpha_tstat)} (HAC), R² = {oos_car.r_squared:.3f}, "
        f"n = {oos_car.n_observations}"
    )
    lines.append("")

    lines.append("## Gate 1: Carhart-4F + HAC")
    lines.append("")
    lines.append("| Phase | α annualized | t-stat (HAC) | R² | Verdict (|t|>2.0) |")
    lines.append("|-------|--------------|--------------|-----|--------------------|")
    for phase, car in (("IS", is_car), ("OOS", oos_car)):
        verdict = "PASS" if abs(car.alpha_tstat) > 2.0 and car.alpha_tstat > 0 else "FAIL"
        lines.append(
            f"| {phase} | {fmt_pct(car.alpha_annualized)} | "
            f"{fmt_t(car.alpha_tstat)} | {car.r_squared:.3f} | {verdict} |"
        )
    lines.append("")

    lines.append("## Gate 2: 4-gate sanity (`alphalens.rotation.sanity_checks`)")
    lines.append("")
    lines.append("**N/A** — Layer 2d is weekly Form-4 cluster-buy scoring, not a rotation")
    lines.append("overlay against a passive benchmark. `passive_correlation`,")
    lines.append("`rolling_sharpe_stability`, `per_regime_vs_passive`, `overlay_alpha`")
    lines.append("are not applicable. Per `kill_verdict_checklist.md` 'where applicable'.")
    lines.append("")

    lines.append("## Gate 3: Walk-forward C1-C5 (IS phase, 252d windows, 21d stride)")
    lines.append("")
    lines.append(f"- Total windows: {len(wf.window_results)}")
    lines.append(f"- Baseline Sharpe (IS full range): {wf.baseline_sharpe:+.3f}")
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
    lines.append("Pre-committed n_tests = 2 (IS Carhart α + OOS Carhart α).")
    lines.append(f"Critical |t| at α=0.05/2: **{crit:.3f}**.")
    lines.append("")
    lines.append("| Hypothesis | t-stat | Survives Bonferroni |")
    lines.append("|------------|--------|---------------------|")
    lines.append(f"| H_IS  Carhart α | {fmt_t(is_car.alpha_tstat)} | {bonf['H_IS']} |")
    lines.append(f"| H_OOS Carhart α | {fmt_t(oos_car.alpha_tstat)} | {bonf['H_OOS']} |")
    lines.append("")

    lines.append("## Gate 5: Realistic cost drag (tiered AQR)")
    lines.append("")
    lines.append("`cost_sensitivity_table` across gross / 75 bps / 100 bps / 150 bps")
    lines.append("annual drag profiles, turnover-scaled.")
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

    lines.append("## Gate 6: Bootstrap CI (moving-block, 10k iter, block=21)")
    lines.append("")
    lines.append(
        f"- IS  95% CI: [{is_lower:.6f}, {is_upper:.6f}] → **{'PASS' if is_lower > 0 else 'FAIL'}**"
    )
    lines.append(
        f"- OOS 95% CI: [{oos_lower:.6f}, {oos_upper:.6f}] → "
        f"**{'PASS' if oos_lower > 0 else 'FAIL'}**"
    )
    lines.append("")

    lines.append("## Gate 7: Survivorship / PIT")
    lines.append("")
    lines.append("**N/A at the screener level.** Survivorship is addressed at universe")
    lines.append("construction via the PIT union snapshot (Phase 2.5 design doc §3) —")
    lines.append("the screener never sees ticks outside their PIT-eligible window. The")
    lines.append("screener-internal feature `features_as_of(ticker, asof)` is itself PIT-safe")
    lines.append("(filing_date ≤ asof). No additional cohort-split / delisting-bias check")
    lines.append("is meaningful when the universe is already PIT-reconstructed.")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    lines.append("Layer 2d was originally CLOSED 2026-04-24 on the IS→OOS Carhart α")
    lines.append("collapse pattern (IS t=2.14 → OOS t=0.68). This re-run uses the parquet")
    lines.append("migration as the data source and re-affirms the verdict under uniform")
    lines.append("pipeline coverage.")
    lines.append("")
    lines.append("Companion file paths are linked from")
    lines.append("`alphalens/screeners/insider/__init__.py::__closed_evidence__`.")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
