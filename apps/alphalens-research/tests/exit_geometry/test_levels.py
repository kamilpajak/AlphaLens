"""Unit tests for the pure exit-geometry level computations.

Covers :func:`atr_bracket_levels` (the ATR bracket stop/TP pair) and
:func:`ceiling_from_52w_high` (the 52w-high peak reconstruction), each split
into one assertion-concept per test.
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.exit_geometry.levels import atr_bracket_levels, ceiling_from_52w_high


class TestAtrBracketLevels(unittest.TestCase):
    def test_happy_path_exact_values(self):
        result = atr_bracket_levels(
            100.0, 10.0, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006
        )
        self.assertEqual(result, (85.0, 115.0))

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


if __name__ == "__main__":
    unittest.main()
