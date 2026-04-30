"""Layer 2 screener pipelines — cross-sectional rank @ time t.

Each subpackage implements `Pipeline.to_candidates(df)` so the CLI can funnel
results through the shared `CandidateQueue`.

Active subpackages:
    prescreener      — Layer 2a S&P 500 composite (RESEARCH_ONLY, manual ad-hoc)
    momentum_lowvol  — RESEARCH_ONLY; BASE for Layer 4 vol-target overlay test

Closed Layer 2 screeners moved to `alphalens.archive.screeners.*` (themed, lean,
insider) per ADR 0005.
"""
