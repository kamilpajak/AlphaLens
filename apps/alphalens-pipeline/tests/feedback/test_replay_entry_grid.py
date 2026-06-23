"""Tests for Task 4: _with_disaster_stop and _replay_synthetic_fill.

TDD: written RED first against a not-yet-implemented surface.  The module
imports two helpers that do not exist yet; discovery will error until they are
added to ladder_replay.py.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.feedback.ladder_replay import (
    _replay_synthetic_fill,
    _with_disaster_stop,
)

# ---------------------------------------------------------------------------
# Helpers: synthetic OHLC bar builders (modelled on test_ladder_chart_payload)
# ---------------------------------------------------------------------------

_T0 = 1_000_000  # epoch ms anchor (arbitrary, avoids real-calendar dependency)
_BAR_MS = 60_000  # 1-minute bar width


def _bar(ts_ms: int, *, o: float, h: float, low: float, c: float) -> dict:
    """Minimal OHLCV bar dict accepted by _LadderWalk.step."""
    return {"t": ts_ms, "o": o, "h": h, "l": low, "c": c, "v": 1_000.0}


def _bars_constant(start_ms: int, n: int, price: float) -> list[dict]:
    """n flat bars starting at start_ms, each bar open==high==low==close==price."""
    return [_bar(start_ms + i * _BAR_MS, o=price, h=price, low=price, c=price) for i in range(n)]


# ---------------------------------------------------------------------------
# A minimal OK trade setup fixture
#
# Entry tiers: E1=100, E2=95 (dip-buy ladder)
# TP tranches:  TP1=108 (100 % tranche, all-in exit)
# Disaster stop: 90.0  (R = 100 - 90 = 10)
# The OWN_STOP used in synthetic tests differs (= 93.0) to prove the gate.
# ---------------------------------------------------------------------------

_SETUP = {
    "status": "OK",
    "schema_version": "1.0.0",
    "suggested_size_pct": 2.0,
    "disaster_stop": 90.0,
    "atr": 2.0,
    "order_ttl_days": 7,
    "entry_tiers": [
        {"limit": 100.0, "alloc_pct": 50.0},
        {"limit": 95.0, "alloc_pct": 50.0},
    ],
    "tp_tranches": [{"target": 108.0, "tranche_pct": 100.0}],
}

_OWN_STOP = 93.0  # deliberately different from _SETUP["disaster_stop"]=90.0
_FILL_PRICE = 102.0  # above all entry limits -> touch gate would NEVER fill here
_FILL_TS = _T0


class TestWithDisasterStop(unittest.TestCase):
    """Unit tests for _with_disaster_stop."""

    def test_swaps_only_disaster_stop(self):
        """The returned dict has the new stop and leaves other keys untouched."""
        result = _with_disaster_stop(_SETUP, 97.5)

        self.assertEqual(result["disaster_stop"], 97.5)
        # entry_tiers and tp_tranches must be the SAME objects (shallow copy)
        self.assertIs(result["entry_tiers"], _SETUP["entry_tiers"])
        self.assertIs(result["tp_tranches"], _SETUP["tp_tranches"])
        self.assertEqual(result["status"], _SETUP["status"])

    def test_does_not_mutate_source(self):
        """Source dict is NOT modified."""
        original_stop = _SETUP["disaster_stop"]
        _with_disaster_stop(_SETUP, 42.0)
        self.assertEqual(_SETUP["disaster_stop"], original_stop)

    def test_returns_new_dict(self):
        """Returns a new dict, not the original."""
        result = _with_disaster_stop(_SETUP, 99.0)
        self.assertIsNot(result, _SETUP)


class TestReplaySyntheticFillTouchGateBypassed(unittest.TestCase):
    """_replay_synthetic_fill fills at fill_price even when no bar's low <= fill_price."""

    def test_fill_above_all_bar_lows(self):
        # fill_price=102, but every bar has low=105 -> standard replay would NEVER fill
        high_bars = [
            _bar(_T0 + i * _BAR_MS, o=106.0, h=109.0, low=105.0, c=106.0) for i in range(5)
        ]
        outcome = _replay_synthetic_fill(
            _SETUP,
            high_bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
        )
        # A real fill must have happened (not NO_FILL) even though no bar dipped
        # to fill_price via the low<=limit touch gate.
        self.assertNotEqual(outcome.classification, "NO_FILL")
        self.assertIsNotNone(outcome.blended_entry)

    def test_blended_entry_equals_fill_price(self):
        # Flat bars well above fill_price -> position stays open
        high_bars = _bars_constant(_T0, 10, price=106.0)
        outcome = _replay_synthetic_fill(
            _SETUP,
            high_bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
        )
        self.assertAlmostEqual(outcome.blended_entry, _FILL_PRICE)


class TestReplaySyntheticFillOwnStop(unittest.TestCase):
    """own_stop is the SL used, not the source disaster_stop."""

    def test_own_stop_triggers_sl_not_source_stop(self):
        # Price path: drops to 92.0, which is:
        #   - below own_stop=93.0 -> should trigger SL
        #   - above source disaster_stop=90.0 -> would NOT trigger the source SL
        # So if own_stop is used, we get SL_HIT; if source stop is used, no SL.
        sl_bars = [
            # First bar: price holds above own_stop
            _bar(_T0, o=102.0, h=103.0, low=94.0, c=102.0),
            # Second bar: dips to 92.0 (below own_stop=93, above source_stop=90)
            _bar(_T0 + _BAR_MS, o=101.0, h=101.0, low=92.0, c=100.0),
        ]
        outcome = _replay_synthetic_fill(
            _SETUP,
            sl_bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
        )
        self.assertTrue(outcome.sl_hit, "SL must fire at own_stop=93, not source stop=90")
        self.assertEqual(outcome.classification, "SL_HIT")

    def test_source_stop_below_own_stop_no_sl_until_own_stop(self):
        # Price dips to 91 (below source_stop=90 would also fire, but above own_stop=93
        # means IF own_stop were 93 it fires; here we use own_stop=91.5 so price at 91 fires).
        # Using own_stop=91.5: price=91 dips below 91.5 -> SL_HIT
        bars = [
            _bar(_T0, o=102.0, h=103.0, low=91.0, c=100.0),
        ]
        outcome = _replay_synthetic_fill(
            _SETUP,
            bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=91.5,  # between source_stop=90 and price=91
        )
        self.assertTrue(outcome.sl_hit)


class TestReplaySyntheticFillTPExit(unittest.TestCase):
    """exit_mark / terminal state comes from the fixed absolute TP target."""

    def test_tp_hit_gives_tp_full_classification(self):
        # Price rallies from 102 to 109 (above TP1=108)
        tp_bars = [
            _bar(_T0, o=102.0, h=109.0, low=101.0, c=109.0),
        ]
        outcome = _replay_synthetic_fill(
            _SETUP,
            tp_bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
        )
        self.assertEqual(outcome.classification, "TP_FULL")
        self.assertIn("TP1", outcome.tps_hit)

    def test_realized_r_is_positive_on_tp(self):
        # fill_price=102, own_stop=93, TP1=108
        # risk = 102-93 = 9; realized_r = (108-102)/9 = 6/9 ≈ 0.667
        tp_bars = [
            _bar(_T0, o=102.0, h=110.0, low=101.0, c=110.0),
        ]
        outcome = _replay_synthetic_fill(
            _SETUP,
            tp_bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
        )
        self.assertIsNotNone(outcome.realized_r)
        assert outcome.realized_r is not None
        self.assertGreater(outcome.realized_r, 0.0)
        expected_r = (108.0 - _FILL_PRICE) / (_FILL_PRICE - _OWN_STOP)
        self.assertAlmostEqual(outcome.realized_r, expected_r, places=6)

    def test_position_expiry_ms_time_stop(self):
        # Flat bars at 104 (above own_stop, below TP1=108).
        # Set position_expiry_ms to the 3rd bar's ts -> time-stop fires there.
        t3 = _T0 + 2 * _BAR_MS
        flat_bars = _bars_constant(_T0, 5, price=104.0)
        outcome = _replay_synthetic_fill(
            _SETUP,
            flat_bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
            position_expiry_ms=t3,
        )
        self.assertEqual(outcome.classification, "TIME_STOP")
        self.assertFalse(outcome.sl_hit)
        self.assertFalse(outcome.horizon_open)


if __name__ == "__main__":
    unittest.main()
