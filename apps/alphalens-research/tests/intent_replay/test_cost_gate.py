"""The take-profit cost gate, against numbers the DAEMON produced.

The daemon's gate cannot be imported: ``min_profitable_exit_price`` lives in
``alphalens_pipeline.brokers.automanager.costs``, and the engine's import
allow-list admits ``broker_contract`` only (``test_module_dependencies.py``).
The fee card cannot move into the contract either
(``test_broker_failure_codes_published``'s sibling
``test_broker_contract_has_no_vendor_economics``). So the arithmetic is
rewritten in the leaf and checked against a GOLDEN TABLE computed from the
daemon's own function.

Provenance of every number below: measured 2026-09-27 by calling
``costs.min_profitable_exit_price(entry_price=..., qty=...,
facts=CostGateFacts(fx_applies=False, min_commission_applies=...,
card=VenueFeeCard(commission_rate=0.0008, min_commission=1.0, label="stated")))``.

Two preconditions, both of which silently move the numbers if forgotten:

* ``facts`` MUST be constructed. Called without it, the daemon falls back to
  ``CostGateFacts.legacy()``, whose ``fx_applies`` is true and adds exactly
  50 bps to the FEE — at an entry of 68.00 and 100 units that is a threshold of
  68.7888 instead of 68.4488. The replay carries no FX term at all, because no
  rate is stated and ``fx_applies: true`` is refused (spec section 8.1).
* ``exit_edge_min_bps`` must be stated as 50.0 in these rows. The daemon reads
  its own module constant ``EXIT_EDGE_MIN_BPS = 50.0``; the replay reads the
  stated value, which the section 5 example states as 5.0.

The knee of the fee card is ``min_commission / commission_rate`` = 1250. Above
it the fee is pure ad valorem, which is why the "above the knee" and "minimum
off" rows land on the SAME threshold.
"""

from __future__ import annotations

import unittest

from intent_replay import cost_gate
from intent_replay.config import Costs
from intent_replay.units import BPS, FRACTION, Quantity

RATE, MIN_COMMISSION, EDGE_BPS = 0.0008, 1.0, 50.0


def _costs(*, min_commission_applies: bool = True, edge_bps: float = EDGE_BPS) -> Costs:
    return Costs(
        commission_rate=Quantity(value=RATE, unit=FRACTION),
        min_commission=Quantity(value=MIN_COMMISSION, unit="USD"),
        min_commission_applies=min_commission_applies,
        fx_applies=False,
        exit_edge_min_bps=Quantity(value=edge_bps, unit=BPS),
    )


class GoldenTableFromTheDaemonTest(unittest.TestCase):
    """Eight rows, each a value ``costs.min_profitable_exit_price`` returned."""

    def test_above_the_knee_the_fee_is_pure_ad_valorem(self) -> None:
        got = cost_gate.min_profitable_exit_price(entry_price=68.0, units=100.0, costs=_costs())
        self.assertEqual(got, 68.44879999999999)

    def test_at_the_knee_the_two_fee_arms_meet(self) -> None:
        got = cost_gate.min_profitable_exit_price(entry_price=12.5, units=100.0, costs=_costs())
        self.assertEqual(got, 12.5825)

    def test_below_the_knee_the_per_fill_minimum_binds(self) -> None:
        got = cost_gate.min_profitable_exit_price(entry_price=68.0, units=10.0, costs=_costs())
        self.assertEqual(got, 68.54)

    def test_with_the_minimum_off_the_small_notional_pays_ad_valorem_only(self) -> None:
        got = cost_gate.min_profitable_exit_price(
            entry_price=68.0, units=10.0, costs=_costs(min_commission_applies=False)
        )
        self.assertEqual(got, 68.44879999999999)

    def test_a_notional_that_underflows_to_zero_still_prices(self) -> None:
        # The daemon's ``notional <= 0`` arm returns a zero fee rather than
        # dividing; dropping it would raise here where the daemon answers.
        got = cost_gate.min_profitable_exit_price(entry_price=1e-200, units=1e-200, costs=_costs())
        self.assertEqual(got, 1.0049999999999999e-200)

    def test_a_notional_small_enough_to_overflow_the_fee_is_unrepresentable(self) -> None:
        got = cost_gate.min_profitable_exit_price(entry_price=1e-152, units=1e-153, costs=_costs())
        self.assertIsNone(got)

    def test_zero_units_is_unpriceable(self) -> None:
        self.assertIsNone(
            cost_gate.min_profitable_exit_price(entry_price=68.0, units=0.0, costs=_costs())
        )

    def test_negative_units_is_unpriceable(self) -> None:
        self.assertIsNone(
            cost_gate.min_profitable_exit_price(entry_price=68.0, units=-1.0, costs=_costs())
        )


class ClearsCostTest(unittest.TestCase):
    """The decision the walk asks for: does this tranche's own level clear its
    own round trip? Refusing BREAKS the batch in the daemon, which is the walk's
    concern and not this function's."""

    def test_a_level_at_the_threshold_clears(self) -> None:
        self.assertTrue(
            cost_gate.clears_cost(price=68.54, entry_price=68.0, units=10.0, costs=_costs())
        )

    def test_a_level_below_the_threshold_is_refused(self) -> None:
        self.assertFalse(
            cost_gate.clears_cost(price=68.53, entry_price=68.0, units=10.0, costs=_costs())
        )

    def test_an_unpriceable_tranche_fires_anyway(self) -> None:
        # FAILS OPEN, the daemon's stance: refusing an exit on unusable data
        # strands a live position, while the disaster stop still guards it.
        self.assertTrue(
            cost_gate.clears_cost(price=68.0, entry_price=68.0, units=0.0, costs=_costs())
        )

    def test_the_stated_edge_moves_the_threshold(self) -> None:
        # The section 5 example states 5.0 rather than the daemon's 50.0, so the
        # stated value has to be the one that is read.
        cheap = cost_gate.min_profitable_exit_price(
            entry_price=68.0, units=100.0, costs=_costs(edge_bps=5.0)
        )
        self.assertEqual(cheap, 68.1428)


if __name__ == "__main__":
    unittest.main()
