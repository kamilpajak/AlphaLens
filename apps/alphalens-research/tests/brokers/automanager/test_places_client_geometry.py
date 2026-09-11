"""#1236: a present ``exit`` is no guarantee of levels to place.

``ExitGeometrySpec.initial_levels`` became optional so a document could declare
how its stop is managed without supplying a bracket to place. Every site that
asks "should I place the client's geometry?" therefore has to ask a second
question it never had to ask before — and there are several of them, each one
dereferencing ``initial_levels.stop`` / ``.tp`` on the money path:

  * ``_geometry_shadow_stamp`` — the telemetry blob on the ``planned`` line;
  * ``_geometry_tranche_ladder`` — the TP ladder actually journaled;
  * ``_journal_tranche_plan_core`` — including its own warning message;
  * ``_planned_exit_levels`` — the stop/TP written to the ``planned`` line;
  * the #1112 round-trip cost gate at drain.

Eleven guards would be eleven chances to forget one, and forgetting one is an
``AttributeError`` inside the placement drain. They route through a single
predicate instead. These tests hold that predicate to its two jobs and prove no
site raises on a levels-less spec.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.control_loop import (
    _geometry_shadow_stamp,
    _geometry_tranche_ladder,
    _places_client_geometry,
)
from broker_contract.exit_geometry.registry import resolve_exit_policy
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    ReanchorOnFill,
    TradeSpec,
    TrailingStop,
)

_PLACING_POLICY = "atr_bracket_1p5"  # applies_geometry=True
_INERT_POLICY = "setup_static"  # applies_geometry=False


def _spec() -> TradeSpec:
    return TradeSpec(
        entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=100.0),),
        disaster_stop=90.0,
        tp_tranches=(),
        suggested_size_pct=3.0,
    )


def _with_levels() -> ExitGeometrySpec:
    return ExitGeometrySpec(
        initial_levels=InitialLevels(stop=90.0, tp=110.0),
        reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0),),
    )


def _levels_less() -> ExitGeometrySpec:
    return ExitGeometrySpec(reaction_plan=(TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),))


class ThePredicateAsksBothQuestionsTest(unittest.TestCase):
    def test_a_placing_policy_with_levels_places_them(self):
        self.assertTrue(
            _places_client_geometry(resolve_exit_policy(_PLACING_POLICY), _with_levels())
        )

    def test_a_placing_policy_without_levels_places_nothing(self):
        """The new case. Before #1236 this combination could not exist, so every
        downstream site dereferenced the levels unconditionally."""
        self.assertFalse(
            _places_client_geometry(resolve_exit_policy(_PLACING_POLICY), _levels_less())
        )

    def test_an_inert_policy_places_nothing_even_with_levels(self):
        """The pre-existing half of the question, unchanged: under a policy that
        applies no geometry the brief's own ladder is placed and the levels are
        telemetry only."""
        self.assertFalse(
            _places_client_geometry(resolve_exit_policy(_INERT_POLICY), _with_levels())
        )

    def test_no_exit_spec_at_all_places_nothing(self):
        self.assertFalse(_places_client_geometry(resolve_exit_policy(_PLACING_POLICY), None))

    def test_a_policy_that_is_absent_places_nothing(self):
        """Directly composed deps (tests, a second broker) may carry no policy."""
        self.assertFalse(_places_client_geometry(None, _with_levels()))


class NoSiteRaisesOnALevelsLessSpecTest(unittest.TestCase):
    """The property that matters on the money path: reaching any of these with a
    levels-less spec must be a refusal, never an ``AttributeError``. Each case
    is paired with the same call on a spec that DOES carry levels, so a site that
    started refusing everything would show up here."""

    def test_the_shadow_stamp_records_absent_levels_rather_than_raising(self):
        policy = resolve_exit_policy(_PLACING_POLICY)
        stamp = _geometry_shadow_stamp(
            _levels_less(), _spec(), use_geometry=False, exit_policy=policy
        )
        assert stamp is not None
        self.assertIsNone(stamp["geometry_stop"])
        self.assertIsNone(stamp["geometry_tp"])

    def test_positive_control_the_shadow_stamp_still_records_present_levels(self):
        policy = resolve_exit_policy(_PLACING_POLICY)
        stamp = _geometry_shadow_stamp(
            _with_levels(), _spec(), use_geometry=True, exit_policy=policy
        )
        assert stamp is not None
        self.assertEqual((stamp["geometry_stop"], stamp["geometry_tp"]), (90.0, 110.0))

    def test_the_tranche_ladder_refuses_a_levels_less_spec(self):
        self.assertIsNone(_geometry_tranche_ladder(_levels_less()))

    def test_positive_control_the_tranche_ladder_builds_from_present_levels(self):
        built = _geometry_tranche_ladder(_with_levels())
        assert built is not None
        ladder, stop = built
        self.assertEqual((len(ladder), ladder[0].target_price, stop), (1, 110.0, 90.0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
