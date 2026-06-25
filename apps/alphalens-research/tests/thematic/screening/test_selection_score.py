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
