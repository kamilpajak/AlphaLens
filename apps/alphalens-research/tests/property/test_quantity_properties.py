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
* At extreme magnitudes the function used to break outright: at
  ``qty = 1_407_374_883_554.0`` on a 0.01 step it returned ``...554.01`` --
  above its input, off the lattice, and not idempotent -- and ``1e308`` raised
  ``OverflowError`` rather than returning the promised ``0.0``. Both are fixed
  in #1520.

  The mechanism recorded here was WRONG and is corrected: it is not the final
  ``round(magnitude, precision)``, which leaves that value untouched. It is the
  slack itself. The slack is ``_ULP_SLACK`` = 2**5 ULPs of the value SCALED by
  ``10**precision``, and ``ulp(2**47) == 2**-5``, so at 2**47 scaled units the
  slack is exactly one whole scaled unit -- one whole STEP on a one-unit step.
  ``units`` was already one too many before any rounding happened. So the two
  bullets above are ONE defect at two magnitudes, not two defects.

  The fix caps the slack at half a scaled unit, and the module then claimed
  that made "the overshoot never reaches half a step" true by construction
  instead of by luck of magnitude, so the unbounded property below could drop
  its upper bound.

  THAT CLAIM WAS FALSE and the unbounded property flaked on it for weeks
  (#1560). Capping the slack is necessary and not sufficient: at half a scaled
  unit the cap EQUALS half a step on a one-unit step, so it consumes the whole
  tolerance and leaves nothing for the error in `abs(qty) * 10**precision`.
  Measured at the then-current `_SCALED_EXACT_LIMIT` of 2**53: 426 violations
  in 200 000 sampled pairs, worst 2.44 STEPS above the input. Removing the
  slack entirely still left 68, so the slack was never the binding term.

  What makes the claim true is the LIMIT, moved to the magnitude where the cap
  starts to bind (2**46 scaled units). The class at the bottom of this file
  pins that, and walks consecutive floats rather than sampling them -- the
  failures live in the last few thousand floats below the boundary, where a
  log-uniform strategy essentially never lands.
"""

from __future__ import annotations

import math
import unittest

from broker_contract.quantity import (
    _MAX_SCALED_SLACK,
    _SCALED_EXACT_LIMIT,
    _ULP_SLACK,
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
# Every finite positive float, denormals and the ceiling included. ONE property
# uses this — the half-step bound, which #1520 made true by construction. The
# bounded strategy above stays bounded for the claims that really are about
# realistic share counts.
ANY_POSITIVE_QUANTITY = st.floats(
    min_value=0.0, exclude_min=True, allow_nan=False, allow_infinity=False
)
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

    @given(qty=ANY_POSITIVE_QUANTITY, lattice=lattices())
    def test_the_half_step_bound_holds_at_every_magnitude(
        self, qty: float, lattice: QuantityLattice
    ) -> None:
        """The same claim with NO upper bound on the quantity (#1520).

        The bounded sibling above is bounded because 1e9 is a share count this
        rail could hold. This one exists because the claim is now true by
        CONSTRUCTION rather than by staying small: the slack is capped at half
        a scaled unit, and a step is at least one scaled unit.

        Before the cap this failed at 1.4e12 shares on a 0.01 step, where the
        result exceeded its input by a whole step, and raised `OverflowError`
        at 1e308 instead of returning 0.0. Both are asserted here rather than
        in prose."""
        got = quantize_down(qty, lattice)
        event("unbounded: zero" if got == 0.0 else "unbounded: positive count")
        self.assertLessEqual(abs(got), abs(qty) + lattice.step / 2.0)
        self.assertTrue(math.isfinite(got), f"{got!r}")

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


class TheLatticeStopsBeingResolvableWhereTheSlackCapBinds(unittest.TestCase):
    """#1560. Above a magnitude the module could already name, it hands back MORE.

    The half-step property flaked because it is unsatisfiable up there, and the
    reason is arithmetic rather than a tolerance that wants widening. The slack
    is ``min(max(32 * ulp(scaled), _ABS_TOL), _MAX_SCALED_SLACK)`` with the cap
    at half a scaled unit. The cap starts binding exactly at ``scaled == 2**46``
    (``32 * ulp(2**46) == 0.5``), and from there the slack alone is the WHOLE
    half-step tolerance on a one-unit step, leaving nothing for the error in
    ``abs(qty) * 10**precision``. So ``_SCALED_EXACT_LIMIT`` belongs at the
    cap-binding point, not six binary orders above it.

    Measured before the fix, over 200 000 sampled (quantity, lattice) pairs:
    426 violations, worst overshoot 2.44 STEPS -- two lattice points above the
    input, on the leaf whose whole premise is that a quantity cannot grow on the
    way through.
    """

    def _lattice(self, precision: int, step_units: int = 1) -> QuantityLattice:
        return QuantityLattice(
            step=step_units / (10**precision),
            min_qty=0.0,
            precision=precision,
            source="issue-1560",
        )

    def test_the_case_the_issue_reported_is_refused_rather_than_overshot(self) -> None:
        # Hypothesis found this one. It returned 450359962737050.6 -- two ULPs
        # above its input, where the tolerance is 0.05 and one ULP is 0.0625.
        self.assertEqual(0.0, quantize_down(450359962737050.5, self._lattice(1)))

    def test_the_worst_measured_overshoot_is_refused(self) -> None:
        # ~5e11 shares on a 1e-4 venue: the sweep's worst case, 2.44 steps above
        # its input.
        self.assertEqual(0.0, quantize_down(503555000000.0, self._lattice(4)))

    def test_the_case_a_sampling_sweep_could_not_find(self) -> None:
        # The one that refuted the first draft of this fix. A 200 000-pair
        # log-uniform sweep returned ZERO violations at a 2**47 limit; a dense
        # walk of consecutive floats just below it found this immediately.
        # Sampling almost never lands in the last few thousand floats under a
        # power of two, which is exactly where the cap and the scaling error
        # meet.
        self.assertEqual(0.0, quantize_down(14073748835532.75, self._lattice(1)))

    def test_the_limit_sits_where_the_slack_cap_starts_to_bind(self) -> None:
        # Pinned literally AND derived, so the number cannot drift back up on a
        # plausible-sounding argument. 2**53 was the old value and it reasoned
        # about consecutive INTEGERS, a different question from whether the
        # LATTICE survives scale -> floor -> rescale.
        self.assertEqual(2**46, _SCALED_EXACT_LIMIT)
        self.assertEqual(
            _MAX_SCALED_SLACK,
            _ULP_SLACK * math.ulp(float(_SCALED_EXACT_LIMIT)),
            "the limit must be the magnitude at which the uncapped slack first "
            "reaches the cap; below it the slack leaves room for the scaling "
            "error, at and above it there is none",
        )

    def test_both_sides_of_the_boundary(self) -> None:
        # One side alone would prove only that refusal happens somewhere.
        for precision in range(5):
            lattice = self._lattice(precision)
            ceiling = _SCALED_EXACT_LIMIT / (10**precision)
            with self.subTest(precision=precision):
                self.assertNotEqual(
                    0.0,
                    quantize_down(ceiling / 2.0, lattice),
                    "a quantity inside the resolvable band must still quantize",
                )
                self.assertEqual(
                    0.0,
                    quantize_down(ceiling * 2.0, lattice),
                    "a quantity outside it must be refused, not approximated",
                )

    def test_nothing_a_real_account_could_hold_changed(self) -> None:
        # The regression guard for the whole change. 1e9 is what the bounded
        # sibling property calls "a share count this rail could hold"; the
        # tightest ceiling here is seven times that.
        for precision in range(5):
            lattice = self._lattice(precision)
            # each at or above one step at every precision tested
            for qty in (1.0, 100.5, 12_345.678, 1e6, 999_999_999.0):
                with self.subTest(precision=precision, qty=qty):
                    got = quantize_down(qty, lattice)
                    self.assertNotEqual(0.0, got, "a realistic quantity was refused")
                    self.assertLessEqual(abs(got), abs(qty) + lattice.step / 2.0)

    def test_a_dense_walk_below_the_limit_never_exceeds_half_a_step(self) -> None:
        # A DENSE walk, deliberately, not a Hypothesis strategy. The failures
        # this fix is about cluster in the last few thousand floats below the
        # boundary, and sampling does not go there. Walking consecutive floats
        # is the instrument with the power to refute.
        for precision in range(5):
            lattice = self._lattice(precision)
            scaled = float(_SCALED_EXACT_LIMIT)
            worst = 0.0
            for _ in range(3000):
                qty = scaled / (10**precision)
                worst = max(worst, abs(quantize_down(qty, lattice)) - abs(qty))
                scaled = math.nextafter(scaled, 0.0)
            with self.subTest(precision=precision):
                self.assertLessEqual(
                    worst,
                    lattice.step / 2.0,
                    f"overshoot {worst / lattice.step:.4f} of a step just below the limit",
                )

    def test_the_refusal_names_the_limit_it_actually_applies(self) -> None:
        # The message used to end "exceeds 2**53" as a literal, so lowering the
        # constant would have left it stating a threshold nobody enforces. A
        # message that misnames its own rule is worse than no message.
        lattice = self._lattice(4)
        reason = quantity_refusal(_SCALED_EXACT_LIMIT / (10**4) * 2.0, lattice)
        self.assertIsNotNone(reason)
        self.assertNotIn("2**53", reason)
        self.assertIn(repr(_SCALED_EXACT_LIMIT), reason)

    def test_positive_control_a_stale_literal_would_be_detected(self) -> None:
        # Without this the assertion above could rot into a no-op if the message
        # stopped naming any threshold at all.
        self.assertIn("2**53", "its scaled form exceeds 2**53")

    def test_the_share_ceiling_per_venue_precision_is_pinned(self) -> None:
        # A scaled limit divides the SHARE ceiling by ten per decimal, and
        # `precision` is not ours: it comes from Saxo's `AmountDecimals`, and the
        # constructor bounds it only from below. At six decimals the ceiling is
        # 70 million shares, which a real account could hold. The fixtures in
        # this tree report 0 and 3, so nothing live is near it -- this table is
        # here so a higher-precision venue arrives as a red test rather than as
        # a refused order.
        expected = {0: 70_368_744_177_664, 4: 7_036_874_417, 6: 70_368_744, 8: 703_687}
        for precision, ceiling in expected.items():
            with self.subTest(precision=precision):
                self.assertEqual(ceiling, int(_SCALED_EXACT_LIMIT / (10**precision)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
