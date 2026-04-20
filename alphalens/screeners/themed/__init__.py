"""Themed screener (Layer 2b).

Daily scan over a curated themed YAML universe (quantum, AI, semis, nuclear, crypto).
Scorer is pluggable: MomentumScorer (classic continuation) or EarlyStageScorer
(base-breakout / VCP / Jegadeesh 11-1). The pipeline's invariant is the
universe source, not the scoring math.
"""
