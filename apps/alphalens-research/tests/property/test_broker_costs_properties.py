"""Properties of the broker cost gate and the tranche apportionment.

Distinct from ``test_cost_model_properties.py``, which targets the RESEARCH
attribution cost model. This one is the live rail: the per-fill commission
schedule that decides whether a take-profit is worth taking, and the
largest-remainder split that turns a declared ladder into whole shares.

TWO CLAIMS THIS MODULE DELIBERATELY DOES NOT MAKE, both because they are false:

* ``round_trip_fee_bps`` is NOT exactly monotone. It is algebraically
  non-increasing on positive notional -- below the per-fill minimum the
  amortised term decays, above it the expression is constant -- but the
  constant region carries float error up to ~3e-14, so the assertion needs a
  tolerance. It is also NOT monotone across zero: ``notional <= 0`` short
  circuits to ``0.0``, a step discontinuity, so the domain starts above zero.
* The difference between the ``fx_applies`` arms is NOT exactly
  ``FX_ROUND_TRIP_RATE * 1e4``. When a per-fill minimum dominates a small
  notional, ``commission / notional`` reaches ~1e7 and the 50 bps FX term is
  absorbed into its low-order bits -- measured error 3e-5 at a 1e-6 notional.
  A tolerance-and-floor version would be true but weak, so it is left out.

Both were in the first draft of this module and both were refuted by running
them. They are recorded here so the next author does not re-derive them from
the same plausible reasoning.
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.brokers.automanager.costs import (
    EXIT_EDGE_MIN_BPS,
    US_FEE_CARD,
    WSE_FEE_CARD,
    XETR_FEE_CARD,
    apportioned_coverage_violation,
    min_profitable_exit_price,
    round_trip_fee_bps,
    single_full_position_tranche_violation,
)
from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    apportion_tranche_quantities,
)
from hypothesis import event, given
from hypothesis import strategies as st

from .base import PropertyTestCase

cards = st.sampled_from([US_FEE_CARD, WSE_FEE_CARD, XETR_FEE_CARD])
# Strictly positive: see the module docstring for the step at zero.
notionals = st.floats(min_value=1e-3, max_value=1e9, allow_nan=False, allow_infinity=False)
prices = st.floats(min_value=1e-2, max_value=1e5, allow_nan=False, allow_infinity=False)
# Bounded well below 2**53, where `round(ref * sum(fracs))` stops being able to
# name the integer it is comparing against. A real position is nowhere near it.
reference_qtys = st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
frac_lists = st.lists(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False), min_size=1, max_size=6
)


class TheFeeScheduleAmortisesTheMinimum(PropertyTestCase):
    @given(notional=notionals, fx=st.booleans(), mc=st.booleans(), card=cards)
    def test_the_fee_is_a_finite_non_negative_number_of_bps(
        self, notional: float, fx: bool, mc: bool, card: object
    ) -> None:
        got = round_trip_fee_bps(
            notional,
            fx_applies=fx,
            min_commission_applies=mc,
            card=card,  # type: ignore[arg-type]
        )
        self.assertTrue(math.isfinite(got), f"{got!r}")
        self.assertGreaterEqual(got, 0.0)

    @given(lo=notionals, extra=notionals, fx=st.booleans(), card=cards)
    def test_a_bigger_fill_never_costs_more_in_bps(
        self, lo: float, extra: float, fx: bool, card: object
    ) -> None:
        # The economic point of a per-fill minimum: it amortises. A tolerance is
        # needed because the constant region above the knee is not bit-stable --
        # measured worst violation 2.8e-14, which is float error, not a step.
        small = round_trip_fee_bps(lo, fx_applies=fx, card=card)  # type: ignore[arg-type]
        large = round_trip_fee_bps(lo + extra, fx_applies=fx, card=card)  # type: ignore[arg-type]
        event("min dominates" if small > 2 * card.commission_rate * 1e4 else "ad valorem")  # type: ignore[attr-defined]
        self.assertLessEqual(large, small + 1e-9)

    @given(notional=notionals, card=cards)
    def test_charging_the_fx_leg_never_makes_a_fill_cheaper(
        self, notional: float, card: object
    ) -> None:
        # The weaker, TRUE sibling of the exact-identity claim the module
        # docstring rejects: the direction holds everywhere even though the
        # magnitude does not.
        with_fx = round_trip_fee_bps(notional, fx_applies=True, card=card)  # type: ignore[arg-type]
        without = round_trip_fee_bps(notional, fx_applies=False, card=card)  # type: ignore[arg-type]
        self.assertGreaterEqual(with_fx, without)


class TheProfitableExitThreshold(PropertyTestCase):
    @given(entry=prices, qty=st.floats(min_value=1e-3, max_value=1e7))
    def test_the_threshold_is_none_or_a_price_above_the_entry(
        self, entry: float, qty: float
    ) -> None:
        got = min_profitable_exit_price(entry_price=entry, qty=qty)
        if got is None:
            event("threshold: refused")
            return
        event("threshold: priced")
        self.assertTrue(math.isfinite(got), f"{got!r}")
        self.assertGreater(got, entry, "an exit that does not clear the entry is not profitable")

    @given(
        entry=prices,
        small=st.floats(min_value=1e-3, max_value=1e4),
        extra=st.floats(min_value=0.0, max_value=1e6),
    )
    def test_a_larger_position_never_needs_a_higher_exit(
        self, entry: float, small: float, extra: float
    ) -> None:
        # The docstring states this and then illustrates it with three measured
        # numbers (60.80 at 10 shares, 61.2667 at 3, 62.60 at 1). Here it is as
        # a claim about every quantity instead of three of them.
        few = min_profitable_exit_price(entry_price=entry, qty=small)
        many = min_profitable_exit_price(entry_price=entry, qty=small + extra)
        if few is not None and many is not None:
            self.assertLessEqual(many, few + 1e-9)

    @given(entry=prices, qty=st.floats(min_value=1e-3, max_value=1e7))
    def test_the_threshold_clears_the_declared_minimum_edge(self, entry: float, qty: float) -> None:
        got = min_profitable_exit_price(entry_price=entry, qty=qty)
        if got is not None:
            self.assertGreaterEqual(got, entry * (1.0 + EXIT_EDGE_MIN_BPS / 1e4) - 1e-9)


class TheApportionmentHandsOutTheDeclaredCoverage(PropertyTestCase):
    @given(ref=reference_qtys, fracs=frac_lists)
    def test_the_total_is_the_declared_coverage(self, ref: float, fracs: list[float]) -> None:
        got = apportion_tranche_quantities(reference_qty=ref, tranche_fracs=tuple(fracs))
        event("coverage: nothing to hand out" if sum(got) == 0 else "coverage: shares handed out")
        self.assertEqual(sum(got), round(ref * sum(fracs)))

    @given(ref=reference_qtys, fracs=frac_lists)
    def test_every_quantity_is_a_non_negative_whole_number(
        self, ref: float, fracs: list[float]
    ) -> None:
        got = apportion_tranche_quantities(reference_qty=ref, tranche_fracs=tuple(fracs))
        self.assertEqual(len(got), len(fracs))
        for q in got:
            self.assertGreaterEqual(q, 0.0)
            self.assertEqual(q, float(int(q)), f"{q!r} is not a whole share")

    @given(
        ref=st.integers(min_value=1, max_value=1_000_000).map(float),
        fracs=st.lists(st.floats(min_value=0.01, max_value=1.0), min_size=1, max_size=5),
    )
    def test_a_ladder_declaring_full_coverage_always_passes_the_coverage_check(
        self, ref: float, fracs: list[float]
    ) -> None:
        # ONE-DIRECTIONAL on purpose. The converse is FALSE: the apportionment
        # rounds `ref * sum(fracs)` while the check rounds `ref`, so a partial
        # ladder passes whenever both land on the same integer -- `ref=1.0` with
        # `fracs=(0.75,)` does. The docstring calls that rounding-target
        # mismatch deliberate; asserting an `iff` here would assert it away.
        #
        # `ref` is WHOLE here because production's is: `reference_qty` is a sum
        # over `math.floor`-ed tier quantities. At a fractional `ref` the
        # property is false for a different and uninteresting reason --
        # normalising by division leaves `sum == 0.9999999999999999`, so at
        # `ref=1.5` the two `round()` calls straddle the halfway point and
        # disagree. Found by the dev profile at 2000 examples; 300 missed it.
        total = sum(fracs)
        normalised = tuple(f / total for f in fracs)
        quantities = apportion_tranche_quantities(reference_qty=ref, tranche_fracs=normalised)
        self.assertIsNone(
            apportioned_coverage_violation(tranche_quantities=quantities, reference_qty=ref)
        )


class TheViolationPredicatesNeverRaise(PropertyTestCase):
    """Both return a human-readable reason or ``None`` and promise never to
    raise on numeric input. A predicate that raised would take down the
    never-naked backstop that calls it."""

    @given(
        quantities=st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=6),
        ref=st.floats(allow_nan=True, allow_infinity=True),
    )
    def test_apportioned_coverage_answers_a_string_or_none(
        self, quantities: list[float], ref: float
    ) -> None:
        got = apportioned_coverage_violation(tranche_quantities=quantities, reference_qty=ref)
        event("coverage: violation" if got is not None else "coverage: clean")
        self.assertTrue(got is None or isinstance(got, str))

    @given(
        quantities=st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=6),
        position=st.floats(allow_nan=True, allow_infinity=True),
    )
    def test_single_full_position_answers_a_string_or_none(
        self, quantities: list[float], position: float
    ) -> None:
        got = single_full_position_tranche_violation(
            tranche_quantities=quantities, position_qty=position
        )
        self.assertTrue(got is None or isinstance(got, str))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
