"""Phase 3b exit-criteria evaluator — design doc §8 gates → GO / KILL / PAPER_TRACK.

Design doc §8 locks the Layer 2d capital-deploy decision rules per
Perplexity R5/R6/R8. This module encodes them as a deterministic function
so the Phase 3b validation report can emit a final verdict.

Gate summary (all must pass for GO):

    carhart_alpha_bonferroni  : Carhart-4F α |t| > Bonferroni threshold at n_tests
    ff5_umd_alpha             : FF5+UMD α |t| > 2.0
    ff5_umd_attenuation       : (Carhart α_t − FF5+UMD α_t) / Carhart α_t < 0.30
    net_alpha_primary         : net α > 0 under primary RealisticCostModel
    net_alpha_stress_k15      : net α > 0 under Almgren-Chriss k=0.15 stress
    bootstrap_ci              : bootstrap 95% CI excludes zero
    sharpe_net                : OOS Sharpe net > 1.0
    regime_collapse_{bull,bear,flat} : regime α_t > 1.5

Q4 is best-effort — when supplied, flagged as diagnostic; missing Q4 does
not fail the decision.

Ambiguous zone (design doc §8 "paper-track 6-12mo"): Carhart α_t in
[1.5, 2.24]. This is returned as PAPER_TRACK verdict — neither GO nor KILL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from phase_robust_backtesting.multiple_testing import bonferroni_critical_tstat

from .factor_analysis import AlphaResult

Verdict = Literal["GO", "KILL", "PAPER_TRACK"]


@dataclass(frozen=True)
class DecisionReport:
    verdict: Verdict
    gates: dict[str, bool]  # gate_name → passes
    failing_gates: list[str]
    notes: list[str]


_FF5_ATTENUATION_THRESHOLD = 0.30
_REGIME_ALPHA_T_FLOOR = 1.5
_SHARPE_NET_FLOOR = 1.0
_AMBIGUOUS_LOWER = 1.5
_AMBIGUOUS_UPPER = 2.24  # matches Bonferroni n=2 at α=0.05


def evaluate_exit_criteria(
    *,
    carhart: AlphaResult,
    ff5_umd: AlphaResult | None,
    q4: AlphaResult | None,
    net_alpha_primary: float,
    net_alpha_stress_k15: float,
    bootstrap_95ci_excludes_zero: bool,
    sharpe_net: float,
    regime_alpha_tstats: Mapping[str, float],
    n_tests: int,
) -> DecisionReport:
    gates: dict[str, bool] = {}
    notes: list[str] = []

    bonferroni = bonferroni_critical_tstat(n_tests)
    carhart_t = abs(carhart.alpha_tstat)

    gates["carhart_alpha_bonferroni"] = carhart_t > bonferroni

    if ff5_umd is not None:
        ff5_t = abs(ff5_umd.alpha_tstat)
        gates["ff5_umd_alpha"] = ff5_t > 2.0
        # Attenuation gate measures ECONOMIC magnitude decay, not statistical
        # significance decay. Per Zen code review (2026-04-24): comparing
        # α_tstat conflates SE inflation from added factors with actual α
        # shrinkage. Correct approach compares α_annualized (Harvey-Liu-Zhu).
        # Negative attenuation (FF5+UMD α > Carhart α) is treated as "no
        # attenuation" — gate passes.
        carhart_alpha = abs(carhart.alpha_annualized)
        if carhart_alpha > 1e-9:
            attenuation = (carhart_alpha - abs(ff5_umd.alpha_annualized)) / carhart_alpha
        else:
            attenuation = 0.0
        gates["ff5_umd_attenuation"] = attenuation < _FF5_ATTENUATION_THRESHOLD
    else:
        gates["ff5_umd_alpha"] = True  # absent, not a blocker
        gates["ff5_umd_attenuation"] = True
        notes.append("FF5+UMD not supplied — robustness check skipped")

    if q4 is None:
        notes.append("Q4 unavailable (best-effort per plan; gap 2025-2026 upstream)")
    else:
        notes.append(f"Q4 α_t = {q4.alpha_tstat:.2f} (diagnostic only)")

    gates["net_alpha_primary"] = net_alpha_primary > 0
    gates["net_alpha_stress_k15"] = net_alpha_stress_k15 > 0
    gates["bootstrap_ci"] = bootstrap_95ci_excludes_zero
    gates["sharpe_net"] = sharpe_net > _SHARPE_NET_FLOOR

    for regime, t in regime_alpha_tstats.items():
        gates[f"regime_collapse_{regime}"] = abs(t) > _REGIME_ALPHA_T_FLOOR

    failing = [name for name, ok in gates.items() if not ok]

    verdict: Verdict
    if not failing:
        verdict = "GO"
    elif _AMBIGUOUS_LOWER < carhart_t <= _AMBIGUOUS_UPPER and "carhart_alpha_bonferroni" in failing:
        verdict = "PAPER_TRACK"
    else:
        verdict = "KILL"

    return DecisionReport(verdict=verdict, gates=gates, failing_gates=failing, notes=notes)
