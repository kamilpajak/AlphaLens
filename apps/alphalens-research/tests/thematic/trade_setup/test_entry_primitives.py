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


if __name__ == "__main__":
    unittest.main()
