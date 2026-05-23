"""Core plumbing — Layer-agnostic infrastructure shared across all layers.

Contents:
- candidates  — `Candidate` dataclass + `AnalysisResult` (screener -> queue contract)
- queue       — SQLite-backed unified queue (`~/.alphalens/candidates.db`)

Nothing here is layer-specific. Layers import from `alphalens_research.core.*`; nothing
in core imports back from a layer.
"""
