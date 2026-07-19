"""Pure ATR-penalty / selection_score math (no I/O)."""

from __future__ import annotations

import unittest

from alphalens_pipeline.thematic.screening import selection_score as ss


class TestAtrPenalty(unittest.TestCase):
    def test_zero_at_or_below_p67_onset(self):
        self.assertEqual(ss.atr_penalty(0.0), 0.0)
        self.assertEqual(ss.atr_penalty(4.0), 0.0)
        self.assertEqual(ss.atr_penalty(ss.ATR_RAMP_LO), 0.0)  # exactly p67 → 0

    def test_full_lambda_at_or_above_p90(self):
        self.assertEqual(ss.atr_penalty(ss.ATR_RAMP_HI), ss.LAMBDA)
        self.assertEqual(ss.atr_penalty(20.0), ss.LAMBDA)

    def test_linear_ramp_midpoint(self):
        mid = (ss.ATR_RAMP_LO + ss.ATR_RAMP_HI) / 2.0
        self.assertAlmostEqual(ss.atr_penalty(mid), ss.LAMBDA / 2.0, places=9)

    def test_monotonic_non_decreasing(self):
        xs = [0, 4, 5.77, 6.5, 7.1, 8.37, 12]
        ps = [ss.atr_penalty(x) for x in xs]
        self.assertEqual(ps, sorted(ps))

    def test_missing_or_nan_or_nonpositive_is_zero(self):
        self.assertEqual(ss.atr_penalty(None), 0.0)
        self.assertEqual(ss.atr_penalty(float("nan")), 0.0)
        self.assertEqual(ss.atr_penalty(-1.0), 0.0)
        self.assertEqual(ss.atr_penalty("bad"), 0.0)  # type: ignore[arg-type]


class TestSelectionScore(unittest.TestCase):
    def test_calm_name_keeps_layer4(self):
        self.assertEqual(ss.selection_score(4, 3.0), 4.0)

    def test_extended_name_loses_one_tier_at_p90(self):
        self.assertEqual(ss.selection_score(4, 9.0), 3.0)

    def test_version_string_is_pinned(self):
        self.assertEqual(ss.SCORER_CONFIG_VERSION, "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37")


class TestAtrPenaltyBreakpointHardening(unittest.TestCase):
    """Pin the FROZEN ramp breakpoints (p67=5.77, p90=8.37) at ABSOLUTE x.

    The existing ``test_linear_ramp_midpoint`` computes its test point FROM
    ``ss.ATR_RAMP_LO/HI``, so the point moves together with a mutated constant
    and cannot detect the shift — which is why the NumberReplacer mutants on
    ATR_RAMP_LO / ATR_RAMP_HI survived. These assertions use fixed x and
    expected values HARDCODED from the frozen constants (never recomputed from
    ``ss.ATR_RAMP_*``), so a constant mutation is caught.
    """

    def test_penalty_exact_at_absolute_x(self):
        # penalty(x) = (x - 5.77) / (8.37 - 5.77) for 5.77 < x < 8.37.
        # Kills the NumberReplacer mutants on ATR_RAMP_LO (5.77 -> 4.77/6.77)
        # and ATR_RAMP_HI (8.37 -> 7.37): each shifts these fixed-x outputs.
        self.assertAlmostEqual(ss.atr_penalty(6.0), 0.08846153846153863, places=12)
        self.assertAlmostEqual(ss.atr_penalty(7.0), 0.4730769230769233, places=12)
        self.assertAlmostEqual(ss.atr_penalty(8.0), 0.857692307692308, places=12)

    def test_onset_is_absolutely_at_5_77(self):
        # Just below the frozen onset -> 0; just above -> strictly positive.
        # Pins ATR_RAMP_LO at 5.77 absolutely (a shift to 4.77 makes 5.7
        # positive; a shift to 6.77 makes 5.8 zero).
        self.assertEqual(ss.atr_penalty(5.7), 0.0)
        self.assertGreater(ss.atr_penalty(5.8), 0.0)

    def test_selection_score_subtracts_absolute_penalty(self):
        # layer4 minus the fixed-x penalty; pins the constants through the
        # public entry point too.
        self.assertAlmostEqual(ss.selection_score(5.0, 7.0), 4.526923076923077, places=12)
