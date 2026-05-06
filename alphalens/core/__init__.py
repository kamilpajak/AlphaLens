"""Core plumbing — Layer-agnostic infrastructure shared across all layers.

Contents:
- candidates  — `Candidate` dataclass + `AnalysisResult` (screener -> queue contract)
- queue       — SQLite-backed unified queue (`~/.alphalens/candidates.db`)
- registry    — source-priority registry

Nothing here is layer-specific. Layers import from `alphalens.core.*`; nothing
in core imports back from a layer.
"""
