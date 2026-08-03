import math
import unittest

from broker_contract.exit_geometry.policy import AtrBracketPolicy, SetupStaticPolicy
from broker_contract.exit_geometry.registry import resolve_exit_policy, resolve_policy


class SetupStaticPolicyTest(unittest.TestCase):
    def test_is_inert(self):
        p = SetupStaticPolicy()
        self.assertEqual(p.name, "setup_static")
        self.assertFalse(p.applies_geometry)
        self.assertFalse(p.requires_amend_stop)
        self.assertIsNone(p.decide_placement_geometry(100.0, 2.0, ceiling_price=None))
        self.assertIsNone(p.decide_reanchor(100.0, 2.0))


class AtrBracketPolicyTest(unittest.TestCase):
    def setUp(self):
        self.p = AtrBracketPolicy(resolve_policy("atr_bracket_1p5"))

    def test_flags(self):
        self.assertEqual(self.p.name, "atr_bracket_1p5")
        self.assertTrue(self.p.applies_geometry)
        self.assertTrue(self.p.requires_amend_stop)
        self.assertGreater(self.p.min_stop_distance_frac, 0.0)

    def test_placement_matches_raw_levels(self):
        want = resolve_policy("atr_bracket_1p5").levels(100.0, 2.0, ceiling_price=None)
        self.assertEqual(self.p.decide_placement_geometry(100.0, 2.0, ceiling_price=None), want)

    def test_reanchor_target(self):
        self.assertTrue(math.isclose(self.p.decide_reanchor(101.0, 2.0), 101.0 - 3.0))

    def test_reanchor_degenerate_is_none(self):
        self.assertIsNone(self.p.decide_reanchor(101.0, 0.0))
        self.assertIsNone(self.p.decide_reanchor(101.0, float("nan")))
        self.assertIsNone(self.p.decide_reanchor(0.0, 2.0))


class ResolveExitPolicyTest(unittest.TestCase):
    def test_known_names(self):
        self.assertIsInstance(resolve_exit_policy("setup_static"), SetupStaticPolicy)
        self.assertIsInstance(resolve_exit_policy("atr_bracket_1p5"), AtrBracketPolicy)

    def test_unknown_raises_valueerror(self):
        with self.assertRaises(ValueError):
            resolve_exit_policy("nope")


if __name__ == "__main__":
    unittest.main()
