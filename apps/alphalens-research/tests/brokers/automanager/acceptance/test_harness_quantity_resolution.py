"""What the acceptance harness can and CANNOT observe about quantity.

A green acceptance run says the manager behaved on the venues the harness can
express. It does NOT say the manager would behave on a venue that trades
fractions — and the difference is not visible from the pass count, which is
exactly why it belongs in a test rather than in a comment.

MEASURED ON THIS TREE. `FakeBroker` treats anything within `_QTY_EPS` of zero
as no position at all. So a rail that sold 0.6 of a 1.0-share holding — leaving
0.4 shares alive with no stop on them — makes the position VANISH here, and the
suite cannot tell that apart from a clean close. On a fractional venue that is
a naked position reported as success.

WHY THAT IS STILL THE RIGHT BEHAVIOUR TODAY. Every venue on the rail trades
whole shares (Saxo, live-probed on our own cohort: `MinimumTradeSize 1`,
`IncrementSize 1`, `FractionalOrderEnabled false`), so half a share genuinely is
not a position. The defect was never the value — it was that the harness
declared its OWN `0.5` under a comment claiming it mirrored the shared
constant. Nothing kept them equal, so the migration to an adapter-reported
lattice would have moved the rail and left the instrument behind, still
measuring the old world and still reporting green.

Since #1125 the value is imported, so the harness follows the rail by
construction. These tests pin the resolution limit that remains, so a later
reader cannot mistake acceptance-green for fractional coverage.
"""

from __future__ import annotations

import unittest

from broker_contract.constants import QTY_PRECISION

from . import fake_broker as fake_broker_module
from .fake_broker import FakeBroker


class TestTheHarnessReadsTheSharedPrecision(unittest.TestCase):
    def test_the_harness_epsilon_is_the_rail_constant_itself(self) -> None:
        # Identity, not equality: two objects that happen to be 0.5 today is
        # the state this closes. `assertIs` fails the moment someone
        # re-declares the value here, even to the same number.
        #
        # Read off the MODULE rather than imported directly, so the assertion
        # is about what `fake_broker` actually binds — importing the name here
        # would only prove that this file can reach the constant.
        self.assertIs(fake_broker_module._QTY_EPS, QTY_PRECISION)


class TestTheResolutionLimitThatRemains(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = FakeBroker()
        self.uic = self.broker.uic_of("XYZ")

    def test_a_position_at_or_below_the_epsilon_cannot_be_expressed(self) -> None:
        for shares in (0.3, 0.5):
            with self.subTest(shares=shares):
                self.broker.set_position("XYZ", shares, avg_price=50.0)
                self.assertIsNone(
                    self.broker._positions.get(self.uic),
                    "the harness cannot hold a position this small — a scenario "
                    "written at this size silently tests nothing",
                )

    def test_a_position_above_the_epsilon_is_held_fractional_or_not(self) -> None:
        # Guard against reading the limit as "fractions are unsupported". They
        # are supported ABOVE the epsilon; the limit is a floor, not a lattice.
        for shares in (0.51, 0.669, 1.4):
            with self.subTest(shares=shares):
                self.broker.set_position("XYZ", shares, avg_price=50.0)
                held = self.broker._positions.get(self.uic)
                assert held is not None, f"{shares} should be expressible"
                self.assertAlmostEqual(held.quantity, shares, places=9)

    def test_a_sell_that_leaves_a_sub_epsilon_remainder_reads_as_a_clean_close(self) -> None:
        # THE false green, pinned. 1.0 - 0.6 = 0.4 shares still held, and the
        # harness reports no position. Whether that remainder kept its stop is
        # unobservable here, so no acceptance test may claim to cover it.
        self.broker.set_position("XYZ", 1.0, avg_price=50.0)
        self.broker.place_market_order(self.uic, "SELL", 0.6)
        self.assertIsNone(self.broker._positions.get(self.uic))
        self.assertEqual(self.broker.get_long_positions(), [])

    def test_the_limit_is_acceptable_only_because_the_venue_step_is_one_share(self) -> None:
        # WHY the limit above is tolerable, stated as the derivation rather
        # than as the number. A 0.6-share sell is not an order any connected
        # venue would accept, so the shape the harness cannot observe is also a
        # shape the rail cannot produce.
        #
        # Written against WHOLE_SHARE_STEP so it fails for the right reason:
        # not "the constant changed" but "the precision is no longer half of
        # this venue's step". When a fractional venue arrives, that is the
        # signal to revisit this file rather than to edit the number here.
        whole_share_step = 1.0
        self.assertEqual(QTY_PRECISION, whole_share_step / 2.0)


if __name__ == "__main__":
    unittest.main()
