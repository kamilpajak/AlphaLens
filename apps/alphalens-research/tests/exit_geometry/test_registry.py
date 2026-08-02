"""Unit tests for the exit-geometry policy registry."""

from __future__ import annotations

import unittest

from broker_contract.exit_geometry.levels import atr_bracket_levels
from broker_contract.exit_geometry.registry import resolve_policy


class TestResolvePolicy(unittest.TestCase):
    def test_atr_bracket_1p5_pinned_values(self):
        policy = resolve_policy("atr_bracket_1p5")
        self.assertEqual(policy.stop_atr_mult, 1.5)
        self.assertEqual(policy.tp_atr_mult, 1.5)
        self.assertEqual(policy.tp_floor_frac, 0.006)

    def test_unknown_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            resolve_policy("no_such_policy")


class TestPolicyLevelsDelegation(unittest.TestCase):
    def test_levels_delegates_to_atr_bracket_levels_identically(self):
        policy = resolve_policy("atr_bracket_1p5")
        expected = atr_bracket_levels(
            100.0,
            10.0,
            stop_atr_mult=policy.stop_atr_mult,
            tp_atr_mult=policy.tp_atr_mult,
            tp_floor_frac=policy.tp_floor_frac,
            ceiling_price=105.0,
        )
        actual = policy.levels(100.0, 10.0, ceiling_price=105.0)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
