"""Unit tests for the ``breakeven_trail`` exit policy: the fractional-giveback
target helper, ``BreakevenTrailPolicy`` itself, the widened ``decide_reanchor``
signature (kw-only ``plan_stop``) on the existing policies, and the
``breakeven_trail`` registry entry.

Lens-fidelity contract: this policy is the live port of the registered what-if
lens ``be_0p5r_trail0p6`` (``ladder_replay.replay_ladder_breakeven``). 1R is
the filled blend minus the BRIEF disaster stop (``plan_stop``) — NOT an ATR
multiple — and once the peak reaches ``entry + activation_r*R`` the stop
target is ``max(entry, entry + trail_frac*(peak - entry))``. At the arming
instant that is already ``entry + activation_r*trail_frac*R`` (entry+0.3R for
the registered 0.5/0.6 parameters), not a flat break-even; the lens behaves
identically and the name is kept for continuity with the lens label.
"""

from __future__ import annotations

import math
import unittest

from broker_contract.exit_geometry.levels import fractional_giveback_target
from broker_contract.exit_geometry.policy import (
    AtrBracketPolicy,
    BreakevenTrailPolicy,
    SetupStaticPolicy,
    TrailingAtrPolicy,
)
from broker_contract.exit_geometry.registry import resolve_exit_policy, resolve_policy


class TestFractionalGivebackTarget(unittest.TestCase):
    def test_target_is_entry_plus_frac_of_gain(self):
        self.assertAlmostEqual(fractional_giveback_target(100.0, 110.0, frac=0.6), 106.0)

    def test_floors_at_entry_when_peak_below_entry(self):
        # Unreachable through an armed policy (arming needs peak above entry),
        # but the leaf must stay honest when called directly.
        self.assertAlmostEqual(fractional_giveback_target(100.0, 99.0, frac=0.6), 100.0)

    def test_none_on_nonpositive_or_nonfinite_prices(self):
        self.assertIsNone(fractional_giveback_target(0.0, 110.0, frac=0.6))
        self.assertIsNone(fractional_giveback_target(100.0, 0.0, frac=0.6))
        self.assertIsNone(fractional_giveback_target(math.nan, 110.0, frac=0.6))
        self.assertIsNone(fractional_giveback_target(100.0, math.inf, frac=0.6))

    def test_frac_one_trails_at_the_peak(self):
        # The upper bound is INCLUSIVE: frac=1.0 is a zero-giveback trail
        # pinned to the peak itself.
        self.assertAlmostEqual(fractional_giveback_target(100.0, 110.0, frac=1.0), 110.0)

    def test_none_on_frac_outside_unit_interval(self):
        self.assertIsNone(fractional_giveback_target(100.0, 110.0, frac=0.0))
        self.assertIsNone(fractional_giveback_target(100.0, 110.0, frac=-0.5))
        self.assertIsNone(fractional_giveback_target(100.0, 110.0, frac=1.5))
        self.assertIsNone(fractional_giveback_target(100.0, 110.0, frac=math.nan))


class TestBreakevenTrailPolicy(unittest.TestCase):
    def _policy(self) -> BreakevenTrailPolicy:
        return BreakevenTrailPolicy(activation_r=0.5, trail_frac=0.6, name="breakeven_trail")

    def test_flags(self):
        p = self._policy()
        self.assertEqual(p.name, "breakeven_trail")
        self.assertIsNone(p.geometry_name)
        self.assertFalse(p.applies_geometry)
        self.assertTrue(p.requires_amend_stop)
        self.assertTrue(p.trails)
        self.assertGreater(p.min_stop_distance_frac, 0.0)

    def test_places_no_geometry(self):
        # applies_geometry=False is what keeps the brief's TP ladder and the
        # brief disaster stop journaled verbatim; the policy places nothing.
        self.assertIsNone(self._policy().decide_placement_geometry(100.0, 2.0, ceiling_price=None))

    def test_dark_without_plan_stop(self):
        self.assertIsNone(self._policy().decide_reanchor(100.0, 2.0, peak=110.0))

    def test_dark_on_degenerate_risk(self):
        # plan_stop at or above the blend means no positive 1R exists.
        p = self._policy()
        self.assertIsNone(p.decide_reanchor(100.0, 2.0, peak=110.0, plan_stop=100.0))
        self.assertIsNone(p.decide_reanchor(100.0, 2.0, peak=110.0, plan_stop=101.0))
        self.assertIsNone(p.decide_reanchor(100.0, 2.0, peak=110.0, plan_stop=math.nan))
        self.assertIsNone(p.decide_reanchor(100.0, 2.0, peak=110.0, plan_stop=-1.0))

    def test_dark_before_activation(self):
        # 1R = 100 - 90 = 10; activation 0.5R needs peak >= 105.
        self.assertIsNone(self._policy().decide_reanchor(100.0, 2.0, peak=104.9, plan_stop=90.0))

    def test_arms_at_exactly_half_r(self):
        # Non-strict trigger, mirroring the lens latch: peak == entry + 0.5R
        # arms, and the target is already entry + 0.6*(peak-entry) = 103.0.
        self.assertAlmostEqual(
            self._policy().decide_reanchor(100.0, 2.0, peak=105.0, plan_stop=90.0), 103.0
        )

    def test_trails_fraction_of_gain_once_armed(self):
        self.assertAlmostEqual(
            self._policy().decide_reanchor(100.0, 2.0, peak=110.0, plan_stop=90.0), 106.0
        )

    def test_monotone_in_peak(self):
        p = self._policy()
        lo = p.decide_reanchor(100.0, 2.0, peak=105.0, plan_stop=90.0)
        hi = p.decide_reanchor(100.0, 2.0, peak=112.0, plan_stop=90.0)
        assert lo is not None and hi is not None
        self.assertGreater(hi, lo)

    def test_atr_is_ignored_entirely(self):
        # The lens carries no ATR term; the policy must not let a degenerate
        # ATR veto a decision it never consumes ATR for.
        p = self._policy()
        want = p.decide_reanchor(100.0, 2.0, peak=110.0, plan_stop=90.0)
        self.assertEqual(p.decide_reanchor(100.0, math.nan, peak=110.0, plan_stop=90.0), want)
        self.assertEqual(p.decide_reanchor(100.0, 999.0, peak=110.0, plan_stop=90.0), want)

    def test_dark_without_peak(self):
        self.assertIsNone(self._policy().decide_reanchor(100.0, 2.0, plan_stop=90.0))

    def test_existing_policies_ignore_plan_stop_bytewise(self):
        atr = AtrBracketPolicy(resolve_policy("atr_bracket_1p5"), name="atr_bracket_1p5")
        self.assertEqual(
            atr.decide_reanchor(100.0, 2.0, plan_stop=90.0),
            atr.decide_reanchor(100.0, 2.0),
        )
        trailing = TrailingAtrPolicy(
            resolve_policy("atr_bracket_1p5"), name="trailing_atr", activation_r=0.5, k_atr=0.6
        )
        self.assertEqual(
            trailing.decide_reanchor(100.0, 2.0, peak=110.0, plan_stop=90.0),
            trailing.decide_reanchor(100.0, 2.0, peak=110.0),
        )
        self.assertIsNone(SetupStaticPolicy().decide_reanchor(100.0, 2.0, plan_stop=90.0))

    def test_registry_resolves_breakeven_trail(self):
        pol = resolve_exit_policy("breakeven_trail")
        self.assertIsInstance(pol, BreakevenTrailPolicy)
        self.assertEqual(pol.name, "breakeven_trail")
        self.assertIsNone(pol.geometry_name)
        self.assertTrue(pol.trails)
        self.assertTrue(pol.requires_amend_stop)
        self.assertFalse(pol.applies_geometry)
        self.assertEqual(pol.activation_r, 0.5)
        self.assertEqual(pol.trail_frac, 0.6)


if __name__ == "__main__":
    unittest.main()
