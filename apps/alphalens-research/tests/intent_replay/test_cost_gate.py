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
  68.7888 instead of 68.4488. Those eight rows are a SAME-CURRENCY run, where
  both arms of ``Fx`` return their argument and no FX term exists; #1592's own
  rows state the cross-currency facts and reach the daemon's ``fx_applies``
  numbers (``TheStatedFxLegTest``).
* ``exit_edge_min_bps`` must be stated as 50.0 in these rows. The daemon reads
  its own module constant ``EXIT_EDGE_MIN_BPS = 50.0``; the replay reads the
  stated value, which the section 5 example states as 5.0.

The knee of the fee card is ``min_commission / commission_rate`` = 1250. Above
it the fee is pure ad valorem, which is why the "above the knee" and "minimum
off" rows land on the SAME threshold.
"""

from __future__ import annotations

import dataclasses
import math
import random
import unittest

from alphalens_pipeline.brokers.automanager import costs as daemon_costs
from intent_replay import cost_gate
from intent_replay.config import Costs
from intent_replay.fx import Fx
from intent_replay.units import BPS, FRACTION, Quantity

RATE, MIN_COMMISSION, EDGE_BPS = 0.0008, 1.0, 50.0
BPS_PER_UNIT = 1e4

# The daemon's own FX round-trip rate, STATED here rather than imported: the
# replay reads it off the configuration (spec section 2.1), and these rows only
# agree with the daemon's numbers because the caller stated the same value.
FX_ROUND_TRIP = 0.005
# USD per one EUR. Chosen so ``(100.0 / MID) * MID == 100.0`` and the same for
# 15.0, which is what lets a golden row from the daemon be asserted with
# equality rather than a tolerance: the replay is handed account-currency units
# and multiplies by the rate to reach the share count the daemon was given.
MID = 1.08
# The card the golden rows were measured on. Imported for the PARITY rows only:
# the daemon is an oracle at test time, which is this file's own practice and
# not a production import -- the engine's allow-list still admits
# ``broker_contract`` alone.
CARD = daemon_costs.VenueFeeCard(
    commission_rate=RATE, min_commission=MIN_COMMISSION, label="stated"
)

SAME = Fx(
    account_currency="EUR",
    instrument_currency="EUR",
    rate=None,
    round_trip_cost_rate=None,
    sizing_buffer_pct=None,
)
CROSS = Fx(
    account_currency="EUR",
    instrument_currency="USD",
    rate=MID,
    round_trip_cost_rate=FX_ROUND_TRIP,
    sizing_buffer_pct=1.0,
)


def _costs(
    *,
    min_commission_applies: bool = True,
    edge_bps: float = EDGE_BPS,
    fx: Fx = SAME,
) -> Costs:
    return Costs(
        commission_rate=Quantity(value=RATE, unit=FRACTION),
        min_commission=Quantity(value=MIN_COMMISSION, unit="USD"),
        min_commission_applies=min_commission_applies,
        exit_edge_min_bps=Quantity(value=edge_bps, unit=BPS),
        fx=fx,
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


class TheThresholdAboveTheKneeDoesNotSeeTheNotionalTest(unittest.TestCase):
    """Why the stated FX rate is almost unobservable here (#1592).

    The rows pin the MECHANISM the decision rests on, and they state the
    notional directly rather than through ``Fx``: a rate reaches this module
    only by scaling the notional, so applying it in the test is applying it
    where the module would. Above the knee the notional cancels, which is why
    an inverted rate leaves the verdict bit-identical and why section 5.2.1 puts
    the direction in the unit token rather than trusting the number. The rows
    that go through the stated facts are ``TheStatedFxLegTest``'s.
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


class TheStatedFxLegTest(unittest.TestCase):
    """The FX leg, priced from the STATED rate (#1592, spec section 5.2.1).

    Two things change at once on a cross-currency run and the tests below keep
    them apart. The gate prices the INSTRUMENT's currency, so the account-currency
    units it is handed are multiplied by the rate; and the round trip pays the
    stated conversion cost on top of commission. Each row names which.

    Every threshold is a value the DAEMON returned, with
    ``CostGateFacts(fx_applies=True, ...)`` and the same card, measured
    2026-10-02. The share counts the daemon was given are 100 and 15; the replay
    is handed those divided by the rate.
    """

    ENTRY = 68.0
    DRAWS = 20_000
    SEED = 20261002

    def test_the_cross_currency_threshold_is_the_daemons_fx_aware_row(self) -> None:
        # Above the knee: 1.08 x 100 x 68.00 = 7344 against a knee of 1250.
        got = cost_gate.min_profitable_exit_price(
            entry_price=self.ENTRY, units=100.0 / MID, costs=_costs(fx=CROSS)
        )
        self.assertEqual(got, 68.78880000000001)

    def test_it_is_the_daemons_row_below_the_knee_too(self) -> None:
        # 1.08 x 15 x 68.00 = 1101.6, under the knee, so the per-fill minimum
        # binds as well -- which is the arm where the RATE is observable.
        got = cost_gate.min_profitable_exit_price(
            entry_price=self.ENTRY, units=15.0 / MID, costs=_costs(fx=CROSS)
        )
        self.assertEqual(got, 68.81333333333333)

    def test_the_same_currency_row_beside_it_is_unmoved(self) -> None:
        # The discriminator: the same two calls on a same-currency run answer
        # exactly what they answered before this issue, so a leaking rate or a
        # leaking cost rate goes red here.
        for units, expected in ((100.0, 68.44879999999999), (15.0, 68.47333333333334)):
            with self.subTest(units):
                got = cost_gate.min_profitable_exit_price(
                    entry_price=self.ENTRY, units=units, costs=_costs()
                )
                self.assertEqual(got, expected)

    def test_the_fee_is_the_daemons_fee_bit_for_bit_in_both_arms(self) -> None:
        # The grouping is why this is a parity test and not an identity. In bps
        # the FX term alone is the stated rate exactly, at every notional,
        # because the notional cancels -- but the daemon sums the two round
        # trips and divides ONCE, so what a reader can subtract out of the
        # published fee is not the term. ``tests/property/test_broker_costs_properties``
        # records that for the daemon in its own docstring, as a claim it
        # deliberately does not make, and this copy inherits it: measured here,
        # the cross-minus-same difference departs from 50 bps on 43.3 per cent
        # of 200 000 notionals drawn log-uniform over [1e-6, 1e9], and by
        # 7.0e15 ulp at 1e-200, where two whole minimum commissions swamp the
        # term entirely. So the claim this module can make is AGREEMENT with the
        # daemon, and the grouping is copied to earn it: 0 of 200 000 draws
        # differ in either arm.
        rnd = random.Random(self.SEED)
        for _ in range(self.DRAWS):
            notional = math.exp(rnd.uniform(math.log(1e-6), math.log(1e9)))
            with self.subTest(notional=notional):
                self.assertEqual(
                    cost_gate.round_trip_fee_bps(notional, costs=_costs(fx=CROSS)),
                    daemon_costs.round_trip_fee_bps(notional, fx_applies=True, card=CARD),
                )
                self.assertEqual(
                    cost_gate.round_trip_fee_bps(notional, costs=_costs()),
                    daemon_costs.round_trip_fee_bps(notional, fx_applies=False, card=CARD),
                )

    def test_the_two_arms_never_coincide_so_that_parity_has_power(self) -> None:
        # Without this the row above would pass on a module that ignored the
        # stated facts and answered the same fee twice. Measured: on the same
        # 200 000 draws the same-currency fee equals the daemon's FX-aware fee
        # on none of them.
        rnd = random.Random(self.SEED)
        for _ in range(self.DRAWS):
            notional = math.exp(rnd.uniform(math.log(1e-6), math.log(1e9)))
            with self.subTest(notional=notional):
                self.assertNotEqual(
                    cost_gate.round_trip_fee_bps(notional, costs=_costs()),
                    daemon_costs.round_trip_fee_bps(notional, fx_applies=True, card=CARD),
                )

    def test_a_non_positive_notional_pays_no_leg_either(self) -> None:
        # The daemon's own arm returns a zero fee rather than dividing, and the
        # FX term is inside that guard rather than beside it.
        self.assertEqual(cost_gate.round_trip_fee_bps(0.0, costs=_costs(fx=CROSS)), 0.0)
        self.assertEqual(cost_gate.round_trip_fee_bps(-1.0, costs=_costs(fx=CROSS)), 0.0)

    def test_the_rate_alone_moves_the_verdict_where_the_minimum_binds(self) -> None:
        # The CONVERSION on its own, with the cost rate switched off: the gate
        # is handed one unit count and prices two different notionals, so a
        # tranche that clears on the account-currency reading is refused on the
        # instrument-currency one. This is the half the old divergence entry
        # named, and it is now priced rather than reported.
        rate_only = dataclasses.replace(CROSS, round_trip_cost_rate=0.0)
        units = 15.0 / MID
        account = cost_gate.min_profitable_exit_price(
            entry_price=self.ENTRY, units=units, costs=_costs()
        )
        instrument = cost_gate.min_profitable_exit_price(
            entry_price=self.ENTRY, units=units, costs=_costs(fx=rate_only)
        )
        self.assertIsNotNone(account)
        self.assertIsNotNone(instrument)
        self.assertLess(instrument, account)
        # And the verdict, not only the number: a level between the two clears
        # one reading and not the other.
        between = (instrument + account) / 2.0
        self.assertTrue(
            cost_gate.clears_cost(
                price=between, entry_price=self.ENTRY, units=units, costs=_costs(fx=rate_only)
            )
        )
        self.assertFalse(
            cost_gate.clears_cost(
                price=between, entry_price=self.ENTRY, units=units, costs=_costs()
            )
        )


if __name__ == "__main__":
    unittest.main()
