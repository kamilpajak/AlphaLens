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
