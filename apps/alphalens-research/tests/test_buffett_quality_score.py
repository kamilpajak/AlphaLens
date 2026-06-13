"""Unit tests for the cheap Buffett quality score (PR-1, card-surfacing design).

``compute_quality_score`` collapses three cheap, already-computed Buffett
numerics — owner-earnings yield, 3-year average ROIC, margin of safety — into a
single 0-100 composite, then shrinks it by data coverage so thin-data names are
neither buried to zero nor over-boosted. The qualitative LLM verdict NEVER feeds
this score (it has no validated alpha link until Buffett×EDGE, deferred). The
score drives the card chip and is display-only in v1 (it does not touch sorting).

Covered:

- full-quality name (all three inputs at/above their clip caps, coverage 1.0)
  scores the maximum 100;
- each input is clipped at its cap so an extreme value cannot dominate;
- a negative ROIC / negative (overvalued) margin of safety contributes zero,
  never a negative term;
- the coverage shrink is ``0.5 + 0.5 * coverage`` applied to the raw composite;
- a missing single input contributes zero but the others still score;
- ALL three scoring inputs missing yields ``None`` (no chip), even when the
  6-field ``data_coverage`` basket is partially filled by the other fields;
- the three component weights are 0.45 / 0.35 / 0.20.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.experts.buffett.comparison import BuffettPanel
from alphalens_pipeline.experts.buffett.quality_score import compute_quality_score


def _panel(
    *,
    owner_earnings_yield_pct: float | None = None,
    roic_3y_avg: float | None = None,
    margin_of_safety_pct: float | None = None,
    roic_latest: float | None = None,
    data_coverage: float = 1.0,
) -> BuffettPanel:
    """A BuffettPanel carrying only the fields the quality score reads.

    Every other field is ``None`` — the score must depend on exactly the four
    inputs named here (the three scored numerics + ``data_coverage``).
    """
    return BuffettPanel(
        ticker="X",
        theme="t",
        market_cap=None,
        owner_earnings_latest=None,
        owner_earnings_yield_pct=owner_earnings_yield_pct,
        roic_latest=roic_latest,
        roic_3y_avg=roic_3y_avg,
        op_margin_latest=None,
        op_margin_3y_avg=None,
        intrinsic_value_per_share=None,
        margin_of_safety_pct=margin_of_safety_pct,
        buyback_pct=None,
        net_buyback=None,
        dividend_yield_pct=None,
        data_coverage=data_coverage,
    )


class TestComputeQualityScore(unittest.TestCase):
    def test_full_quality_name_scores_100(self) -> None:
        # OE-yield 10% (cap), ROIC_3y 30% (cap), MoS 50% (cap), full coverage.
        panel = _panel(
            owner_earnings_yield_pct=10.0,
            roic_3y_avg=30.0,
            margin_of_safety_pct=50.0,
            data_coverage=1.0,
        )
        self.assertAlmostEqual(compute_quality_score(panel), 100.0)

    def test_each_input_is_clipped_at_its_cap(self) -> None:
        # Values well past every cap must not exceed the full-quality 100.
        panel = _panel(
            owner_earnings_yield_pct=25.0,
            roic_3y_avg=80.0,
            margin_of_safety_pct=120.0,
            data_coverage=1.0,
        )
        self.assertAlmostEqual(compute_quality_score(panel), 100.0)

    def test_negative_roic_and_mos_contribute_zero(self) -> None:
        # Loss-making 3y ROIC and an overvalued (negative MoS) name: only the
        # OE-yield term survives. raw = 100 * 0.45 * (10/10) = 45.
        panel = _panel(
            owner_earnings_yield_pct=10.0,
            roic_3y_avg=-40.0,
            margin_of_safety_pct=-30.0,
            data_coverage=1.0,
        )
        self.assertAlmostEqual(compute_quality_score(panel), 45.0)

    def test_coverage_shrink_halves_at_zero_coverage_band(self) -> None:
        # Same full inputs, coverage 0.5 -> multiplier 0.75.
        panel = _panel(
            owner_earnings_yield_pct=10.0,
            roic_3y_avg=30.0,
            margin_of_safety_pct=50.0,
            data_coverage=0.5,
        )
        self.assertAlmostEqual(compute_quality_score(panel), 75.0)

    def test_component_weights_are_45_35_20(self) -> None:
        # Only ROIC_3y at its cap -> raw = 100 * 0.35 = 35 (coverage 1.0).
        roic_only = _panel(roic_3y_avg=30.0, data_coverage=1.0)
        self.assertAlmostEqual(compute_quality_score(roic_only), 35.0)
        # Only OE-yield at its cap -> raw = 100 * 0.45 = 45.
        oe_only = _panel(owner_earnings_yield_pct=10.0, data_coverage=1.0)
        self.assertAlmostEqual(compute_quality_score(oe_only), 45.0)
        # Only MoS at its cap -> raw = 100 * 0.20 = 20.
        mos_only = _panel(margin_of_safety_pct=50.0, data_coverage=1.0)
        self.assertAlmostEqual(compute_quality_score(mos_only), 20.0)

    def test_all_three_inputs_missing_returns_none(self) -> None:
        # No scoring input present -> no meaningful score, even though the
        # 6-field coverage basket may be partly filled by the other fields.
        panel = _panel(data_coverage=0.5)
        self.assertIsNone(compute_quality_score(panel))

    def test_partial_input_still_scores_with_shrink(self) -> None:
        # Only OE-yield present at half its cap, coverage 0.5.
        # raw = 100 * 0.45 * (5/10) = 22.5 ; score = 22.5 * 0.75 = 16.875.
        panel = _panel(owner_earnings_yield_pct=5.0, data_coverage=0.5)
        self.assertAlmostEqual(compute_quality_score(panel), 16.875)

    def test_roic_latest_is_not_a_scoring_input(self) -> None:
        # The score uses the 3-year average ROIC, not the latest single year.
        # A panel with only roic_latest (no 3y avg, no OE, no MoS) is unscored.
        panel = _panel(roic_latest=30.0, data_coverage=0.5)
        self.assertIsNone(compute_quality_score(panel))


if __name__ == "__main__":
    unittest.main()
