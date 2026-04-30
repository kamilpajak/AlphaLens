"""Core plumbing — Layer-agnostic infrastructure shared across all layers.

Contents:
- candidates  — `Candidate` dataclass + `CandidateResult` (Layer 1 → Layer 3 contract)
- queue       — SQLite-backed unified queue (`~/.alphalens/candidates.db`)
- runner      — TradingAgents wrapper (Layer 3 ad-hoc analysis)
- worker      — queue drain loop
- registry    — source-priority registry
- scorer_stats — historical scorer summary helpers
- config_gemini — `build_gemini_config()` wrapper around upstream DEFAULT_CONFIG

Nothing here is layer-specific. Layers import from `alphalens.core.*`; nothing
in core imports back from a layer.
"""
