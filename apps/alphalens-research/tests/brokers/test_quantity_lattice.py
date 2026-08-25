"""The quantity lattice: one place that knows how many shares are sayable.

Today whole-share arithmetic — a Saxo property, not a system one — is baked
into the contract (`sizing.py` `math.floor`) and the pipeline (`round()` in
`live_exit_engine`), under names that read as universal. Design memo:
`docs/research/broker_quantity_quantization_design_2026_08_25.md`.

These pin the contract of the pure leaf. Nothing imports it yet, so the tests
ARE the specification.

Two rules the industry learned the hard way and this module encodes:

- **Floor the MAGNITUDE, never round to nearest, never floor a signed
  negative.** `floor(-1.23) == -2` moves away from zero and *increases* a sale.
  ccxt truncates, Hummingbot floor-divides, LEAN works on the absolute amount
  and restores the sign.
- **Precision is not step.** Two decimal places permit `1.03`; a step of `0.05`
  does not. They are separate fields doing separate jobs — `precision` is the
  exponent that makes the lattice arithmetic exact in float, `step` is the
  lattice.
"""

from __future__ import annotations

import unittest

from broker_contract.quantity import (
    QuantityLattice,
    allocate_units,
    covers,
    exceeds,
    is_on_lattice,
    is_tradable,
    lattice_units,
    quantize_down,
    same_quantity,
    split_position,
)

# Saxo, live-probed on our own cohort: IncrementSize 1, MinimumTradeSize 1,
# AmountDecimals 0, FractionalOrderEnabled false.
WHOLE = QuantityLattice(step=1.0, min_qty=1.0, precision=0)
# A fractional venue, for the shapes today's code cannot express.
MILLI = QuantityLattice(step=0.001, min_qty=0.001, precision=3)
# A step that is NOT a power of ten — the case Decimal.quantize() alone cannot
# handle, and the reason `precision` and `step` are separate fields.
NICKEL = QuantityLattice(step=0.05, min_qty=0.05, precision=2)


class TestQuantizeDown(unittest.TestCase):
    def test_never_increases_the_magnitude(self) -> None:
        # THE safety property. The measured incident was `round(0.669) == 1`
        # selling a share that was not held; no rounding-up primitive may exist
        # anywhere on this rail.
        for lattice in (WHOLE, MILLI, NICKEL):
            for qty in (0.0001, 0.51, 0.669, 1.4, 2.5, 99.999, 1234.5678):
                with self.subTest(lattice=lattice.step, qty=qty):
                    self.assertLessEqual(abs(quantize_down(qty, lattice)), abs(qty) + 1e-12)

    def test_the_incident_quantity_floors_to_zero_on_a_whole_share_venue(self) -> None:
        # 0.669 shares is not a tradable quantity where shares are whole.
        # `round()` said 1. This says 0, so nothing is sold and no stop is
        # cancelled on the strength of a phantom share.
        self.assertEqual(quantize_down(0.669, WHOLE), 0.0)

    def test_a_fractional_venue_keeps_the_fraction(self) -> None:
        self.assertAlmostEqual(quantize_down(0.669, MILLI), 0.669, places=9)

    def test_a_negative_quantity_floors_toward_zero_not_away_from_it(self) -> None:
        # math.floor(-1.23) == -2 would INCREASE a sale. The magnitude is
        # floored and the sign restored.
        self.assertAlmostEqual(quantize_down(-1.23, WHOLE), -1.0, places=9)
        self.assertAlmostEqual(quantize_down(-1.23, NICKEL), -1.20, places=9)

    def test_on_lattice_values_are_fixed_points(self) -> None:
        for lattice, qty in ((WHOLE, 46.0), (MILLI, 0.125), (NICKEL, 1.35)):
            with self.subTest(step=lattice.step):
                self.assertAlmostEqual(quantize_down(qty, lattice), qty, places=9)

    def test_is_idempotent(self) -> None:
        for lattice in (WHOLE, MILLI, NICKEL):
            for qty in (0.669, 1.4, 99.999, 1234.5678):
                once = quantize_down(qty, lattice)
                self.assertAlmostEqual(quantize_down(once, lattice), once, places=9)

    def test_a_representation_artefact_is_absorbed(self) -> None:
        # `0.1 + 0.2` is `0.30000000000000004` — a binary-float artefact of
        # `0.3`, about 1e-16 relative. It must scale as `0.3`, or a genuinely
        # on-lattice quantity would lose a whole step to a naive
        # divide-and-floor.
        self.assertAlmostEqual(quantize_down(0.3, MILLI), 0.3, places=12)
        self.assertAlmostEqual(quantize_down(0.1 + 0.2, MILLI), 0.3, places=12)
        # The true artefact of 3.0 at one decimal place.
        self.assertAlmostEqual(quantize_down(2.9999999999999996, QuantityLattice(0.1, 0.1, 1)), 3.0)

    def test_a_real_value_just_below_a_step_is_NOT_absorbed(self) -> None:
        # The other side of that boundary, and the one that keeps the safety
        # property honest. A literal 2.9999999999 is not an artefact — it is a
        # real value 1e-10 below three, and rounding it UP would be the very
        # thing this module exists to forbid.
        self.assertAlmostEqual(quantize_down(2.9999999999, QuantityLattice(0.1, 0.1, 1)), 2.9)
        self.assertAlmostEqual(quantize_down(0.6689999999, MILLI), 0.668, places=12)

    def test_a_two_decimal_precision_does_not_admit_an_off_step_value(self) -> None:
        # Precision does not imply step: 1.03 has two decimals but is not a
        # multiple of 0.05. This is the distinction ccxt users routinely trip on.
        self.assertAlmostEqual(quantize_down(1.03, NICKEL), 1.00, places=9)

    def test_a_large_quantity_just_below_a_step_boundary_does_not_snap_up(self) -> None:
        # The tolerance that absorbs representation error must not GROW past a
        # step. A tolerance stated as a fixed fraction of the quantity does
        # exactly that: at a million shares, 1e-12 of the quantity is 1e-6 —
        # wide enough to swallow a real 5e-7 gap and hand back a share that is
        # not held. Representation error scales with the float's own
        # resolution, so the tolerance must too.
        qty = 1_000_000.0 - 5e-7
        self.assertEqual(quantize_down(qty, WHOLE), 999_999.0)
        self.assertLess(quantize_down(qty, WHOLE), qty)

    def test_the_slack_can_never_reach_a_step(self) -> None:
        # The honest statement of the safety property, and the reason the
        # tolerance is stated in ULPs. Absorbing representation error means the
        # result CAN exceed the input — but only by the float's own resolution
        # there, ~7e-15 of the quantity. Two bounds, both pinned:
        #
        #   1. the overshoot stays proportional to the QUANTITY, so it does not
        #      grow relative to itself the way a fixed relative tolerance does;
        #   2. it stays far below half a STEP, which is the unit that matters —
        #      crossing one is selling a share that is not held. It would take
        #      ~1e14 steps' worth of position before the slack got that wide.
        for lattice in (WHOLE, MILLI, NICKEL):
            for qty in (0.9999999999, 999.9999999999, 9_214_075.715999965, 1e7 - 1e-9):
                with self.subTest(step=lattice.step, qty=qty):
                    overshoot = quantize_down(qty, lattice) - qty
                    self.assertLess(overshoot, abs(qty) * 1e-13)
                    self.assertLess(overshoot, lattice.step / 2.0)

    def test_the_no_exceed_property_holds_across_magnitudes(self) -> None:
        # Same trap swept: for each lattice, a value one hair below every
        # decade boundary. Random draws never land here, which is why the
        # original sweep measured a clean zero and still shipped the defect.
        for lattice in (WHOLE, MILLI, NICKEL):
            for decade in (1e2, 1e3, 1e4, 1e5, 1e6, 1e7):
                for gap in (5e-7, 1e-6, 1e-4):
                    qty = decade - gap
                    with self.subTest(step=lattice.step, qty=qty):
                        self.assertLessEqual(quantize_down(qty, lattice), qty)


class TestPredicates(unittest.TestCase):
    def test_same_quantity_is_half_a_step(self) -> None:
        # THE derivation that makes the eventual migration reviewable:
        # QTY_PRECISION = 0.5 was never an arbitrary epsilon, it is step/2 at
        # step 1.0. So every existing comparison keeps its exact meaning on Saxo.
        self.assertTrue(same_quantity(46.0, 45.9999999, WHOLE))
        self.assertTrue(same_quantity(46.0, 46.4, WHOLE))
        self.assertFalse(same_quantity(46.0, 46.6, WHOLE))
        # And on a finer venue the same predicate is correspondingly finer.
        self.assertFalse(same_quantity(0.125, 0.126, MILLI))

    def test_is_tradable_fails_closed_on_anything_unusable(self) -> None:
        for bad in (None, float("nan"), float("inf"), -1.0, 0.0, True, "3", object()):
            with self.subTest(bad=bad):
                self.assertFalse(is_tradable(bad, WHOLE))

    def test_is_tradable_answers_by_the_venue_not_by_the_size_of_the_fraction(self) -> None:
        # The failure class this kills: today 0.3 is "not real" and 0.669 IS,
        # because both are compared to a bare 0.5. The answer must depend on
        # the venue alone.
        self.assertFalse(is_tradable(0.3, WHOLE))
        self.assertFalse(is_tradable(0.669, WHOLE))
        self.assertTrue(is_tradable(0.3, MILLI))
        self.assertTrue(is_tradable(0.669, MILLI))

    def test_below_the_minimum_is_not_tradable_even_when_on_lattice(self) -> None:
        lattice = QuantityLattice(step=0.001, min_qty=0.5, precision=3)
        self.assertTrue(is_on_lattice(0.1, lattice))
        self.assertFalse(is_tradable(0.1, lattice))
        self.assertTrue(is_tradable(0.5, lattice))

    def test_covers_and_exceeds_are_tolerant_by_half_a_step(self) -> None:
        self.assertTrue(covers(46.0, 45.9999999, WHOLE))
        self.assertFalse(covers(45.0, 46.0, WHOLE))
        self.assertTrue(exceeds(47.0, 46.0, WHOLE))
        self.assertFalse(exceeds(46.0, 45.9999999, WHOLE))


class TestAllocateUnits(unittest.TestCase):
    def test_allocation_sums_to_the_total_exactly(self) -> None:
        # The property that makes an exit ladder closeable. Flooring each
        # weight independently loses units; rounding to nearest overshoots.
        for total in (1, 3, 6, 27, 100, 999):
            for weights in ((1.0,), (0.5, 0.5), (1 / 3, 1 / 3, 1 / 3), (0.6, 0.4)):
                with self.subTest(total=total, weights=weights):
                    parts = allocate_units(total, weights)
                    self.assertEqual(sum(parts), total)
                    self.assertEqual(len(parts), len(weights))
                    self.assertTrue(all(p >= 0 for p in parts))

    def test_the_thirds_case_that_produces_a_remainder(self) -> None:
        # 100 units over three equal weights is 33.33 each; the leftover unit
        # goes by largest fractional remainder, never silently dropped.
        self.assertEqual(sum(allocate_units(100, (1 / 3, 1 / 3, 1 / 3))), 100)

    def test_a_single_unit_over_three_tranches_is_not_three_zeros(self) -> None:
        # Today `round(1 * 0.33)` is 0 for every tranche and the position can
        # never exit. Someone must get the unit.
        parts = allocate_units(1, (1 / 3, 1 / 3, 1 / 3))
        self.assertEqual(sum(parts), 1)

    def test_zero_total_allocates_nothing(self) -> None:
        self.assertEqual(allocate_units(0, (0.5, 0.5)), (0, 0))


class TestSplitPosition(unittest.TestCase):
    def test_the_parts_sum_back_to_the_quantized_whole(self) -> None:
        parts = split_position(6.0, (1 / 3, 1 / 3, 1 / 3), WHOLE)
        self.assertAlmostEqual(sum(parts), 6.0, places=9)

    def test_a_fractional_position_splits_on_a_fractional_venue(self) -> None:
        parts = split_position(0.669, (1 / 3, 1 / 3, 1 / 3), MILLI)
        self.assertAlmostEqual(sum(parts), 0.669, places=9)
        self.assertTrue(all(p > 0 for p in parts))

    def test_a_single_share_over_three_tranches_still_exits(self) -> None:
        # The measured [0, 0, 0] case. One of the tranches must carry the share.
        parts = split_position(1.0, (1 / 3, 1 / 3, 1 / 3), WHOLE)
        self.assertAlmostEqual(sum(parts), 1.0, places=9)

    def test_the_parts_carry_no_float_dust(self) -> None:
        # `quantize_down` rounds to the venue precision so a caller never sees
        # `3.0000000000000004` on the wire; the split has to agree, or the same
        # share count reads differently depending on which function produced it.
        for part in split_position(0.7, (1 / 3, 1 / 3, 1 / 3), MILLI):
            with self.subTest(part=part):
                self.assertEqual(part, round(part, MILLI.precision))

    def test_never_splits_more_than_the_position_holds(self) -> None:
        for qty in (0.669, 1.0, 6.0, 27.0):
            for lattice in (WHOLE, MILLI):
                with self.subTest(qty=qty, step=lattice.step):
                    parts = split_position(qty, (0.5, 0.5), lattice)
                    self.assertLessEqual(sum(parts), qty + 1e-12)


class TestLatticeConstruction(unittest.TestCase):
    def test_a_non_positive_step_is_refused(self) -> None:
        for step in (0.0, -1.0):
            with self.subTest(step=step), self.assertRaises(ValueError):
                QuantityLattice(step=step, min_qty=1.0, precision=0)

    def test_a_step_the_precision_cannot_express_is_refused(self) -> None:
        # The vendor contradicting itself: a 0.001 step with 2 decimal places.
        # Caught once, at construction, rather than assumed at 50 call sites.
        with self.assertRaises(ValueError):
            QuantityLattice(step=0.001, min_qty=0.001, precision=2)

    def test_a_step_that_rounds_to_zero_units_is_refused(self) -> None:
        # The self-contradiction the first check let through: a 1e-10 step at
        # zero decimal places is "within 1e-9 of an integer" — the integer
        # being ZERO. The lattice was accepted and every later division by the
        # step count raised ZeroDivisionError out of a pure function, past
        # every `except BrokerError` on the rail. A lattice must be at least
        # one whole unit wide.
        with self.assertRaises(ValueError):
            QuantityLattice(step=1e-10, min_qty=0.0, precision=0)

    def test_a_refused_lattice_cannot_reach_the_arithmetic(self) -> None:
        # Stated as the property rather than the instance: whatever the venue
        # says, either construction refuses it or the arithmetic runs.
        for step, precision in ((1e-10, 0), (1e-4, 2), (0.5, 0)):
            with self.subTest(step=step, precision=precision):
                try:
                    lattice = QuantityLattice(step=step, min_qty=0.0, precision=precision)
                except ValueError:
                    continue
                self.assertIsInstance(lattice_units(1.0, lattice), int)

    def test_lattice_units_counts_whole_steps(self) -> None:
        self.assertEqual(lattice_units(6.0, WHOLE), 6)
        self.assertEqual(lattice_units(0.669, WHOLE), 0)
        self.assertEqual(lattice_units(0.669, MILLI), 669)
        self.assertEqual(lattice_units(1.03, NICKEL), 20)


if __name__ == "__main__":
    unittest.main()
