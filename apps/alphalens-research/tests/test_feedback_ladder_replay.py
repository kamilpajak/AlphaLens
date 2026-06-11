"""Tests for the broker-free ladder-outcome replay engine.

Design memo: ``docs/research/ladder_order_ideal_scenario_2026_06_03.md`` §5.0/§5.1.

The engine is PURE: a parsed trade-setup dict + OHLC bars in, a ``LadderOutcome``
out. These tests pin the load-bearing math (the partial-fill realized-R re-basing
bug-#1 fix), the geometry / ambiguity / monotonicity edge cases (bugs #2-#4), the
MFE / MAE / forward-return substrate layer, and the ratchet what-if pass.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.feedback.ladder_replay import (
    GRID_CONFIGS,
    parse_ladder,
    realized_r_full_fill,
    replay_ladder,
    replay_ladder_grid,
)


def _bar(t: int, low: float, high: float, close: float) -> dict:
    return {"t": t, "l": low, "h": high, "c": close}


def _setup(
    *,
    entries: list[tuple[float, float]],
    tps: list[tuple[float, float]],
    stop: float,
    status: str = "OK",
    atr: float | None = None,
) -> dict:
    """Build a brief_trade_setup dict. entries/tps = list of (price, weight_pct)."""
    setup: dict = {
        "status": status,
        "disaster_stop": stop,
        "entry_tiers": [{"limit": p, "alloc_pct": w} for p, w in entries],
        "tp_tranches": [{"target": p, "tranche_pct": w} for p, w in tps],
    }
    if atr is not None:
        setup["atr"] = atr
    return setup


# Three equal-alloc entries (E1=99, E2=97, E3=95), SL=92, three TPs.
_EQUAL_3 = {
    "entries": [(99.0, 33.3), (97.0, 33.3), (95.0, 33.3)],
    "tps": [(102.0, 33.3), (107.0, 33.3), (112.0, 33.3)],
    "stop": 92.0,
}


class TestPostExitFillsDoNotCorruptHeadline(unittest.TestCase):
    """zen HIGH: once the as-specified position has EXITED it is flat — a later
    dip to a still-unfilled deeper entry tier must NOT be added to ``filled``
    (which would retroactively change blended_entry / filled_frac / realized_r),
    and post-exit bars must NOT extend the in-trade MFE/MAE window."""

    def test_post_exit_dip_does_not_fill_unused_tier(self):
        # E1=100, E2=90 (equal alloc), SL=80, TPs 110/120/130. Only E1 fills,
        # then all three TPs hit (exit_reached), then a late bar dips to E2's
        # limit (90). E2 must NOT fill; blended stays 100; realized_r unchanged.
        setup = _setup(
            entries=[(100.0, 50.0), (90.0, 50.0)],
            tps=[(110.0, 33.3), (120.0, 33.3), (130.0, 33.3)],
            stop=80.0,
        )
        bars = [
            _bar(1, low=100.0, high=101.0, close=100.5),  # fills E1 only
            _bar(2, low=105.0, high=130.0, close=129.0),  # all 3 TPs hit -> flat
            _bar(3, low=90.0, high=95.0, close=92.0),  # dip to E2 limit AFTER exit
        ]
        outcome = replay_ladder(setup, bars)
        self.assertEqual(outcome.entries_filled, ("E1",))  # E2 NOT filled post-exit
        self.assertAlmostEqual(outcome.blended_entry, 100.0, places=3)
        self.assertEqual(outcome.classification, "TP_FULL")
        # in-trade MAE must not include the post-exit dip to 90 (low while held
        # was 100 on bar 1 -> MAE 0, not negative from the post-exit 90).
        self.assertGreaterEqual(outcome.mae, 0.0)


class TestReplayLadderGrid(unittest.TestCase):
    """PR-2: re-replay the SAME bars under alternate EXIT ladders to separate
    ladder-capture from selection. The candidate (bars), entry, and stop are held
    fixed; only the TP policy varies."""

    # E1=100 (full alloc), SL=90 (risk=10), TPs 110/120/130. Path: fill E1, hit
    # TP1 (110) only, then reverse down to the stop (90).
    _SETUP = {
        "entries": [(100.0, 100.0)],
        "tps": [(110.0, 33.3), (120.0, 33.3), (130.0, 33.3)],
        "stop": 90.0,
    }
    _BARS = [
        _bar(1, low=100.0, high=101.0, close=100.5),  # fills E1
        _bar(2, low=105.0, high=112.0, close=108.0),  # TP1 (110) only
        _bar(3, low=88.0, high=95.0, close=90.0),  # reverse to SL (90)
    ]

    def test_grid_has_all_config_keys(self):
        grid = replay_ladder_grid(_setup(**self._SETUP), self._BARS)
        self.assertEqual(set(grid), set(GRID_CONFIGS))

    def test_exit_policy_separates_capture_on_one_path(self):
        # Same price path, three exit ladders -> different realized R, proving the
        # grid isolates trade-management from the (fixed) pick.
        grid = replay_ladder_grid(_setup(**self._SETUP), self._BARS)
        # Bank everything at TP1 -> captured the +1R move before the reversal.
        self.assertGreater(grid["single_tp_first"], 0.0)
        # Hold for the top target (130, never reached) -> rode down to the stop.
        self.assertLess(grid["single_tp_last"], 0.0)
        # No profit-taking -> rode to the stop too.
        self.assertLess(grid["no_tp_ride"], 0.0)
        # Early TP strictly beat letting it ride on THIS path.
        self.assertGreater(grid["single_tp_first"], grid["no_tp_ride"])

    def test_no_tps_yields_none_for_single_tp_configs(self):
        setup = _setup(entries=[(100.0, 100.0)], tps=[], stop=90.0)
        grid = replay_ladder_grid(setup, self._BARS)
        self.assertIsNone(grid["single_tp_first"])
        self.assertIsNone(grid["single_tp_last"])
        # no_tp_ride is still defined (it never needed TPs).
        self.assertIsNotNone(grid["no_tp_ride"])

    def test_unparseable_setup_or_no_bars_returns_all_none(self):
        all_none = dict.fromkeys(GRID_CONFIGS)
        self.assertEqual(replay_ladder_grid(None, self._BARS), all_none)
        self.assertEqual(replay_ladder_grid(_setup(**self._SETUP), []), all_none)

    def test_grid_holds_entry_and_stop_fixed(self):
        # The single_tp_first config must use the SAME entry + stop as as-specified
        # (only the TP ladder changed): a path that fills E1 and banks TP1 at 100%
        # gives exactly +1R (TP1=110, entry=100, risk=10).
        grid = replay_ladder_grid(_setup(**self._SETUP), self._BARS)
        self.assertAlmostEqual(grid["single_tp_first"], 1.0, places=2)


class TestRealizedRFullFill(unittest.TestCase):
    """PR-3: the entry-side counterfactual. Replay the exit ladder from the
    all-tier (full-fill) blended entry, holding the exit ladder + bars fixed, so
    the gap vs the as-specified realized_r is the entry-tier-spacing drag."""

    # E1=100, E2=96 equal alloc -> full blend = 98. SL=90, single TP at 108.
    _SETUP = {
        "entries": [(100.0, 50.0), (96.0, 50.0)],
        "tps": [(108.0, 100.0)],
        "stop": 90.0,
    }
    # Dips to 97 (fills the 98 full-blend limit AND E1@100, but NOT E2@96), then
    # rallies through TP1 (108).
    _BARS = [
        _bar(1, low=97.0, high=101.0, close=99.0),
        _bar(2, low=105.0, high=108.0, close=107.0),
    ]

    def test_full_fill_uses_all_tier_blend_and_beats_partial(self):
        full = realized_r_full_fill(_setup(**self._SETUP), self._BARS)
        actual = replay_ladder(_setup(**self._SETUP), self._BARS).realized_r
        # Full-fill entry = 98 -> R = (108-98)/(98-90) = 1.25.
        self.assertAlmostEqual(full, 1.25, places=2)
        # As-specified only filled E1@100 -> R = (108-100)/(100-90) = 0.8. The
        # full-fill counterfactual is the BETTER (deeper) entry, so the partial
        # fill left capture on the table -> entry-fill drag is positive.
        self.assertAlmostEqual(actual, 0.8, places=2)
        self.assertGreater(full, actual)

    def test_none_for_unparseable_or_no_bars(self):
        self.assertIsNone(realized_r_full_fill(None, self._BARS))
        self.assertIsNone(realized_r_full_fill(_setup(**self._SETUP), []))

    def test_single_tier_setup_full_fill_equals_as_specified(self):
        # With ONE entry tier, the full-fill blend IS that tier, so the
        # counterfactual must equal the as-specified realized_r on a path that
        # fills it.
        setup = _setup(entries=[(100.0, 100.0)], tps=[(110.0, 100.0)], stop=90.0)
        bars = [
            _bar(1, low=99.0, high=101.0, close=100.0),
            _bar(2, low=108.0, high=110.0, close=109.0),
        ]
        self.assertAlmostEqual(
            realized_r_full_fill(setup, bars),
            replay_ladder(setup, bars).realized_r,
            places=4,
        )


class TestParseLadder(unittest.TestCase):
    """Step 2: parse_ladder exposes total_entry_alloc + atr."""

    def test_total_entry_alloc_is_sum_of_filled_weights(self):
        parsed = parse_ladder(_setup(**_EQUAL_3))
        self.assertTrue(parsed.ok)
        self.assertAlmostEqual(parsed.total_entry_alloc, 99.9)

    def test_atr_parsed_when_present(self):
        parsed = parse_ladder(_setup(**_EQUAL_3, atr=2.5))
        self.assertEqual(parsed.atr, 2.5)

    def test_atr_none_when_absent(self):
        parsed = parse_ladder(_setup(**_EQUAL_3))
        self.assertIsNone(parsed.atr)

    def test_not_ok_setup_returns_not_ok(self):
        self.assertFalse(parse_ladder(_setup(**_EQUAL_3, status="NO_STRUCTURE")).ok)
        self.assertFalse(parse_ladder(None).ok)


class TestRealizedRPartialFill(unittest.TestCase):
    """Step 3 (bug #1): TP tranche weights re-based over the FILLED fraction.

    Worked example from the plan: E1=99/E2=97/E3=95 equal alloc, SL=92,
    TP1=102/TP2=107/TP3=112. Only E1+E2 fill (blended=98, risk=6, filled_frac=2/3).
    TP1 hits then SL. realized_r = +0.333 - 0.5 = -0.167.
    """

    def test_partial_fill_tp1_then_sl_rebased(self):
        # E3 sits BELOW the stop (85 < SL 92) so a dip to the stop fills only
        # E1+E2 (the touch model fills every limit AT or ABOVE the bar low, and
        # E3=85 is below 92). This realises the worked example: blended=98,
        # risk=6, filled_frac=2/3, TP1 then SL.
        setup = _setup(
            entries=[(99.0, 33.3), (97.0, 33.3), (85.0, 33.3)],
            tps=[(102.0, 33.3), (107.0, 33.3), (112.0, 33.3)],
            stop=92.0,
        )
        bars = [
            _bar(1, low=97.0, high=99.0, close=98.0),  # fills E1 (99) + E2 (97), NOT E3 (85)
            _bar(2, low=98.0, high=102.0, close=101.0),  # TP1 (102) hit
            _bar(3, low=92.0, high=99.0, close=93.0),  # SL (92) hit, still above E3 (85)
        ]
        outcome = replay_ladder(setup, bars)
        self.assertEqual(outcome.status, "OK")
        self.assertEqual(outcome.entries_filled, ("E1", "E2"))
        self.assertEqual(outcome.tps_hit, ("TP1",))
        self.assertTrue(outcome.sl_hit)
        self.assertAlmostEqual(outcome.blended_entry, 98.0, places=3)
        self.assertEqual(outcome.classification, "PARTIAL_TP_THEN_SL")
        self.assertAlmostEqual(outcome.realized_r, -0.167, places=3)

    def test_all_tiers_fill_reduces_to_full_position_weighting(self):
        # When ALL tiers fill (filled_frac=1) the re-based formula must reduce
        # EXACTLY to full-position tranche weighting. blended = mean(99,97,95)=97,
        # risk = 97-92 = 5. TP1 hit (share 1/3) then SL on the remaining 2/3.
        # TP1 contrib = (1/3)*(102-97)/5 = (1/3)*1.0 = +0.3333
        # remainder (2/3) at SL = (2/3)*(92-97)/5 = (2/3)*(-1.0) = -0.6667
        # realized_r = 0.3333 - 0.6667 = -0.3333
        bars = [
            _bar(1, low=95.0, high=97.0, close=96.0),  # fills E1+E2+E3
            _bar(2, low=96.0, high=102.0, close=101.0),  # TP1 hit
            _bar(3, low=92.0, high=99.0, close=93.0),  # SL hit
        ]
        outcome = replay_ladder(_setup(**_EQUAL_3), bars)
        self.assertEqual(outcome.entries_filled, ("E1", "E2", "E3"))
        self.assertAlmostEqual(outcome.blended_entry, 97.0, places=3)
        self.assertAlmostEqual(outcome.realized_r, -1.0 / 3.0, places=3)


class TestBugFixes(unittest.TestCase):
    """Steps 4: BAD_GEOMETRY, entry+SL same bar, monotonic bars."""

    def test_bad_geometry_when_risk_non_positive(self):
        # disaster_stop ABOVE the blended entry → risk <= 0. Must classify
        # BAD_GEOMETRY with realized_r explicitly None (not a silent 0).
        setup = _setup(
            entries=[(99.0, 50.0), (97.0, 50.0)],
            tps=[(110.0, 100.0)],
            stop=100.0,  # above both entries -> blended < stop -> risk < 0
        )
        bars = [_bar(1, low=97.0, high=99.0, close=98.0)]  # fills both entries
        outcome = replay_ladder(setup, bars)
        self.assertEqual(outcome.classification, "BAD_GEOMETRY")
        self.assertIsNone(outcome.realized_r)

    def test_entry_and_sl_same_bar_is_ambiguous_sl_first(self):
        # A single bar fills the only entry AND its low pierces the stop.
        # SL-first, flagged ambiguous, ambiguous_bars incremented.
        setup = _setup(entries=[(99.0, 100.0)], tps=[(110.0, 100.0)], stop=95.0)
        bars = [_bar(1, low=94.0, high=99.5, close=96.0)]  # fills E1(99) + pierces SL(95)
        outcome = replay_ladder(setup, bars)
        self.assertTrue(outcome.sl_hit)
        self.assertEqual(outcome.ambiguous_bars, 1)
        last = outcome.sequence[-1]
        self.assertEqual(last.level_id, "SL")
        self.assertTrue(last.same_bar_ambiguous)

    def test_bars_sorted_ascending_defensively(self):
        # Out-of-order bars must be sorted before the walk so the crossing
        # sequence is correct regardless of input order.
        ordered = [
            _bar(1, low=97.0, high=99.0, close=98.0),
            _bar(2, low=98.0, high=102.0, close=101.0),
            _bar(3, low=92.0, high=99.0, close=93.0),
        ]
        shuffled = [ordered[2], ordered[0], ordered[1]]
        out_sorted = replay_ladder(_setup(**_EQUAL_3), ordered)
        out_shuffled = replay_ladder(_setup(**_EQUAL_3), shuffled)
        self.assertEqual(out_sorted.sequence_str(), out_shuffled.sequence_str())


class TestSubstrateLayer(unittest.TestCase):
    """Step 5: MFE / MAE (in-trade, R + pct) and forward_return."""

    def test_mfe_mae_in_trade_only(self):
        # Pre-fill bars with extreme highs/lows must NOT count toward MFE/MAE —
        # excursion is anchored to the blended entry and measured from first fill.
        setup = _setup(entries=[(99.0, 100.0)], tps=[(130.0, 100.0)], stop=80.0)
        bars = [
            _bar(1, low=120.0, high=200.0, close=150.0),  # PRE-FILL (no entry yet, 99 not touched)
            _bar(2, low=98.0, high=105.0, close=100.0),  # fills E1(99); high 105, low 98
            _bar(3, low=95.0, high=110.0, close=108.0),  # high 110, low 95
        ]
        outcome = replay_ladder(setup, bars)
        # blended = 99, risk = 99-80 = 19. In-trade high = 110, low = 95.
        self.assertAlmostEqual(outcome.blended_entry, 99.0, places=3)
        self.assertAlmostEqual(outcome.mfe, (110.0 - 99.0) / 19.0, places=4)
        self.assertAlmostEqual(outcome.mae, (95.0 - 99.0) / 19.0, places=4)
        self.assertAlmostEqual(outcome.mfe_pct, (110.0 - 99.0) / 99.0, places=4)
        self.assertAlmostEqual(outcome.mae_pct, (95.0 - 99.0) / 99.0, places=4)

    def test_forward_return_independent_of_fills(self):
        # forward_return = (last close - reference_close)/reference_close,
        # computed even when NOTHING fills.
        setup = _setup(entries=[(50.0, 100.0)], tps=[(60.0, 100.0)], stop=45.0)
        bars = [
            _bar(1, low=98.0, high=102.0, close=100.0),
            _bar(2, low=99.0, high=110.0, close=105.0),
        ]
        outcome = replay_ladder(setup, bars, reference_close=100.0)
        self.assertEqual(outcome.entries_filled, ())  # nothing filled (50 never touched)
        self.assertAlmostEqual(outcome.forward_return, (105.0 - 100.0) / 100.0, places=4)
        # NO_FILL leaves mfe/mae None
        self.assertIsNone(outcome.mfe)
        self.assertIsNone(outcome.mae)


class TestRatchetPass(unittest.TestCase):
    """Step 6: ratchet what-if. Never overrides realized_r."""

    def test_ratchet_beats_static_when_be_stop_saves_a_trade(self):
        # Path: fill E1, hit TP1, then dip below the ORIGINAL stop (which the
        # static replay closes at SL) but ABOVE break-even. The ratchet moved
        # the stop to break-even after TP1, so the remainder exits at BE+ not SL.
        setup = _setup(
            entries=[(100.0, 100.0)],
            tps=[(105.0, 50.0), (110.0, 50.0)],
            stop=95.0,
        )
        bars = [
            _bar(1, low=99.0, high=101.0, close=100.0),  # fills E1(100)
            _bar(2, low=100.0, high=105.0, close=104.0),  # TP1(105) hit -> ratchet stop -> BE(100)
            _bar(3, low=94.0, high=99.0, close=96.0),  # dips to 94: static SL(95), ratchet BE(100)
        ]
        outcome = replay_ladder(setup, bars)
        self.assertIsNotNone(outcome.realized_r)
        self.assertIsNotNone(outcome.ratchet_realized_r)
        self.assertGreater(outcome.ratchet_realized_r, outcome.realized_r)

    def test_ratchet_does_not_change_realized_r(self):
        # The headline realized_r must be identical whether or not the ratchet
        # field is populated — ratchet is a parallel field, never an override.
        setup = _setup(
            entries=[(100.0, 100.0)],
            tps=[(105.0, 50.0), (110.0, 50.0)],
            stop=95.0,
        )
        bars = [
            _bar(1, low=99.0, high=101.0, close=100.0),
            _bar(2, low=100.0, high=105.0, close=104.0),
            _bar(3, low=94.0, high=99.0, close=96.0),
        ]
        outcome = replay_ladder(setup, bars)
        # static: blended=100, risk=5. TP1 share 0.5 -> +0.5*(105-100)/5 = +0.5.
        # remainder 0.5 at SL(95) -> 0.5*(95-100)/5 = -0.5. realized_r = 0.0.
        self.assertAlmostEqual(outcome.realized_r, 0.0, places=4)


class TestTimeAwareness(unittest.TestCase):
    """PR-1: entry-TTL (``entry_expiry_ms``) + position time-stop
    (``position_expiry_ms``). Both default None -> byte-identical legacy
    behaviour. Cutoffs are ABSOLUTE epoch-ms scalars; the engine never imports
    datetime."""

    def test_no_expiry_params_unchanged(self):
        # Defaults None must reproduce the pre-change outcome exactly. Baseline
        # captured fresh from the same call without any expiry kwargs.
        setup = _setup(**_EQUAL_3)
        bars = [
            _bar(1, low=95.0, high=97.0, close=96.0),  # fills E1+E2+E3
            _bar(2, low=96.0, high=102.0, close=101.0),  # TP1 hit
            _bar(3, low=92.0, high=99.0, close=93.0),  # SL hit
        ]
        baseline = replay_ladder(setup, bars)
        with_none = replay_ladder(setup, bars, entry_expiry_ms=None, position_expiry_ms=None)
        self.assertEqual(with_none.classification, baseline.classification)
        self.assertEqual(with_none.realized_r, baseline.realized_r)
        self.assertEqual(with_none.sequence_str(), baseline.sequence_str())

    def test_entry_ttl_no_fill(self):
        # E1's limit (99) is only touched on a bar at ts == entry_expiry_ms.
        # A touch at-or-after the cutoff must NOT fill -> NO_FILL, no entries,
        # realized_r None.
        setup = _setup(entries=[(99.0, 100.0)], tps=[(110.0, 100.0)], stop=92.0)
        bars = [
            _bar(1, low=100.0, high=101.0, close=100.5),  # above E1 limit, no fill
            _bar(2, low=98.0, high=100.0, close=99.0),  # touches E1(99) but AT cutoff
        ]
        outcome = replay_ladder(setup, bars, entry_expiry_ms=2)
        self.assertEqual(outcome.classification, "NO_FILL")
        self.assertEqual(outcome.entries_filled, ())
        self.assertIsNone(outcome.realized_r)

    def test_entry_ttl_allows_fill_before_cutoff(self):
        # E1(99) touched strictly before the cutoff fills; deeper E2(95) only
        # touched AT/after the cutoff does NOT fill -> blended == E1 price.
        setup = _setup(
            entries=[(99.0, 50.0), (95.0, 50.0)],
            tps=[(110.0, 100.0)],
            stop=90.0,
        )
        bars = [
            _bar(1, low=98.0, high=100.0, close=99.0),  # fills E1(99) before cutoff
            _bar(2, low=94.0, high=99.0, close=96.0),  # touches E2(95) AT cutoff -> no fill
        ]
        outcome = replay_ladder(setup, bars, entry_expiry_ms=2)
        self.assertEqual(outcome.entries_filled, ("E1",))
        self.assertAlmostEqual(outcome.blended_entry, 99.0, places=3)

    def test_time_stop_sideways_drift(self):
        # Entry fills; price drifts between SL(92) and TP1(102), never touching
        # either; bars continue past position_expiry_ms. The remainder marks at
        # the close of the FIRST bar at/after the cutoff (distinct from the final
        # bar close to prove the mark point).
        setup = _setup(entries=[(99.0, 100.0)], tps=[(102.0, 100.0)], stop=92.0)
        bars = [
            _bar(1, low=98.0, high=100.0, close=99.0),  # fills E1(99); blended=99, risk=7
            _bar(2, low=98.0, high=101.0, close=100.0),  # drift, ts < cutoff
            _bar(3, low=97.0, high=101.0, close=98.0),  # expiry bar (ts == cutoff): mark @ 98
            _bar(4, low=96.0, high=100.0, close=99.5),  # AFTER exit: must NOT change mark
        ]
        outcome = replay_ladder(setup, bars, position_expiry_ms=3)
        self.assertEqual(outcome.classification, "TIME_STOP")
        self.assertFalse(outcome.horizon_open)
        self.assertTrue(outcome.sequence_str().endswith("TIME_STOP"))
        # expiry_close = 98 (bar 3, FIRST at/after cutoff), NOT 99.5 (bar 4).
        self.assertAlmostEqual(outcome.realized_r, (98.0 - 99.0) / 7.0, places=4)

    def test_time_stop_partial_tp_then_expiry(self):
        # TP1(105) hits, remainder drifts, then time-stops. classification
        # TIME_STOP; tps_hit==("TP1",); realized_r = TP1 contribution + remainder
        # marked at expiry close.
        setup = _setup(
            entries=[(100.0, 100.0)],
            tps=[(105.0, 50.0), (110.0, 50.0)],
            stop=95.0,
        )
        bars = [
            _bar(1, low=99.0, high=101.0, close=100.0),  # fills E1(100); blended=100, risk=5
            _bar(2, low=100.0, high=105.0, close=104.0),  # TP1(105) hit (share 0.5)
            _bar(3, low=99.0, high=104.0, close=101.0),  # expiry bar (ts == cutoff): mark @ 101
        ]
        outcome = replay_ladder(setup, bars, position_expiry_ms=3)
        self.assertEqual(outcome.classification, "TIME_STOP")
        self.assertEqual(outcome.tps_hit, ("TP1",))
        # TP1: 0.5*(105-100)/5 = +0.5 ; remainder 0.5 marked @ 101: 0.5*(101-100)/5 = +0.1
        self.assertAlmostEqual(outcome.realized_r, 0.5 + 0.1, places=4)

    def test_real_sl_on_cutoff_bar_wins(self):
        # SL touched on the SAME bar as position_expiry_ms -> a real SL outranks
        # the synthetic time-stop -> SL_HIT, not TIME_STOP.
        setup = _setup(entries=[(99.0, 100.0)], tps=[(110.0, 100.0)], stop=92.0)
        bars = [
            _bar(1, low=98.0, high=100.0, close=99.0),  # fills E1(99)
            _bar(2, low=91.0, high=99.0, close=93.0),  # SL(92) pierced AND ts == cutoff
        ]
        outcome = replay_ladder(setup, bars, position_expiry_ms=2)
        self.assertEqual(outcome.classification, "SL_HIT")
        self.assertTrue(outcome.sl_hit)

    def test_full_tp_before_cutoff_unaffected(self):
        # Full scale-out before the cutoff -> TP_FULL; the time-stop never fires.
        setup = _setup(entries=[(99.0, 100.0)], tps=[(102.0, 100.0)], stop=92.0)
        bars = [
            _bar(1, low=98.0, high=100.0, close=99.0),  # fills E1(99)
            _bar(2, low=99.0, high=103.0, close=102.5),  # TP1(102) hit -> fully scaled out
            _bar(3, low=97.0, high=101.0, close=98.0),  # past cutoff but already flat
        ]
        outcome = replay_ladder(setup, bars, position_expiry_ms=3)
        self.assertEqual(outcome.classification, "TP_FULL")


class TestStatusGuards(unittest.TestCase):
    def test_no_structure(self):
        self.assertEqual(replay_ladder(None, [_bar(1, 1, 2, 1.5)]).status, "NO_STRUCTURE")

    def test_no_data(self):
        self.assertEqual(replay_ladder(_setup(**_EQUAL_3), []).status, "NO_DATA")


if __name__ == "__main__":
    unittest.main()
