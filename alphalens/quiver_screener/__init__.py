"""Quiver Quantitative congressional-trades screener.

Used for the Quiver Congress long/short validation (memory:
project_quiver_validation.md). Backtest produced alpha t=-2.14 HAC, robust to
drop-top-50 filter — signal so over-mined it inverted relative to the academic
publication. Verdict: KILL.

Code retained as anti-pattern record per ADR 0005 (closed-layers as
anti-pattern catalog) and to support `scripts/quiver_validate.py` /
`scripts/quiver_robustness.py` research replay.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-22"
__closed_reason__ = (
    "Quiver Congress L/S validation: alpha t=-2.14 HAC, robust to drop-top-50 "
    "filter — signal inverted from published academic edge, evidence of "
    "extreme alt-data crowding (AP-9)."
)
__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": "docs/research/5_paradigm_failures_postmortem.md",
    "sanity_checks_4gate": "N/A: alt-data L/S screen, not rotation overlay",
    "walk_forward_oos": "docs/research/5_paradigm_failures_postmortem.md",
    "multiple_testing_correction": "N/A: single hypothesis, inverted sign sufficient",
    "cost_drag": "N/A: alt-data crowding kill, cost not relevant to verdict",
    "bootstrap_ci": "docs/research/5_paradigm_failures_postmortem.md",
    "survivorship_pit": "N/A: live alt-data feed, no historical backfill bias",
}
