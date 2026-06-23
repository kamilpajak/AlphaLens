"""Tests for entry_primitives: market_at_arrival_fill + vwap_arrival_fill.

_window_vwap behavior (confirmed from bar_window.py source):
- Uses close price ``c``, NOT typical price (h+l+c)/3.
- Window is [start_ms, end_ms) — exclusive end.
- Takes datetime objects, converts internally to epoch ms.
- Returns None when no bar falls in window.
- Degrades to unweighted mean of closes when total volume is zero.
"""

import unittest

from alphalens_pipeline.thematic.trade_setup.entry_primitives import (
    arm_disaster_stop,
    build_narrow_tiers_arm,
    build_single_at_close_arm,
    market_at_arrival_fill,
    vwap_arrival_fill,
)

_OPEN = 1_700_000_000_000  # arrival session open (epoch ms)
_MIN = 60_000


def _bar(ts, o, h, low, c, v=1000.0):
    return {"t": ts, "o": o, "h": h, "l": low, "c": c, "v": v}


class TestMarketAtArrival(unittest.TestCase):
    def _close(self):
        return _OPEN + 6 * 3600 * 1000  # ~6h later

    def test_fills_at_first_rth_open_gap_up(self):
        bars = [
            _bar(_OPEN, 110.0, 111.0, 109.0, 110.5),
            _bar(_OPEN + _MIN, 110.5, 112.0, 110.0, 111.0),
        ]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.status, "OK")
        self.assertEqual(f.fill_price, 110.0)  # open of first in-window bar, not low/close
        self.assertFalse(f.late_open)

    def test_fills_at_open_gap_down(self):
        bars = [_bar(_OPEN, 92.0, 93.0, 91.0, 92.5)]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.fill_price, 92.0)

    def test_ignores_premarket_bar(self):
        bars = [
            _bar(_OPEN - _MIN, 999.0, 999.0, 999.0, 999.0),
            _bar(_OPEN, 110.0, 111.0, 109.0, 110.5),
        ]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.fill_price, 110.0)  # pre-market 999 ignored

    def test_no_bar_in_window_is_no_fill_not_next_session(self):
        next_session = _OPEN + 24 * 3600 * 1000
        bars = [_bar(next_session, 50.0, 51.0, 49.0, 50.0)]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.status, "NO_FILL")
        self.assertIsNone(f.fill_price)

    def test_late_open_flag(self):
        bars = [_bar(_OPEN + 45 * _MIN, 110.0, 111.0, 109.0, 110.5)]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.status, "OK")
        self.assertTrue(f.late_open)


class TestMarketAtArrivalNaN(unittest.TestCase):
    """NaN-safety: a bar with o=NaN must yield NO_FILL, not propagate NaN."""

    def test_nan_open_price_returns_no_fill(self):
        # The earliest in-window bar has o=NaN — must not propagate as fill_price.
        bars = [
            _bar(_OPEN, float("nan"), float("nan"), float("nan"), float("nan")),
        ]
        f = market_at_arrival_fill(
            bars,
            arrival_open_ms=_OPEN,
            arrival_close_ms=_OPEN + 6 * 3600 * 1000,
        )
        self.assertEqual(f.status, "NO_FILL")
        self.assertIsNone(f.fill_price)


class TestVwapArrival(unittest.TestCase):
    def test_volume_weighted_vwap(self):
        # Two 1-min bars in the 30-min window.
        # _window_vwap uses close price (c), weighted by volume.
        # Both bars have h=l=c=price so VWAP = (100*100 + 110*300) / 400 = 107.5
        bars = [
            _bar(_OPEN, 100.0, 100.0, 100.0, 100.0, v=100.0),
            _bar(_OPEN + _MIN, 110.0, 110.0, 110.0, 110.0, v=300.0),
        ]
        f = vwap_arrival_fill(bars, arrival_open_ms=_OPEN)
        self.assertEqual(f.status, "OK")
        self.assertAlmostEqual(f.fill_price, (100.0 * 100.0 + 110.0 * 300.0) / 400.0)

    def test_empty_window_no_fill(self):
        bars = [_bar(_OPEN + 40 * _MIN, 100.0, 100.0, 100.0, 100.0)]  # outside 30-min window
        f = vwap_arrival_fill(bars, arrival_open_ms=_OPEN)
        self.assertEqual(f.status, "NO_FILL")

    def test_nan_close_price_returns_no_fill(self):
        # _window_vwap: NaN close passes the `close is None` guard and propagates
        # through the weighted sum → vwap_arrival_fill must catch the NaN result.
        bars = [
            _bar(_OPEN, float("nan"), float("nan"), float("nan"), float("nan")),
        ]
        f = vwap_arrival_fill(bars, arrival_open_ms=_OPEN)
        self.assertEqual(f.status, "NO_FILL")
        self.assertIsNone(f.fill_price)


class TestArmBuilders(unittest.TestCase):
    def test_narrow_tiers_collapse_under_default_spacing(self):
        # default 0.5*ATR spacing collapses 0.10/0.175/0.25*ATR tiers to <=1
        arm = build_narrow_tiers_arm(
            close=100.0, atr=10.0, min_spacing_mult=0.5, min_stop_dist_mult=0.5
        )
        self.assertTrue(arm.geometry_collapsed)

    def test_narrow_tiers_keep_all_three_with_small_spacing(self):
        arm = build_narrow_tiers_arm(
            close=100.0, atr=10.0, min_spacing_mult=0.05, min_stop_dist_mult=0.05
        )
        self.assertEqual(len(arm.entry_tiers), 3)
        self.assertFalse(arm.geometry_collapsed)

    def test_single_at_close_one_tier_at_close(self):
        arm = build_single_at_close_arm(close=100.0, atr=5.0)
        self.assertEqual(arm.status, "OK")
        self.assertEqual(len(arm.entry_tiers), 1)
        self.assertAlmostEqual(float(arm.entry_tiers[0]["limit"]), 100.0)

    def test_arm_disaster_stop_below_blended_and_positive_risk(self):
        stop = arm_disaster_stop(arm_blended=100.0, atr=5.0, close=100.0)
        self.assertLess(stop, 100.0)
        self.assertGreater(100.0 - stop, 0.0)

    def test_arm_disaster_stop_gap_down_recomputes_off_lower_blended(self):
        # Gap-down: blended_entry = 90 while close = 100; stop should be
        # based on blended not close, and must be < 90.
        stop = arm_disaster_stop(arm_blended=90.0, atr=5.0, close=100.0)
        self.assertLess(stop, 90.0)
        self.assertGreater(90.0 - stop, 0.0)

    def test_narrow_tiers_no_structure_atr_zero(self):
        arm = build_narrow_tiers_arm(
            close=100.0, atr=0.0, min_spacing_mult=0.5, min_stop_dist_mult=0.5
        )
        self.assertEqual(arm.status, "NO_STRUCTURE")

    def test_narrow_tiers_no_structure_close_zero(self):
        arm = build_narrow_tiers_arm(
            close=0.0, atr=5.0, min_spacing_mult=0.5, min_stop_dist_mult=0.5
        )
        self.assertEqual(arm.status, "NO_STRUCTURE")

    def test_single_at_close_no_structure_atr_zero(self):
        arm = build_single_at_close_arm(close=100.0, atr=0.0)
        self.assertEqual(arm.status, "NO_STRUCTURE")

    def test_single_at_close_just_below_mult_shifts_tier(self):
        # just_below_mult=0.1 places tier at close - 0.1*atr = 100 - 0.5 = 99.5
        arm = build_single_at_close_arm(close=100.0, atr=5.0, just_below_mult=0.1)
        self.assertEqual(arm.status, "OK")
        self.assertEqual(len(arm.entry_tiers), 1)
        self.assertAlmostEqual(float(arm.entry_tiers[0]["limit"]), 99.5)

    def test_build_baseline_arm_passthrough(self):
        from alphalens_pipeline.thematic.trade_setup.entry_primitives import (
            ArmSetup,
            build_baseline_arm,
        )

        source = {
            "status": "OK",
            "entry_tiers": ({"limit": 95.0, "weight": 1.0},),
            "tp_tranches": ({"price": 110.0, "weight": 1.0},),
            "disaster_stop": 88.0,
        }
        arm = build_baseline_arm(source)
        self.assertIsInstance(arm, ArmSetup)
        self.assertEqual(arm.status, "OK")
        self.assertEqual(arm.entry_tiers, source["entry_tiers"])
        self.assertEqual(arm.tp_tranches, source["tp_tranches"])
        self.assertEqual(arm.disaster_stop, 88.0)

    def test_arm_disaster_stop_k_override(self):
        # With k=2.0 the stop should be further below blended than with default k=1.0
        stop_default = arm_disaster_stop(arm_blended=100.0, atr=5.0, close=100.0)
        stop_wide = arm_disaster_stop(arm_blended=100.0, atr=5.0, close=100.0, k=2.0)
        self.assertLess(stop_wide, stop_default)

    def test_stop_atr_buffer_k_re_export(self):
        from alphalens_pipeline.thematic.trade_setup import builder
        from alphalens_pipeline.thematic.trade_setup.entry_primitives import STOP_ATR_BUFFER_K

        self.assertEqual(STOP_ATR_BUFFER_K, builder._STOP_ATR_BUFFER)


class TestNaNGuards(unittest.TestCase):
    """NaN inputs must produce NO_STRUCTURE / NaN, never propagate silently."""

    def test_narrow_tiers_nan_close_returns_no_structure(self):
        arm = build_narrow_tiers_arm(
            close=float("nan"),
            atr=10.0,
            min_spacing_mult=0.05,
            min_stop_dist_mult=0.05,
        )
        self.assertEqual(arm.status, "NO_STRUCTURE")

    def test_narrow_tiers_nan_atr_returns_no_structure(self):
        arm = build_narrow_tiers_arm(
            close=100.0,
            atr=float("nan"),
            min_spacing_mult=0.05,
            min_stop_dist_mult=0.05,
        )
        self.assertEqual(arm.status, "NO_STRUCTURE")

    def test_single_at_close_nan_atr_returns_no_structure(self):
        arm = build_single_at_close_arm(close=100.0, atr=float("nan"))
        self.assertEqual(arm.status, "NO_STRUCTURE")

    def test_single_at_close_nan_close_returns_no_structure(self):
        arm = build_single_at_close_arm(close=float("nan"), atr=10.0)
        self.assertEqual(arm.status, "NO_STRUCTURE")

    def test_arm_disaster_stop_nan_blended_returns_nan(self):
        import math

        result = arm_disaster_stop(arm_blended=float("nan"), atr=10.0, close=100.0)
        self.assertTrue(math.isnan(result))

    def test_arm_disaster_stop_nan_atr_returns_nan(self):
        import math

        result = arm_disaster_stop(arm_blended=100.0, atr=float("nan"), close=100.0)
        self.assertTrue(math.isnan(result))

    def test_arm_disaster_stop_nan_close_returns_nan(self):
        import math

        result = arm_disaster_stop(arm_blended=100.0, atr=10.0, close=float("nan"))
        self.assertTrue(math.isnan(result))


if __name__ == "__main__":
    unittest.main()
