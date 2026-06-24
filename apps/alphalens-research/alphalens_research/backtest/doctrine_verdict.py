"""Doctrine verdict — wire the project's pre-registered PASS bars into code.

``phase_robust_backtesting.multi_phase.robust_verdict`` only checks
offset-phase STABILITY (PASS at ``αt >= 1.5`` across rebalance-offset phases
within one window). The project-DOCTRINE bars — full-sample net αt >= 3.5,
phase-mean >= 2.5 across IS/OOS/FL, every phase > 0, net αt >= 2.0 at the
15bps cost arm, plus the AV-PIT gate — are a SEPARATE, stricter judgement that
lived only in memo / ledger prose. The PEAD v2 §17 adversarial review flagged
this decoupling as the dominant false-PASS channel: a harness "PASS" (the 1.5
gate) read off the same JSON as a 2.39 class-internal number could be mistaken
for a doctrine PASS. This module enforces the doctrine stack explicitly.

The doctrine verdict consumes per-window ``run_audit`` output JSONs: one
full-span run (2018..2026 = gate 1's full sample) plus the three IS/OOS/FL
phase runs (gates 2-4). Each window's net αt for a cost arm is the mean of the
aggregated ``alpha_t_net`` across that window's rebalance-offset phases.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

# Pre-registered doctrine bars (memo §8 / ledger pead_v5_pss_2026_05_13).
DOCTRINE_JOINT_T = 3.5  # gate 1 — full-sample net αt
DOCTRINE_PHASE_MEAN_T = 2.5  # gate 2 — mean net αt across IS/OOS/FL
DOCTRINE_PER_PHASE_T = 0.0  # gate 3 — strictly positive net αt in each phase
DOCTRINE_G4_NET15_T = 2.0  # gate 4 — net αt at the 15bps cost arm, per phase

# Cost arms the gates read (half-spread bps).
_BASELINE_COST_BPS = 5.0  # gates 1-3 read the 5bps net αt
_STRESS_COST_BPS = 15.0  # gate 4 knockout reads the 15bps net αt


@dataclass(frozen=True)
class DoctrineVerdict:
    """Result of applying the doctrine stack. ``verdict`` is one of
    ``PASS`` / ``PASS_MARGINAL`` / ``FAIL``; ``gates`` is the per-gate
    boolean breakdown so a reader can see exactly which bar bound."""

    verdict: str
    gates: dict[str, bool]
    full_sample_alpha_t: float
    phase_mean_alpha_t: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "gates": self.gates,
            "full_sample_alpha_t": self.full_sample_alpha_t,
            "phase_mean_alpha_t": self.phase_mean_alpha_t,
            "reason": self.reason,
            "note": (
                "DOCTRINE verdict (bars 3.5/2.5/per-phase>0/net-15bps>=2.0/AV-PIT) — "
                "distinct from phase_robust_backtesting.robust_verdict, which only "
                "checks offset-phase stability at alpha_t>=1.5."
            ),
        }


def evaluate_doctrine(
    *,
    full_sample_alpha_t: float,
    per_phase_alpha_t: list[float],
    per_phase_alpha_t_15bps: list[float],
    av_pit_passed: bool,
) -> DoctrineVerdict:
    """Apply the five pre-registered doctrine gates.

    ``per_phase_alpha_t`` / ``per_phase_alpha_t_15bps`` are the per-phase
    (IS/OOS/FL) net αt at the 5bps and 15bps cost arms respectively.
    """
    phase_mean = fmean(per_phase_alpha_t) if per_phase_alpha_t else 0.0
    gates = {
        "g1_joint_3_5": full_sample_alpha_t >= DOCTRINE_JOINT_T,
        "g2_phase_mean_2_5": bool(per_phase_alpha_t) and phase_mean >= DOCTRINE_PHASE_MEAN_T,
        "g3_per_phase_positive": bool(per_phase_alpha_t)
        and all(t > DOCTRINE_PER_PHASE_T for t in per_phase_alpha_t),
        "g4_net15_2_0": bool(per_phase_alpha_t_15bps)
        and all(t >= DOCTRINE_G4_NET15_T for t in per_phase_alpha_t_15bps),
        "g5_av_pit": bool(av_pit_passed),
    }

    if all(gates.values()):
        verdict = "PASS"
        reason = "all five doctrine gates pass"
    elif (
        gates["g3_per_phase_positive"]
        and gates["g4_net15_2_0"]
        and gates["g5_av_pit"]
        and DOCTRINE_PHASE_MEAN_T <= full_sample_alpha_t < DOCTRINE_JOINT_T
    ):
        verdict = "PASS_MARGINAL"
        reason = (
            f"joint αt {full_sample_alpha_t:.2f} in marginal band "
            f"[{DOCTRINE_PHASE_MEAN_T}, {DOCTRINE_JOINT_T}); gates 3/4/5 pass — "
            "paper-trade observation only, NOT capital deploy"
        )
    else:
        verdict = "FAIL"
        failed = [k for k, ok in gates.items() if not ok]
        reason = f"doctrine FAIL — gates failed: {', '.join(failed)}"

    return DoctrineVerdict(
        verdict=verdict,
        gates=gates,
        full_sample_alpha_t=float(full_sample_alpha_t),
        phase_mean_alpha_t=float(phase_mean),
        reason=reason,
    )


def net_alpha_t_from_audit(audit: dict, cost_bps: float) -> float:
    """Mean-across-offset-phases net Carhart αt for the given cost arm in a
    ``run_audit`` output JSON. Raises ``KeyError`` if the cost config or the
    aggregated ``alpha_t_net`` is absent (a silent fallback would mask a
    broken audit and could manufacture a false verdict)."""
    # Match run_audit's exact key format (``cost={cost_bps:.0f}bps``) so a
    # fractional arm rounds the same way the producer does rather than
    # truncating into a silent KeyError.
    config_key = f"cost={cost_bps:.0f}bps"
    for cfg in audit.get("configs", []):
        if cfg.get("config") == config_key:
            summary = cfg.get("summary", {})
            net = summary.get("alpha_t_net")
            if not net or "mean" not in net:
                raise KeyError(
                    f"config {config_key!r} has no aggregated alpha_t_net.mean "
                    "(audit produced no parseable net-of-cost rows)"
                )
            return float(net["mean"])
    raise KeyError(f"cost arm {config_key!r} not found in audit configs")


def evaluate_doctrine_from_jsons(
    *,
    full: Path,
    is_: Path,
    oos: Path,
    fl: Path,
    av_pit_passed: bool,
) -> DoctrineVerdict:
    """Read the four per-window ``run_audit`` JSONs and apply the doctrine
    stack. ``full`` is the full-span (2018..2026) run for gate 1; ``is_`` /
    ``oos`` / ``fl`` are the phase runs for gates 2-4."""
    full_audit = json.loads(Path(full).read_text())
    phase_audits = [json.loads(Path(p).read_text()) for p in (is_, oos, fl)]
    return evaluate_doctrine(
        full_sample_alpha_t=net_alpha_t_from_audit(full_audit, _BASELINE_COST_BPS),
        per_phase_alpha_t=[net_alpha_t_from_audit(a, _BASELINE_COST_BPS) for a in phase_audits],
        per_phase_alpha_t_15bps=[net_alpha_t_from_audit(a, _STRESS_COST_BPS) for a in phase_audits],
        av_pit_passed=av_pit_passed,
    )
