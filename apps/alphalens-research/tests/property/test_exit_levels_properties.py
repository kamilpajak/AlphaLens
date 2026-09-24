"""Properties of the exit-geometry level arithmetic (`broker_contract.exit_geometry.levels`).

The module docstring promises every function is "a total, side-effect-free
mapping from primitive inputs to either a price tuple or ``None`` on a
degenerate / missing input". The 21 example tests next door are all NAMED
degenerate cases -- each one probes a guard the code HAS. None probe overflow,
underflow, denormals, or a parameter the code does not guard, which is why they
find nothing here and a generator does.

WHAT THE DOMAIN BOUNDS MEAN. All five functions are now total over every float
and asserted as such. Until #1521 two of them were not, and the history is kept
here because it is what the bounds below were originally for:

* ``ceiling_from_52w_high`` returned ``inf`` when ``pct`` sat a hair above -100
  — the ``denom <= 0`` guard does not stop a positive DENORMAL denominator from
  overflowing the division. It now guards its RESULT.
* ``atr_bracket_levels`` self-guarded ``atr`` and nothing else, so a NaN in any
  of the other three floats passed straight through. The one that mattered was
  ``tp_atr_mult``: it did not even surface as a NaN, because
  ``max(tp_floor, blended + nan)`` returns ``tp_floor``, so the function handed
  back a finite, plausible take-profit built from a poisoned input. All four
  floats are guarded now.

One thing #1521 deliberately did NOT change: an UNDERFLOWING division in
``ceiling_from_52w_high`` still returns a denormal (``5e-324``), because that is
a finite positive price and honest arithmetic, and it fails safe where it is
used. The bounded properties further down stay bounded — their claims are about
realistic prices, not about totality.

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

# A price a venue could actually quote: penny stock to Berkshire-A. The bound is
# about what the CLAIMS below are about (ordering, the cost floor, a binding
# ceiling), not about totality any more — since #1521 every function is total
# over every float and the class above asserts exactly that.
prices = st.floats(min_value=1e-2, max_value=1e6, allow_nan=False, allow_infinity=False)
# An ATR is a distance in the instrument's currency, never a multiple.
atrs = st.floats(min_value=1e-6, max_value=1e5, allow_nan=False, allow_infinity=False)
multiples = st.floats(min_value=1e-3, max_value=10.0, allow_nan=False, allow_infinity=False)
fractions = st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False)

# Every float there is. All five functions answer `None` or a usable price here.
ANY_FLOAT = st.floats(allow_nan=True, allow_infinity=True)


def _usable(value: object) -> bool:
    """The module's own output contract: ``None`` or a finite positive price."""
    return value is None or (isinstance(value, float) and math.isfinite(value) and value > 0.0)


class TheTotalFunctionsAreTotalOverEveryFloat(PropertyTestCase):
    """All five keep the module promise without any domain bound: NaN, the
    infinities, zero and denormals all answer ``None`` or a usable price.

    It was three until #1521 guarded the two that were not (see the module
    docstring). The last two properties here are what makes that a checked
    claim rather than a fixed docstring."""

    @given(avg_price=ANY_FLOAT, atr=ANY_FLOAT, k=ANY_FLOAT)
    def test_reanchor_target_never_returns_a_bad_stop(
        self, avg_price: float, atr: float, k: float
    ) -> None:
        self.assertTrue(_usable(reanchor_target(avg_price, atr, k=k)))

    @given(entry=ANY_FLOAT, peak=ANY_FLOAT, frac=ANY_FLOAT)
    def test_fractional_giveback_never_returns_a_bad_level(
        self, entry: float, peak: float, frac: float
    ) -> None:
        self.assertTrue(_usable(fractional_giveback_target(entry, peak, kept_gain_frac=frac)))

    @given(prior=ANY_FLOAT, proposed=ANY_FLOAT, anchor=ANY_FLOAT, min_dist=ANY_FLOAT)
    def test_clamp_never_returns_a_bad_stop(
        self, prior: float, proposed: float, anchor: float, min_dist: float
    ) -> None:
        got = clamp_reanchor_target(
            prior, proposed, anchor_price=anchor, min_distance_frac=min_dist
        )
        self.assertTrue(_usable(got))

    @given(
        blended=ANY_FLOAT,
        atr=ANY_FLOAT,
        stop_mult=ANY_FLOAT,
        tp_mult=ANY_FLOAT,
        floor=ANY_FLOAT,
        ceiling=st.one_of(st.none(), ANY_FLOAT),
    )
    def test_the_atr_bracket_never_returns_a_bad_pair(
        self,
        blended: float,
        atr: float,
        stop_mult: float,
        tp_mult: float,
        floor: float,
        ceiling: float | None,
    ) -> None:
        got = atr_bracket_levels(
            blended,
            atr,
            stop_atr_mult=stop_mult,
            tp_atr_mult=tp_mult,
            tp_floor_frac=floor,
            ceiling_price=ceiling,
        )
        if got is None:
            event("bracket over every float: refused")
            return
        event("bracket over every float: priced")
        stop, tp = got
        self.assertTrue(_usable(stop), f"stop {stop!r}")
        self.assertTrue(_usable(tp), f"tp {tp!r}")

    @given(asof_close=ANY_FLOAT, pct=ANY_FLOAT)
    def test_the_52w_ceiling_never_returns_a_bad_price(self, asof_close: float, pct: float) -> None:
        got = ceiling_from_52w_high({"asof_close": asof_close}, pct)
        if got is None:
            event("ceiling over every float: refused")
            return
        event("ceiling over every float: priced")
        self.assertTrue(_usable(got), f"{got!r}")


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
        got = fractional_giveback_target(entry, peak, kept_gain_frac=frac)
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
        low = fractional_giveback_target(entry, lo, kept_gain_frac=frac)
        high = fractional_giveback_target(entry, lo + gain, kept_gain_frac=frac)
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
