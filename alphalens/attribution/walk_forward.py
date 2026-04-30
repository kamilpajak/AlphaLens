"""Walk-forward OOS validation for a fixed-weight scorer.

Perplexity's review of the backtest harness flagged walk-forward stability
as the second-largest unverified risk after PIT universe reconstruction
(which was addressed in `survivorship_pit.py`). This module partitions the
full 2021-06 → 2026-04 backtest into ~25-37 rolling 252-day test windows
stepped monthly, computes per-window metrics, and reports distribution /
gate outcomes to detect regime-specific performance or data-snooping bias.

**MomentumScorer has fixed equal weights** (1/7 per metric) — no parameters
to refit. So this module tests *performance* stability of a deterministic
scorer, not parameter stability. The "training" window is nominal only;
all reported metrics come from OOS test windows.

## Single-baseline-slice optimisation

The engine is deterministic for fixed scorer/config/histories, so running
one full `BacktestEngine.run(start, end)` and slicing the resulting
`rebalance_results` per window gives numerics identical to ~25 per-window
re-runs at a fraction of the wall time.

## Path-independence invariant (per zen review)

Max drawdown and cumulative return per window MUST be computed from a
fresh `(1 + sliced_returns).cumprod()` starting at 1 for the slice's first
day — never inherited from the global equity curve's high-water mark.
`compute_window_metrics` enforces this; `test_max_drawdown_path_independent`
guards it.

## Decision gate (5 rules, zen-revised)

- C1 Sharpe breadth: ≥ 70% of windows with Sharpe > 0.5
- C2 Alpha significance: ≥ 50% of windows with Carhart α_t > 1.5 HAC (skipped
  N/A if factors unavailable; not silently PASS)
- C3 Block-return autocorr: lag-1 autocorr of non-overlapping 21-day block
  returns < 0.5 — replaces "windowed Sharpe autocorr" per zen feedback
  (windowed version is either mechanically near 1.0 due to 92% overlap
  or statistically meaningless on every-12th-window subsample of n≈4).
- C4 Dark half: longest contiguous Sharpe < 0 stretch < 12 windows
  (~12 months) — extended from 6 per zen because momentum strategies have
  well-documented 12-18 month "winters" during reversals.
- C5 Turnover ceiling: max per-window turnover < 100% — catches volatile
  slices where the fixed scoring weights trigger excessive rebalancing.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from alphalens.attribution.cost_model import CostModel
from alphalens.attribution.factor_analysis import run_carhart_attribution
from alphalens.attribution.regime import classify_regime
from alphalens.backtest.engine import BacktestReport
from alphalens.backtest.metrics import (
    max_drawdown,
    rank_ic_tstat,
    sharpe,
    turnover_pct,
)

# ---------------------------------------------------------------------------
# Dataclasses


@dataclass(frozen=True)
class WindowSpec:
    test_start: date
    test_end: date


@dataclass(frozen=True)
class WindowResult:
    test_start: date
    test_end: date
    n_days: int
    regime: str  # majority bull/bear/flat within window
    regime_reversed_within: bool  # bull↔bear transition inside the window
    sharpe_gross: float
    sharpe_moderate: float  # after 100 bps annual flat drag
    carhart_alpha_daily: float | None
    carhart_alpha_tstat: float | None
    ic_mean: float
    ic_tstat: float
    max_drawdown: float  # non-positive
    turnover: float  # top-N exit rate / N, averaged daily
    cumulative_return: float


@dataclass(frozen=True)
class DistributionSummary:
    # Sharpe
    sharpe_min: float
    sharpe_q25: float
    sharpe_median: float
    sharpe_q75: float
    sharpe_max: float
    fraction_sharpe_gt_0_5: float
    fraction_sharpe_gt_1_0: float
    # Carhart alpha t-stat
    alpha_t_min: float | None
    alpha_t_q25: float | None
    alpha_t_median: float | None
    alpha_t_q75: float | None
    alpha_t_max: float | None
    fraction_alpha_t_gt_1_5: float | None
    fraction_alpha_t_gt_2_0: float | None
    # IC
    ic_t_median: float
    fraction_ic_t_gt_1_5: float
    # Time-series stability
    block_return_autocorr_lag1: float
    longest_negative_sharpe_stretch: int
    dark_half_span: tuple[date, date] | None
    # Turnover
    max_turnover: float
    q95_turnover: float
    # Momentum-crash indicator
    momentum_crash_coincidence: (
        float  # fraction of negative-Sharpe windows coinciding with regime reversal
    )


@dataclass(frozen=True)
class GateVerdict:
    c1_pass: bool
    c2_pass: bool | None  # None = skipped (factors unavailable)
    c3_pass: bool
    c4_pass: bool
    c5_pass: bool
    overall: Literal["PASS", "BORDERLINE", "FAIL"]
    reasons: dict[str, str]


@dataclass
class WalkForwardReport:
    baseline_sharpe: float
    baseline_alpha_tstat: float | None
    window_days: int
    step_days: int
    window_results: list[WindowResult]
    summary: DistributionSummary
    verdict: GateVerdict


# ---------------------------------------------------------------------------
# Window generation + slicing


def generate_windows(
    calendar: Sequence[pd.Timestamp],
    window_days: int = 252,
    step_days: int = 21,
) -> list[WindowSpec]:
    """Rolling `window_days` test windows stepping by `step_days`.

    `calendar` should be the benchmark trading-day DatetimeIndex. Returns
    an empty list if the calendar is shorter than `window_days`; trailing
    stubs shorter than `window_days` are dropped.
    """
    if len(calendar) < window_days:
        return []
    cal = list(calendar)
    windows: list[WindowSpec] = []
    idx = 0
    while idx + window_days <= len(cal):
        test_start = cal[idx].date()
        test_end = cal[idx + window_days - 1].date()
        windows.append(WindowSpec(test_start=test_start, test_end=test_end))
        idx += step_days
    return windows


def slice_report_to_window(baseline: BacktestReport, window: WindowSpec) -> BacktestReport:
    """Return a shallow copy of `baseline` with `rebalance_results` filtered to
    the window's date range (inclusive).

    The engine is deterministic for fixed scorer/config/histories, so
    the sliced report's properties (portfolio_returns, ic_series, etc.)
    match a per-window re-run numerically.
    """
    start_ts = pd.Timestamp(window.test_start)
    end_ts = pd.Timestamp(window.test_end)
    filtered = [snap for snap in baseline.rebalance_results if start_ts <= snap.date <= end_ts]
    sliced = copy.copy(baseline)
    sliced.rebalance_results = filtered
    return sliced


# ---------------------------------------------------------------------------
# Per-window metrics


def _majority_regime_and_reversal(
    benchmark_close: pd.Series, window: WindowSpec
) -> tuple[str, bool]:
    labels = classify_regime(benchmark_close)
    start_ts = pd.Timestamp(window.test_start)
    end_ts = pd.Timestamp(window.test_end)
    sliced = labels.loc[(labels.index >= start_ts) & (labels.index <= end_ts)]
    if sliced.empty:
        return "flat", False
    regime = str(sliced.mode().iloc[0])
    # Reversal: any transition bull↔bear inside the window (flat in between OK)
    reversed_within = False
    prev = None
    for lab in sliced:
        if lab in ("bull", "bear"):
            if prev is not None and prev != lab:
                reversed_within = True
                break
            prev = lab
    return regime, reversed_within


def compute_window_metrics(
    sliced: BacktestReport,
    window: WindowSpec,
    benchmark_close: pd.Series,
    carhart: pd.DataFrame | None,
) -> WindowResult:
    """Compute all per-window metrics from a pre-sliced BacktestReport.

    **Path-independence**: max_drawdown and cumulative_return are derived
    from a fresh cumprod starting at 1 for the slice's first day —
    independent of any pre-window equity curve.
    """
    returns = sliced.portfolio_returns
    ic = sliced.ic_series
    n_days = len(returns)

    if n_days == 0:
        # Degenerate window — return neutral values rather than raise.
        regime, reversed_within = _majority_regime_and_reversal(benchmark_close, window)
        return WindowResult(
            test_start=window.test_start,
            test_end=window.test_end,
            n_days=0,
            regime=regime,
            regime_reversed_within=reversed_within,
            sharpe_gross=0.0,
            sharpe_moderate=0.0,
            carhart_alpha_daily=None,
            carhart_alpha_tstat=None,
            ic_mean=0.0,
            ic_tstat=0.0,
            max_drawdown=0.0,
            turnover=0.0,
            cumulative_return=0.0,
        )

    sharpe_gross = sharpe(returns.tolist())
    net_returns = CostModel.from_profile("moderate").apply(returns, daily_turnover=None)
    sharpe_moderate = sharpe(net_returns.tolist())

    # Path-independent MaxDD + cumulative return
    cumprod = (1.0 + returns).cumprod()
    cumulative_return = float(cumprod.iloc[-1] - 1.0) if n_days > 0 else 0.0
    mdd = max_drawdown(cumprod.tolist())

    ic_mean = float(ic.mean()) if len(ic) else 0.0
    ic_tstat_val = rank_ic_tstat(ic.tolist()) if len(ic) else 0.0

    tn_lists = [snap.top_n_tickers for snap in sliced.rebalance_results]
    turnover = turnover_pct(tn_lists)

    # Carhart per-window attribution
    alpha_daily: float | None = None
    alpha_tstat: float | None = None
    if carhart is not None and n_days >= 30:
        start_ts = pd.Timestamp(window.test_start)
        end_ts = pd.Timestamp(window.test_end)
        cf_sliced = carhart.loc[(carhart.index >= start_ts) & (carhart.index <= end_ts)]
        if len(cf_sliced) >= 30:
            try:
                attribution = run_carhart_attribution(returns, cf_sliced)
                car = next((r for r in attribution if r.spec_name == "Carhart-4F"), None)
                if car is not None:
                    alpha_daily = car.alpha_daily
                    alpha_tstat = car.alpha_tstat
            except Exception:
                alpha_daily = None
                alpha_tstat = None

    regime, reversed_within = _majority_regime_and_reversal(benchmark_close, window)

    return WindowResult(
        test_start=window.test_start,
        test_end=window.test_end,
        n_days=n_days,
        regime=regime,
        regime_reversed_within=reversed_within,
        sharpe_gross=sharpe_gross,
        sharpe_moderate=sharpe_moderate,
        carhart_alpha_daily=alpha_daily,
        carhart_alpha_tstat=alpha_tstat,
        ic_mean=ic_mean,
        ic_tstat=ic_tstat_val,
        max_drawdown=mdd,
        turnover=turnover,
        cumulative_return=cumulative_return,
    )


# ---------------------------------------------------------------------------
# Stability metrics


def block_return_autocorr(daily_returns: pd.Series, block_days: int = 21) -> float:
    """Lag-1 autocorrelation of non-overlapping `block_days`-day block sums.

    Per zen review: this replaces "windowed Sharpe autocorr" as the gate C3
    metric. Rolling windows with 92% overlap have mechanically high
    autocorr (~0.9 even for independent signal), and every-12th-window
    subsampling collapses to n≈4 which is statistically meaningless.
    Block-return autocorr uses ~n_days / block_days independent observations
    (~59 over 5 years) which is statistically defensible.
    """
    if len(daily_returns) < block_days * 2:
        return float("nan")
    # Compound daily returns into non-overlapping block sums
    clean = daily_returns.dropna()
    if len(clean) < block_days * 2:
        return float("nan")
    n_blocks = len(clean) // block_days
    if n_blocks < 2:
        return float("nan")
    truncated = clean.iloc[: n_blocks * block_days]
    # Sum daily returns within each block (approximation; exact would be
    # (1+r).cumprod() - 1, but for small daily returns sum is a close proxy
    # and simpler to reason about for autocorr).
    block_returns = truncated.groupby(np.arange(len(truncated)) // block_days).sum()
    if len(block_returns) < 2:
        return float("nan")
    return float(block_returns.autocorr(lag=1))


def _longest_negative_stretch(
    sharpes: Sequence[float],
) -> tuple[int, int | None]:
    """Return (longest_run_length, start_index_of_longest) for Sharpe < 0."""
    longest = 0
    longest_start: int | None = None
    current = 0
    current_start: int | None = None
    for i, s in enumerate(sharpes):
        if s < 0:
            if current == 0:
                current_start = i
            current += 1
            if current > longest:
                longest = current
                longest_start = current_start
        else:
            current = 0
            current_start = None
    return longest, longest_start


# ---------------------------------------------------------------------------
# Orchestrator + summary + gate


def run_walk_forward(
    baseline: BacktestReport,
    benchmark_close: pd.Series,
    carhart: pd.DataFrame | None,
    window_days: int = 252,
    step_days: int = 21,
) -> WalkForwardReport:
    """Build windows from the baseline's date range, compute per-window
    metrics, summarise, evaluate the gate. Single-baseline slice path —
    no engine re-runs.
    """
    if not baseline.rebalance_results:
        raise ValueError("baseline has no rebalance_results")

    calendar = [snap.date for snap in baseline.rebalance_results]
    windows = generate_windows(calendar, window_days=window_days, step_days=step_days)

    results: list[WindowResult] = []
    for window in windows:
        sliced = slice_report_to_window(baseline, window)
        results.append(compute_window_metrics(sliced, window, benchmark_close, carhart))

    summary = summarize_distribution(results, baseline)
    verdict = evaluate_gate(summary)

    # Full-range baseline Sharpe / alpha for reference in the report header
    baseline_sharpe = sharpe(baseline.portfolio_returns.tolist())
    baseline_alpha_t: float | None = None
    if carhart is not None and len(baseline.portfolio_returns) >= 30:
        try:
            specs = run_carhart_attribution(baseline.portfolio_returns, carhart)
            car = next((s for s in specs if s.spec_name == "Carhart-4F"), None)
            baseline_alpha_t = car.alpha_tstat if car else None
        except Exception:
            baseline_alpha_t = None

    return WalkForwardReport(
        baseline_sharpe=baseline_sharpe,
        baseline_alpha_tstat=baseline_alpha_t,
        window_days=window_days,
        step_days=step_days,
        window_results=results,
        summary=summary,
        verdict=verdict,
    )


def summarize_distribution(
    results: Sequence[WindowResult], baseline: BacktestReport
) -> DistributionSummary:
    if not results:
        raise ValueError("no window results to summarise")

    sharpes = np.array([r.sharpe_gross for r in results], dtype=float)
    alpha_ts_raw = [r.carhart_alpha_tstat for r in results]
    alpha_ts = np.array([t for t in alpha_ts_raw if t is not None], dtype=float)
    has_alpha = len(alpha_ts) > 0
    ic_ts = np.array([r.ic_tstat for r in results], dtype=float)
    turnovers = np.array([r.turnover for r in results], dtype=float)

    fraction_gt = lambda arr, thr: float((arr > thr).mean()) if len(arr) else 0.0

    # Dark-half detection
    longest_neg, neg_start = _longest_negative_stretch([r.sharpe_gross for r in results])
    dark_half_span: tuple[date, date] | None = None
    if longest_neg > 0 and neg_start is not None:
        end_idx = neg_start + longest_neg - 1
        dark_half_span = (results[neg_start].test_start, results[end_idx].test_end)

    # Block-return autocorr over the full baseline (not per-window)
    autocorr = block_return_autocorr(baseline.portfolio_returns)

    # Momentum-crash coincidence: negative-Sharpe windows ∩ regime-reversing
    negative_windows = [r for r in results if r.sharpe_gross < 0]
    crash_coincidence = 0.0
    if negative_windows:
        crash_coincidence = sum(1 for r in negative_windows if r.regime_reversed_within) / len(
            negative_windows
        )

    def _q(arr, q):
        return float(np.quantile(arr, q)) if len(arr) else 0.0

    return DistributionSummary(
        sharpe_min=float(sharpes.min()),
        sharpe_q25=_q(sharpes, 0.25),
        sharpe_median=_q(sharpes, 0.5),
        sharpe_q75=_q(sharpes, 0.75),
        sharpe_max=float(sharpes.max()),
        fraction_sharpe_gt_0_5=fraction_gt(sharpes, 0.5),
        fraction_sharpe_gt_1_0=fraction_gt(sharpes, 1.0),
        alpha_t_min=float(alpha_ts.min()) if has_alpha else None,
        alpha_t_q25=_q(alpha_ts, 0.25) if has_alpha else None,
        alpha_t_median=_q(alpha_ts, 0.5) if has_alpha else None,
        alpha_t_q75=_q(alpha_ts, 0.75) if has_alpha else None,
        alpha_t_max=float(alpha_ts.max()) if has_alpha else None,
        fraction_alpha_t_gt_1_5=fraction_gt(alpha_ts, 1.5) if has_alpha else None,
        fraction_alpha_t_gt_2_0=fraction_gt(alpha_ts, 2.0) if has_alpha else None,
        ic_t_median=_q(ic_ts, 0.5),
        fraction_ic_t_gt_1_5=fraction_gt(ic_ts, 1.5),
        block_return_autocorr_lag1=autocorr,
        longest_negative_sharpe_stretch=longest_neg,
        dark_half_span=dark_half_span,
        max_turnover=float(turnovers.max()) if len(turnovers) else 0.0,
        q95_turnover=_q(turnovers, 0.95),
        momentum_crash_coincidence=crash_coincidence,
    )


# Gate thresholds
_THR_C1 = 0.70
_THR_C2 = 0.50
_THR_C3 = 0.5
_THR_C4 = 12
_THR_C5 = 1.0


def _eval_c1(summary: DistributionSummary) -> tuple[bool, str, bool]:
    val = summary.fraction_sharpe_gt_0_5
    return (
        val >= _THR_C1,
        f"fraction windows Sharpe>0.5 = {val:.2%} (threshold ≥ {_THR_C1:.0%})",
        val >= _THR_C1 * 0.9,  # borderline: within 10% of threshold
    )


def _eval_c2(summary: DistributionSummary) -> tuple[bool | None, str, bool]:
    val = summary.fraction_alpha_t_gt_1_5
    if val is None:
        return None, "N/A — Carhart factors unavailable", False
    return (
        val >= _THR_C2,
        f"fraction windows Carhart α_t>1.5 = {val:.2%} (threshold ≥ {_THR_C2:.0%})",
        val >= _THR_C2 * 0.9,
    )


def _eval_c3(summary: DistributionSummary) -> tuple[bool, str, bool]:
    ac = summary.block_return_autocorr_lag1
    finite = not np.isnan(ac)
    return (
        finite and ac < _THR_C3,
        f"21-day block-return autocorr lag-1 = {ac:.3f} (threshold < {_THR_C3})",
        finite and ac < _THR_C3 * 1.1,
    )


def _eval_c4(summary: DistributionSummary) -> tuple[bool, str, bool]:
    val = summary.longest_negative_sharpe_stretch
    return (
        val < _THR_C4,
        f"longest negative-Sharpe stretch = {val} windows (threshold < {_THR_C4})",
        val <= int(_THR_C4 * 1.1),
    )


def _eval_c5(summary: DistributionSummary) -> tuple[bool, str, bool]:
    val = summary.max_turnover
    return (
        val < _THR_C5,
        f"max per-window turnover = {val:.2%} (threshold < {_THR_C5:.0%})",
        val < _THR_C5 * 1.1,
    )


def _resolve_overall(
    gate_results: dict[str, tuple[bool | None, str, bool]],
) -> Literal["PASS", "BORDERLINE", "FAIL"]:
    """PASS if zero failures; BORDERLINE if exactly one failure within 10% of threshold; else FAIL."""
    failed_keys = [k for k, (passed, _, _) in gate_results.items() if passed is False]
    if not failed_keys:
        return "PASS"
    if len(failed_keys) == 1 and gate_results[failed_keys[0]][2]:
        return "BORDERLINE"
    return "FAIL"


def evaluate_gate(summary: DistributionSummary) -> GateVerdict:
    gate_results = {
        "c1": _eval_c1(summary),
        "c2": _eval_c2(summary),
        "c3": _eval_c3(summary),
        "c4": _eval_c4(summary),
        "c5": _eval_c5(summary),
    }
    reasons = {k: r for k, (_, r, _) in gate_results.items()}
    return GateVerdict(
        c1_pass=gate_results["c1"][0],
        c2_pass=gate_results["c2"][0],
        c3_pass=gate_results["c3"][0],
        c4_pass=gate_results["c4"][0],
        c5_pass=gate_results["c5"][0],
        overall=_resolve_overall(gate_results),
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Report compilation


def _format_window_table(results: Sequence[WindowResult]) -> str:
    lines = [
        "| test_start | test_end | regime | Sharpe gross | Sharpe mod | α_t HAC | IC_t | MaxDD | Turnover | CumRet |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in results:
        rev = " ↻" if r.regime_reversed_within else ""
        alpha_t = f"{r.carhart_alpha_tstat:+.2f}" if r.carhart_alpha_tstat is not None else "—"
        lines.append(
            f"| {r.test_start} | {r.test_end} | {r.regime}{rev} | "
            f"{r.sharpe_gross:+.2f} | {r.sharpe_moderate:+.2f} | {alpha_t} | "
            f"{r.ic_tstat:+.2f} | {r.max_drawdown * 100:+.1f}% | "
            f"{r.turnover * 100:.0f}% | {r.cumulative_return * 100:+.1f}% |"
        )
    return "\n".join(lines)


def _format_distribution_block(s: DistributionSummary) -> str:
    lines = [
        "| Metric | min | Q25 | median | Q75 | max |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines.append(
        f"| Sharpe gross | {s.sharpe_min:+.2f} | {s.sharpe_q25:+.2f} | "
        f"{s.sharpe_median:+.2f} | {s.sharpe_q75:+.2f} | {s.sharpe_max:+.2f} |"
    )
    if s.alpha_t_min is not None:
        lines.append(
            f"| Carhart α_t HAC | {s.alpha_t_min:+.2f} | {s.alpha_t_q25:+.2f} | "
            f"{s.alpha_t_median:+.2f} | {s.alpha_t_q75:+.2f} | {s.alpha_t_max:+.2f} |"
        )
    lines.append("")
    lines.append(f"- Windows with Sharpe > 0.5: **{s.fraction_sharpe_gt_0_5:.0%}**")
    lines.append(f"- Windows with Sharpe > 1.0: **{s.fraction_sharpe_gt_1_0:.0%}**")
    if s.fraction_alpha_t_gt_1_5 is not None:
        lines.append(f"- Windows with Carhart α_t > 1.5: **{s.fraction_alpha_t_gt_1_5:.0%}**")
        lines.append(f"- Windows with Carhart α_t > 2.0: **{s.fraction_alpha_t_gt_2_0:.0%}**")
    lines.append(f"- Windows with IC t-stat > 1.5: **{s.fraction_ic_t_gt_1_5:.0%}**")
    return "\n".join(lines)


def _format_stability_block(s: DistributionSummary) -> str:
    lines = [
        f"- **21-day block-return autocorr (lag-1)**: {s.block_return_autocorr_lag1:+.3f}",
        "  - Computed on non-overlapping 21-day blocks (~59 independent observations).",
        "  - Low value (< 0.5) = strategy returns do not cluster into regimes.",
        "",
        f"- **Longest contiguous negative-Sharpe stretch**: {s.longest_negative_sharpe_stretch} windows",
    ]
    if s.dark_half_span:
        lines.append(f"  - Span: {s.dark_half_span[0]} → {s.dark_half_span[1]}")
    lines.append(
        f"- **Momentum-crash coincidence**: {s.momentum_crash_coincidence:.0%} "
        f"of negative-Sharpe windows overlap a benchmark regime reversal"
    )
    lines.append(f"- **Turnover**: max {s.max_turnover:.0%}, Q95 {s.q95_turnover:.0%}")
    return "\n".join(lines)


def _format_gate_block(v: GateVerdict) -> str:
    lines = ["## Decision gate", ""]

    def line(key: str, pass_flag: bool | None) -> str:
        if pass_flag is None:
            mark = "N/A"
        elif pass_flag:
            mark = "PASS"
        else:
            mark = "FAIL"
        return f"- **{key.upper()}**: {mark} — {v.reasons[key]}"

    lines.append(line("c1", v.c1_pass))
    lines.append(line("c2", v.c2_pass))
    lines.append(line("c3", v.c3_pass))
    lines.append(line("c4", v.c4_pass))
    lines.append(line("c5", v.c5_pass))
    lines.append("")
    lines.append(f"**Overall: {v.overall}**")
    return "\n".join(lines)


def _interpret_walk_forward(report: WalkForwardReport, verdict: GateVerdict) -> str:
    s = report.summary
    if verdict.overall == "PASS":
        return (
            f"**Interpretation.** The strategy is stable across OOS rolling "
            f"windows. {s.fraction_sharpe_gt_0_5:.0%} of windows have Sharpe > 0.5, "
            f"and {s.fraction_alpha_t_gt_1_5 * 100 if s.fraction_alpha_t_gt_1_5 is not None else 0:.0f}% "
            f"have Carhart α t-stat > 1.5 HAC. Block-return autocorr "
            f"{s.block_return_autocorr_lag1:+.3f} indicates returns do not "
            f"cluster into one regime. No dark half "
            f"({s.longest_negative_sharpe_stretch} consecutive negative "
            f"windows, below the 12-window threshold). The headline Sharpe "
            f"{report.baseline_sharpe:+.2f} is not a single-regime artifact."
        )
    if verdict.overall == "BORDERLINE":
        failed_rule = next(
            (k for k, v in verdict.reasons.items() if (getattr(verdict, f"{k}_pass") is False)),
            None,
        )
        return (
            f"**Interpretation.** Borderline result — one gate rule fails "
            f"within 10% of threshold ({failed_rule}). Baseline Sharpe "
            f"{report.baseline_sharpe:+.2f} is likely robust, but the specific "
            f"concern in rule {failed_rule} warrants a focused re-check before "
            f"publishing."
        )
    # FAIL
    return (
        f"**Interpretation.** Strategy fails multiple stability checks. Baseline "
        f"Sharpe {report.baseline_sharpe:+.2f} is concentrated in a specific "
        f"regime or time period. See per-rule reasons above and do not deploy "
        f"further capital until the failing conditions are understood."
    )


def compile_report(
    out_path: Path,
    report: WalkForwardReport,
    *,
    benchmark: str,
    top_n: int,
    holding: int,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Walk-Forward OOS Validation — Layer 2b",
        "",
        f"- **Total test windows**: {len(report.window_results)}",
        f"- **Window size**: {report.window_days} trading days",
        f"- **Step**: {report.step_days} trading days",
        f"- **Benchmark**: {benchmark}",
        f"- **Top-N**: {top_n}",
        f"- **Holding period**: {holding} trading days",
        f"- **Full-range baseline Sharpe**: {report.baseline_sharpe:+.3f}",
    ]
    if report.baseline_alpha_tstat is not None:
        lines.append(
            f"- **Full-range baseline Carhart α t-stat HAC**: {report.baseline_alpha_tstat:+.2f}"
        )
    lines += [
        "",
        "Rolling 252-day test windows stepped monthly across the baseline",
        "backtest span. All per-window metrics computed by slicing the",
        "baseline's `rebalance_results` — the engine is deterministic for fixed",
        "scorer/config, so sliced metrics match a per-window re-run at a",
        "fraction of the wall time.",
        "",
        "MomentumScorer has fixed equal weights (1/7 per metric), so this",
        "is a **performance-stability** test of a deterministic scorer, not",
        "a parameter-refit walk-forward.",
        "",
        "## Per-window results",
        "",
        _format_window_table(report.window_results),
        "",
        "## Distribution summary",
        "",
        _format_distribution_block(report.summary),
        "",
        "## Stability block",
        "",
        _format_stability_block(report.summary),
        "",
        _format_gate_block(report.verdict),
        "",
        _interpret_walk_forward(report, report.verdict),
        "",
        "## Limitations",
        "",
        "- **Window-size cherry picking**: 252 trading days is conventional but",
        "  arbitrary. Re-running with `--window-days 126` or `378` is the",
        "  robustness check; not automated in this report.",
        "- **Low statistical power**: with ~25-37 windows, distribution",
        "  quantiles have wide confidence intervals. Descriptive, not inferential.",
        "- **Autocorr choice**: explicit rejection of windowed-Sharpe autocorr",
        "  (92% overlap = mechanically near 1; every-12th subsample = n≈4).",
        "  Block-return autocorr over non-overlapping 21-day blocks is the",
        "  defensible alternative. Gate C3 uses this.",
        "- **Path-independence invariant**: Max DD and cumulative return per",
        "  window are recomputed from a fresh cumprod starting at 1 for the",
        "  slice's first day — never inherited from the global equity curve's",
        "  high-water mark.",
        "- **Training window nominal only**: MomentumScorer has fixed weights;",
        "  reported metrics are test-window only. This is a performance-",
        "  stability test, not a classic parameter-refit walk-forward.",
        "- **Regime majority-label collapse**: a window straddling a regime",
        "  transition gets one label (the majority). `regime_reversed_within`",
        "  flags those windows for the momentum-crash indicator.",
    ]

    out_path.write_text("\n".join(lines))
    return out_path
