"""Mutation-hardening tests for feedback/ladder_replay.py (cosmic-ray survivors, batch 1).

Each class pins one operator/constant mutation the pre-existing suite missed
(2163 mutants, 947 survived at 56%). Auto-assembled from per-mutant proposals,
then verified by a targeted cosmic-ray re-run. See docs/research/mutation_testing_targets_2026_07_18.md.
"""

from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace

from alphalens_pipeline.feedback.ladder_replay import (
    LadderOutcome,
    LevelCrossing,
    _arm_excess_from_outcome,
    _ArmExcessContext,
    _blended_entry,
    _excursions,
    _filled_frac,
    _finalize,
    _forward_return,
    _grid_realized_r,
    _LadderWalk,
    _Level,
    _notouch_arm_reward,
    _replay_synthetic_fill,
    _touch_arm_reward,
    parse_ladder,
    realized_r_fill_anchored,
    replay_entry_grid,
    replay_ladder,
    replay_ladder_atr_bracket,
    replay_ladder_breakeven,
)


class TestLevelCrossingFrozen_L60(unittest.TestCase):
    def test_frozen(self):
        """Kills @dataclass(frozen=True) -> frozen=False on LevelCrossing."""
        lc = LevelCrossing("E1", "ENTRY", 10.0, 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lc.price = 11.0


class TestHorizonOpenDefault_L68(unittest.TestCase):
    def test_default_false(self):
        """Kills LadderOutcome.horizon_open default False -> True."""
        self.assertFalse(LadderOutcome(status="OK").horizon_open)
        self.assertFalse(replay_ladder(None, []).horizon_open)


class TestTotalEntryAllocDefault_L131(unittest.TestCase):
    def test_default_zero(self):
        """Kills _ParsedLadder.total_entry_alloc default 0.0 -> 1.0."""
        self.assertEqual(parse_ladder(None).total_entry_alloc, 0.0)


class TestStatusGuard_L144(unittest.TestCase):
    def test_non_ok_status_rejected(self):
        """Kills status != \"OK\" -> status < \"OK\" (later-sorting status passes)."""
        p = parse_ladder(
            {
                "status": "REJECTED",
                "disaster_stop": 9.0,
                "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            }
        )
        self.assertFalse(p.ok)


class TestAllocPctDefault_L152(unittest.TestCase):
    def test_missing_alloc_pct_is_zero(self):
        """Kills t.get(\"alloc_pct\", 0.0) -> t.get(\"alloc_pct\", -1.0)."""
        p = parse_ladder({"status": "OK", "disaster_stop": 5.0, "entry_tiers": [{"limit": 10.0}]})
        self.assertEqual(p.entries[0].weight, 0.0)


class TestBlendedEntryFallback_L194(unittest.TestCase):
    def test_nonpositive_wsum_equal_weight(self):
        """Kills wsum > 0 -> wsum != 0 and the fallback sum/len -> sum+len."""
        self.assertEqual(_blended_entry([_Level("E1", 10.0, -1.0), _Level("E2", 8.0, -3.0)]), 9.0)
        self.assertEqual(_blended_entry([_Level("E1", 10.0, 0.0), _Level("E2", 8.0, 0.0)]), 9.0)


class TestFilledFracAllocWeighted_L208(unittest.TestCase):
    def test_alloc_weighted(self):
        """Kills total > 0 -> total < 0 (alloc-weighted path must be used)."""
        ladder = parse_ladder(
            {
                "status": "OK",
                "disaster_stop": 5.0,
                "entry_tiers": [
                    {"limit": 10.0, "alloc_pct": 70.0},
                    {"limit": 8.0, "alloc_pct": 30.0},
                ],
            }
        )
        self.assertAlmostEqual(_filled_frac(ladder, ladder.entries[:1]), 0.7)


class TestFilledFracLenFallback_L211(unittest.TestCase):
    def test_len_fallback(self):
        """Kills len/len -> len%len / len+len / len-len in the no-alloc fallback."""
        ladder = parse_ladder(
            {"status": "OK", "disaster_stop": 5.0, "entry_tiers": [{"limit": 10.0}, {"limit": 8.0}]}
        )
        self.assertEqual(_filled_frac(ladder, ladder.entries[:1]), 0.5)


class TestFilledFracClamp_L214(unittest.TestCase):
    def test_clamped_at_zero(self):
        """Kills max(frac, 0.0) -> max(frac, -1.0)."""
        ladder = parse_ladder(
            {
                "status": "OK",
                "disaster_stop": 5.0,
                "entry_tiers": [
                    {"limit": 10.0, "alloc_pct": -50.0},
                    {"limit": 5.0, "alloc_pct": 150.0},
                ],
            }
        )
        self.assertEqual(_filled_frac(ladder, ladder.entries[:1]), 0.0)


class TestRatchetRiskUnit_L274(unittest.TestCase):
    def test_ratchet_r_full_tp(self):
        """Kills risk_per_share (blended - stop) -> (blended ** stop)."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 12.0, "tranche_pct": 100.0}],
        }
        bars = [{"t": 1, "l": 9.5, "h": 10.5, "c": 10.0}, {"t": 2, "l": 10.0, "h": 12.5, "c": 12.0}]
        self.assertAlmostEqual(replay_ladder(setup, bars).ratchet_realized_r, 1.0)


class TestGridRealizedRKwOnly_L305(unittest.TestCase):
    def test_positional_call_rejected(self):
        """Kills the '*,' -> '/,' marker swap in _grid_realized_r's signature."""
        with self.assertRaises(TypeError):
            _grid_realized_r({}, [], None, None)


class TestSyntheticFillKwOnly_L416(unittest.TestCase):
    def test_positional_call_rejected(self):
        """Kills the '*,' -> '/,' marker swap in _replay_synthetic_fill's signature."""
        with self.assertRaises(TypeError):
            _replay_synthetic_fill({}, [], 10.0, 0, 8.0)


class TestFillAnchoredAtrThreshold_L575(unittest.TestCase):
    def test_small_positive_atr_evaluates(self):
        """Kills atr <= 0 -> atr <= 1 in realized_r_fill_anchored."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "atr": 0.5,
        }
        bars = [{"t": 1, "l": 9.9, "h": 10.2, "c": 10.0}]
        self.assertEqual(realized_r_fill_anchored(setup, bars), 0.0)


class TestAtrBracketKwOnly_L593(unittest.TestCase):
    def test_positional_call_rejected(self):
        """Kills the '*,' -> '/,' marker swap in replay_ladder_atr_bracket's signature."""
        with self.assertRaises(TypeError):
            replay_ladder_atr_bracket(None, [], 1.5)


class TestBracketAtrGuard_L641(unittest.TestCase):
    def test_zero_atr_is_none(self):
        """Kills atr <= 0 -> atr <= -1: atr=0 must return None, not a replayed R."""
        setup = {
            "status": "OK",
            "disaster_stop": 5.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 50.0}, {"limit": 8.0, "alloc_pct": 50.0}],
            "atr": 0.0,
        }
        bars = [{"t": 1, "l": 9.5, "h": 9.6, "c": 9.55}, {"t": 2, "l": 7.9, "h": 8.5, "c": 8.0}]
        self.assertIsNone(replay_ladder_atr_bracket(setup, bars))


class TestBracketStopMultGuard_L643(unittest.TestCase):
    def test_negative_mult_is_none(self):
        """Kills stop_atr_mult <= 0 -> == 0 (negative multiplier must return None)."""
        setup = {
            "status": "OK",
            "disaster_stop": 5.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 50.0}, {"limit": 8.0, "alloc_pct": 50.0}],
            "atr": 0.5,
        }
        bars = [{"t": 1, "l": 9.5, "h": 9.6, "c": 9.55}, {"t": 2, "l": 7.9, "h": 8.5, "c": 8.0}]
        self.assertIsNone(replay_ladder_atr_bracket(setup, bars, stop_atr_mult=-1.0))


class TestBracketTpFormula_L664(unittest.TestCase):
    def test_tp_is_blended_plus_atr_term(self):
        """Kills tp = blended + tp_atr_mult*atr -> blended / tp_atr_mult * atr."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "atr": 0.5,
        }
        bars = [{"t": 1, "l": 9.9, "h": 10.1, "c": 10.0}, {"t": 2, "l": 10.0, "h": 10.5, "c": 10.2}]
        res = replay_ladder_atr_bracket(setup, bars)
        self.assertAlmostEqual(res, (10.2 - 10.0) / 0.75)


class TestTimeStopInit_L702(unittest.TestCase):
    def test_open_position_not_time_stop(self):
        """Kills _LadderWalk.time_stop init False -> True."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
        }
        out = replay_ladder(setup, [{"t": 1, "l": 9.9, "h": 10.1, "c": 10.5}])
        self.assertEqual(out.classification, "OPEN")


class TestCleanSlNotAmbiguous_L759(unittest.TestCase):
    def test_no_ambiguity_flag(self):
        """Kills tp_crossable 'and' -> 'or' and high >= t.price -> high is not t.price."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 12.0, "tranche_pct": 100.0}],
        }
        bars = [{"t": 1, "l": 9.5, "h": 10.5, "c": 10.0}, {"t": 2, "l": 7.9, "h": 8.5, "c": 8.0}]
        out = replay_ladder(setup, bars)
        self.assertEqual(out.classification, "SL_HIT")
        self.assertEqual(out.ambiguous_bars, 0)


class TestKindValueEquality_L761(unittest.TestCase):
    def test_non_interned_kind_string(self):
        """Kills c.kind == _ENTRY -> c.kind is _ENTRY (value vs identity)."""
        ladder = parse_ladder(
            {
                "status": "OK",
                "disaster_stop": 8.0,
                "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            }
        )
        walk = _LadderWalk(ladder, 8.0, entry_expiry_ms=None, position_expiry_ms=None)
        ts = 777000
        kind = "".join(["EN", "TRY"])  # == "ENTRY" but not the interned constant
        walk.filled.append(ladder.entries[0])
        walk.filled_ids.add("E1")
        walk.seq.append(LevelCrossing("E1", kind, 10.0, ts))
        walk.step({"t": ts, "l": 7.9, "h": 8.1, "c": 8.0})
        self.assertEqual(walk.ambiguous_bars, 1)


class TestSameTsValueEquality_L761(unittest.TestCase):
    def test_equal_value_distinct_timestamp_objects(self):
        """Kills c.bar_ts_ms == ts -> c.bar_ts_ms is ts (value vs identity)."""
        ts_a = int("500000")
        ts_b = int("500000")  # equal value, distinct object (not small-int cached)
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
        }
        bars = [
            {"t": ts_a, "l": 9.5, "h": 10.5, "c": 10.0},
            {"t": ts_b, "l": 7.9, "h": 8.5, "c": 8.0},
        ]
        out = replay_ladder(setup, bars)
        self.assertEqual(out.ambiguous_bars, 1)


class TestTakeTpsContinues_L774(unittest.TestCase):
    def test_later_tp_recorded_after_skipped_one(self):
        """Kills continue -> break in the _take_tps scan loop."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [
                {"target": 11.0, "tranche_pct": 50.0},
                {"target": 12.0, "tranche_pct": 50.0},
            ],
        }
        bars = [{"t": 1, "l": 9.5, "h": 11.2, "c": 11.0}, {"t": 2, "l": 10.9, "h": 12.5, "c": 12.0}]
        out = replay_ladder(setup, bars)
        self.assertEqual(out.tps_hit, ("TP1", "TP2"))
        self.assertEqual(out.classification, "TP_FULL")


class TestFinalizeKwOnly_L796(unittest.TestCase):
    def test_positional_call_rejected(self):
        """Kills the '*,' -> '/,' marker swap in _finalize's signature."""
        ladder = parse_ladder(
            {
                "status": "OK",
                "disaster_stop": 8.0,
                "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            }
        )
        walk = _LadderWalk(ladder, 8.0, entry_expiry_ms=None, position_expiry_ms=None)
        with self.assertRaises(TypeError):
            _finalize(walk, None, None, None)


class TestZeroRiskBadGeometry_L840(unittest.TestCase):
    def test_stop_at_entry_is_bad_geometry(self):
        """Kills risk <= 0 -> risk < 0 in both the _finalize BAD_GEOMETRY guard
        and the _replay_ratchet guard (mutants divide by zero instead)."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 8.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 12.0, "tranche_pct": 100.0}],
        }
        out = replay_ladder(setup, [{"t": 1, "l": 7.5, "h": 8.2, "c": 8.0}])
        self.assertEqual(out.classification, "BAD_GEOMETRY")
        self.assertIsNone(out.realized_r)


class TestFractionalTpWeights_L926(unittest.TestCase):
    def test_weighted_shares_below_one(self):
        """Kills tp_wsum > 0 -> tp_wsum > 1 and tp_wsum > 0 -> tp_wsum <= 0."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [
                {"target": 11.0, "tranche_pct": 0.6},
                {"target": 12.0, "tranche_pct": 0.4},
            ],
        }
        bars = [{"t": 1, "l": 9.5, "h": 10.2, "c": 10.0}, {"t": 2, "l": 10.5, "h": 12.5, "c": 12.0}]
        self.assertAlmostEqual(replay_ladder(setup, bars).realized_r, 0.7)


class TestZeroTpWeightsFallback_L929(unittest.TestCase):
    def test_equal_share_fallback(self):
        """Kills 1.0/len -> 1.0//len, 1.0+len, and 1.0&len in the zero-weight fallback."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 11.0}, {"target": 12.0}],
        }
        bars = [{"t": 1, "l": 9.5, "h": 10.2, "c": 10.0}, {"t": 2, "l": 10.5, "h": 12.5, "c": 12.0}]
        self.assertAlmostEqual(replay_ladder(setup, bars).realized_r, 0.75)


class TestZeroFilledFracShare_L931(unittest.TestCase):
    def test_zero_fraction_avoids_division(self):
        """Kills filled_frac > 0 -> filled_frac >= 0 (mutant divides by 0.0)."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [
                {"limit": 10.0, "alloc_pct": -50.0},
                {"limit": 5.0, "alloc_pct": 150.0},
            ],
            "tp_tranches": [{"target": 12.0, "tranche_pct": 100.0}],
        }
        out = replay_ladder(setup, [{"t": 1, "l": 9.5, "h": 10.5, "c": 10.0}])
        self.assertEqual(out.filled_fraction, 0.0)
        self.assertEqual(out.realized_r, 0.0)


class TestNegativeTrancheSkipped_L934(unittest.TestCase):
    def test_later_tranche_still_sells(self):
        """Kills continue -> break on the share <= 0 tranche skip."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [
                {"target": 11.0, "tranche_pct": -50.0},
                {"target": 12.0, "tranche_pct": 150.0},
            ],
        }
        bars = [
            {"t": 1, "l": 9.5, "h": 10.2, "c": 10.0},
            {"t": 2, "l": 10.5, "h": 12.5, "c": 12.0},
            {"t": 3, "l": 10.8, "h": 11.0, "c": 11.0},
        ]
        out = replay_ladder(setup, bars)
        self.assertAlmostEqual(out.realized_r, 1.0)
        self.assertEqual(out.realized_tp_ids, ("TP2",))


class TestFullTpNotHorizonOpen_L940(unittest.TestCase):
    def test_full_scale_out_closes_horizon(self):
        """Kills remaining > 1e-9 -> remaining != 1e-9."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 12.0, "tranche_pct": 100.0}],
        }
        bars = [{"t": 1, "l": 9.5, "h": 10.5, "c": 10.0}, {"t": 2, "l": 11.0, "h": 12.5, "c": 12.2}]
        out = replay_ladder(setup, bars)
        self.assertEqual(out.classification, "TP_FULL")
        self.assertFalse(out.horizon_open)


class TestOpenRemainderMark_L948(unittest.TestCase):
    def test_marked_to_last_close(self):
        """Kills remaining * (last_close - blended) / risk -> remaining /
        (last_close - blended) / risk, and horizon_open = True -> False."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
        }
        out = replay_ladder(setup, [{"t": 1, "l": 9.9, "h": 10.1, "c": 10.5}])
        self.assertAlmostEqual(out.realized_r, 0.25)
        self.assertTrue(out.horizon_open)


class TestMfePct_L971(unittest.TestCase):
    def test_excursion_above_double(self):
        """Kills (in_trade_high - blended) -> (in_trade_high % blended) in mfe_pct."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
        }
        out = replay_ladder(setup, [{"t": 1, "l": 9.5, "h": 21.0, "c": 11.0}])
        self.assertAlmostEqual(out.mfe_pct, 1.1)


class TestExcursionsNegativeRisk_L973(unittest.TestCase):
    def test_negative_risk_has_no_r_units(self):
        """Kills risk <= 0 -> risk == 0 in _excursions."""
        mfe, mae, mfe_pct, mae_pct = _excursions(10.0, -2.0, 12.0, 9.0)
        self.assertIsNone(mfe)
        self.assertIsNone(mae)
        self.assertAlmostEqual(mfe_pct, 0.2)
        self.assertAlmostEqual(mae_pct, -0.1)


class TestForwardReturn_L982(unittest.TestCase):
    def test_reference_close_of_one_is_valid(self):
        """Kills reference_close == 0 -> reference_close == 1."""
        self.assertEqual(_forward_return(1.0, 1.5), 0.5)

    def test_subtraction_not_modulo(self):
        """Kills (last_close - reference_close) -> (last_close % reference_close)."""
        self.assertAlmostEqual(_forward_return(10.0, 25.0), 1.5)


class TestRatchetExactTouchFill_L990(unittest.TestCase):
    def test_exact_touch_fills_ratchet_walk(self):
        """Kills low <= lvl.price -> low < lvl.price in _fill_entry_ids."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 11.0, "tranche_pct": 100.0}],
        }
        bars = [
            {"t": 1, "l": 10.0, "h": 10.4, "c": 10.2},
            {"t": 2, "l": 10.1, "h": 11.5, "c": 10.0},
        ]
        self.assertAlmostEqual(replay_ladder(setup, bars).ratchet_realized_r, 0.5)


class TestRatchetSkipsNoFillBars_L1046(unittest.TestCase):
    def test_prefill_bar_is_skipped_not_terminal(self):
        """Kills continue -> break on the ratchet walk's no-fill bars."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 11.0, "tranche_pct": 100.0}],
        }
        bars = [
            {"t": 1, "l": 10.5, "h": 10.8, "c": 10.6},
            {"t": 2, "l": 9.9, "h": 10.2, "c": 10.0},
            {"t": 3, "l": 10.0, "h": 11.5, "c": 11.0},
        ]
        self.assertAlmostEqual(replay_ladder(setup, bars).ratchet_realized_r, 0.5)


class TestRatchetSl_L1051(unittest.TestCase):
    def test_sl_exits_at_effective_stop(self):
        """Kills sl_hit = True -> False and high >= t.price -> 'is not' / '!=' in
        the ratchet walk (phantom TP hits or mark-to-close would not be -1.0)."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 11.0, "tranche_pct": 100.0}],
        }
        bars = [{"t": 1, "l": 9.5, "h": 10.2, "c": 10.0}, {"t": 2, "l": 7.5, "h": 9.0, "c": 7.0}]
        self.assertAlmostEqual(replay_ladder(setup, bars).ratchet_realized_r, -1.0)


class TestBreakevenKwOnly_L1072(unittest.TestCase):
    def test_positional_call_rejected(self):
        """Kills the '*,' -> '/,' marker swap in replay_ladder_breakeven's signature."""
        with self.assertRaises(TypeError):
            replay_ladder_breakeven(None, [], 1.0)


class TestBreakevenSmallRisk_L1116(unittest.TestCase):
    def test_risk_below_one_still_evaluates(self):
        """Kills risk <= 0 -> risk <= 1 in replay_ladder_breakeven."""
        setup = {
            "status": "OK",
            "disaster_stop": 9.5,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
        }
        bars = [{"t": 1, "l": 9.9, "h": 10.1, "c": 10.0}]
        res = replay_ladder_breakeven(setup, bars, mfe_trigger_r=float("inf"))
        self.assertEqual(res, 0.0)


class TestBreakevenTpGapThrough_L1127(unittest.TestCase):
    def test_high_above_target_counts(self):
        """Kills high >= t.price -> high == t.price in _scan_tp_hits."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 11.0, "tranche_pct": 100.0}],
        }
        bars = [{"t": 1, "l": 9.9, "h": 10.1, "c": 10.0}, {"t": 2, "l": 10.5, "h": 11.5, "c": 10.6}]
        res = replay_ladder_breakeven(setup, bars, mfe_trigger_r=float("inf"))
        self.assertAlmostEqual(res, 0.5)


class TestBreakevenSlBreaks_L1167(unittest.TestCase):
    def test_sl_terminates_walk(self):
        """Kills break -> continue after the breakeven walk's SL pierce."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 11.0, "tranche_pct": 100.0}],
        }
        bars = [
            {"t": 1, "l": 9.9, "h": 10.1, "c": 10.0},
            {"t": 2, "l": 7.5, "h": 9.0, "c": 7.8},
            {"t": 3, "l": 10.0, "h": 11.5, "c": 11.0},
        ]
        res = replay_ladder_breakeven(setup, bars, mfe_trigger_r=float("inf"))
        self.assertAlmostEqual(res, -1.0)


class TestBreakevenTriggerBoundary_L1171(unittest.TestCase):
    def test_exact_mfe_threshold_triggers(self):
        """Kills (peak - blended)/risk >= mfe_trigger_r -> > (boundary trigger)."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 100.0, "tranche_pct": 100.0}],
        }
        bars = [{"t": 1, "l": 9.9, "h": 11.0, "c": 10.5}, {"t": 2, "l": 9.5, "h": 9.8, "c": 9.7}]
        res = replay_ladder_breakeven(setup, bars, mfe_trigger_r=0.5)
        self.assertAlmostEqual(res, 0.0)


class TestArmExcessNoneBlend_L1282(unittest.TestCase):
    def test_none_blend_maps_to_cash(self):
        """Kills 'b is None or r is None' -> 'b is None and r is None'."""
        ctx = _ArmExcessContext(
            benchmark_window_return=0.0,
            market_cap=None,
            first_rth_bar=None,
            apply_haircut_fn=lambda raw, **kw: raw,
            implausible_threshold=0.6,
            cash_reward=0.25,
        )
        out = LadderOutcome(status="OK", classification="OPEN", blended_entry=None, realized_r=0.5)
        res = _arm_excess_from_outcome(out, own_stop=8.0, arm_name="baseline", ctx=ctx)
        self.assertEqual(res, 0.25)


class TestImplausibleBoundary_L1287(unittest.TestCase):
    def test_return_exactly_at_threshold_passes(self):
        """Kills abs(exit_mark/b - 1) > threshold -> >= (boundary rejection)."""
        ctx = _ArmExcessContext(
            benchmark_window_return=0.0,
            market_cap=None,
            first_rth_bar=None,
            apply_haircut_fn=lambda raw, **kw: raw,
            implausible_threshold=0.5,
            cash_reward=0.25,
        )
        out = LadderOutcome(
            status="OK", classification="SL_HIT", blended_entry=10.0, realized_r=2.5
        )
        res = _arm_excess_from_outcome(out, own_stop=8.0, arm_name="baseline", ctx=ctx)
        self.assertEqual(res, 0.5)


class TestTouchArmNoStructureByValue_L1320(unittest.TestCase):
    def test_dynamic_status_string_is_uncomputable(self):
        """Kills arm_setup.status == \"NO_STRUCTURE\" -> is (value vs identity)."""
        arm = SimpleNamespace(status="".join(["NO_", "STRUCTURE"]))
        ctx = _ArmExcessContext(
            benchmark_window_return=0.0,
            market_cap=None,
            first_rth_bar=None,
            apply_haircut_fn=lambda raw, **kw: raw,
            implausible_threshold=0.6,
            cash_reward=0.25,
        )
        res = _touch_arm_reward(
            "baseline",
            arm,
            8.0,
            trade_setup={},
            ordered_bars=[],
            entry_expiry_ms=None,
            position_expiry_ms=None,
            ctx=ctx,
        )
        self.assertIsNone(res)


class TestNotouchKwOnly_L1355(unittest.TestCase):
    def test_positional_call_rejected(self):
        """Kills the '*,' -> '/,' marker swap in _notouch_arm_reward's signature."""
        ctx = _ArmExcessContext(
            benchmark_window_return=0.0,
            market_cap=None,
            first_rth_bar=None,
            apply_haircut_fn=lambda raw, **kw: raw,
            implausible_threshold=0.6,
            cash_reward=0.25,
        )
        fill = SimpleNamespace(status="NO_FILL", fill_price=None, fill_ts_ms=None)
        with self.assertRaises(TypeError):
            _notouch_arm_reward("market_at_arrival", fill, {}, [], 0, None, 1.0, 10.0, ctx)


class TestNotouchNoFillByValue_L1369(unittest.TestCase):
    def test_dynamic_status_string_is_cash(self):
        """Kills arm_fill.status == \"NO_FILL\" -> is (value vs identity)."""
        fill = SimpleNamespace(status="".join(["NO_", "FILL"]), fill_price=5.0, fill_ts_ms=None)
        ctx = _ArmExcessContext(
            benchmark_window_return=0.0,
            market_cap=None,
            first_rth_bar=None,
            apply_haircut_fn=lambda raw, **kw: raw,
            implausible_threshold=0.6,
            cash_reward=0.25,
        )
        res = _notouch_arm_reward(
            "market_at_arrival",
            fill,
            trade_setup={},
            ordered_bars=[],
            arrival_open_ms=0,
            position_expiry_ms=None,
            atr=float("nan"),
            close=10.0,
            ctx=ctx,
        )
        self.assertEqual(res, 0.25)


class TestGridArrivalWindowFilter_L1526(unittest.TestCase):
    def test_first_rth_bar_at_close_boundary(self):
        """Kills the arrival-window comparison swaps on the first_rth_bar filter
        (open <= t -> open >= t / open == t; t <= close -> t < close /
        t is not close). The zero-spread bar exactly AT the session close must
        be first_rth_bar, so the market_at_arrival haircut is impact-only
        (12 bps -> reward -0.0012); mutants pick no bar (-0.0037) or the wide
        after-close bar (-0.0512). Small-int timestamps expose the identity swap."""
        setup = {
            "status": "OK",
            "disaster_stop": 8.0,
            "entry_tiers": [{"limit": 10.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 12.0, "tranche_pct": 100.0}],
            "atr": 1.0,
            "asof_close": 10.0,
        }
        bars = [
            {"t": 200, "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 1000},
            {"t": 300, "o": 10.0, "h": 10.5, "l": 9.5, "c": 10.0, "v": 1000},
        ]
        grid = replay_entry_grid(
            setup,
            bars,
            arrival_open_ms=100,
            arrival_close_ms=200,
            benchmark_window_return=0.0,
            market_cap=None,
        )
        self.assertAlmostEqual(grid["market_at_arrival"], -0.0012, places=9)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
