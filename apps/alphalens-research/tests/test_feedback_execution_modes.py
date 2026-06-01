"""Tests for the Track A v2 PR-4 execution-mode gating estimator.

``recommend_execution_modes`` is a pure function over matured feedback rows
``(regime, fill_status, shadow_return, realized_return)``. The load-bearing
behaviour is that it stays INERT (safe-default LIMIT) below the ≥50 pooled gate
and on every undefined / non-finite path — "limit→market is NOT a free fix".
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.feedback.execution_modes import (
    DEFAULT_POOLED_GATE_N,
    POOLED_KEY,
    recommend_execution_modes,
)

# Row = (regime, fill_status, shadow_return, realized_return).
FILLED = "FILLED"
UNFILLED = "UNFILLED"


def _filled(regime: str, shadow: float, realized: float) -> tuple[str, str, float, float]:
    return (regime, FILLED, shadow, realized)


def _unfilled(regime: str, shadow: float) -> tuple[str, str, float, None]:
    return (regime, UNFILLED, shadow, None)


class TestProgramGateInert(unittest.TestCase):
    def test_below_pooled_gate_all_cells_inert_limit(self):
        # 10 finite labelled rows (< 50) => program gate fires; no break-even.
        rows = [_filled("mid", 0.05, 0.0) for _ in range(6)] + [
            _unfilled("mid", 0.08) for _ in range(4)
        ]
        recs = recommend_execution_modes(rows)
        for rec in recs.values():
            self.assertEqual(rec.recommended_mode, "limit")
        self.assertTrue(recs[POOLED_KEY].gated_reason.startswith("below_pooled_gate"))
        self.assertTrue(recs["mid"].gated_reason.startswith("below_pooled_gate"))

    def test_empty_input_is_inert_limit(self):
        recs = recommend_execution_modes([])
        self.assertEqual(set(recs), {POOLED_KEY})
        self.assertEqual(recs[POOLED_KEY].recommended_mode, "limit")
        self.assertEqual(recs[POOLED_KEY].n, 0)
        self.assertTrue(recs[POOLED_KEY].gated_reason.startswith("below_pooled_gate"))

    def test_below_gate_with_null_shadow_excluded_is_inert(self):
        # The real failure mode the maturity-predicate fix guards: rows whose
        # shadow_return is None (cheap-pass-only, not yet priced) must NOT count
        # toward the pool. Here every row is None-shadow => pool n == 0 => inert.
        rows = [("mid", FILLED, None, None) for _ in range(80)]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs[POOLED_KEY].n, 0)
        self.assertEqual(recs[POOLED_KEY].recommended_mode, "limit")
        self.assertTrue(recs[POOLED_KEY].gated_reason.startswith("below_pooled_gate"))


class TestUnknownRegime(unittest.TestCase):
    def test_all_unknown_regime_no_actionable_cells(self):
        rows = [_filled("unknown", 0.05, 0.0) for _ in range(60)] + [
            _unfilled("unknown", 0.08) for _ in range(20)
        ]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["unknown"].recommended_mode, "limit")
        self.assertEqual(recs["unknown"].gated_reason, "regime_unknown_not_actionable")
        # Pool excludes unknown => n_pool == 0 => pooled is below-gate.
        self.assertEqual(recs[POOLED_KEY].n, 0)


class TestAdmissibility(unittest.TestCase):
    def test_partial_fill_dropped_from_all_stats(self):
        rows = [_filled("mid", 0.05, 0.0) for _ in range(30)] + [
            ("mid", "PARTIAL", 0.05, 0.02) for _ in range(10)
        ]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["mid"].n, 30)  # PARTIAL not counted

    def test_nonfinite_shadow_dropped(self):
        rows = [_filled("mid", 0.05, 0.0) for _ in range(30)] + [
            ("mid", UNFILLED, math.nan, None),
            ("mid", UNFILLED, math.inf, None),
        ]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["mid"].n_unfilled, 0)  # non-finite shadow dropped
        self.assertEqual(recs["mid"].n, 30)

    def test_nonfinite_realized_excluded_from_gap(self):
        rows = [_filled("mid", 0.05, math.nan) for _ in range(50)]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["mid"].n_filled, 50)
        self.assertEqual(recs["mid"].n_gap, 0)  # NaN realized excluded from gap


class TestCellFloor(unittest.TestCase):
    def test_cell_below_floor_adopts_pooled(self):
        # Pool >= 50 (40 mid-filled + 10 mid-unfilled = 50) and a thin low cell.
        rows = (
            [_filled("mid", 0.05, 0.0) for _ in range(40)]
            + [_unfilled("mid", 0.06) for _ in range(10)]
            + [_filled("low", 0.05, 0.0) for _ in range(5)]
        )
        recs = recommend_execution_modes(rows)
        self.assertGreaterEqual(recs[POOLED_KEY].n, DEFAULT_POOLED_GATE_N)
        self.assertEqual(recs["low"].n, 5)
        self.assertEqual(recs["low"].recommended_mode, recs[POOLED_KEY].recommended_mode)
        self.assertTrue(recs["low"].gated_reason.startswith("cell_below_floor_uses_pooled"))


class TestBreakEvenEdges(unittest.TestCase):
    def test_no_unfilled_nothing_to_recover(self):
        # Target cell past floor with 0 unfilled => MO undefined => limit.
        rows = (
            [_filled("low", 0.05, 0.0) for _ in range(40)]
            + [_unfilled("mid", 0.06) for _ in range(15)]
            + [_filled("mid", 0.05, 0.0) for _ in range(20)]
        )
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["low"].n_unfilled, 0)
        self.assertEqual(recs["low"].recommended_mode, "limit")
        self.assertEqual(recs["low"].gated_reason, "no_unfilled_nothing_to_recover")

    def test_no_filled_cannot_price_impact(self):
        # Target cell past floor with 0 filled => cannot price market impact.
        rows = [_unfilled("low", 0.08) for _ in range(40)] + [
            _filled("mid", 0.05, 0.0) for _ in range(20)
        ]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["low"].n_filled, 0)
        self.assertEqual(recs["low"].recommended_mode, "limit")
        self.assertEqual(recs["low"].gated_reason, "no_filled_cannot_price_impact")

    def test_negative_missed_opportunity_stays_limit(self):
        # All unfilled shadows negative => MO* < 0 => staying limit is correct.
        rows = [_filled("mid", 0.01, 0.0) for _ in range(40)] + [
            _unfilled("mid", -0.05) for _ in range(20)
        ]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["mid"].recommended_mode, "limit")
        self.assertEqual(recs["mid"].gated_reason, "negative_missed_opportunity_limit_correct")

    def test_dead_band_strict_inequality_margin_zero_stays_limit(self):
        # Pooled stats engineered so Δ == 0 exactly: fr=0.5, MO=0.06, g=MI=0.02
        # => (1-.5)*.06 - .02 - .5*.02 = .03 - .02 - .01 = 0. Strict '>' => limit.
        rows = [_filled("mid", 0.02, 0.0) for _ in range(25)] + [
            _unfilled("mid", 0.06) for _ in range(25)
        ]
        recs = recommend_execution_modes(rows)
        self.assertAlmostEqual(recs[POOLED_KEY].switch_margin, 0.0)
        self.assertEqual(recs[POOLED_KEY].recommended_mode, "limit")
        self.assertEqual(recs[POOLED_KEY].gated_reason, "no_positive_margin")

    def test_genuine_switch_fires_market(self):
        # Same structure, larger MO (0.10): Δ = .5*.10 - .02 - .5*.02 = .02 > 0.
        rows = [_filled("mid", 0.02, 0.0) for _ in range(25)] + [
            _unfilled("mid", 0.10) for _ in range(25)
        ]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["mid"].recommended_mode, "market")
        self.assertTrue(recs["mid"].gated_reason.startswith("switch_breakeven_passed"))
        self.assertAlmostEqual(recs["mid"].switch_margin, 0.02)

    def test_negative_gap_floors_market_impact_at_zero(self):
        # Filled limit did WORSE than arrival (realized > shadow) => g < 0 =>
        # MI* = max(g,0) = 0 (adverse fill selection must not look like cheap
        # market impact). With positive MO and fill_rate < 1, switch can fire.
        rows = [_filled("mid", 0.02, 0.05) for _ in range(25)] + [
            _unfilled("mid", 0.10) for _ in range(25)
        ]
        recs = recommend_execution_modes(rows)
        self.assertEqual(recs["mid"].expected_market_impact, 0.0)
        self.assertLess(recs[POOLED_KEY].observed_execution_gap, 0.0)


class TestShrinkage(unittest.TestCase):
    def test_shrinkage_weight_is_n_over_n_plus_k(self):
        # A 'low' cell with n=20 reports fill_rate weight 20/(20+20) = 0.5.
        rows = (
            [_filled("mid", 0.05, 0.0) for _ in range(40)]
            + [_unfilled("mid", 0.06) for _ in range(20)]
            + [_filled("low", 0.05, 0.0) for _ in range(20)]
        )
        recs = recommend_execution_modes(rows)
        self.assertAlmostEqual(recs["low"].shrinkage_weight, 0.5)

    def test_thin_cell_mo_shrinks_toward_pool(self):
        # A thin 'low' cell's MO* is pulled toward the pooled MO. low has 2
        # unfilled at 0.20; pool MO is dominated by 30 mid-unfilled at 0.02.
        # mo_shrunk_low must lie strictly between the two raw means.
        rows = (
            [_filled("mid", 0.01, 0.0) for _ in range(30)]
            + [_unfilled("mid", 0.02) for _ in range(30)]
            + [_unfilled("low", 0.20) for _ in range(2)]
        )
        recs = recommend_execution_modes(rows)
        low = recs["low"]
        self.assertAlmostEqual(low.missed_opportunity, 0.20)  # raw
        self.assertIsNotNone(low.missed_opportunity_shrunk)
        self.assertLess(low.missed_opportunity_shrunk, 0.20)
        self.assertGreater(low.missed_opportunity_shrunk, 0.02)


if __name__ == "__main__":
    unittest.main()
