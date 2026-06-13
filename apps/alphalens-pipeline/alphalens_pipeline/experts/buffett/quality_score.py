"""Cheap Buffett quality score — a 0-100 composite over already-computed numerics.

The daily brief card surfaces a single ``buffett NN/100`` chip. This module
computes that number from the THREE cheap Buffett numerics the
:class:`~alphalens_pipeline.experts.buffett.comparison.BuffettPanel` already carries —
owner-earnings yield, 3-year average ROIC, and margin of safety — and shrinks it
by the panel's ``data_coverage`` so a thin-data name is neither buried to zero
nor over-boosted past a fully-covered high-quality name.

Design contract (see ``docs/research/buffett_card_surfacing_design_2026_06_12.md``):

- **Cheap inputs only.** The qualitative LLM verdict (moat / trend / candor) is
  NEVER an input — it has no validated alpha link until the deferred Buffett×EDGE
  study (N≥30, ~2026-09+), so it stays display-only. This score, built from
  fundamentals already fetched in the Layer-4 pass, is the only Buffett term the
  pipeline may later let touch sorting; in v1 it is display-only too.

- **Hand-chosen, unvalidated weights.** The 0.45 / 0.35 / 0.20 split and the clip
  caps are a screening heuristic, not a fitted optimum — they are module
  constants on purpose so the lens is auditable. Treat the result as a
  comparison signal, not a claimed-optimal quality measure.

- **Honest absence.** When NONE of the three scoring inputs resolves, the score
  is ``None`` (the card hides the chip) rather than a misleading zero. A missing
  single input contributes zero while the others still score.
"""

from __future__ import annotations

from alphalens_pipeline.experts.buffett.comparison import BuffettPanel

# Component weights (sum to 1.0 so the pre-shrink composite lands in [0, 100]).
# Owner-earnings yield leads (the cash a Buffett owner actually keeps), ROIC
# next (capital-efficiency durability), margin of safety last (a conservative
# screen, deliberately the lightest term).
_W_OWNER_EARNINGS_YIELD = 0.45
_W_ROIC = 0.35
_W_MARGIN_OF_SAFETY = 0.20

# Clip caps (in the same percent units as the panel fields). A value at or above
# its cap earns full credit for that term; anything past it cannot dominate the
# composite. Negative values floor at zero (a loss-making ROIC or an overvalued,
# negative margin of safety contributes nothing — never a negative term).
_CLIP_OWNER_EARNINGS_YIELD_PCT = 10.0
_CLIP_ROIC_PCT = 30.0
_CLIP_MARGIN_OF_SAFETY_PCT = 50.0

# Coverage shrink: multiplier = ``_COVERAGE_BASE + (1 - _COVERAGE_BASE) * cov``.
# At full coverage the composite is unchanged; at zero coverage it is halved.
#
# Deliberate: ``data_coverage`` is the panel's full 6-field basket fraction, but
# the score reads only 3 of those fields. A name that resolved only ROIC through
# a sparse EDGAR filing is therefore shrunk the same as one that missed OE-yield
# for the same reason — thin data should not pass a quality screen that a fully-
# covered name survives. Tuning the basket to the 3 scoring fields is deferred to
# the Buffett×EDGE calibration (this score is display-only until then).
_COVERAGE_BASE = 0.5


def _clipped_fraction(value: float | None, *, cap: float) -> float:
    """``clip(value, 0, cap) / cap`` in [0, 1]; ``None`` (or non-positive) -> 0.0."""
    if value is None or value <= 0:
        return 0.0
    if value >= cap:
        return 1.0
    return value / cap


def compute_quality_score(panel: BuffettPanel) -> float | None:
    """The 0-100 Buffett quality composite for one candidate, or ``None``.

    Returns ``None`` when none of the three scoring inputs (owner-earnings yield,
    3-year average ROIC, margin of safety) resolved — a score built from zero
    present components would be noise, so the card shows no chip instead. When at
    least one input is present, missing inputs contribute zero and the composite
    is shrunk by ``data_coverage``.
    """
    oe_yield = panel.owner_earnings_yield_pct
    roic_3y = panel.roic_3y_avg
    mos = panel.margin_of_safety_pct
    if oe_yield is None and roic_3y is None and mos is None:
        return None

    raw = 100.0 * (
        _W_OWNER_EARNINGS_YIELD * _clipped_fraction(oe_yield, cap=_CLIP_OWNER_EARNINGS_YIELD_PCT)
        + _W_ROIC * _clipped_fraction(roic_3y, cap=_CLIP_ROIC_PCT)
        + _W_MARGIN_OF_SAFETY * _clipped_fraction(mos, cap=_CLIP_MARGIN_OF_SAFETY_PCT)
    )
    shrink = _COVERAGE_BASE + (1.0 - _COVERAGE_BASE) * panel.data_coverage
    return raw * shrink


__all__ = ["compute_quality_score"]
