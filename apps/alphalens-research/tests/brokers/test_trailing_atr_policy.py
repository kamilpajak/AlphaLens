"""Unit tests for the ``trailing_atr`` exit policy: the Chandelier
``peak - k*atr`` target helper, ``TrailingAtrPolicy`` itself, the widened
``decide_reanchor`` signature on the two existing policies, and the
``trailing_atr`` registry entry.
"""

from __future__ import annotations

import math
import unittest

from broker_contract.exit_geometry.levels import chandelier_target
from broker_contract.exit_geometry.policy import (
    AtrBracketPolicy,
    SetupStaticPolicy,
    TrailingAtrPolicy,
)
from broker_contract.exit_geometry.registry import resolve_exit_policy, resolve_policy


class TestChandelierTarget(unittest.TestCase):
    def test_target_is_peak_minus_k_atr(self):
        self.assertAlmostEqual(chandelier_target(110.0, 2.0, k=0.6), 108.8)

    def test_none_on_nonpositive_or_nonfinite(self):
        self.assertIsNone(chandelier_target(0.0, 2.0, k=0.6))
        self.assertIsNone(chandelier_target(110.0, 0.0, k=0.6))
        self.assertIsNone(chandelier_target(math.nan, 2.0, k=0.6))
        self.assertIsNone(chandelier_target(110.0, math.inf, k=0.6))

    def test_none_when_target_nonpositive(self):
        self.assertIsNone(chandelier_target(1.0, 100.0, k=0.6))  # 1 - 60 < 0


class TestTrailingAtrPolicy(unittest.TestCase):
    def _policy(self):
        return TrailingAtrPolicy(
            resolve_policy("atr_bracket_1p5"), name="trailing_atr", activation_r=0.5, k_atr=0.6
        )

    def test_trails_flag_true_here_false_elsewhere(self):
        self.assertTrue(self._policy().trails)
        self.assertFalse(SetupStaticPolicy().trails)
        self.assertFalse(
            AtrBracketPolicy(resolve_policy("atr_bracket_1p5"), name="atr_bracket_1p5").trails
        )

    def test_dark_before_activation(self):
        # risk = stop_atr_mult(1.5)*atr(2)=3; activation 0.5R => need peak >= avg+1.5
        self.assertIsNone(self._policy().decide_reanchor(100.0, 2.0, peak=101.0))

    def test_chandelier_once_armed(self):
        # peak 110 >= 100+1.5 armed => 110 - 0.6*2 = 108.8
        self.assertAlmostEqual(self._policy().decide_reanchor(100.0, 2.0, peak=110.0), 108.8)

    def test_none_without_peak(self):
        self.assertIsNone(self._policy().decide_reanchor(100.0, 2.0))

    def test_existing_policies_ignore_peak_bytewise(self):
        atr = AtrBracketPolicy(resolve_policy("atr_bracket_1p5"), name="atr_bracket_1p5")
        self.assertEqual(
            atr.decide_reanchor(100.0, 2.0, peak=999.0),
            atr.decide_reanchor(100.0, 2.0),
        )
        self.assertIsNone(SetupStaticPolicy().decide_reanchor(100.0, 2.0, peak=999.0))

    def test_registry_resolves_trailing_atr(self):
        pol = resolve_exit_policy("trailing_atr")
        self.assertTrue(pol.trails)
        self.assertTrue(pol.requires_amend_stop)
        # The policy names ITSELF, and the geometry it wraps stays a separate
        # fact (issue #1138). This assertion used to read the other way round,
        # with a comment explaining that the name was the geometry's.
        self.assertEqual(pol.name, "trailing_atr")
        self.assertEqual(pol.geometry_name, "atr_bracket_1p5")


if __name__ == "__main__":
    unittest.main()
