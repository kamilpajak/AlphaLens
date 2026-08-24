"""The live geometry stamp must NAME the anchor and the floor it used (#1114).

The stamp already carried the planned blend as a VALUE under a field called
``policy_name: "atr_bracket_1p5"`` -- the same string the ``/edge`` what-if lens
was registered under. A reader therefore had no way to tell that the live number
came from the planned blend while the lens carrying that name used the realised
blend. That is the mechanism by which the divergence stayed silent, so the stamp
gains the two facts that were missing: which anchor produced the levels, and the
take-profit floor fraction that was applied.

Additive telemetry only. Nothing about placement moves here.
"""

from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager.control_loop import _geometry_shadow_stamp
from alphalens_pipeline.paper.sizing import build_exit_geometry_spec, parse_brief_to_spec
from broker_contract.exit_geometry.registry import resolve_policy

from tests.incident_1112_fixture import SMG_PLANNED_BLEND, smg_brief_trade_setup

_POLICY_NAME = "atr_bracket_1p5"
# bezpazery v1 pre-registered cost floor (docs/research/bezpazery_lens_design_2026_07_16.md
# section 2). A literal on purpose -- see test_the_stamp_names_the_take_profit_floor_fraction.
_EXPECTED_TP_FLOOR_FRAC = 0.006


class TestGeometryStampNamesItsAnchor(unittest.TestCase):
    def setUp(self) -> None:
        setup = smg_brief_trade_setup()
        self.exit_spec = build_exit_geometry_spec(setup)
        assert self.exit_spec is not None
        self.spec = parse_brief_to_spec(setup)

    def _stamp(self) -> dict:
        stamp = _geometry_shadow_stamp(self.exit_spec, self.spec, use_geometry=True)
        assert stamp is not None
        return stamp

    def test_the_stamp_names_the_anchor_mode_it_used(self):
        # The live builder anchors on the PLANNED blend over all intended tiers.
        # Saying so in the journal is what lets a reader match a live line to
        # the right /edge lens instead of to the one that shares its name.
        self.assertEqual(self._stamp()["anchor_mode"], "planned")

    def test_the_stamp_names_the_take_profit_floor_fraction(self):
        # The 0.6% floor is applied on BOTH sides through the shared
        # atr_bracket_levels leaf, but until now only the lens said so.
        #
        # Pinned as a LITERAL, not as `resolve_policy(...).tp_floor_frac`:
        # that expression is character-for-character what the production line
        # computes, so it could only fail if the key went missing. The number
        # is the pre-registered bezpazery v1 floor (memo section 2) and a
        # change to it is a change to a registered policy, which must break a
        # test rather than pass silently.
        self.assertEqual(self._stamp()["tp_floor_frac"], _EXPECTED_TP_FLOOR_FRAC)

    def test_the_registry_still_carries_the_preregistered_floor(self):
        # Second half of the same pin: the stamp copies the registry, so the
        # registry itself must still hold the pre-registered value.
        self.assertEqual(resolve_policy(_POLICY_NAME).tp_floor_frac, _EXPECTED_TP_FLOOR_FRAC)

    def test_the_stamp_resolves_no_policy_on_the_hot_path(self):
        # _geometry_shadow_stamp runs on every watch_open inside the unattended
        # drain and its own docstring says nothing in it may raise. resolve_policy
        # raises ValueError on an unknown name, so the lookup belongs at import
        # time, not per tick. Making the registry lookup explode proves the hot
        # path no longer performs one.
        with mock.patch(
            "broker_contract.exit_geometry.registry.resolve_policy",
            side_effect=AssertionError("resolved a policy inside the drain"),
        ):
            self.assertEqual(self._stamp()["tp_floor_frac"], _EXPECTED_TP_FLOOR_FRAC)

    def test_the_two_new_keys_are_additive_and_the_old_ones_are_unchanged(self):
        # Every existing reader uses .get(), so adding keys is safe -- but the
        # OLD keys must keep their old values, or this stops being telemetry.
        stamp = self._stamp()
        self.assertEqual(stamp["policy_name"], _POLICY_NAME)
        self.assertEqual(stamp["policy_version"], 1)
        self.assertAlmostEqual(stamp["planned_blend"], SMG_PLANNED_BLEND, places=9)
        self.assertTrue(stamp["applied"])
        self.assertEqual(
            set(stamp),
            {
                "policy_name",
                "policy_version",
                "planned_blend",
                "geometry_stop",
                "geometry_tp",
                "k_atr",
                "atr",
                "ceiling_price",
                "applied",
                "anchor_mode",
                "tp_floor_frac",
            },
        )
