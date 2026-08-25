"""A TP tranche weight means the same thing at every consumer.

The brief speaks PERCENTAGES. `TpTrancheSpec.tranche_pct` documents itself as
"a PERCENTAGE (0-100)", and production briefs really do emit `33.333...` and
`100.0` (read from `~/.alphalens/thematic_briefs/` on 2026-08-25).

The live exit sizer reads the plan-side weight as a FRACTION:

    live_exit_engine.plan_tranche_exits
        qty = min(round(reference_qty * t.<weight>), available)

with no `/100`. `control_loop`'s arm gate copies that expression verbatim. So a
brief-supplied `33.333` would size a tranche at 33x the position, clamped by
`available` into selling the WHOLE position on the first tranche instead of a
third of it.

Meanwhile the fee estimator (`control_loop._estimate_round_trip_fee_bps`) reads
the same field as a percentage and divides by 100 — pricing the exit leg at 1%
of the position and understating the exit side of the fee floor by 100x.

`alloc_pct`, the entry-side sibling, IS converted (`sizing.py` divides by 100).
`tranche_pct` is copied verbatim. That asymmetry is the defect.

These tests pin the units at the boundary, so no consumer has to guess.
"""

from __future__ import annotations

import unittest

from broker_contract.sizing import compute_setup_plan
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    TpTrancheSpec,
    TradeSpec,
)

# The real shape, straight off a production brief: three equal tranches, stated
# as percentages that sum to 100.
_BRIEF_TRANCHE_PCT = 100.0 / 3.0


def _spec() -> TradeSpec:
    return TradeSpec(
        entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=100.0, tag="E1"),),
        disaster_stop=90.0,
        tp_tranches=(
            TpTrancheSpec(price=110.0, tranche_pct=_BRIEF_TRANCHE_PCT, r_multiple=1.0, tag="TP1"),
            TpTrancheSpec(price=120.0, tranche_pct=_BRIEF_TRANCHE_PCT, r_multiple=2.0, tag="TP2"),
            TpTrancheSpec(price=130.0, tranche_pct=_BRIEF_TRANCHE_PCT, r_multiple=3.0, tag="TP3"),
        ),
        suggested_size_pct=10.0,
    )


class TestTrancheWeightUnits(unittest.TestCase):
    def test_plan_side_weight_is_a_fraction_not_a_percentage(self) -> None:
        # The single conversion point. A brief's 33.33 PERCENT must reach the
        # plan as 0.3333 of the position — the unit the live sizer multiplies
        # by. `alloc_pct` is already converted this way one function over.
        plan = compute_setup_plan(_spec(), paper_equity=100_000.0, scale_factor=1.0)
        weights = [t.tranche_frac for t in plan.tp_tranches]
        for weight in weights:
            self.assertAlmostEqual(weight, 1.0 / 3.0, places=9)

    def test_plan_side_weights_sum_to_the_whole_position(self) -> None:
        # The property that makes the exit ladder closeable at all: the weights
        # a sizer multiplies by must total one position, not one hundred.
        plan = compute_setup_plan(_spec(), paper_equity=100_000.0, scale_factor=1.0)
        self.assertAlmostEqual(sum(t.tranche_frac for t in plan.tp_tranches), 1.0, places=9)

    def test_a_single_full_tranche_brief_reaches_the_plan_as_one(self) -> None:
        # The geometry path synthesizes `1.0` meaning the whole position. A
        # brief expressing the same thing says `100.0`. Both must arrive as 1.0.
        spec = TradeSpec(
            entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=100.0, tag="E1"),),
            disaster_stop=90.0,
            tp_tranches=(TpTrancheSpec(price=110.0, tranche_pct=100.0, r_multiple=1.0, tag="TP1"),),
            suggested_size_pct=10.0,
        )
        plan = compute_setup_plan(spec, paper_equity=100_000.0, scale_factor=1.0)
        self.assertAlmostEqual(plan.tp_tranches[0].tranche_frac, 1.0, places=9)

    def test_the_spec_side_keeps_the_brief_percentage_untouched(self) -> None:
        # The two sides deliberately carry different units under different
        # names. Collapsing them to one name is what allowed the ambiguity.
        spec = _spec()
        self.assertAlmostEqual(spec.tp_tranches[0].tranche_pct, _BRIEF_TRANCHE_PCT, places=9)


if __name__ == "__main__":
    unittest.main()
