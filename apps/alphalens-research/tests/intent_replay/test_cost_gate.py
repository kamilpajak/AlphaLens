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

import math
import unittest

from intent_replay import cost_gate
from intent_replay.config import Costs
from intent_replay.units import BPS, FRACTION, Quantity

RATE, MIN_COMMISSION, EDGE_BPS = 0.0008, 1.0, 50.0
BPS_PER_UNIT = 1e4


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


class TheThresholdAboveTheKneeDoesNotSeeTheNotionalTest(unittest.TestCase):
    """Why a stated FX rate will be almost unobservable here (#1592).

    No FX term exists yet: ``fx_applies`` is refused in ``config``, so these
    rows pin the MECHANISM the decision rests on rather than the decision. The
    mechanism is that a rate reaches this module only by scaling the notional,
    and above the knee the notional cancels. Section 8.1 publishes the
    consequence, so these rows keep its numbers honest; the FX-aware rows arrive
    with the keys.
    """

    KNEE = MIN_COMMISSION / RATE
    ENTRY = 66.50
    BUDGET = 8000.0  # the standard manual-pick position, in the account currency
    # The two candidate rates of section 8.1. The TEST applies them to the
    # budget, because the gate takes a notional and has no FX input yet.
    USD_PER_PLN, PLN_PER_USD = 1.0 / 3.7, 3.7

    def _threshold(self, notional: float) -> float | None:
        return cost_gate.min_profitable_exit_price(
            entry_price=self.ENTRY, units=notional / self.ENTRY, costs=_costs()
        )

    def test_the_threshold_is_one_value_across_notionals_spanning_a_factor_of_a_thousand(
        self,
    ) -> None:
        above = (self.KNEE, 2162.16, 8000.0, 29600.0, 987648.0, self.KNEE * 1000.0)
        self.assertEqual(len({self._threshold(notional) for notional in above}), 1)
        self.assertEqual(self._threshold(8000.0), 66.93889999999999)

    def test_a_rate_and_its_inverse_give_notionals_the_gate_cannot_tell_apart(
        self,
    ) -> None:
        # The named witness for section 8.1's published pair, subsumed by the
        # row above and kept because a published number needs one. A rate stated
        # upside down is 13.7x wrong on this account and still leaves the
        # verdict bit-identical, which is why section 5.2.1 puts the direction
        # in the unit and prints the derived notional rather than trusting the
        # rate.
        self.assertEqual(
            self._threshold(self.BUDGET * self.USD_PER_PLN),
            self._threshold(self.BUDGET * self.PLN_PER_USD),
        )

    def test_below_the_knee_that_same_pair_prices_differently(self) -> None:
        # The existence control: without it, a gate that ignored the notional
        # everywhere would satisfy the two rows above.
        small = 500.0
        self.assertLess(small * self.USD_PER_PLN, self.KNEE)
        self.assertNotEqual(
            self._threshold(small * self.USD_PER_PLN),
            self._threshold(small * self.PLN_PER_USD),
        )

    def test_the_fee_is_pure_ad_valorem_to_within_one_ulp_not_exactly(self) -> None:
        # The invariance is EXACT at the threshold and only near-exact in the
        # fee: 15.02 per cent of 200 000 notionals drawn above the knee differ
        # from 2 x rate, by at most 1.0 ulp. Stating the claim about the fee
        # would be stating it one step away from where it holds.
        ad_valorem_bps = 2.0 * RATE * BPS_PER_UNIT
        wobbling = 1250.0000001
        self.assertNotEqual(cost_gate.round_trip_fee_bps(wobbling, costs=_costs()), ad_valorem_bps)
        self.assertLessEqual(
            abs(cost_gate.round_trip_fee_bps(wobbling, costs=_costs()) - ad_valorem_bps),
            math.ulp(ad_valorem_bps),
        )
