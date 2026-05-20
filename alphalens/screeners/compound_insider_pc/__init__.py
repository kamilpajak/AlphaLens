"""Compound: insider_form4_opportunistic x pc_abnormal_volume (2-way EW z-score average).

Pre-reg: insider_pc_compound_2026_05_10 (Bonferroni effective n=34, |t| >= 2.974).
Design memo: docs/research/insider_pc_compound_design_2026_05_10.md
Verdict memo: docs/research/insider_pc_compound_audit_postmortem_2026_05_12_verdict.md

CLOSED 2026-05-12: joint FAIL on both pre-reg windows (OOS αt=-0.034 G1
trip; FL excess_net=-0.28% G3 trip). Memo §7 #5 mitigation engaged:
"file each component separately and archive the compound design as a
research artifact."
"""

from typing import Literal

from alphalens.screeners.compound_insider_pc.zscore_compound import (
    compound_score_from_components,
)

__all__ = ["compound_score_from_components"]


__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-05-12"
__closed_reason__ = (
    "Joint FAIL on pre-reg LOCKED audit: OOS 2018-2023 mean αt=-0.034 (G1 trip), "
    "Final-Lock 2024-2026 mean excess_net_ann=-0.28% (G3 trip, αt=+0.674 also "
    "below 2.50). Memo §7 #5 pre-predicted this failure mode (Blume-Easley "
    "selection-bias amplification on luck-marginal signals); §7 #1 explained "
    "the mechanism (cross-sectional ρ≈0 does not translate to portfolio-return "
    "independence when both bases are extreme counter-cyclical). Components "
    "remain registered separately (insider_form4_opportunistic_2026_05_08_v2 "
    "PASS_MARGINAL paper-trade; pc_abnormal_volume INCONCLUSIVE paper-trade)."
)
_VERDICT_MEMO = "docs/research/insider_pc_compound_audit_postmortem_2026_05_12_verdict.md"

__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": _VERDICT_MEMO,
    "sanity_checks_4gate": "N/A: not a rotation overlay; compound is Layer 1 fusion screener per ADR 0007",
    "walk_forward_oos": _VERDICT_MEMO,
    "multiple_testing_correction": "docs/research/insider_pc_compound_design_2026_05_10.md",
    "cost_drag": _VERDICT_MEMO,
    "bootstrap_ci": _VERDICT_MEMO,
    "survivorship_pit": _VERDICT_MEMO,
}
