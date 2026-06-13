"""Panel of orthogonal expert lenses over a candidate.

Each expert (Buffett = value / quality today; O'Neil = momentum, numeric-only,
next) produces a numeric panel + a 0-100 score + an optional qualitative
classification. Every expert output is a CHARACTERISTIC, display-only — nothing
here feeds candidate selection or ordering until each expert's Expert×EDGE
correlation is validated. See ``docs/research/expert_panel_design_2026_06_13.md``.

PR-1 ships the abstraction (:mod:`~alphalens_pipeline.experts.base`), the registry
(:mod:`~alphalens_pipeline.experts.registry`), and the first expert
(:mod:`~alphalens_pipeline.experts.buffett`) moved here intact. The generalized
enrichment + the ``experts enrich`` CLI follow in PR-2.
"""

# Informational only: this package lives under alphalens_pipeline, which is NOT a
# layer root, so test_layer_status does not gate it. Kept for parity with the
# moved buffett subpackage's own __status__.
__status__ = "ACTIVE"
