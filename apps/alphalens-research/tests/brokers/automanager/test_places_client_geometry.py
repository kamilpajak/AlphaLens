"""#1414: the DOCUMENT decides what is placed, and nothing else does.

``ExitGeometrySpec.initial_levels`` became optional in #1236 so a document could
declare how its stop is managed without supplying a bracket to place. #1414 made
its presence the WHOLE placement instruction: supply levels and they are placed,
omit them and the brief's own ladder is. Until then a process-wide environment
variable answered half the question, and the deployed value answered "never
place the client's levels" — so a producer could say how its stop was managed
but not what reached the broker.

Every site that asks "should I place the client's geometry?" dereferences
``initial_levels.stop`` / ``.tp`` right after, on the money path, and a forgotten
guard is an ``AttributeError`` inside the unattended placement drain. They route
through one predicate instead. These tests hold that predicate to its job, prove
no site raises on a levels-less spec, and pin the property the issue exists for:
no environment can change the answer.
"""

from __future__ import annotations

import contextlib
import unittest
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.control_loop import (
    _geometry_tranche_ladder,
    _placed_geometry_stamp,
    _places_client_geometry,
)
from broker_contract.sizing import TpTranchePlan
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    ReanchorOnFill,
    TradeSpec,
    TrailingStop,
)


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


class ThePredicateAsksTheDocumentTest(unittest.TestCase):
    def test_a_document_that_supplies_levels_places_them(self):
        self.assertTrue(_places_client_geometry(_with_levels()))

    def test_a_document_that_supplies_none_places_the_brief_ladder(self):
        self.assertFalse(_places_client_geometry(_levels_less()))

    def test_no_exit_spec_at_all_places_nothing(self):
        self.assertFalse(_places_client_geometry(None))

    def test_the_predicate_takes_no_policy_at_all(self):
        """The #1414 regression, stated where it cannot be argued with.

        While this function took a policy, a deployment could veto a document's
        levels — and the deployed one did, for every pick, which is why the
        brief path computed a bracket no broker ever saw. A signature that
        cannot accept a policy cannot grow that veto back by accident.
        """
        import inspect

        self.assertEqual(list(inspect.signature(_places_client_geometry).parameters), ["exit_spec"])


class TheLadderFollowsTheDocumentTest(unittest.TestCase):
    """The choice that actually reaches the broker, made where it is made:
    ``_journal_tranche_plan_core`` is the one place both placement paths share."""

    def _journal(self, exit_spec: Any) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        plan = type(
            "_Plan",
            (),
            {
                "entry_tiers": (),
                "tp_tranches": (
                    TpTranchePlan(
                        tranche_index=0,
                        target_price=120.0,
                        tranche_frac=1.0,
                        r_multiple=2.0,
                        tag="tp1",
                    ),
                ),
            },
        )()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(cl, "_append_standalone_stop_journal", lines.append)
            )
            cl._journal_tranche_plan_core(
                plan=plan,
                exit_spec=exit_spec,
                stop_price=90.0,
                reference_qty=10.0,
                uic=1,
            )
        return lines

    def test_a_document_with_levels_journals_its_own_single_tranche(self):
        line = self._journal(_with_levels())[0]
        self.assertEqual([t["target_price"] for t in line["tp_tranches"]], [110.0])
        self.assertAlmostEqual(line["stop_price"], 90.0)

    def test_a_document_without_levels_journals_the_brief_ladder(self):
        line = self._journal(_levels_less())[0]
        self.assertEqual([t["target_price"] for t in line["tp_tranches"]], [120.0])

    def test_no_exit_spec_journals_the_brief_ladder(self):
        line = self._journal(None)[0]
        self.assertEqual([t["target_price"] for t in line["tp_tranches"]], [120.0])


class NoSiteRaisesOnALevelsLessSpecTest(unittest.TestCase):
    """Reaching any of these with a levels-less spec must be a refusal, never an
    ``AttributeError``. Each case is paired with the same call on a spec that
    DOES carry levels, so a site that started refusing everything shows up."""

    def test_the_stamp_records_absent_levels_rather_than_raising(self):
        stamp = _placed_geometry_stamp(_levels_less())
        assert stamp is not None
        self.assertEqual(
            (stamp["geometry_stop"], stamp["geometry_tp"], stamp["applied"]),
            (None, None, False),
        )

    def test_positive_control_the_stamp_still_records_present_levels(self):
        stamp = _placed_geometry_stamp(_with_levels())
        assert stamp is not None
        self.assertEqual(
            (stamp["geometry_stop"], stamp["geometry_tp"], stamp["applied"]),
            (90.0, 110.0, True),
        )

    def test_no_exit_spec_stamps_nothing(self):
        self.assertIsNone(_placed_geometry_stamp(None))

    def test_the_tranche_ladder_refuses_a_levels_less_spec(self):
        self.assertIsNone(_geometry_tranche_ladder(_levels_less()))

    def test_positive_control_the_tranche_ladder_builds_from_present_levels(self):
        built = _geometry_tranche_ladder(_with_levels())
        assert built is not None
        ladder, stop = built
        self.assertEqual((len(ladder), ladder[0].target_price, stop), (1, 110.0, 90.0))


class TheStampIsTheRecordOfWhatWasPlacedTest(unittest.TestCase):
    """#1414 narrowed the stamp to the three fields something reads.

    The other ten were the shadow of the 2026-08-24 exit-policy comparison,
    voided on 2026-08-27 before its cohort opened. ``applied`` and
    ``geometry_tp`` stay because ``_stamped_exit_target`` reads them to choose
    which family of #1112 arm gates prices a tier."""

    _RETIRED = (
        "planned_blend",
        "k_atr",
        "atr",
        "ceiling_price",
        "anchor_mode",
        "tp_floor_frac",
        "policy_name",
        "policy_version",
        "exit_policy_name",
    )

    def test_the_stamp_carries_exactly_the_three_read_fields(self):
        stamp = _placed_geometry_stamp(_with_levels())
        assert stamp is not None
        self.assertEqual(set(stamp), {"geometry_stop", "geometry_tp", "applied"})

    def test_none_of_the_retired_fields_come_back(self):
        stamp = _placed_geometry_stamp(_with_levels())
        assert stamp is not None
        for key in self._RETIRED:
            with self.subTest(field=key):
                self.assertNotIn(key, stamp)

    def test_the_arm_gate_reads_the_stamp_back_off_a_journal_line(self):
        """The reason the stamp exists at all: the later hop has no intent."""
        stamp = _placed_geometry_stamp(_with_levels())
        self.assertAlmostEqual(cl._stamped_exit_target({"geometry": stamp}), 110.0)

    def test_and_returns_none_for_a_line_whose_document_placed_nothing(self):
        stamp = _placed_geometry_stamp(_levels_less())
        self.assertIsNone(cl._stamped_exit_target({"geometry": stamp}))


class PlacingADocumentsOwnLevelsIsAnnouncedTest(unittest.TestCase):
    """#1414 removed the fleet-wide veto over client geometry, so the first pick
    to place its own levels is announced rather than discovered afterwards. No
    pick has taken that path since 2026-08-19."""

    def _alerts(self, exit_spec: Any) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []

        def _throttled(message: str, reason: str) -> bool:
            seen.append((message, reason))
            return True

        cl._announce_client_geometry(exit_spec, "KO", _throttled)
        return seen

    def test_a_document_with_levels_pages_once_naming_them(self):
        alerts = self._alerts(_with_levels())
        self.assertEqual(len(alerts), 1)
        message, reason = alerts[0]
        self.assertIn("90.0", message)
        self.assertIn("110.0", message)
        self.assertEqual(reason, "client-geometry-placed:KO")

    def test_a_declaration_only_document_is_silent(self):
        self.assertEqual(self._alerts(_levels_less()), [])

    def test_no_exit_spec_is_silent(self):
        self.assertEqual(self._alerts(None), [])

    def test_a_missing_alert_sink_is_tolerated(self):
        """Direct calls and second brokers carry none; this must never raise on
        the money path."""
        cl._announce_client_geometry(_with_levels(), "KO", None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
