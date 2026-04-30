"""Themed screener (Layer 2b).

Daily scan over a curated themed YAML universe (quantum, AI, semis, nuclear, crypto).
Scorer is pluggable: MomentumScorer (classic continuation) or EarlyStageScorer
(base-breakout / VCP / Jegadeesh 11-1). The pipeline's invariant is the
universe source, not the scoring math.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-22"
__closed_reason__ = "Momentum overfit OOS; realistic execution cost ~100% ann eats signal"
__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": "docs/research/multiple_testing_audit_2026-04.md",
    "sanity_checks_4gate": "N/A: momentum screen, not rotation overlay",
    "walk_forward_oos": "docs/research/walk_forward_oos_validation.md",
    "multiple_testing_correction": "docs/research/multiple_testing_audit_2026-04.md",
    "cost_drag": "docs/backtest/cost_validation.md",
    "bootstrap_ci": "docs/research/layer2b_audit_final.md",
    "survivorship_pit": "docs/research/pit_universe_backtest.md",
}
