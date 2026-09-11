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
        self.assertGreaterEqual(len(registry), 4)
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
        # Honest absence rather than a placeholder string: setup_static and
        # breakeven_trail place no geometry at all, so there is none to name.
        for key in ("setup_static", "breakeven_trail"):
            with self.subTest(key=key):
                self.assertIsNone(resolve_exit_policy(key).geometry_name)


class SetupStaticPolicyTest(unittest.TestCase):
    def test_is_inert(self):
        p = SetupStaticPolicy()
        self.assertEqual(p.name, "setup_static")
        self.assertFalse(p.requires_amend_stop)
        self.assertIsNone(p.decide_placement_geometry(100.0, 2.0, ceiling_price=None))
        self.assertIsNone(p.decide_reanchor(100.0, 2.0))


class AtrBracketPolicyTest(unittest.TestCase):
    def setUp(self):
        self.p = AtrBracketPolicy(resolve_policy("atr_bracket_1p5"), name="atr_bracket_1p5")

    def test_flags(self):
        self.assertEqual(self.p.name, "atr_bracket_1p5")
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


class AnAbsentAtrIsThePolicysOwnBusinessTest(unittest.TestCase):
    """Whether a policy NEEDS an ATR is the policy's fact, not its caller's.

    ``position_manager._maybe_trail`` refuses today on a missing / degenerate
    ``plan.reanchor.atr`` before it ever calls in — so a pick armed without a
    geometry stamp never trails, whatever policy is active. That guard is doing
    two jobs at once: it enforces a real requirement of the ATR family, and it
    also vetoes ``breakeven_trail``, which discards ``atr`` entirely. Issue
    #1236 needs the second half gone, and the first half has to live somewhere
    before it can be removed from the caller.

    So ``decide_reanchor`` takes ``atr: float | None``: the ATR family refuses
    cleanly on ``None``, and a policy that never reads it is undisturbed. Today
    the ATR family RAISES ``TypeError`` on ``None`` — removing the caller's
    veto first would turn a veto into a crash inside the protection pass.
    """

    def test_every_registered_policy_accepts_an_absent_atr_without_raising(self):
        # Enumerated from the registry so a policy added later cannot quietly
        # reintroduce the raise.
        for key, policy in exit_policy_registry().items():
            with self.subTest(key=key):
                policy.decide_reanchor(100.0, None, peak=130.0, last_price=129.0, plan_stop=90.0)

    def test_the_atr_family_refuses_an_absent_atr(self):
        for key in ("atr_bracket_1p5", "trailing_atr"):
            with self.subTest(key=key):
                policy = resolve_exit_policy(key)
                self.assertIsNone(
                    policy.decide_reanchor(
                        100.0, None, peak=130.0, last_price=129.0, plan_stop=90.0
                    )
                )

    def test_a_policy_that_never_reads_the_atr_returns_the_same_target_without_one(self):
        # The discriminator for the change: breakeven_trail's target is a
        # function of (avg_price, peak, plan_stop) only, so an absent ATR must
        # not change it. Numbers are the AMBA 2026-09-04 LIVE round trip
        # (entry 59.00, disaster stop 55.00, session peak 62.78).
        policy = resolve_exit_policy("breakeven_trail")
        kwargs = {"peak": 62.78, "last_price": 62.40, "plan_stop": 55.00}
        with_atr = policy.decide_reanchor(59.00, 0.4674, **kwargs)
        without_atr = policy.decide_reanchor(59.00, None, **kwargs)
        self.assertIsNotNone(with_atr)
        self.assertEqual(with_atr, without_atr)

    def test_the_inert_policy_still_refuses_either_way(self):
        policy = resolve_exit_policy("setup_static")
        self.assertIsNone(policy.decide_reanchor(100.0, 2.0))
        self.assertIsNone(policy.decide_reanchor(100.0, None))


class ResolveExitPolicyTest(unittest.TestCase):
    def test_known_names(self):
        self.assertIsInstance(resolve_exit_policy("setup_static"), SetupStaticPolicy)
        self.assertIsInstance(resolve_exit_policy("atr_bracket_1p5"), AtrBracketPolicy)

    def test_unknown_raises_valueerror(self):
        with self.assertRaises(ValueError):
            resolve_exit_policy("nope")


if __name__ == "__main__":
    unittest.main()
