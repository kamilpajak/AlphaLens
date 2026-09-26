"""Unit tests for ``intent_replay/interpreter.py`` — the document DECLARED as
pending orders and reactions (spec sections 4.2, 4.3.1 and 5.4).

The interpreter reads a document and says what it DECLARES; it does not say
which of the declared levels rests at a broker. That is deliberate and it is
not tidiness: on both deployments a document supplying ``exit.initial_levels``
still rests ``spec.disaster_stop`` (the ``planned`` journal line, written on
the entry-trail path from ``record["disaster_stop"]``, is the one the
protection pass places and both stop arms read as the never-below floor and
the 1R denominator), which is the opposite of what spec section 5.1 says. The
walk chooses, once that is decided; this module hands over both levels.

One invariant ties the reading to the classification gate of section 4.3.1:
**every path the reader records lands in a field of the plan, and every field
of the plan comes from a recorded path.** So no read here is an echo in the
sense of that section, in any shape of document — which is what makes the
gate's proof by reading honest rather than a formality.

The refusals asserted here are the interpreter's own: ``entry_mode_unsupported``
for the ``immediate`` tranche v1 does not model (the door ADMITS it), and the
gate's ``path_unclassified``, which no admissible document can provoke today
and which is therefore exercised with the class tables patched.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import typing
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from broker_contract.trade_intent.schema import (
    ModelPush,
    ReactionPrimitive,
    ReanchorOnFill,
    TrailingStop,
)
from intent_replay import classification
from intent_replay.classification import PATH_UNCLASSIFIED_CODE, PathUnclassifiedError
from intent_replay.door import admit
from intent_replay.interpreter import (
    ENTRY_MODE_UNSUPPORTED_CODE,
    HONOURED_REACTION_KINDS,
    DeclaredTranche,
    EntryModeUnsupportedError,
    PendingEntry,
    Plan,
    interpret,
)

from tests.intent_replay.test_classification import expected_interpreted

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
EXAMPLES = WORKSPACE_ROOT / "apps" / "alphalens-broker-contract" / "examples" / "manual-pick"
STATED_TRADE_DATE = "2026-09-23"

# The published templates carry no `meta.trade_date`; the replay has no clock
# to fill it from, so every document here states it (PR 4, spec section 6.4).
TWO_TIERS = "pullback-two-tiers"
TRAILING = "pullback-trailing-stop"
IMMEDIATE = "immediate-plus-pullback"

# The budget of every published template, and the ladder that spends it.
TEMPLATE_NOTIONAL = 1500.0

TRAILING_REACTION = {"kind": "trailing_stop", "arm_trigger_r": 0.5, "trail_frac": 0.6}
REANCHOR_REACTION = {"kind": "reanchor_on_fill", "k_atr": 1.5, "atr": 1.2}
# A stop and a take-profit the document supplies ITSELF, beside the ladder it
# also declares. The arming door admits exactly this shape.
DECLARED_LEVELS = {"stop": 64.0, "tp": 72.0}

_ENTRY_PATHS = frozenset(
    {
        "spec.entry_tiers[].limit_price",
        "spec.entry_tiers[].alloc_pct",
        "spec.entry_tiers[].entry_mode",
    }
)
_SIZE_AND_FLOOR = frozenset({"spec.size.notional_acct", "spec.disaster_stop"})
_LADDER_PATHS = frozenset({"spec.tp_tranches[].price", "spec.tp_tranches[].tranche_pct"})
_LEVEL_PATHS = frozenset({"exit.initial_levels.stop", "exit.initial_levels.tp"})
_TRAIL_PATHS = frozenset(
    {
        "exit.reaction_plan[].kind",
        "exit.reaction_plan[].arm_trigger_r",
        "exit.reaction_plan[].trail_frac",
    }
)
_REANCHOR_PATHS = frozenset(
    {"exit.reaction_plan[].kind", "exit.reaction_plan[].k_atr", "exit.reaction_plan[].atr"}
)


def _document(name: str = TWO_TIERS, **changes: Any) -> dict[str, Any]:
    """A published template with its trade date stated, plus any change."""
    document = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    document["meta"] = {**document["meta"], "trade_date": STATED_TRADE_DATE}
    document.update(changes)
    return document


def _spec(document: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    return {**document["spec"], **changes}


def _planned(document: Mapping[str, Any]) -> Plan:
    """The plan of a document that passes the door — the only path a caller has."""
    admitted = admit(document)
    return interpret(admitted.intent, admitted.document)


def _refusal(exc: EntryModeUnsupportedError | PathUnclassifiedError) -> tuple[str, Any]:
    failure = exc.failure
    assert failure.retryable is False, "a refused document is never retryable"
    return failure.code, failure.details


class AcceptanceTest(unittest.TestCase):
    """The published pullback templates, and what the plan says about them."""

    def test_the_two_tier_template_becomes_two_pending_rungs(self) -> None:
        plan = _planned(_document())
        self.assertEqual(
            plan.entries,
            (
                PendingEntry(tier_index=0, limit_price=68.0, notional=900.0),
                PendingEntry(tier_index=1, limit_price=66.5, notional=600.0),
            ),
        )
        self.assertEqual(plan.notional, TEMPLATE_NOTIONAL)
        self.assertAlmostEqual(sum(entry.notional for entry in plan.entries), plan.notional)

    def test_the_template_declares_its_floor_and_its_ladder_and_no_levels(self) -> None:
        plan = _planned(_document())
        self.assertEqual(plan.declared_floor, 63.0)
        self.assertIsNone(plan.declared_stop)
        self.assertIsNone(plan.declared_take_profit)
        self.assertEqual(
            plan.declared_tranches,
            (DeclaredTranche(tranche_index=0, price=74.0, fraction=1.0),),
        )
        self.assertIsNone(plan.reaction)

    def test_the_trailing_template_carries_its_declared_primitive(self) -> None:
        document = _document(TRAILING)
        admitted = admit(document)
        plan = interpret(admitted.intent, admitted.document)
        self.assertIsNotNone(admitted.intent.exit)
        assert admitted.intent.exit is not None
        self.assertIs(plan.reaction, admitted.intent.exit.reaction_plan[0])
        self.assertIsInstance(plan.reaction, TrailingStop)

    def test_the_directory_holds_exactly_the_templates_these_tests_name(self) -> None:
        # The same control test_door.py keeps: a fourth published template is a
        # document nobody here interprets, and the epic must see it.
        self.assertEqual(
            {path.stem for path in EXAMPLES.glob("*.json")}, {TWO_TIERS, TRAILING, IMMEDIATE}
        )


class BudgetTest(unittest.TestCase):
    """`spec.size.notional_acct` is spent as stated, in the ACCOUNT currency.

    No share quantity and no FX: the published examples price a USD instrument
    with a EUR budget, and the rate is the decision of issue #1592. The plan
    hands PR 7 the budget per rung and its limit price, which is enough for
    every number spec section 5 asks for once that decision lands.
    """

    def test_each_rung_takes_its_stated_share_of_the_budget(self) -> None:
        document = _document(
            spec=_spec(
                _document(),
                entry_tiers=[
                    {"limit_price": 68.0, "alloc_pct": 25.0},
                    {"limit_price": 66.0, "alloc_pct": 75.0},
                ],
            )
        )
        plan = _planned(document)
        self.assertEqual([entry.notional for entry in plan.entries], [375.0, 1125.0])

    def test_an_integer_budget_is_read_as_a_number(self) -> None:
        # `json.loads` gives an int for `1500`, the codec keeps it, and the door
        # admits it; the reader must not refuse what the door let through.
        document = _document(
            spec=_spec(_document(), size={"notional_acct": 1500, "currency": "EUR"})
        )
        plan = _planned(document)
        self.assertEqual(plan.notional, TEMPLATE_NOTIONAL)
        self.assertIsInstance(plan.notional, float)

    def test_a_boolean_is_not_a_number(self) -> None:
        # `True` is an `int` in Python, so an unguarded reader would size the
        # whole ladder off a budget of 1.0. The door refuses it first — checked:
        # `$.spec.size.notional_acct: True is not of type 'number'` — so the
        # intent is built by hand here, the way the contract's quantity leaf
        # guards the same confusion at its own boundary.
        admitted = admit(_document())
        size = dataclasses.replace(admitted.intent.spec.size, notional_acct=True)
        spec = dataclasses.replace(admitted.intent.spec, size=size)
        intent = dataclasses.replace(admitted.intent, spec=spec)
        with self.assertRaises(TypeError) as caught:
            interpret(intent, admitted.document)
        self.assertIn("spec.size.notional_acct", str(caught.exception))

    def test_a_rung_whose_share_buys_nothing_is_still_a_rung(self) -> None:
        # Deliberate: whole-share flooring is a venue fact the replay is not
        # given (issue #1592). A rung the daemon would not place therefore
        # exists here, and that difference is named in the PR rather than
        # silently rounded away.
        document = _document(
            spec=_spec(
                _document(),
                entry_tiers=[
                    {"limit_price": 68.0, "alloc_pct": 99.9},
                    {"limit_price": 66.0, "alloc_pct": 0.1},
                ],
            )
        )
        plan = _planned(document)
        self.assertAlmostEqual(plan.entries[1].notional, 1.5)


class DeclaredLevelsTest(unittest.TestCase):
    """A document may declare a floor AND its own levels; the plan carries both.

    Spec section 5.1 says the levels are what rests. Run against the deployed
    daemon that is false — the `planned` line carries `spec.disaster_stop` on
    the entry-trail path, and a geometry document cannot reach the classic
    bracket path at all — so this module decides nothing and reports both.
    """

    def _geometry_document(self, **exit_changes: Any) -> dict[str, Any]:
        exit_spec: dict[str, Any] = {"initial_levels": dict(DECLARED_LEVELS)}
        exit_spec.update(exit_changes)
        return _document(exit=exit_spec)

    def test_a_document_supplying_levels_keeps_its_floor_and_its_ladder(self) -> None:
        plan = _planned(self._geometry_document(reaction_plan=[dict(TRAILING_REACTION)]))
        self.assertEqual(plan.declared_floor, 63.0)
        self.assertEqual(plan.declared_stop, 64.0)
        self.assertEqual(plan.declared_take_profit, 72.0)
        self.assertEqual(
            plan.declared_tranches,
            (DeclaredTranche(tranche_index=0, price=74.0, fraction=1.0),),
        )

    def test_an_exit_with_no_levels_declares_none(self) -> None:
        plan = _planned(_document(exit={"initial_levels": None, "reaction_plan": []}))
        self.assertIsNone(plan.declared_stop)
        self.assertIsNone(plan.declared_take_profit)
        self.assertIsNone(plan.reaction)

    def test_an_empty_exit_object_declares_nothing(self) -> None:
        plan = _planned(_document(exit={}))
        self.assertIsNone(plan.declared_stop)
        self.assertIsNone(plan.reaction)
        self.assertEqual(plan.declared_floor, 63.0)

    def test_an_empty_take_profit_ladder_stays_empty(self) -> None:
        plan = _planned(_document(spec=_spec(_document(), tp_tranches=[])))
        self.assertEqual(plan.declared_tranches, ())

    def test_a_ladder_summing_just_over_100_is_not_refused_here(self) -> None:
        # `validate_intent` tolerates 1e-6 of float noise on the sum, so a
        # fraction can exceed 1.0 by that much. A bound of our own would refuse
        # a document the door admits, and would do it with a traceback rather
        # than a published refusal.
        document = _document(
            spec=_spec(_document(), tp_tranches=[{"price": 74.0, "tranche_pct": 100.0000005}])
        )
        plan = _planned(document)
        self.assertGreater(plan.declared_tranches[0].fraction, 1.0)


class EntryModeTest(unittest.TestCase):
    """`immediate` is refused here, and the door is where it is ADMITTED."""

    def test_the_published_immediate_template_is_refused_naming_its_tier(self) -> None:
        with self.assertRaises(EntryModeUnsupportedError) as caught:
            _planned(_document(IMMEDIATE))
        code, details = _refusal(caught.exception)
        self.assertEqual(code, ENTRY_MODE_UNSUPPORTED_CODE)
        self.assertEqual(details["tiers"], [0])

    def test_a_single_immediate_rung_reaches_the_code_this_module_owns(self) -> None:
        # The reading pass reads every tier, including an `immediate` one, so
        # the classification gate has nothing left unread and the refusal the
        # caller gets is about the entry mode rather than about a path.
        document = _document(
            spec=_spec(
                _document(),
                entry_tiers=[{"limit_price": 70.0, "alloc_pct": 100.0, "entry_mode": "immediate"}],
            )
        )
        with self.assertRaises(EntryModeUnsupportedError) as caught:
            _planned(document)
        self.assertEqual(_refusal(caught.exception)[1]["tiers"], [0])

    def test_the_refusal_carries_no_reason(self) -> None:
        # One code, one failure mode: spec section 5.4 gives this code
        # `details.tiers` and no reason vocabulary.
        with self.assertRaises(EntryModeUnsupportedError) as caught:
            _planned(_document(IMMEDIATE))
        self.assertNotIn("reason", caught.exception.failure.details)


class ReactionTest(unittest.TestCase):
    """The declared reaction reaches the walk as the DECODED primitive."""

    def test_a_reanchor_primitive_passes_through_unchanged(self) -> None:
        admitted = admit(_document(exit={"reaction_plan": [dict(REANCHOR_REACTION)]}))
        plan = interpret(admitted.intent, admitted.document)
        assert admitted.intent.exit is not None
        self.assertIs(plan.reaction, admitted.intent.exit.reaction_plan[0])
        self.assertIsInstance(plan.reaction, ReanchorOnFill)

    def test_a_kind_this_module_does_not_honour_is_a_programming_error(self) -> None:
        # `validate_intent` already refuses `ModelPush` (`reaction_kind_unsupported`),
        # so this is unreachable through the door — and it must stay loud,
        # because `resolve_declared_policy` DEGRADES such a primitive to the
        # inert policy, which would report "the stop never moved" as a result.
        admitted = admit(_document(exit={"reaction_plan": [dict(TRAILING_REACTION)]}))
        assert admitted.intent.exit is not None
        pushed = dataclasses.replace(admitted.intent.exit, reaction_plan=(ModelPush(),))
        intent = dataclasses.replace(admitted.intent, exit=pushed)
        with self.assertRaises(ValueError) as caught:
            interpret(intent, admitted.document)
        self.assertIn("model", str(caught.exception))

    def test_the_honoured_kinds_are_the_contract_union_minus_the_reserved_tag(self) -> None:
        # The ONLY barrier against a fourth primitive class arriving in the
        # contract and being silently degraded to the inert policy: nothing
        # else in this package or the suite would notice.
        union = {cls.__name__ for cls in typing.get_args(ReactionPrimitive)}
        self.assertEqual(union, {"ReanchorOnFill", "TrailingStop", "ModelPush"})
        self.assertEqual(HONOURED_REACTION_KINDS, frozenset({"reanchor_on_fill", "trailing_stop"}))


class ReadSetTest(unittest.TestCase):
    """The gate's `read` set, in both directions against spec section 4.3.1."""

    def _read(self, document: Mapping[str, Any]) -> frozenset[str]:
        return _planned(document).read

    def test_a_template_reads_its_ladder_its_budget_and_its_floor(self) -> None:
        self.assertEqual(self._read(_document()), _ENTRY_PATHS | _SIZE_AND_FLOOR | _LADDER_PATHS)

    def test_a_trailing_template_also_reads_the_trail_arm(self) -> None:
        self.assertEqual(
            self._read(_document(TRAILING)),
            _ENTRY_PATHS | _SIZE_AND_FLOOR | _LADDER_PATHS | _TRAIL_PATHS,
        )

    def test_a_document_supplying_levels_reads_both_candidates(self) -> None:
        document = _document(
            exit={"initial_levels": dict(DECLARED_LEVELS), "reaction_plan": [REANCHOR_REACTION]}
        )
        self.assertEqual(
            self._read(document),
            _ENTRY_PATHS | _SIZE_AND_FLOOR | _LADDER_PATHS | _LEVEL_PATHS | _REANCHOR_PATHS,
        )

    def test_no_document_reads_a_path_the_spec_does_not_call_interpreted(self) -> None:
        for name in (TWO_TIERS, TRAILING):
            with self.subTest(name):
                self.assertLessEqual(self._read(_document(name)), expected_interpreted())

    def test_the_documents_together_read_every_interpreted_path(self) -> None:
        # The control the epic could not run before this module existed: a path
        # spec section 4.3.1 calls `interpreted` that nothing reads is red here.
        documents = (
            _document(),
            _document(TRAILING),
            _document(
                exit={
                    "initial_levels": dict(DECLARED_LEVELS),
                    "reaction_plan": [dict(REANCHOR_REACTION)],
                }
            ),
        )
        union: frozenset[str] = frozenset()
        for document in documents:
            union |= self._read(document)
        self.assertEqual(union, expected_interpreted())


class GateTest(unittest.TestCase):
    """The fifth gate runs inside `interpret`, and it runs before the refusal.

    No admissible document can provoke it: once those 14 paths are read, every
    path of the published input schema is classified, the one path that is not
    (`spec.tp_tranches[].r_multiple`) is refused by door gate 0, and any other
    key an author adds is refused by the fixed point. It is a tripwire for the
    day the contract grows a field, so it is exercised with a class row removed
    — the shape `test_classification.py` already uses.
    """

    def _without_the_ticker_row(self) -> Any:
        thinner = {
            path: why
            for path, why in classification.OUT_OF_SCOPE.items()
            if path != "instrument.ticker"
        }
        return mock.patch.object(classification, "OUT_OF_SCOPE", thinner)

    def test_an_unclassified_path_is_refused_and_named(self) -> None:
        with self._without_the_ticker_row(), self.assertRaises(PathUnclassifiedError) as caught:
            _planned(_document())
        code, details = _refusal(caught.exception)
        self.assertEqual(code, PATH_UNCLASSIFIED_CODE)
        self.assertEqual(details["paths"], ["instrument.ticker"])

    def test_the_gate_precedes_the_entry_mode_refusal(self) -> None:
        # Gate order is the contract, not a detail: section 4.3.1 calls this the
        # FIFTH gate of the door's chain, and an interpretation refusal comes
        # after the document is understood.
        with self._without_the_ticker_row(), self.assertRaises(PathUnclassifiedError):
            _planned(_document(IMMEDIATE))


class PurityTest(unittest.TestCase):
    def test_interpret_does_not_mutate_what_it_is_given(self) -> None:
        admitted = admit(_document(TRAILING))
        document = copy.deepcopy(dict(admitted.document))
        intent = copy.deepcopy(admitted.intent)
        interpret(admitted.intent, admitted.document)
        self.assertEqual(admitted.document, document)
        self.assertEqual(admitted.intent, intent)


if __name__ == "__main__":
    unittest.main()
