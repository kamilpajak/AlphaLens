"""Unit tests for the pure exit-geometry level computations.

Covers :func:`atr_bracket_levels` (the ATR bracket stop/TP pair) and
:func:`ceiling_from_52w_high` (the 52w-high peak reconstruction), each split
into one assertion-concept per test.
"""

from __future__ import annotations

import math
import unittest

from broker_contract.exit_geometry.levels import (
    atr_bracket_levels,
    ceiling_from_52w_high,
    clamp_reanchor_target,
)


class TestAtrBracketLevels(unittest.TestCase):
    def test_happy_path_exact_values(self):
        result = atr_bracket_levels(
            100.0, 10.0, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006
        )
        self.assertEqual(result, (85.0, 115.0))

    def test_non_finite_atr_returns_none(self):
        # A public leaf must self-guard: a NaN/inf atr from a future caller
        # would otherwise poison the arithmetic into NaN levels.
        self.assertIsNone(
            atr_bracket_levels(
                100.0, math.nan, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006
            )
        )
        self.assertIsNone(
            atr_bracket_levels(
                100.0, math.inf, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006
            )
        )

    def test_non_positive_atr_returns_none(self):
        self.assertIsNone(
            atr_bracket_levels(100.0, 0.0, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006)
        )
        self.assertIsNone(
            atr_bracket_levels(100.0, -5.0, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006)
        )

    def test_non_positive_stop_atr_mult_returns_none(self):
        result = atr_bracket_levels(
            100.0, 10.0, stop_atr_mult=0.0, tp_atr_mult=1.5, tp_floor_frac=0.006
        )
        self.assertIsNone(result)

    def test_bracket_stop_at_or_below_zero_returns_none(self):
        # atr wider than 1/stop_atr_mult of the entry: 10 - 1.5*10 = -5 <= 0.
        result = atr_bracket_levels(
            10.0, 10.0, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006
        )
        self.assertIsNone(result)

    def test_ceiling_price_none_leaves_tp_uncapped(self):
        result = atr_bracket_levels(
            100.0,
            10.0,
            stop_atr_mult=1.5,
            tp_atr_mult=1.5,
            tp_floor_frac=0.006,
            ceiling_price=None,
        )
        self.assertEqual(result, (85.0, 115.0))

    def test_finite_ceiling_at_or_below_tp_floor_returns_none(self):
        # tp_floor = 100 * 1.006 = 100.6; ceiling below that is degenerate.
        result = atr_bracket_levels(
            100.0,
            10.0,
            stop_atr_mult=1.5,
            tp_atr_mult=1.5,
            tp_floor_frac=0.006,
            ceiling_price=100.5,
        )
        self.assertIsNone(result)

    def test_finite_ceiling_caps_tp(self):
        # Unc capped tp would be 115; a ceiling of 105 (above the 100.6 floor)
        # caps it, stop is unaffected.
        result = atr_bracket_levels(
            100.0,
            10.0,
            stop_atr_mult=1.5,
            tp_atr_mult=1.5,
            tp_floor_frac=0.006,
            ceiling_price=105.0,
        )
        self.assertEqual(result, (85.0, 105.0))

    def test_cost_floor_binds_over_tiny_atr_target(self):
        # blended + tp_atr_mult*atr = 100 + 1.5*0.1 = 100.15, below the
        # 0.6% cost floor of 100.6 -> the floor wins.
        result = atr_bracket_levels(
            100.0, 0.1, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006
        )
        self.assertEqual(result, (99.85, 100.6))


class TestEveryFloatParameterIsGuarded(unittest.TestCase):
    """Issue #1521. The module promises every function is total, and the
    docstring gives the reason ``atr`` is self-guarded: "so future direct
    callers cannot poison the arithmetic into NaN levels". The same argument
    covers the other three floats, which were not checked.

    ``tp_atr_mult`` is the one that matters most and the one the issue missed.
    A NaN there does NOT surface as a NaN level: ``max(tp_floor, blended +
    nan)`` returns ``tp_floor``, so the function hands back a finite,
    plausible-looking take-profit built from a poisoned input. The other three
    produce a NaN a caller could at least detect."""

    _BASE = {"stop_atr_mult": 1.5, "tp_atr_mult": 1.5, "tp_floor_frac": 0.006}

    def test_non_finite_blended_returns_none(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(blended=bad):
                self.assertIsNone(atr_bracket_levels(bad, 1.0, **self._BASE))

    def test_non_finite_stop_atr_mult_returns_none(self):
        for bad in (math.nan, math.inf):
            with self.subTest(stop_atr_mult=bad):
                kwargs = {**self._BASE, "stop_atr_mult": bad}
                self.assertIsNone(atr_bracket_levels(99.0, 1.0, **kwargs))

    def test_non_finite_tp_atr_mult_returns_none(self):
        # The silent one: without a guard this returns (97.5, 99.594) — a
        # finite pair with no NaN in it, from a NaN input.
        for bad in (math.nan, math.inf):
            with self.subTest(tp_atr_mult=bad):
                kwargs = {**self._BASE, "tp_atr_mult": bad}
                self.assertIsNone(atr_bracket_levels(99.0, 1.0, **kwargs))

    def test_non_finite_tp_floor_frac_returns_none(self):
        for bad in (math.nan, math.inf):
            with self.subTest(tp_floor_frac=bad):
                kwargs = {**self._BASE, "tp_floor_frac": bad}
                self.assertIsNone(atr_bracket_levels(99.0, 1.0, **kwargs))

    def test_a_take_profit_at_or_below_zero_returns_none(self):
        # Not in #1521 — found by widening the property domain while fixing it.
        # `stop_atr_mult` was guarded against <= 0 and the take-profit side was
        # not, so a negative multiplier with a -1 floor yields a tp of exactly
        # 0.0: a "price tuple" containing something that is not a price.
        result = atr_bracket_levels(
            2.0, 1.0, stop_atr_mult=1.5, tp_atr_mult=-2.0, tp_floor_frac=-1.0
        )
        self.assertIsNone(result)

    def test_a_take_profit_equal_to_the_entry_returns_none(self):
        # Zero ATR distance AND a zero cost floor: the target lands exactly on
        # the entry, so the bracket promises no profit at all.
        result = atr_bracket_levels(2.0, 1.0, stop_atr_mult=1.5, tp_atr_mult=0.0, tp_floor_frac=0.0)
        self.assertIsNone(result)


class TestTheTakeProfitSideIsJudgedOnItsResult(unittest.TestCase):
    """The degenerate thing is the TARGET, not the parameters that produce it.

    A draft of #1521 refused `tp_atr_mult <= 0` and `tp_floor_frac < 0` on the
    inputs. Measuring showed that rejects three coherent configurations out of
    five, so the check moved to the result. These are the three that must keep
    working."""

    def test_zero_atr_multiple_with_a_cost_floor_is_a_valid_bracket(self):
        # Upside from the cost floor alone, no ATR distance. Coherent: exit as
        # soon as the move clears round-trip cost.
        result = atr_bracket_levels(
            2.0, 1.0, stop_atr_mult=1.5, tp_atr_mult=0.0, tp_floor_frac=0.006
        )
        assert result is not None
        self.assertAlmostEqual(result[1], 2.012)

    def test_negative_atr_multiple_rescued_by_the_floor_is_valid(self):
        result = atr_bracket_levels(
            2.0, 1.0, stop_atr_mult=1.5, tp_atr_mult=-2.0, tp_floor_frac=0.006
        )
        assert result is not None
        self.assertAlmostEqual(result[1], 2.012)

    def test_a_negative_floor_that_never_binds_is_valid(self):
        # max(100*0.5, 100 + 1.5*10) = 115: the floor simply loses.
        result = atr_bracket_levels(
            100.0, 10.0, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=-0.5
        )
        self.assertEqual(result, (85.0, 115.0))

    def test_an_overflowing_cost_floor_returns_none(self):
        # Every INPUT here is finite; the product is not. Found by the property,
        # not by reading: blended * (1 + tp_floor_frac) overflows to inf, so a
        # guard on the inputs alone cannot catch it.
        result = atr_bracket_levels(
            1.5095537870197658e150,
            1.0,
            stop_atr_mult=1.0,
            tp_atr_mult=0.0,
            tp_floor_frac=1e308,
        )
        self.assertIsNone(result)

    def test_ordinary_inputs_are_untouched_by_the_guards(self):
        # The negative control. None of the guards may change an answer the
        # function already gave.
        self.assertEqual(
            atr_bracket_levels(
                100.0, 10.0, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006
            ),
            (85.0, 115.0),
        )


class TestCeilingFrom52wHigh(unittest.TestCase):
    def test_reconstructs_known_peak(self):
        # peak=125, last=100 -> pct = 100*(100-125)/125 = -20.
        result = ceiling_from_52w_high({"asof_close": 100.0}, -20.0)
        self.assertAlmostEqual(result, 125.0)

    def test_pct_zero_equals_asof_close(self):
        result = ceiling_from_52w_high({"asof_close": 100.0}, 0.0)
        self.assertEqual(result, 100.0)

    def test_none_trade_setup_returns_none(self):
        result = ceiling_from_52w_high(None, -20.0)
        self.assertIsNone(result)

    def test_none_pct_returns_none(self):
        result = ceiling_from_52w_high({"asof_close": 100.0}, None)
        self.assertIsNone(result)

    def test_non_finite_pct_returns_none(self):
        result = ceiling_from_52w_high({"asof_close": 100.0}, math.nan)
        self.assertIsNone(result)

    def test_non_finite_asof_close_returns_none(self):
        result = ceiling_from_52w_high({"asof_close": math.inf}, -20.0)
        self.assertIsNone(result)

    def test_asof_close_non_positive_returns_none(self):
        result = ceiling_from_52w_high({"asof_close": 0.0}, -20.0)
        self.assertIsNone(result)

    def test_denom_non_positive_returns_none(self):
        # pct <= -100 -> denom = 1 + pct/100 <= 0.
        result = ceiling_from_52w_high({"asof_close": 100.0}, -150.0)
        self.assertIsNone(result)

    def test_overflowing_division_returns_none(self):
        # Issue #1521. The `denom <= 0` guard does not stop a positive
        # DENORMAL denominator from overflowing the division, so the peak came
        # back as `inf` — which reads downstream as "no ceiling at all", the
        # opposite of the cap this value exists to impose.
        result = ceiling_from_52w_high({"asof_close": 1e308}, -100 + 1e-13)
        self.assertIsNone(result)

    def test_underflow_all_the_way_to_zero_returns_none(self):
        # Also not in #1521, which said the underflow bottoms out at a
        # denormal. It reaches exactly 0.0, and a zero peak is not a price.
        result = ceiling_from_52w_high(
            {"asof_close": 1.1125369292536007e-308}, 4.5035996273704954e17
        )
        self.assertIsNone(result)

    def test_underflow_to_a_denormal_is_still_returned(self):
        # The negative control, and a correction to the issue, which said this
        # returns 0.0. It returns 5e-324: a FINITE, positive price. The guard
        # is for non-finite results only. A denormal ceiling is honest
        # arithmetic and it already fails safe at the call site, where a
        # ceiling below the cost floor makes the bracket non-constructible.
        result = ceiling_from_52w_high({"asof_close": 1.1459e-156}, 4.63e169)
        assert result is not None
        self.assertTrue(math.isfinite(result))
        self.assertGreater(result, 0.0)


class ClampReanchorTargetTest(unittest.TestCase):
    def test_tighter_target_passes(self):
        out = clamp_reanchor_target(98.0, 99.0, anchor_price=101.0, min_distance_frac=0.002)
        self.assertTrue(math.isclose(out, 99.0))

    def test_below_brief_floor_refused(self):
        self.assertIsNone(
            clamp_reanchor_target(98.0, 97.0, anchor_price=101.0, min_distance_frac=0.002)
        )

    def test_too_close_clamped_to_floor(self):
        out = clamp_reanchor_target(98.0, 100.9, anchor_price=101.0, min_distance_frac=0.002)
        self.assertTrue(math.isclose(out, 101.0 * (1 - 0.002)))

    def test_degenerate_inputs_return_none(self):
        self.assertIsNone(
            clamp_reanchor_target(0.0, 99.0, anchor_price=101.0, min_distance_frac=0.002)
        )
        self.assertIsNone(
            clamp_reanchor_target(98.0, float("nan"), anchor_price=101.0, min_distance_frac=0.002)
        )
        self.assertIsNone(
            clamp_reanchor_target(98.0, 99.0, anchor_price=-1.0, min_distance_frac=0.002)
        )


if __name__ == "__main__":
    unittest.main()
