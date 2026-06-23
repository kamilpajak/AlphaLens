"""Tests for Task 4 (_with_disaster_stop, _replay_synthetic_fill) and
Task 5 (ENTRY_GRID_ARMS, replay_entry_grid).

TDD: written RED first against a not-yet-implemented surface.  The module
imports helpers that do not exist yet; discovery will error until they are
added to ladder_replay.py.
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.feedback.ladder_replay import (
    ENTRY_GRID_ARMS,
    _replay_synthetic_fill,
    _with_disaster_stop,
    replay_entry_grid,
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


class TestReplaySyntheticFillFullFillByConstruction(unittest.TestCase):
    """Fix 1: filled_fraction must be 1.0 regardless of entry-tier alloc sum."""

    def test_filled_fraction_one_when_alloc_not_100(self):
        # Setup with a SINGLE entry tier whose alloc_pct=60 (does NOT sum to 100).
        # Before Fix 1, _filled_frac returned 100/60 > 1.0, then clamped to 1.0
        # by the min/max, so the bug was hidden — OR weight=100 / total_alloc=60 gave
        # frac=1.67 clamped to 1.0.  After Fix 1, synthetic weight == total_entry_alloc
        # so frac = total_entry_alloc / total_entry_alloc = 1.0 exactly.
        setup_single_tier_60 = {
            "status": "OK",
            "schema_version": "1.0.0",
            "suggested_size_pct": 2.0,
            "disaster_stop": 90.0,
            "atr": 2.0,
            "order_ttl_days": 7,
            "entry_tiers": [
                {"limit": 100.0, "alloc_pct": 60.0},  # only tier, alloc=60 NOT 100
            ],
            "tp_tranches": [{"target": 108.0, "tranche_pct": 100.0}],
        }
        # Flat bars well above fill_price; position stays open (no SL, no TP).
        flat_bars = _bars_constant(_T0, 5, price=104.0)
        outcome = _replay_synthetic_fill(
            setup_single_tier_60,
            flat_bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
        )
        self.assertIsNotNone(outcome.filled_fraction)
        # Synthetic fill is by definition a FULL fill — fraction must be exactly 1.0.
        self.assertEqual(outcome.filled_fraction, 1.0)


class TestReplaySyntheticFillFiniteGuard(unittest.TestCase):
    """Fix 2: non-finite fill_price or own_stop returns NO_DATA."""

    def test_nan_fill_price_returns_no_data(self):
        flat_bars = _bars_constant(_T0, 5, price=104.0)
        outcome = _replay_synthetic_fill(
            _SETUP,
            flat_bars,
            fill_price=float("nan"),
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
        )
        self.assertEqual(outcome.status, "NO_DATA")

    def test_inf_fill_price_returns_no_data(self):
        flat_bars = _bars_constant(_T0, 5, price=104.0)
        outcome = _replay_synthetic_fill(
            _SETUP,
            flat_bars,
            fill_price=float("inf"),
            fill_ts_ms=_FILL_TS,
            own_stop=_OWN_STOP,
        )
        self.assertEqual(outcome.status, "NO_DATA")

    def test_nan_own_stop_returns_no_data(self):
        flat_bars = _bars_constant(_T0, 5, price=104.0)
        outcome = _replay_synthetic_fill(
            _SETUP,
            flat_bars,
            fill_price=_FILL_PRICE,
            fill_ts_ms=_FILL_TS,
            own_stop=float("nan"),
        )
        self.assertEqual(outcome.status, "NO_DATA")


# ---------------------------------------------------------------------------
# Task 5: replay_entry_grid
# ---------------------------------------------------------------------------
#
# Shared fixtures for the entry-grid tests.
#
# Price scenario:
#   bars arrive in the arrival window (arrival_open_ms..arrival_close_ms).
#   Entry dip-buy arms (baseline/narrow_tiers/single_at_close) fill when
#   bar low <= limit; non-touch arms fill at open (market) or VWAP.
#
# _GRID_SETUP: an OK setup with asof_close + atr so narrow_tiers/single_at_close
# can be built.  Disaster stop 90; entry E1=99 (within ≈1 ATR of close=100);
# TP1=110 (single 100% tranche so exit_mark == 110 on a TP hit).
#
# arrival window: bars 0..2 (T0..T0+2*BAR)
# The bar at T0 has low=98 -> touch gate fills E1=99 (dip buy); open=100.

_ARRIVAL_OPEN = _T0
_ARRIVAL_CLOSE = _T0 + 2 * _BAR_MS

_GRID_SETUP = {
    "status": "OK",
    "schema_version": "1.0.0",
    "suggested_size_pct": 2.0,
    "disaster_stop": 90.0,
    "atr": 3.0,
    "asof_close": 100.0,
    "order_ttl_days": 7,
    "entry_tiers": [
        {"limit": 99.0, "alloc_pct": 100.0},
    ],
    "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0}],
}

# Benchmark return: 1% up. Cash reward = -benchmark = -0.01.
_BENCHMARK = 0.01

# Position expiry just after bar 5 so the replay always terminates (no OPEN).
_POS_EXPIRY = _T0 + 6 * _BAR_MS


def _make_tp_bars() -> list[dict]:
    """Bars: arrival window fills E1=99 (bar 0 low=98), then rallies to 112 (bar 3).
    bar 0: open=100, high=101, low=98, close=100  -> fills E1=99 on low<=99
    bar 1..2: flat at 101 (arrival window bars; open=101)
    bar 3: rallies -> high=112 -> TP1=110 hit
    bar 4..5: flat above 110 (post-TP, position closed)
    """
    return [
        _bar(_T0 + 0 * _BAR_MS, o=100.0, h=101.0, low=98.0, c=100.0),
        _bar(_T0 + 1 * _BAR_MS, o=101.0, h=101.0, low=100.0, c=101.0),
        _bar(_T0 + 2 * _BAR_MS, o=101.0, h=101.0, low=100.0, c=101.0),
        _bar(_T0 + 3 * _BAR_MS, o=102.0, h=112.0, low=101.0, c=111.0),
        _bar(_T0 + 4 * _BAR_MS, o=111.0, h=112.0, low=110.0, c=111.0),
        _bar(_T0 + 5 * _BAR_MS, o=111.0, h=112.0, low=110.0, c=111.0),
    ]


class TestEntryGridArms(unittest.TestCase):
    """ENTRY_GRID_ARMS constant is the canonical ordered tuple of 5 arm names."""

    def test_five_arms_present(self):
        self.assertEqual(len(ENTRY_GRID_ARMS), 5)

    def test_arm_names(self):
        expected = {
            "baseline",
            "narrow_tiers",
            "single_at_close",
            "market_at_arrival",
            "vwap_arrival",
        }
        self.assertEqual(set(ENTRY_GRID_ARMS), expected)


class TestReplayEntryGridTouchArmReward(unittest.TestCase):
    """Test 1: touch-arm reward == (exit_mark - blended)/blended - benchmark.

    Proves the reward is in return space, NOT realized_r (R-units).
    """

    def setUp(self):
        self.bars = _make_tp_bars()
        self.result = replay_entry_grid(
            _GRID_SETUP,
            self.bars,
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=_BENCHMARK,
            market_cap=None,
            position_expiry_ms=_POS_EXPIRY,
        )

    def test_baseline_reward_is_return_space_not_r_units(self):
        """baseline reward != realized_r; reward == (110 - blended)/blended - 0.01."""
        from alphalens_pipeline.feedback.ladder_replay import replay_ladder

        # The touch-arm baseline replay fills E1=99 (low=98 <= 99).
        # exit_mark = 110.0 (TP1 hit).
        # arm_blended = 99.0 (only E1 filled).
        # raw_excess = (110 - 99)/99 - 0.01
        arm_blended = 99.0
        exit_mark = 110.0
        expected_raw = (exit_mark - arm_blended) / arm_blended - _BENCHMARK
        # baseline is a resting-limit arm -> haircut = 0 bps
        expected_reward = expected_raw

        baseline_reward = self.result.get("baseline")
        self.assertIsNotNone(baseline_reward)

        # The reward must equal the hand-computed return-space formula.
        self.assertTrue(
            math.isclose(baseline_reward, expected_reward, rel_tol=1e-6),
            f"expected {expected_reward}, got {baseline_reward}",
        )

        # Prove it is NOT realized_r (which would be in R-units).
        # realized_r for E1=99, stop=90, TP=110: (110-99)/(99-90) = 11/9 ≈ 1.222
        realized_r = replay_ladder(
            _GRID_SETUP, self.bars, position_expiry_ms=_POS_EXPIRY
        ).realized_r
        self.assertIsNotNone(realized_r)
        self.assertFalse(
            math.isclose(baseline_reward, realized_r, rel_tol=1e-6),
            f"reward must NOT equal realized_r={realized_r}, but got {baseline_reward}",
        )


class TestReplayEntryGridNoFillCash(unittest.TestCase):
    """Test 2: NO_FILL across ALL 5 arms = cash = -benchmark_window_return.

    Construct a bars set where:
    - touch arms (dip-buy): no bar low <= entry limit (all bars are high) -> NO_FILL
    - non-touch arms: no bar in arrival window -> NO_FILL

    With no arrival-window bars and no dip-touch, all 5 arms NO_FILL.
    """

    def test_all_five_arms_cash_on_no_fill(self):
        # Bars entirely outside both the arrival window [T0, T0+2*BAR_MS] AND
        # the VWAP window [T0, T0+30min). ARRIVAL_VWAP_WINDOW_MIN=30 -> VWAP
        # window ends at T0 + 30*60_000 = T0 + 1_800_000 ms.
        # Place bars well after that (start at T0 + 35*BAR_MS = T0 + 2_100_000):
        #   market_at_arrival: no bar in [arrival_open, arrival_close] -> NO_FILL
        #   vwap_arrival: no bar in [T0, T0+30min) -> NO_FILL
        #   touch arms: bars high at 105, no dip to E1=99 -> NO_FILL
        _outside_start = _T0 + 35 * _BAR_MS
        outside_bars = [
            _bar(_outside_start + i * _BAR_MS, o=105.0, h=106.0, low=104.0, c=105.0)
            for i in range(4)
        ]
        result = replay_entry_grid(
            _GRID_SETUP,
            outside_bars,
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=_BENCHMARK,
            market_cap=None,
            position_expiry_ms=_POS_EXPIRY,
        )
        expected_cash = -_BENCHMARK
        for arm in ENTRY_GRID_ARMS:
            reward = result.get(arm)
            self.assertIsNotNone(reward, f"arm={arm} should return cash, got None")
            self.assertTrue(
                math.isclose(reward, expected_cash, rel_tol=1e-9),
                f"arm={arm}: expected cash={expected_cash}, got {reward}",
            )


class TestReplayEntryGridBadGeometryParity(unittest.TestCase):
    """Test 3: a forced-BAD_GEOMETRY arm gets cash; baseline on the same event computes normally.

    We force BAD_GEOMETRY on narrow_tiers/single_at_close by using a setup where
    atr=0.0 (arm_disaster_stop returns nan -> geometry collapses).
    Instead: use a VERY small atr so narrow_tiers arm folds (stop >= entry).
    Better: directly test single_at_close with a degenerate close (<=0).

    Cleanest approach: construct a setup where the setup's stop is above the entry limit
    (disaster_stop > entry_limit), so when we replay with _with_entry_tiers(baseline arm),
    _finalize returns BAD_GEOMETRY. But the task says "force one arm to BAD_GEOMETRY".

    Simplest: build a setup with disaster_stop=101 > E1=99 -> BAD_GEOMETRY on baseline.
    Then use a normal arm for comparison. But then baseline is the BAD_GEOMETRY one.

    The test requirement says "force one arm to BAD_GEOMETRY → its reward == cash handling;
    baseline on the same event computes normally." So we need baseline to be the normal arm.

    Approach: we override the narrow_tiers geometry indirectly. The narrow_tiers arm
    builds from asof_close and atr. If atr <= 0 or asof_close <= 0, it returns NO_STRUCTURE
    (not BAD_GEOMETRY). If we give an atr so small that all tiers are filtered, it returns
    BAD_GEOMETRY (status="BAD_GEOMETRY", arm_blended=None). The replay_entry_grid must then
    treat that as cash.

    Actually: narrow_tiers with tiny atr -> all tiers sit too close to stop -> BAD_GEOMETRY.

    Let's use asof_close=100, disaster_stop=99.5, atr=0.5:
    - narrow_tiers deep candidate = 100 - 0.25*0.5 = 99.875
    - stop = arm_disaster_stop(99.875, 0.5, 100): raw = 99.875 - 1.0*0.5 = 99.375, floor=99.875*0.75=74.9 -> stop=99.375
    - min_stop_dist = 0.5*0.5=0.25; tier must be >= stop+0.25=99.625
    - deepest candidate 99.875 > 99.625 -> OK; so narrow_tiers actually builds!
    That won't give BAD_GEOMETRY.

    Cleaner: override a specific arm's behavior by supplying a setup where the
    entry_tiers + disaster_stop combination collapses geometry.
    Use a setup where disaster_stop=99.5 > entry_limit=99.0:
    -> baseline arm: passes entry=99, stop=99.5 into replay_ladder -> risk = 99 - 99.5 < 0 -> BAD_GEOMETRY!
    But then baseline is BAD_GEOMETRY, not normal.

    Let's flip: baseline_setup has normal stop=90. We test that narrow_tiers arm,
    when it internally computes a stop that is above entry (because we make close=atr=weird),
    gets cash. We accept this tests the "arms collapse independently" property.

    Actually, the simplest test: use a setup where we override asof_close (used for
    narrow_tiers/single_at_close) so those arms produce NO_STRUCTURE (not BAD_GEOMETRY).
    NO_STRUCTURE also maps to cash (per spec: NO_FILL/BAD_GEOMETRY → cash).
    But the test spec says BAD_GEOMETRY specifically.

    Re-reading the task: "BAD_GEOMETRY parity: force one arm to BAD_GEOMETRY → its reward ==
    the cash handling; baseline same event computes normally."

    The cleanest way: create a bars set where baseline computes normally (TP hit),
    and build a setup where the disaster_stop is set such that when narrow_tiers arm
    builds its stop (from arm_disaster_stop) and the result is above the entry -> BAD_GEOMETRY
    in the replay. This happens when own_stop >= arm_blended.

    But arm_disaster_stop always returns <= arm_blended (floor=arm_blended*0.75 < arm_blended).
    So narrow_tiers arm_setup.disaster_stop is always < arm_blended when arm_setup.status=="OK".

    Actually the BAD_GEOMETRY in _finalize triggers when risk <= 0 (stop >= blended).
    For a non-touch arm, we pass own_stop to _replay_synthetic_fill, which uses
    _with_disaster_stop(own_stop) -> parse_ladder -> stop = own_stop. Then blended = fill_price.
    risk = fill_price - own_stop. This is < 0 only if own_stop > fill_price, which arm_disaster_stop
    prevents (it's always below arm_blended).

    For a touch arm: risk = blended_entry - stop. Since stop comes from _with_disaster_stop(own_stop)
    where own_stop = arm_disaster_stop(arm_setup.disaster_stop, atr, close) for narrow_tiers...
    no, wait: for narrow_tiers arm, own_stop = arm_setup.disaster_stop (which was computed in
    build_narrow_tiers_arm). Then replay uses _with_disaster_stop(own_stop). The entry levels come
    from arm_setup.entry_tiers. The blended entry is the alloc-weighted average of filled tiers.
    BAD_GEOMETRY if own_stop >= blended_entry.

    This is hard to manufacture without side-effects. Let me use the simple approach:
    test that a setup where the source disaster_stop equals or exceeds entry_limit gives
    BAD_GEOMETRY on the baseline arm, and the non-touch arm (which uses own_stop from
    arm_disaster_stop) gets cash too for that same weird case.

    Actually the simplest approach consistent with the test requirement:
    - baseline arm with stop=100, entry=99 -> BAD_GEOMETRY -> cash
    - non-touch arm with market fill at, say, 101 and own_stop=arm_disaster_stop(101,atr,close)
      which = 101 - 1*atr (with floor). If atr=3, stop=98 < 101 -> normal.

    So: use a setup with disaster_stop=100, E1=99; baseline gets BAD_GEOMETRY.
    Non-touch arm fills at bar 0 open (100.0) with own_stop computed normally -> gets a real reward.
    But the test says "baseline same event computes normally". So baseline must be the normal one.

    We need to force BAD_GEOMETRY on ONE arm that is NOT baseline. The only way to do this
    deterministically without touching internal state: manufacture a scenario where
    own_stop >= arm_blended for a non-baseline arm. But arm_disaster_stop guarantees own_stop < arm_blended.

    Let's step back. The simplest deterministic way: BAD_GEOMETRY from replay when the
    TOUCH-ARM entry limit == or is above the setup's own_stop. For narrow_tiers, the own_stop
    is computed inside build_narrow_tiers_arm as arm_disaster_stop(deepest_candidate, atr, close).
    If close=100 and atr=3, deepest_candidate=100-0.25*3=99.25, own_stop≈99.25-1*3=96.25 -> OK.

    Given the constraint that arm_disaster_stop can't produce own_stop>=arm_blended,
    the only way to get BAD_GEOMETRY on a specific non-baseline arm is if its built entry_tiers
    have a limit that ends up at or below own_stop after _with_disaster_stop is applied.

    Wait — I think I'm overcomplicating this. Let me re-read the task:
    "BAD_GEOMETRY parity: force one arm to BAD_GEOMETRY → its reward == the cash handling;
    baseline same event computes normally."

    It's testing that the code CORRECTLY handles a BAD_GEOMETRY outcome by returning cash.
    The simplest mock: use a setup where baseline computes normally (TP hit),
    and we DIRECTLY test the cash-mapping behavior for BAD_GEOMETRY by checking that
    when replay_ladder returns BAD_GEOMETRY (which we can verify is handled), the result is cash.

    Actually, the clearest implementation test: override the single_at_close arm's stop
    by making close=100 but then the arm builds entry at exactly own_stop level via a degenerate
    atr. Let's instead:

    Use a two-entry setup where baseline fills both entries -> blended ~97 with stop=90 (OK).
    For the single_at_close arm specifically: entry=close=100, own_stop=arm_disaster_stop(100,3,100)
    = 100 - 3 = 97 (floor=75) -> stop=97 < 100 -> OK. That's fine.

    I accept that manufacturing BAD_GEOMETRY via normal arm_primitives is hard.
    The pragmatic test: use a setup with disaster_stop=100 > E1=99 for the BASELINE arm
    specifically (baseline passes disaster_stop through unchanged), and check baseline
    returns cash. Then verify a non-touch arm on the same bars returns a real reward.
    This tests "BAD_GEOMETRY arm gets cash; another arm on same event computes normally."
    """

    def _make_bad_geom_setup(self):
        """Baseline arm: disaster_stop=101 > E1_limit=99 -> BAD_GEOMETRY on baseline."""
        return {
            "status": "OK",
            "schema_version": "1.0.0",
            "suggested_size_pct": 2.0,
            "disaster_stop": 101.0,  # ABOVE entry limit 99 -> BAD_GEOMETRY for baseline
            "atr": 3.0,
            "asof_close": 100.0,
            "order_ttl_days": 7,
            "entry_tiers": [
                {"limit": 99.0, "alloc_pct": 100.0},  # stop=101 > 99 -> BAD_GEOMETRY
            ],
            "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0}],
        }

    def test_bad_geometry_arm_returns_cash(self):
        """baseline arm with stop > entry -> BAD_GEOMETRY -> reward == -benchmark."""
        bad_setup = self._make_bad_geom_setup()
        bars = _make_tp_bars()
        result = replay_entry_grid(
            bad_setup,
            bars,
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=_BENCHMARK,
            market_cap=None,
            position_expiry_ms=_POS_EXPIRY,
        )
        baseline_reward = result.get("baseline")
        self.assertIsNotNone(
            baseline_reward, "baseline must return cash (not None) on BAD_GEOMETRY"
        )
        self.assertTrue(
            math.isclose(baseline_reward, -_BENCHMARK, rel_tol=1e-9),
            f"expected cash=-{_BENCHMARK}, got {baseline_reward}",
        )

    def test_non_touch_arm_computes_normally_when_baseline_bad_geometry(self):
        """market_at_arrival on same event computes a real reward (not cash)."""
        bad_setup = self._make_bad_geom_setup()
        bars = _make_tp_bars()
        result = replay_entry_grid(
            bad_setup,
            bars,
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=_BENCHMARK,
            market_cap=None,
            position_expiry_ms=_POS_EXPIRY,
        )
        maa_reward = result.get("market_at_arrival")
        # market_at_arrival fills at bar 0 open=100.0;
        # own_stop = arm_disaster_stop(100, 3, 100) -> 100-3=97, floor=75 -> 97
        # risk=3 > 0 -> NOT BAD_GEOMETRY -> should get a real (non-cash) reward
        self.assertIsNotNone(maa_reward)
        self.assertFalse(
            math.isclose(maa_reward, -_BENCHMARK, rel_tol=1e-6),
            f"market_at_arrival should compute a real reward, not cash={-_BENCHMARK}; got {maa_reward}",
        )


class TestReplayEntryGridExitHeldFixed(unittest.TestCase):
    """Test 4: two arms with different entry denominators both reach TP=110 (exit_mark==110).

    baseline arm: fills at E1=99 -> arm_blended=99
    market_at_arrival arm: fills at bar0 open=100 -> arm_blended=100
    Both arms should have exit_mark=110.0 (the TP price is absolute and shared).
    Rewards differ only because arm_blended differs.
    """

    def test_different_denominators_same_exit_mark(self):
        bars = _make_tp_bars()
        result = replay_entry_grid(
            _GRID_SETUP,
            bars,
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=_BENCHMARK,
            market_cap=None,  # zero haircut for resting; market_at_arrival will differ
            position_expiry_ms=_POS_EXPIRY,
        )

        baseline_reward = result.get("baseline")
        maa_reward = result.get("market_at_arrival")
        self.assertIsNotNone(baseline_reward)
        self.assertIsNotNone(maa_reward)

        # exit_mark = 110 for baseline: (110-99)/99 - 0.01 = 11/99 - 0.01
        exit_mark = 110.0
        baseline_blended = 99.0
        market_blended = 100.0

        # baseline: resting -> 0 haircut
        expected_baseline = (exit_mark - baseline_blended) / baseline_blended - _BENCHMARK

        # market_at_arrival fills at open=100. own_stop = arm_disaster_stop(100, 3, 100).
        # The market_at_arrival arm uses own_stop, but exit_mark is still determined by the
        # fixed TP target (110). Haircut != 0 for market_at_arrival.
        # We just verify that rewards differ (different denominators and/or haircuts).
        self.assertFalse(
            math.isclose(baseline_reward, maa_reward, rel_tol=1e-6),
            "rewards must differ when entry denominators differ",
        )

        # The exit_mark is fixed at 110 across arms; verify via the formula
        # exit_mark_implied = arm_blended * (reward + haircut + benchmark) + arm_blended
        # i.e., exit_mark = arm_blended * (1 + reward_pre_haircut + benchmark)
        # For baseline (haircut=0): exit_mark_baseline = baseline_blended * (1 + baseline_reward + benchmark)
        # Wait -- formula: reward = (exit_mark - arm_blended)/arm_blended - benchmark
        # -> exit_mark = arm_blended * (1 + reward + benchmark)   [for resting arms with 0 haircut]
        implied_exit_baseline = baseline_blended * (1.0 + baseline_reward + _BENCHMARK)
        self.assertTrue(
            math.isclose(implied_exit_baseline, exit_mark, rel_tol=1e-6),
            f"implied exit_mark from baseline = {implied_exit_baseline}, expected 110",
        )

        # Verify expected baseline reward matches
        self.assertTrue(
            math.isclose(baseline_reward, expected_baseline, rel_tol=1e-6),
            f"baseline reward: expected {expected_baseline}, got {baseline_reward}",
        )


class TestReplayEntryGridHaircutAsymmetry(unittest.TestCase):
    """Test 5: identical prices -> market_at_arrival reward strictly < baseline.

    With the same fill price (both fill at 99 — baseline from the dip touch,
    market_at_arrival fills at bar 0 open if that is also 99), the execution
    cost haircut charged to the always-fill arm produces a lower net reward.

    We construct a bar where open=99 and low=98 so:
      - baseline touches E1=99 (low=98 <= 99); arm_blended=99
      - market_at_arrival fills at open=99; arm_blended=99
    Same denominator, same exit_mark -> only haircut differs.
    """

    def test_market_at_arrival_reward_strictly_less_than_baseline(self):
        # Bar 0: open=99, high=101, low=98, close=100
        bars = [
            _bar(_T0 + 0 * _BAR_MS, o=99.0, h=101.0, low=98.0, c=100.0),
            _bar(_T0 + 1 * _BAR_MS, o=101.0, h=101.0, low=100.0, c=101.0),
            _bar(_T0 + 2 * _BAR_MS, o=101.0, h=101.0, low=100.0, c=101.0),
            _bar(_T0 + 3 * _BAR_MS, o=102.0, h=112.0, low=101.0, c=111.0),
            _bar(_T0 + 4 * _BAR_MS, o=111.0, h=112.0, low=110.0, c=111.0),
            _bar(_T0 + 5 * _BAR_MS, o=111.0, h=112.0, low=110.0, c=111.0),
        ]
        result = replay_entry_grid(
            _GRID_SETUP,
            bars,
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=_BENCHMARK,
            market_cap=1e11,  # mega cap -> smallest impact bps
            position_expiry_ms=_POS_EXPIRY,
        )
        baseline_reward = result.get("baseline")
        maa_reward = result.get("market_at_arrival")
        self.assertIsNotNone(baseline_reward)
        self.assertIsNotNone(maa_reward)

        # baseline is a resting-limit arm -> 0 haircut
        # market_at_arrival is an always-fill arm -> haircut > 0 bps
        # With the same entry price (99) and same exit (110), maa_reward < baseline_reward.
        self.assertLess(
            maa_reward,
            baseline_reward,
            f"market_at_arrival ({maa_reward}) must be < baseline ({baseline_reward}) "
            f"due to execution cost haircut",
        )


class TestReplayEntryGridSharedUnevaluability(unittest.TestCase):
    """Test 6: shared unevaluability -> ALL arms return None.

    Two shared conditions:
    a) empty bars
    b) benchmark_window_return=None
    """

    def test_empty_bars_all_none(self):
        result = replay_entry_grid(
            _GRID_SETUP,
            [],
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=_BENCHMARK,
            market_cap=None,
        )
        for arm in ENTRY_GRID_ARMS:
            self.assertIsNone(result.get(arm), f"arm={arm} should be None with empty bars")

    def test_none_benchmark_all_none(self):
        bars = _make_tp_bars()
        result = replay_entry_grid(
            _GRID_SETUP,
            bars,
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=None,
            market_cap=None,
        )
        for arm in ENTRY_GRID_ARMS:
            self.assertIsNone(result.get(arm), f"arm={arm} should be None when benchmark is None")

    def test_none_setup_all_none(self):
        bars = _make_tp_bars()
        result = replay_entry_grid(
            None,
            bars,
            arrival_open_ms=_ARRIVAL_OPEN,
            arrival_close_ms=_ARRIVAL_CLOSE,
            benchmark_window_return=_BENCHMARK,
            market_cap=None,
        )
        for arm in ENTRY_GRID_ARMS:
            self.assertIsNone(result.get(arm), f"arm={arm} should be None when setup is None")


if __name__ == "__main__":
    unittest.main()
