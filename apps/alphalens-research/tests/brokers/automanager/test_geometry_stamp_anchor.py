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

from alphalens_pipeline.brokers.automanager.control_loop import _geometry_shadow_stamp
from alphalens_pipeline.paper.sizing import build_exit_geometry_spec, parse_brief_to_spec
from broker_contract.exit_geometry.registry import resolve_policy

from tests.incident_1112_fixture import SMG_PLANNED_BLEND, smg_brief_trade_setup

_POLICY_NAME = "atr_bracket_1p5"


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
        # atr_bracket_levels leaf, but until now only the lens said so. The
        # value comes from the policy registry, never a literal.
        self.assertEqual(self._stamp()["tp_floor_frac"], resolve_policy(_POLICY_NAME).tp_floor_frac)

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
