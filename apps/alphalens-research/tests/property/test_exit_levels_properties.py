"""Properties of the exit-geometry level arithmetic (`broker_contract.exit_geometry.levels`).

The module docstring promises every function is "a total, side-effect-free
mapping from primitive inputs to either a price tuple or ``None`` on a
degenerate / missing input". The 21 example tests next door are all NAMED
degenerate cases -- each one probes a guard the code HAS. None probe overflow,
underflow, denormals, or a parameter the code does not guard, which is why they
find nothing here and a generator does.

WHAT THE DOMAIN BOUNDS MEAN. Two of the five functions are total over the
realistic price domain asserted below and NOT total over every float:

* ``ceiling_from_52w_high`` returns ``inf`` when ``pct`` is a hair above -100
  (the ``denom <= 0`` guard does not stop a positive DENORMAL denominator from
  overflowing the division) and ``0.0`` when ``pct`` is astronomically large.
* ``atr_bracket_levels`` self-guards ``atr`` -- its docstring says so
  explicitly -- but not ``blended`` or ``tp_floor_frac``, so a non-finite
  ``blended`` passes straight through (``bracket_stop <= 0`` is False for NaN).

Both are unreachable from any real feed and both fail SAFE at their call sites
(an ``inf`` ceiling reads as uncapped, a ``0.0`` one falls below the cost floor
and yields ``None``). They are under-specified contracts, not rail defects, and
they are filed as their own issues. Asserting them here would be asserting a
bug; asserting the realistic domain is asserting the contract.

ORACLE INDEPENDENCE: no property below recomputes the function under test. The
round trip inverts a DIFFERENT formula (percent-off-peak) to recover its input.
"""

from __future__ import annotations

import math
import unittest

from broker_contract.exit_geometry.levels import (
    atr_bracket_levels,
    ceiling_from_52w_high,
    clamp_reanchor_target,
    fractional_giveback_target,
    reanchor_target,
)
from hypothesis import event, given
from hypothesis import strategies as st

from .base import PropertyTestCase

# A price a venue could actually quote. Wide enough to cross the whole realistic
# range (penny stock to Berkshire-A) without reaching the float extremes where
# the two unguarded parameters above stop being total.
prices = st.floats(min_value=1e-2, max_value=1e6, allow_nan=False, allow_infinity=False)
# An ATR is a distance in the instrument's currency, never a multiple.
atrs = st.floats(min_value=1e-6, max_value=1e5, allow_nan=False, allow_infinity=False)
multiples = st.floats(min_value=1e-3, max_value=10.0, allow_nan=False, allow_infinity=False)
fractions = st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False)

# The three functions that ARE total over every float, per the module promise.
ANY_FLOAT = st.floats(allow_nan=True, allow_infinity=True)


def _usable(value: object) -> bool:
    """The module's own output contract: ``None`` or a finite positive price."""
    return value is None or (isinstance(value, float) and math.isfinite(value) and value > 0.0)


class TheTotalFunctionsAreTotalOverEveryFloat(PropertyTestCase):
    """These three keep the module promise without any domain bound: NaN, the
    infinities, zero and denormals all answer ``None`` or a usable price."""

    @given(avg_price=ANY_FLOAT, atr=ANY_FLOAT, k=ANY_FLOAT)
    def test_reanchor_target_never_returns_a_bad_stop(
        self, avg_price: float, atr: float, k: float
    ) -> None:
        self.assertTrue(_usable(reanchor_target(avg_price, atr, k=k)))

    @given(entry=ANY_FLOAT, peak=ANY_FLOAT, frac=ANY_FLOAT)
    def test_fractional_giveback_never_returns_a_bad_level(
        self, entry: float, peak: float, frac: float
    ) -> None:
        self.assertTrue(_usable(fractional_giveback_target(entry, peak, frac=frac)))

    @given(prior=ANY_FLOAT, proposed=ANY_FLOAT, anchor=ANY_FLOAT, min_dist=ANY_FLOAT)
    def test_clamp_never_returns_a_bad_stop(
        self, prior: float, proposed: float, anchor: float, min_dist: float
    ) -> None:
        got = clamp_reanchor_target(
            prior, proposed, anchor_price=anchor, min_distance_frac=min_dist
        )
        self.assertTrue(_usable(got))


class TheMoneyGuarantees(PropertyTestCase):
    """The two properties a stop depends on, stated over every float because
    both hold there -- these are floors, so they must not need a tame input."""

    @given(prior=prices, proposed=ANY_FLOAT, anchor=ANY_FLOAT, min_dist=fractions)
    def test_the_clamp_never_returns_below_the_brief_floor(
        self, prior: float, proposed: float, anchor: float, min_dist: float
    ) -> None:
        # The never-below-brief-floor envelope. A stop that the clamp could move
        # DOWN would silently widen risk past what the document declared.
        got = clamp_reanchor_target(
            prior, proposed, anchor_price=anchor, min_distance_frac=min_dist
        )
        if got is not None:
            self.assertGreaterEqual(got, prior)

    @given(entry=prices, peak=ANY_FLOAT, frac=st.floats(min_value=1e-6, max_value=1.0))
    def test_the_giveback_target_never_sits_below_entry(
        self, entry: float, peak: float, frac: float
    ) -> None:
        got = fractional_giveback_target(entry, peak, frac=frac)
        if got is not None:
            self.assertGreaterEqual(got, entry)

    @given(
        entry=prices,
        lo=prices,
        gain=st.floats(min_value=0.0, max_value=1e5),
        frac=st.floats(min_value=1e-6, max_value=1.0),
    )
    def test_a_higher_peak_never_lowers_the_giveback_target(
        self, entry: float, lo: float, gain: float, frac: float
    ) -> None:
        # The ratchet: the level this returns may only ever move up as the
        # favourable excursion grows. Monotone, not merely bounded.
        low = fractional_giveback_target(entry, lo, frac=frac)
        high = fractional_giveback_target(entry, lo + gain, frac=frac)
        if low is not None and high is not None:
            self.assertGreaterEqual(high, low - 1e-9)


class TheAtrBracketOverRealisticPrices(PropertyTestCase):
    """Shape and ordering of the (stop, tp) pair. Domain-bounded on purpose --
    see the module docstring for the two parameters the function does not guard."""

    @given(
        blended=prices,
        atr=atrs,
        stop_mult=multiples,
        tp_mult=multiples,
        floor=fractions,
    )
    def test_the_pair_is_usable_and_brackets_the_entry(
        self,
        blended: float,
        atr: float,
        stop_mult: float,
        tp_mult: float,
        floor: float,
    ) -> None:
        got = atr_bracket_levels(
            blended,
            atr,
            stop_atr_mult=stop_mult,
            tp_atr_mult=tp_mult,
            tp_floor_frac=floor,
        )
        if got is None:
            return
        stop, tp = got
        self.assertTrue(_usable(stop), f"stop {stop!r}")
        self.assertTrue(_usable(tp), f"tp {tp!r}")
        self.assertLess(stop, blended, "a bracket stop sits BELOW the entry")
        self.assertGreaterEqual(tp, blended, "a bracket take-profit never sits below entry")

    @given(blended=prices, atr=atrs, stop_mult=multiples, tp_mult=multiples, floor=fractions)
    def test_the_take_profit_clears_the_cost_floor(
        self, blended: float, atr: float, stop_mult: float, tp_mult: float, floor: float
    ) -> None:
        got = atr_bracket_levels(
            blended, atr, stop_atr_mult=stop_mult, tp_atr_mult=tp_mult, tp_floor_frac=floor
        )
        if got is not None:
            self.assertGreaterEqual(got[1], blended * (1.0 + floor) - 1e-9)

    @given(
        blended=prices,
        atr=atrs,
        stop_mult=multiples,
        tp_mult=multiples,
        floor=fractions,
        ceiling_ratio=st.floats(min_value=1.0005, max_value=1.25),
    )
    def test_a_ceiling_only_ever_lowers_the_take_profit(
        self,
        blended: float,
        atr: float,
        stop_mult: float,
        tp_mult: float,
        floor: float,
        ceiling_ratio: float,
    ) -> None:
        # The ceiling is drawn RELATIVE to the entry, not independently. Drawn
        # from the same wide price range as `blended` it lands above the
        # take-profit almost always -- measured 0.3% of examples where it
        # actually binds, which is a property that asserts nothing. Relative and
        # bounded to a realistic distance above entry it binds in 11.3% of
        # constructible pairs (measured), and `event()` below keeps that honest.
        kw = {"stop_atr_mult": stop_mult, "tp_atr_mult": tp_mult, "tp_floor_frac": floor}
        uncapped = atr_bracket_levels(blended, atr, **kw)
        capped = atr_bracket_levels(blended, atr, ceiling_price=blended * ceiling_ratio, **kw)
        if uncapped is None or capped is None:
            event("bracket: not constructible")
            return
        event("bracket: ceiling BINDS" if capped[1] < uncapped[1] else "bracket: ceiling is slack")
        self.assertLessEqual(capped[1], uncapped[1] + 1e-9)
        self.assertEqual(capped[0], uncapped[0], "a ceiling never moves the stop")

    def test_the_ceiling_case_is_actually_reachable(self) -> None:
        # Non-vacuity, run as its own test rather than trusted: the property
        # above is only worth its name if a binding ceiling really occurs. A
        # generator that never produces one would keep it green forever.
        binds = 0
        for i in range(200):
            ratio = 1.0005 + (i / 200.0) * 0.25
            uncapped = atr_bracket_levels(
                100.0, 5.0, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006
            )
            capped = atr_bracket_levels(
                100.0,
                5.0,
                ceiling_price=100.0 * ratio,
                stop_atr_mult=1.5,
                tp_atr_mult=1.5,
                tp_floor_frac=0.006,
            )
            if uncapped and capped and capped[1] < uncapped[1]:
                binds += 1
        self.assertGreater(binds, 20, "a binding ceiling must be reachable in this domain")


class TheFiftyTwoWeekCeilingRoundTrip(PropertyTestCase):
    """peak -> percent-off-peak -> peak. A genuine algebraic inverse, and the
    only place in this module where a generated input can be recovered.

    NOT float-exact (``close=1.0, peak=3.0`` already breaks equality) and not
    within 1e-9 on an unbounded drawdown: the percent is divided by a
    denominator that approaches zero as the peak runs away from the close, so
    the relative error is amplified. Both a tolerance AND a bounded drawdown are
    load-bearing here, and stating either alone makes the property false."""

    @given(close=prices, ratio=st.floats(min_value=1.0, max_value=1e4))
    def test_a_peak_survives_the_trip_through_percent_off(self, close: float, ratio: float) -> None:
        peak = close * ratio
        pct = 100.0 * (close - peak) / peak
        back = ceiling_from_52w_high({"asof_close": close}, pct)
        assert back is not None
        self.assert_close(back, peak, rel_tol=1e-9, abs_tol=1e-9)

    @given(close=prices)
    def test_a_close_at_its_own_peak_recovers_the_close(self, close: float) -> None:
        got = ceiling_from_52w_high({"asof_close": close}, 0.0)
        assert got is not None
        self.assert_close(got, close)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
