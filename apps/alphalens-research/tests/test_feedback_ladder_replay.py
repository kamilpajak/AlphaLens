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
    parse_ladder,
    replay_ladder,
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


class TestStatusGuards(unittest.TestCase):
    def test_no_structure(self):
        self.assertEqual(replay_ladder(None, [_bar(1, 1, 2, 1.5)]).status, "NO_STRUCTURE")

    def test_no_data(self):
        self.assertEqual(replay_ladder(_setup(**_EQUAL_3), []).status, "NO_DATA")


if __name__ == "__main__":
    unittest.main()
