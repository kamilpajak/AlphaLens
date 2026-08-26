import math
import unittest

from broker_contract.exit_geometry.policy import AtrBracketPolicy, SetupStaticPolicy
from broker_contract.exit_geometry.registry import (
    exit_policy_registry,
    resolve_exit_policy,
    resolve_policy,
)


class BehaviouralIdentityIsNotTheGeometryNameTest(unittest.TestCase):
    """A policy's name must say which POLICY ran, not which geometry it wraps.

    Both bracket policies used to report ``self.geom.name``, so the trailing
    policy the LIVE unit pins and the static one it does not were indis-
    tinguishable by name. Three operator-facing log lines and one capability
    error printed that name, so the record could not answer "which policy ran"
    for a real trade (issue #1112 recorded that question and could not close
    it).
    """

    def test_every_registered_policy_reports_its_own_registry_key(self):
        # The anti-rot guard: enumerated from the registry itself, so a policy
        # added later cannot quietly inherit its wrapped geometry's name again.
        registry = exit_policy_registry()
        self.assertGreaterEqual(len(registry), 3)
        for key, policy in registry.items():
            with self.subTest(key=key):
                self.assertEqual(policy.name, key)

    def test_the_two_bracket_policies_are_distinguishable_by_name(self):
        trailing = resolve_exit_policy("trailing_atr")
        static = resolve_exit_policy("atr_bracket_1p5")
        # They genuinely differ in behaviour...
        self.assertTrue(trailing.trails)
        self.assertFalse(static.trails)
        # ...so they must differ in the one field a log line prints.
        self.assertNotEqual(trailing.name, static.name)

    def test_the_wrapped_geometry_name_stays_reachable_and_is_shared(self):
        # The geometry is a real, separate fact: both bracket policies place
        # against the SAME geometry and differ only in how the exit then moves.
        for key in ("trailing_atr", "atr_bracket_1p5"):
            with self.subTest(key=key):
                self.assertEqual(resolve_exit_policy(key).geometry_name, "atr_bracket_1p5")

    def test_a_policy_that_wraps_no_geometry_has_no_geometry_name(self):
        # Honest absence rather than a placeholder string: setup_static places
        # no geometry at all, so there is no geometry to name.
        self.assertIsNone(resolve_exit_policy("setup_static").geometry_name)


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
        self.p = AtrBracketPolicy(resolve_policy("atr_bracket_1p5"), name="atr_bracket_1p5")

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
