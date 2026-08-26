"""Tests for the pre-registered net-cash exit-policy replay (#1115, memo §10.1).

Governing document: ``docs/research/exit_policy_comparison_prereg_2026_08_24.md``.
Where a clause and this file disagree, the clause wins. The four test groups the
memo itself mandates are marked in the class docstrings: the SMG incident pin,
the arm-B fallback equivalence, the parity-with-live test (a §11 item 4 HALT
tripwire during accrual), and the production-store write guard.

Bars here are HAND-AUTHORED on purpose: these are unit tests of exit geometry
arithmetic on constructed paths, not claims about production data. The
production-data fidelity check is the PR's separate read-only verification
against ``~/.alphalens/population_ladders``.
"""

from __future__ import annotations

import math
import unittest

from alphalens_research.diagnostics import exit_policy_replay as epr
from broker_contract.exit_geometry.levels import atr_bracket_levels, clamp_reanchor_target

from tests.incident_1112_fixture import (
    SMG_ATR,
    SMG_DISASTER_STOP,
    SMG_PLANNED_BLEND,
    SMG_TP_TRANCHES,
    smg_brief_trade_setup,
)

_MIN = 60_000  # one minute in ms


def _bars(*lhc: tuple[float, float, float]) -> list[dict]:
    """One bar per (low, high, close) triple, one minute apart from t=0."""
    return [
        {"t": i * _MIN, "l": low, "h": high, "c": close} for i, (low, high, close) in enumerate(lhc)
    ]


def _setup(
    *,
    entries: list[tuple[float, float]],
    tps: list[tuple[float, float]],
    stop: float,
    atr: float | None = 1.0,
) -> dict:
    return {
        "status": "OK",
        "disaster_stop": stop,
        "entry_tiers": [{"limit": lim, "alloc_pct": pct} for lim, pct in entries],
        "tp_tranches": [{"target": tgt, "tranche_pct": pct} for tgt, pct in tps],
        "atr": atr,
    }


# A one-tier, one-tranche candidate: entry 100 (100%), TP 110, stop 90, ATR 4.
def _simple_setup() -> dict:
    return _setup(entries=[(100.0, 100.0)], tps=[(110.0, 100.0)], stop=90.0, atr=4.0)


class TestCommonFeasibility(unittest.TestCase):
    """Memo §5.1: four rules, evaluated on the brief row + bar store alone."""

    def test_a_well_formed_candidate_is_feasible(self):
        self.assertIsNone(epr.infeasibility_reason(_simple_setup(), bars_cover_window=True))

    def test_each_rule_refuses_with_its_own_reason(self):
        bad_ladder = _setup(entries=[], tps=[(110.0, 100.0)], stop=90.0)
        no_atr = _setup(entries=[(100.0, 100.0)], tps=[(110.0, 100.0)], stop=90.0, atr=None)
        neg_atr = _setup(entries=[(100.0, 100.0)], tps=[(110.0, 100.0)], stop=90.0, atr=-1.0)
        no_tp = _setup(entries=[(100.0, 100.0)], tps=[], stop=90.0)
        for setup, cover, expected in (
            (bad_ladder, True, "setup_not_ok"),
            (no_atr, True, "atr_missing_or_nonpositive"),
            (neg_atr, True, "atr_missing_or_nonpositive"),
            (no_tp, True, "no_tp_tranche"),
            (_simple_setup(), False, "bars_missing"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(epr.infeasibility_reason(setup, bars_cover_window=cover), expected)


class TestArmACash(unittest.TestCase):
    def test_full_tp_exit_at_zero_slippage_matches_hand_arithmetic(self):
        # Buy 100 @ 100 -> TP sells 100% @ 110. N0=10_000 -> 100 shares.
        # Gross P&L = 100 * 10 = 1000. Fees: buy fill + one sell fill.
        bars = _bars((100.0, 100.0, 100.0), (100.0, 111.0, 110.0))
        out = epr.replay_arm(
            _simple_setup(),
            bars,
            arm=epr.ARM_A,
            notional=10_000.0,
            slippage_bps=0.0,
            position_expiry_ms=10 * _MIN,
        )
        buy_fee = max(1.0, 0.0008 * 10_000.0) + 0.0025 * 10_000.0
        sell_notional = 100.0 * 110.0
        sell_fee = max(1.0, 0.0008 * sell_notional) + 0.0025 * sell_notional
        self.assertAlmostEqual(out.net_cash, 1000.0 - buy_fee - sell_fee, places=6)
        self.assertEqual(out.chargeable_fills, 2)
        self.assertFalse(out.used_fallback)

    def test_no_fill_is_exactly_zero_cash_and_zero_fills(self):
        # §5.1: not filling is an OUTCOME. Price never touches the limit.
        bars = _bars((101.0, 102.0, 101.5), (101.2, 103.0, 102.0))
        for arm in (epr.ARM_A, epr.ARM_B):
            with self.subTest(arm=arm):
                out = epr.replay_arm(
                    _simple_setup(),
                    bars,
                    arm=arm,
                    notional=10_000.0,
                    slippage_bps=40.0,
                    position_expiry_ms=10 * _MIN,
                )
                self.assertEqual(out.net_cash, 0.0)
                self.assertEqual(out.chargeable_fills, 0)

    def test_staged_tranches_pay_the_per_fill_minimum_up_to_three_times(self):
        # §5.4: the per-fill USD minimum hits arm A's sell side once PER TRANCHE.
        # Tiny notional so every fee is the $1 minimum + FX leg.
        setup = _setup(
            entries=[(100.0, 100.0)],
            tps=[(101.0, 34.0), (102.0, 33.0), (103.0, 33.0)],
            stop=90.0,
            atr=1.0,
        )
        bars = _bars((100.0, 100.0, 100.0), (100.5, 104.0, 103.0))
        out = epr.replay_arm(
            setup,
            bars,
            arm=epr.ARM_A,
            notional=100.0,
            slippage_bps=0.0,
            position_expiry_ms=10 * _MIN,
        )
        self.assertEqual(out.chargeable_fills, 4)  # 1 buy + 3 tranche sells

    def test_time_stop_marks_at_close_without_a_fill_charge(self):
        # §5.4 marking: a position open at H is MARKED, not filled — no fee, no
        # slippage on the mark.
        bars = _bars((100.0, 100.0, 100.0), (99.0, 101.0, 100.5), (99.5, 101.0, 105.0))
        out = epr.replay_arm(
            _simple_setup(),
            bars,
            arm=epr.ARM_A,
            notional=10_000.0,
            slippage_bps=0.0,
            position_expiry_ms=2 * _MIN,
        )
        buy_fee = max(1.0, 0.0008 * 10_000.0) + 0.0025 * 10_000.0
        self.assertAlmostEqual(out.net_cash, 100.0 * (105.0 - 100.0) - buy_fee, places=6)
        self.assertEqual(out.chargeable_fills, 1)  # the buy only

    def test_horizon_open_remainder_is_marked_at_the_last_close(self):
        # Engine semantics: an OPEN remainder (no expiry passed) is marked to
        # the LAST close — the mark carries no fee and no slippage.
        bars = _bars((100.0, 100.0, 100.0), (99.0, 101.0, 100.5), (99.5, 101.0, 103.0))
        out = epr.replay_arm(
            _simple_setup(),
            bars,
            arm=epr.ARM_A,
            notional=10_000.0,
            slippage_bps=0.0,
            position_expiry_ms=None,
            charge_fees=False,
        )
        self.assertEqual(out.classification, "OPEN")
        self.assertAlmostEqual(out.net_cash, 100.0 * (103.0 - 100.0), places=6)

    def test_arm_a_pre_fee_cash_matches_the_engine_realized_r(self):
        # Convention parity with ladder_replay: at S=0 and fees off, arm A's
        # cash must equal realized_r * risk_per_share * filled_shares from the
        # SAME replay_ladder walk — one engine, two renderings.
        from alphalens_pipeline.feedback.ladder_replay import replay_ladder

        setup = _setup(
            entries=[(100.0, 60.0), (95.0, 40.0)],
            tps=[(104.0, 50.0), (108.0, 50.0)],
            stop=90.0,
            atr=2.0,
        )
        bars = _bars(
            (100.0, 100.5, 100.2),
            (94.0, 100.0, 96.0),
            (95.0, 105.0, 104.5),
            (96.0, 109.0, 108.5),
        )
        n0 = 10_000.0
        out = epr.replay_arm(
            setup,
            bars,
            arm=epr.ARM_A,
            notional=n0,
            slippage_bps=0.0,
            position_expiry_ms=10 * _MIN,
            charge_fees=False,
        )
        engine = replay_ladder(setup, bars, position_expiry_ms=10 * _MIN)
        assert engine.blended_entry is not None and engine.realized_r is not None
        shares = epr.filled_shares(setup, engine.entries_filled, notional=n0)
        risk = engine.blended_entry - 90.0
        self.assertAlmostEqual(out.net_cash, engine.realized_r * risk * shares, places=6)


class TestArmBGeometry(unittest.TestCase):
    def test_bracket_tp_is_clamped_to_the_first_brief_tranche(self):
        # §5.2: tp = max(atr bracket tp, first brief tranche). Entry 100, ATR 4
        # -> bracket TP 106; first tranche 110 -> clamped to 110.
        bars = _bars((100.0, 100.0, 100.0), (100.0, 110.5, 110.0))
        out = epr.replay_arm(
            _simple_setup(),
            bars,
            arm=epr.ARM_B,
            notional=10_000.0,
            slippage_bps=0.0,
            position_expiry_ms=10 * _MIN,
        )
        self.assertAlmostEqual(out.exit_levels.tp, 110.0)
        self.assertFalse(out.used_fallback)
        # And the whole position exits at the clamped level.
        self.assertAlmostEqual(out.gross_cash, 100.0 * 10.0, places=6)

    def test_stop_reanchors_to_the_slipped_fill_blend(self):
        # §5.2: on fill the stop moves to (fill blend + slippage) - 1.5*ATR,
        # through clamp_reanchor_target. One tier fills at 100, S=40bps ->
        # blend 100.4, stop 100.4 - 6 = 94.4 (initial planned-blend stop 94.0).
        # A dip to 94.2 must stop the position OUT (the reanchored stop is
        # ABOVE the initial one).
        bars = _bars((100.0, 100.0, 100.0), (94.2, 100.0, 95.0))
        out = epr.replay_arm(
            _simple_setup(),
            bars,
            arm=epr.ARM_B,
            notional=10_000.0,
            slippage_bps=40.0,
            position_expiry_ms=10 * _MIN,
        )
        self.assertEqual(out.classification, "SL_HIT")
        self.assertAlmostEqual(out.exit_levels.stop, 100.0 * 1.004 - 1.5 * 4.0, places=9)

    def test_refused_reanchor_leaves_the_resting_stop_where_it_is(self):
        # §5.2 via clamp_reanchor_target: None = "do NOT reanchor". E1 fills at
        # 100 -> reanchor to 100 - 6 = 94 (>= floor 78). A deep E2 fill drops
        # the blend to 80 -> proposed 74 < floor 78 -> REFUSED -> the resting
        # stop stays at the PRIOR reanchored 94, and the same bar's low (60)
        # stops the position out there (SL-first on the fill/stop ambiguity).
        setup = _setup(
            entries=[(100.0, 50.0), (60.0, 50.0)],
            tps=[(150.0, 100.0)],
            stop=78.0,
            atr=4.0,
        )
        bars = _bars((100.0, 100.0, 100.0), (60.0, 100.0, 61.0), (75.0, 80.0, 76.0))
        out = epr.replay_arm(
            setup,
            bars,
            arm=epr.ARM_B,
            notional=10_000.0,
            slippage_bps=0.0,
            position_expiry_ms=10 * _MIN,
        )
        self.assertEqual(out.classification, "SL_HIT")
        self.assertAlmostEqual(out.exit_levels.stop, 94.0, places=9)

    def test_arm_argument_is_mandatory(self):
        # §10.1: the arm is explicit and defaultless — same discipline #1114
        # imposed on the anchor.
        with self.assertRaises(TypeError):
            epr.replay_arm(
                _simple_setup(),
                _bars((100.0, 100.0, 100.0)),
                notional=1.0,
                slippage_bps=0.0,
                position_expiry_ms=_MIN,
            )  # type: ignore[call-arg]


class TestArmBFallback(unittest.TestCase):
    """Memo §5.3: when atr_bracket_levels returns None, arm B is the classic
    per-tier bracket on the brief's own tranche levels and disaster stop —
    equivalence with brokers/execution.py pinned here."""

    def _unbuildable_bracket_setup(self) -> dict:
        # ATR wider than ~2/3 of the blend -> the bracket stop lands below
        # zero -> atr_bracket_levels returns None (§5.3 first degenerate arm).
        return _setup(
            entries=[(100.0, 60.0), (95.0, 40.0)],
            tps=[(104.0, 50.0), (108.0, 50.0)],
            stop=90.0,
            atr=70.0,
        )

    def test_fallback_fires_when_the_bracket_is_not_constructible(self):
        setup = self._unbuildable_bracket_setup()
        blended = epr.planned_blend(setup)
        assert blended is not None
        self.assertIsNone(
            atr_bracket_levels(
                blended,
                70.0,
                stop_atr_mult=1.5,
                tp_atr_mult=1.5,
                tp_floor_frac=0.006,
                ceiling_price=None,
            )
        )
        bars = _bars((100.0, 100.0, 100.0), (94.0, 100.0, 96.0), (95.0, 109.0, 108.5))
        out = epr.replay_arm(
            setup,
            bars,
            arm=epr.ARM_B,
            notional=10_000.0,
            slippage_bps=0.0,
            position_expiry_ms=10 * _MIN,
        )
        self.assertTrue(out.used_fallback)

    def test_fallback_matches_the_classic_per_tier_bracket_assignment(self):
        # execution.py: tier i exits at tp_tranches[min(i, len-1)].target, the
        # shared disaster stop, tier-sized quantity. Two tiers, two tranches:
        # tier0 -> 104, tier1 -> 108. Path: both fill, then rises through both
        # targets. Expected cash (fees off): q0*(104-100) + q1*(108-95).
        setup = self._unbuildable_bracket_setup()
        bars = _bars((100.0, 100.0, 100.0), (94.0, 100.0, 96.0), (95.0, 109.0, 108.5))
        n0 = 10_000.0
        out = epr.replay_arm(
            setup,
            bars,
            arm=epr.ARM_B,
            notional=n0,
            slippage_bps=0.0,
            position_expiry_ms=10 * _MIN,
            charge_fees=False,
        )
        # Share-proportional quantities: qty_i = N0 * w_i / sum(w_j * p_j).
        denom = 60.0 * 100.0 + 40.0 * 95.0
        q0 = n0 * 60.0 / denom
        q1 = n0 * 40.0 / denom
        self.assertAlmostEqual(out.net_cash, q0 * 4.0 + q1 * 13.0, places=6)


class TestFallbackIndexClampedLast(unittest.TestCase):
    def test_third_tier_reuses_the_last_tranche_target(self):
        # execution.py: take_profit = tp_tranches[min(tier_index, len-1)].
        # Three tiers, two tranches -> tier2 exits at TP2 (index-clamped-last).
        setup = _setup(
            entries=[(100.0, 40.0), (95.0, 30.0), (90.0, 30.0)],
            tps=[(104.0, 50.0), (108.0, 50.0)],
            stop=80.0,
            atr=70.0,  # bracket stop below zero -> fallback
        )
        bars = _bars(
            (100.0, 100.0, 100.0),
            (89.0, 100.0, 92.0),  # all three tiers fill
            (90.0, 109.0, 108.5),  # both targets trade
        )
        n0 = 10_000.0
        out = epr.replay_arm(
            setup,
            bars,
            arm=epr.ARM_B,
            notional=n0,
            slippage_bps=0.0,
            position_expiry_ms=10 * _MIN,
            charge_fees=False,
        )
        self.assertTrue(out.used_fallback)
        denom = 40.0 * 100.0 + 30.0 * 95.0 + 30.0 * 90.0
        q = [n0 * w / denom for w in (40.0, 30.0, 30.0)]
        expected = q[0] * (104.0 - 100.0) + q[1] * (108.0 - 95.0) + q[2] * (108.0 - 90.0)
        self.assertAlmostEqual(out.net_cash, expected, places=6)


class TestSmgIncidentPin(unittest.TestCase):
    """Memo §10.1: pinned on the SMG 2026-08-24 numbers via the shared fixture,
    and parity with the LIVE build_exit_geometry_spec — the §11 item 4 HALT
    tripwire. This test going red during accrual is a HALT, not a flake."""

    def test_arm_b_levels_equal_the_live_spec_with_the_step3_clamp(self):
        from alphalens_pipeline.paper.sizing import build_exit_geometry_spec

        setup = smg_brief_trade_setup()
        spec = build_exit_geometry_spec(setup, pct_off_52w_high=None)
        assert spec is not None
        levels = epr.arm_b_initial_levels(setup, pct_off_52w_high=None)
        assert levels is not None
        self.assertAlmostEqual(levels.stop, spec.initial_levels.stop, places=9)
        self.assertAlmostEqual(levels.tp, spec.initial_levels.tp, places=9)
        # The clamp is live on SMG: bracket tp (blend + 1.5*ATR = 59.6277) sits
        # BELOW the first tranche 65.25, so the spec tp IS the tranche.
        self.assertAlmostEqual(levels.tp, SMG_TP_TRANCHES[0], places=9)
        self.assertAlmostEqual(levels.stop, SMG_PLANNED_BLEND - 1.5 * SMG_ATR, places=6)

    def test_smg_reanchor_mirrors_the_live_clamp_call(self):
        # E1-only fill at its limit + 40bps slippage; the replay's reanchor
        # must equal the live composition policy.decide_reanchor ->
        # clamp_reanchor_target(brief floor, ..., anchor=fill blend,
        # min_distance_frac=0.002).
        fill_blend = 59.786017 * (1.0 + 40.0 / 10_000.0)
        expected = clamp_reanchor_target(
            SMG_DISASTER_STOP,
            fill_blend - 1.5 * SMG_ATR,
            anchor_price=fill_blend,
            min_distance_frac=0.002,
        )
        assert expected is not None
        got = epr.arm_b_reanchored_stop(fill_blend, SMG_ATR, brief_disaster_stop=SMG_DISASTER_STOP)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, expected, places=12)


class TestWriteGuard(unittest.TestCase):
    def test_module_touches_no_files(self):
        # §10.1: never write to ~/.alphalens/population_ladders/. Stronger pin:
        # the module performs NO file IO at all — pure functions over inputs.
        import inspect

        src = inspect.getsource(epr)
        for token in ("population_ladders", "to_parquet", "open(", "write_text", "Path.home"):
            self.assertNotIn(token, src.replace("population_ladders/`` is", ""))


class TestZeroQuantityFills(unittest.TestCase):
    def test_a_zero_alloc_tier_charges_no_fee_and_counts_no_fill(self):
        # execution.py skips zero-qty tiers outright; a zero-share "fill" must
        # not collect the $1 per-fill minimum out of thin air.
        setup = _setup(
            entries=[(100.0, 100.0), (95.0, 0.0)],
            tps=[(110.0, 100.0)],
            stop=90.0,
            atr=4.0,
        )
        bars = _bars((100.0, 100.0, 100.0), (94.0, 100.0, 96.0), (95.0, 111.0, 110.0))
        for arm in (epr.ARM_A, epr.ARM_B):
            with self.subTest(arm=arm):
                out = epr.replay_arm(
                    setup,
                    bars,
                    arm=arm,
                    notional=10_000.0,
                    slippage_bps=0.0,
                    position_expiry_ms=10 * _MIN,
                )
                self.assertEqual(out.chargeable_fills, 2)  # E1 buy + one sell


class TestFeeDecomposition(unittest.TestCase):
    def test_buy_plus_equal_notional_sell_equals_round_trip_fee_bps(self):
        # §5.4: fees are charged per fill "through round_trip_fee_bps" — the
        # per-fill decomposition must sum back to the live gate's round trip.
        from alphalens_pipeline.brokers.automanager.costs import round_trip_fee_bps

        for notional in (100.0, 1_250.0, 3_750.0, 10_000.0):
            with self.subTest(notional=notional):
                per_fill = epr._per_fill_fee(notional)
                round_trip = round_trip_fee_bps(
                    notional, fx_applies=True, min_commission_applies=True
                )
                assert round_trip is not None
                self.assertAlmostEqual(2 * per_fill, round_trip / 10_000.0 * notional, places=9)


class TestNetCashIsFinite(unittest.TestCase):
    def test_every_outcome_field_is_finite(self):
        bars = _bars((100.0, 100.0, 100.0), (90.0, 111.0, 95.0))
        for arm in (epr.ARM_A, epr.ARM_B):
            out = epr.replay_arm(
                _simple_setup(),
                bars,
                arm=arm,
                notional=10_000.0,
                slippage_bps=40.0,
                position_expiry_ms=10 * _MIN,
            )
            self.assertTrue(math.isfinite(out.net_cash))
            self.assertTrue(math.isfinite(out.total_fees))


if __name__ == "__main__":
    unittest.main()
