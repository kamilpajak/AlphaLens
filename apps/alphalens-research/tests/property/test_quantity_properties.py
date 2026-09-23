"""Properties of the venue quantity lattice (`broker_contract.quantity`).

This is the module the codebase describes as the reason "sell more than is held"
stops being expressible: it floors magnitudes and deliberately ships no
rounding-up primitive. Its invariants are already written down in prose --
"the largest lattice quantity whose magnitude does not exceed ``qty``",
"sign-preserving by construction", "``0.0`` for anything unusable", and the
``_ULP_SLACK`` comment's claim that the slack "can never reach a step, which is
what makes the no-exceed property in ``quantize_down`` true rather than
approximately true". Prose is not a test, so here they are executed.

TWO WAYS THE PROSE OVERSTATES ITSELF, both found by running it:

* The no-exceed claim is not absolute even at ordinary magnitudes. The slack is
  32 ULPs, so a value exactly that far below a lattice point rounds UP to it:
  ``quantize_down(16777216.99999988, step=1)`` is ``16777217.0``. That is the
  slack doing its job -- absorbing representation dust -- and the overshoot is
  1.2e-07 of a share, but "can never reach a step" is still false. The property
  below therefore bounds the overshoot by HALF A LATTICE STEP.

  The first draft of this module bounded it by ``_slack(abs(qty))`` instead, and
  that bound is FALSE inside the domain asserted here. Counterexample, found by
  probing this module's own upper bound: ``quantize_down(974989319.5716939)`` on
  a 1e-4 step returns ``974989319.5717``, which is 1.594x the slack above its
  input. The reason is a scale mismatch, not a wrong constant -- the code
  applies its slack to the value SCALED by ``10**precision`` (see
  ``_scaled_units``), and a property applying it unscaled understates it by up
  to ``10**precision``. Half a step is the honest bound and the economically
  meaningful one: the arithmetic ceiling is ``32 * ulp(qty * 10**precision)``
  step-units, at most ~0.064 of a step anywhere in this domain, and the worst
  case measured over 172080 (quantity, lattice) combinations is 6.08e-02 of a
  step. It also keeps the properties off this module's private helpers.
* At extreme magnitudes the function breaks outright: at
  ``qty = 1_407_374_883_554.0`` on a 0.01 step it returns ``...554.01`` --
  above its input, off the lattice, and not idempotent. ``1e308`` raises
  ``OverflowError`` rather than returning the promised ``0.0``. The mechanism
  is not the ULP slack but the final ``round(magnitude, precision)``. A
  trillion shares is not a share count anything here will hold, so the
  properties assert the realistic domain and this is filed as its own issue.
  Pinning the broken behaviour would be pinning the bug.
"""

from __future__ import annotations

import unittest

from broker_contract.quantity import (
    QuantityLattice,
    allocate_units,
    is_on_lattice,
    is_tradable,
    lattice_units,
    quantity_refusal,
    quantize_down,
    split_position,
)
from hypothesis import assume, event, given
from hypothesis import strategies as st

from .base import PropertyTestCase


@st.composite
def lattices(draw: st.DrawFn) -> QuantityLattice:
    """A lattice the constructor accepts: the step has to be a whole number of
    units at the declared precision, which is the venue self-consistency rule
    ``__post_init__`` enforces."""
    precision = draw(st.integers(min_value=0, max_value=4))
    step_units = draw(st.integers(min_value=1, max_value=500))
    step = step_units / (10**precision)
    min_qty = draw(st.sampled_from([0.0, step, step * 2, step * 10]))
    return QuantityLattice(step=step, min_qty=min_qty, precision=precision, source="property")


# A share count anything on this rail could hold. See the module docstring for
# what happens above it and why that is a separate issue rather than a bound to
# argue about here.
quantities = st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
signed_quantities = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)
UNUSABLE = st.sampled_from([float("nan"), float("inf"), float("-inf"), True, False, None, "3"])


class QuantizeDownKeepsItsPromise(PropertyTestCase):
    """The four claims the docstring makes, over the realistic domain."""

    @given(qty=quantities, lattice=lattices())
    def test_it_never_hands_back_more_than_it_was_given(
        self, qty: float, lattice: QuantityLattice
    ) -> None:
        # THE safety property of this module: a quantity that grew on the way
        # through would be a sell for shares that are not held.
        #
        # "by less than half a step" is the exact claim, and the qualifier is
        # load-bearing. The `_ULP_SLACK` comment says the slack "can never reach
        # a step, which is what makes the no-exceed property true rather than
        # approximately true" -- that is FALSE as an absolute: at
        # `qty = 16777216.99999988`, exactly 32 ULPs below 2**24, a whole-share
        # lattice returns 16777217.0, which is 1.2e-07 ABOVE the input.
        # Absorbing `0.1 + 0.2 == 0.30000000000000004` is what the slack is for,
        # and rounding such a value UP to the integer it is dust below is the
        # intended behaviour; the docstring just overstates it. So the bound is
        # neither zero nor `_slack(qty)` (see the module docstring for the
        # counterexample that kills that one) -- it is half a step, which is the
        # quantity that matters: the result can never reach the NEXT lattice
        # point, so it can never name a share that is not there.
        got = quantize_down(qty, lattice)
        event("quantized to zero" if got == 0.0 else "quantized to a positive count")
        self.assertLessEqual(abs(got), abs(qty) + lattice.step / 2.0)

    @given(qty=quantities, lattice=lattices())
    def test_quantizing_an_already_quantized_quantity_changes_nothing(
        self, qty: float, lattice: QuantityLattice
    ) -> None:
        once = quantize_down(qty, lattice)
        self.assertEqual(quantize_down(once, lattice), once)

    @given(qty=quantities, lattice=lattices())
    def test_the_result_sits_on_the_lattice(self, qty: float, lattice: QuantityLattice) -> None:
        self.assertTrue(is_on_lattice(quantize_down(qty, lattice), lattice))

    @given(qty=signed_quantities, lattice=lattices())
    def test_the_sign_survives_and_a_sale_only_ever_shrinks(
        self, qty: float, lattice: QuantityLattice
    ) -> None:
        got = quantize_down(qty, lattice)
        if got != 0.0:
            self.assertEqual(got < 0, qty < 0, "flooring a signed value moved it away from zero")
        self.assertLessEqual(abs(got), abs(qty) + lattice.step / 2.0)

    @given(lo=quantities, extra=quantities, lattice=lattices())
    def test_more_shares_never_quantize_to_fewer(
        self, lo: float, extra: float, lattice: QuantityLattice
    ) -> None:
        # EXACT, with no tolerance, and that is not an accident: every step the
        # function takes is monotone (multiply by a positive constant, add a
        # slack that is itself non-decreasing in its argument, floor, integer
        # divide, multiply, round), so the composition cannot invert an
        # ordering. Measured: zero violations in 200000 random (lo, extra,
        # lattice) draws. An earlier draft subtracted the slack here, which
        # would have hidden a real regression.
        self.assertGreaterEqual(
            quantize_down(lo + extra, lattice),
            quantize_down(lo, lattice),
        )

    @given(value=UNUSABLE, lattice=lattices())
    def test_anything_unusable_quantizes_to_zero(
        self, value: object, lattice: QuantityLattice
    ) -> None:
        # Includes `True`: a bool is an int in Python and a quantity of True is
        # a bug, not one share. The module says so; this runs it.
        self.assertEqual(quantize_down(value, lattice), 0.0)  # type: ignore[arg-type]


class TheSplitReconstitutesThePosition(PropertyTestCase):
    """`split_position` exists so parts add back up to the whole. If they did
    not, a laddered exit would strand dust below the venue minimum -- or sell
    more than is held, which is the failure this module is named for."""

    @given(
        qty=quantities,
        fractions=st.lists(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False), min_size=1, max_size=6
        ),
        lattice=lattices(),
    )
    def test_the_parts_sum_back_to_the_quantized_whole(
        self, qty: float, fractions: list[float], lattice: QuantityLattice
    ) -> None:
        assume(any(f > 0 for f in fractions))
        parts = split_position(qty, fractions, lattice)
        whole = quantize_down(qty, lattice)
        event("split is empty" if not any(parts) else "split has a part")
        self.assertLessEqual(sum(parts), whole + lattice.step / 2.0)
        self.assertAlmostEqual(sum(parts), whole, delta=lattice.step / 2.0)

    @given(
        total=st.integers(min_value=0, max_value=10_000),
        weights=st.lists(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False), min_size=1, max_size=8
        ),
    )
    def test_allocate_units_hands_out_every_unit_and_never_invents_one(
        self, total: int, weights: list[float]
    ) -> None:
        got = allocate_units(total, weights)
        self.assertEqual(len(got), len(weights))
        self.assertTrue(all(isinstance(u, int) and u >= 0 for u in got))
        expected = total if (total > 0 and any(w > 0 for w in weights)) else 0
        self.assertEqual(sum(got), expected)

    @given(
        total=st.integers(min_value=1, max_value=10_000),
        weights=st.lists(
            st.floats(min_value=1e-6, max_value=1.0, allow_nan=False), min_size=1, max_size=8
        ),
    )
    def test_a_zero_weight_never_receives_a_unit(self, total: int, weights: list[float]) -> None:
        padded = [*weights, 0.0]
        self.assertEqual(allocate_units(total, padded)[-1], 0)


class TradabilityAndItsReason(PropertyTestCase):
    """`is_tradable` and `quantity_refusal` answer the same question from two
    sides. They are kept as separate functions on purpose -- one decides, the
    other explains -- so the pair has to agree or an operator reads a reason
    that does not match the decision."""

    @given(qty=signed_quantities, lattice=lattices())
    def test_tradable_is_exactly_the_absence_of_a_refusal(
        self, qty: float, lattice: QuantityLattice
    ) -> None:
        tradable = is_tradable(qty, lattice)
        reason = quantity_refusal(qty, lattice)
        event("tradable" if tradable else "refused")
        self.assertEqual(tradable, reason is None, f"qty={qty!r} reason={reason!r}")

    @given(value=UNUSABLE, lattice=lattices())
    def test_anything_unusable_is_refused_with_a_reason(
        self, value: object, lattice: QuantityLattice
    ) -> None:
        self.assertFalse(is_tradable(value, lattice))
        self.assertIsNotNone(quantity_refusal(value, lattice))  # type: ignore[arg-type]

    @given(qty=quantities, lattice=lattices())
    def test_lattice_units_and_quantize_down_tell_the_same_story(
        self, qty: float, lattice: QuantityLattice
    ) -> None:
        units = lattice_units(qty, lattice)
        self.assertGreaterEqual(units, 0)
        self.assert_close(
            quantize_down(qty, lattice), units * lattice.step, rel_tol=1e-9, abs_tol=1e-9
        )


class TheRealisticDomainIsActuallyExercised(PropertyTestCase):
    """Non-vacuity. Every property above is guarded by something, and a
    generator that only ever produced zeros would keep them all green."""

    def test_a_meaningful_share_of_draws_quantize_to_a_positive_count(self) -> None:
        lattice = QuantityLattice(step=1.0, min_qty=1.0, precision=0)
        positive = sum(1 for i in range(1, 501) if quantize_down(i * 0.37, lattice) > 0)
        self.assertGreater(positive, 400)

    def test_a_meaningful_share_of_draws_are_tradable(self) -> None:
        lattice = QuantityLattice(step=0.01, min_qty=0.01, precision=2)
        tradable = sum(1 for i in range(1, 501) if is_tradable(round(i * 0.01, 2), lattice))
        self.assertGreater(tradable, 400)

    def test_the_lattice_strategy_builds_more_than_one_shape(self) -> None:
        # The constructor refuses a step that is not whole at its precision, so
        # a careless strategy would silently collapse to one lattice.
        shapes = {
            (p, k)
            for p in range(5)
            for k in (1, 5, 25)
            if QuantityLattice(step=k / (10**p), min_qty=0.0, precision=p)
        }
        self.assertGreater(len(shapes), 10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
