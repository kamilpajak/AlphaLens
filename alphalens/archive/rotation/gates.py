"""R12 gate evaluator for Tactical Sector Rotation.

Each gate is a pure function taking some subset of (OverlayBacktestResult,
benchmark_close, Carhart AlphaResult, config) → GateResult. ``evaluate_all_gates``
aggregates them into a ``GateReport`` where ``passed = all(gates)``.

Gates implemented (Phase 7):
  1. regime_decomp  — α > 0 in bull AND bear AND flat
  2. bootstrap_ci   — 95% moving-block bootstrap lower bound excludes zero
  3. cost_drag      — (gross_mean - net_mean) / gross_mean < max_drag_ratio
  4. rolling_sharpe — min 252d rolling Sharpe > threshold
  5. carhart_alpha_t — Carhart-4F α t-stat > min_t (OOS threshold 1.5)
  6. bonferroni      — Carhart α t-stat > Bonferroni critical for n_tests
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from alphalens.archive.rotation.config import GateConfig
from alphalens.archive.rotation.overlay_engine import OverlayBacktestResult
from alphalens.attribution.factor_analysis import AlphaResult
from alphalens.attribution.regime import classify_regime
from alphalens.backtest.metrics import sharpe
from alphalens.backtest.multiple_testing import bonferroni_critical_tstat


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    value: float
    threshold: float
    detail: str


@dataclass(frozen=True)
class GateReport:
    gates: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)


def gate_regime_decomp(
    result: OverlayBacktestResult,
    benchmark_close: pd.Series,
    *,
    lookback: int = 60,
    bull_threshold: float = 0.05,
    bear_threshold: float = -0.05,
) -> GateResult:
    labels = classify_regime(
        benchmark_close,
        lookback=lookback,
        bull_threshold=bull_threshold,
        bear_threshold=bear_threshold,
    )
    aligned = pd.concat(
        [result.daily_returns_net.rename("r"), labels.rename("regime")],
        axis=1,
        join="inner",
    )
    per_regime = {}
    for label in ("bull", "bear", "flat"):
        slice_ = aligned[aligned["regime"] == label]["r"]
        if slice_.empty:
            continue
        per_regime[label] = float(slice_.mean())
    worst = min(per_regime.values()) if per_regime else float("nan")
    passed = bool(per_regime) and all(v > 0 for v in per_regime.values())
    detail = ", ".join(f"{k}={v:.5f}" for k, v in sorted(per_regime.items()))
    return GateResult(
        name="regime_decomp",
        passed=passed,
        value=worst,
        threshold=0.0,
        detail=f"per-regime net daily mean: {detail}",
    )


def gate_bootstrap_ci(
    result: OverlayBacktestResult,
    *,
    n_bootstrap: int = 1000,
    block_size: int = 21,
    alpha: float = 0.05,
    seed: int = 42,
) -> GateResult:
    """Moving-block bootstrap lower bound for mean(net_returns)."""
    r = result.daily_returns_net.dropna().to_numpy()
    if len(r) < block_size * 2:
        return GateResult(
            name="bootstrap_ci",
            passed=False,
            value=float("nan"),
            threshold=0.0,
            detail=f"insufficient observations ({len(r)}) for block={block_size}",
        )
    rng = np.random.default_rng(seed)
    n = len(r)
    n_blocks = n // block_size
    starts_max = n - block_size
    means = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        sample = np.concatenate([r[s : s + block_size] for s in starts])
        means[i] = sample.mean()
    lower = float(np.quantile(means, alpha / 2))
    upper = float(np.quantile(means, 1 - alpha / 2))
    passed = lower > 0.0
    return GateResult(
        name="bootstrap_ci",
        passed=passed,
        value=lower,
        threshold=0.0,
        detail=f"95% CI [{lower:.6f}, {upper:.6f}]",
    )


def gate_cost_drag(result: OverlayBacktestResult, *, max_drag_ratio: float = 0.5) -> GateResult:
    gross_mean = float(result.daily_returns_gross.mean())
    net_mean = float(result.daily_returns_net.mean())
    if gross_mean <= 0:
        return GateResult(
            name="cost_drag",
            passed=False,
            value=float("inf"),
            threshold=max_drag_ratio,
            detail=f"gross α not positive (gross_mean={gross_mean:.6f})",
        )
    drag_ratio = (gross_mean - net_mean) / gross_mean
    passed = drag_ratio < max_drag_ratio
    return GateResult(
        name="cost_drag",
        passed=passed,
        value=drag_ratio,
        threshold=max_drag_ratio,
        detail=f"drag {drag_ratio:.2%} of gross (gross={gross_mean:.5f}, net={net_mean:.5f})",
    )


def gate_rolling_sharpe(
    result: OverlayBacktestResult,
    *,
    window: int = 252,
    min_sharpe: float = 0.30,
) -> GateResult:
    r = result.daily_returns_net.dropna()
    if len(r) < window:
        return GateResult(
            name="rolling_sharpe",
            passed=False,
            value=float("nan"),
            threshold=min_sharpe,
            detail=f"series too short ({len(r)}) for window={window}",
        )
    rolling = r.rolling(window).apply(lambda w: sharpe(w.tolist()), raw=False).dropna()
    worst = float(rolling.min())
    passed = worst > min_sharpe
    return GateResult(
        name="rolling_sharpe",
        passed=passed,
        value=worst,
        threshold=min_sharpe,
        detail=f"min rolling {window}d Sharpe = {worst:.2f}",
    )


def gate_carhart_alpha_t(carhart: AlphaResult, *, min_t: float = 1.5) -> GateResult:
    t = float(carhart.alpha_tstat)
    passed = abs(t) > min_t and t > 0  # positive α required
    return GateResult(
        name="carhart_alpha_t",
        passed=passed,
        value=t,
        threshold=min_t,
        detail=f"Carhart-4F α t = {t:.2f} (spec={carhart.spec_name})",
    )


def gate_bonferroni(carhart: AlphaResult, *, n_tests: int, alpha: float = 0.05) -> GateResult:
    critical = bonferroni_critical_tstat(n_tests=n_tests, alpha=alpha)
    t = abs(float(carhart.alpha_tstat))
    passed = carhart.alpha_tstat > 0 and t > critical
    return GateResult(
        name="bonferroni",
        passed=passed,
        value=carhart.alpha_tstat,
        threshold=critical,
        detail=f"n_tests={n_tests} critical |t|={critical:.2f}, actual t={carhart.alpha_tstat:.2f}",
    )


def evaluate_all_gates(
    *,
    result: OverlayBacktestResult,
    benchmark_close: pd.Series,
    carhart_result: AlphaResult,
    gates: GateConfig,
    n_tests: int,
    alpha: float = 0.05,
) -> GateReport:
    items: Sequence[GateResult] = (
        gate_regime_decomp(result, benchmark_close),
        gate_bootstrap_ci(result),
        gate_cost_drag(result),
        gate_rolling_sharpe(result, window=252, min_sharpe=gates.rolling_sharpe_min),
        gate_carhart_alpha_t(carhart_result, min_t=gates.carhart_oos_t_min),
        gate_bonferroni(carhart_result, n_tests=n_tests, alpha=alpha),
    )
    return GateReport(gates=tuple(items))
