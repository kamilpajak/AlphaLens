"""The replay door (spec sections 4.3 and 6.4): template completion, then the
gates the arming door runs, in its order, with its reasons.

The three published templates are the acceptance control. They carry no
``meta.trade_date``, and the replay has neither the clock nor the calendar the
arming door fills it from, so every accepted example here is the template plus
one stated date (decided 2026-09-25 with PR 4). ``immediate-plus-pullback`` is
ACCEPTED by the door: the door admits ``entry_mode: "immediate"``, and the
refusal ``entry_mode_unsupported`` belongs to the interpreter (PR 5), which
flips that assertion.

Every refusal is asserted by its reason AND its details, and one test walks a
document that is wrong in five ways through the gates one refusal at a time,
because the order is the contract, not a detail.
"""

from __future__ import annotations

import copy
import functools
import json
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from broker_contract.trade_intent.validate import IntentInvalidError
from intent_replay.door import (
    DOOR_REASONS,
    SENTINEL_ARMED_TS,
    SENTINEL_INTENT_ID,
    Admitted,
    DoorRefusalError,
    admit,
    complete,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
EXAMPLES = WORKSPACE_ROOT / "apps" / "alphalens-broker-contract" / "examples" / "manual-pick"
TEMPLATE_NAMES = ("pullback-two-tiers", "pullback-trailing-stop", "immediate-plus-pullback")
STATED_TRADE_DATE = "2026-09-23"

# The six reasons the replay door raises; `not_json` and `duplicate_key` are the
# parser's and belong to the CLI's vocabulary (spec section 5.4).
EXPECTED_REASONS = frozenset(
    {
        "derived_field_supplied",
        "schema_violation",
        "trade_date_required",
        "trade_date_malformed",
        "undecodable",
        "key_discarded",
    }
)


def _template(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def _example(name: str = "pullback-two-tiers") -> dict[str, Any]:
    document = _template(name)
    document["meta"] = {**document["meta"], "trade_date": STATED_TRADE_DATE}
    return document


def _refusal(test: unittest.TestCase, action: Callable[[], Any]) -> tuple[str, Mapping[str, Any]]:
    with test.assertRaises(DoorRefusalError) as caught:
        action()
    exc = caught.exception
    test.assertIn(exc.reason, DOOR_REASONS, "every raised reason is in the published vocabulary")
    test.assertFalse(hasattr(exc, "failure"), "the door never names a code; the CLI does")
    return exc.reason, exc.details


def _invalid(test: unittest.TestCase, action: Callable[[], Any]) -> str:
    with test.assertRaises(IntentInvalidError) as caught:
        action()
    failure = caught.exception.failure
    test.assertEqual(failure.code, "intent_invalid")
    return str(failure.details["reason"])


class AcceptanceTest(unittest.TestCase):
    def test_the_directory_holds_exactly_the_published_templates(self) -> None:
        """Spec section 6.3 makes the published examples the acceptance control,
        so the control must be the DIRECTORY and not a list that can fall behind
        it. A fourth template is a red build a human resolves loop by loop, not a
        file the replay silently never sees. The arming door keeps the same
        guard one directory over (`test_manual_pick_templates.py`)."""
        self.assertEqual({path.stem for path in EXAMPLES.glob("*.json")}, set(TEMPLATE_NAMES))

    def test_every_published_template_with_a_stated_date_is_admitted(self) -> None:
        for name in TEMPLATE_NAMES:
            with self.subTest(template=name):
                admitted = admit(_example(name))
                self.assertIsInstance(admitted, Admitted)
                self.assertEqual(admitted.intent.intent_id, SENTINEL_INTENT_ID)
                self.assertEqual(admitted.intent.meta.armed_ts, SENTINEL_ARMED_TS)
                self.assertEqual(admitted.intent.meta.trade_date, STATED_TRADE_DATE)

    def test_the_immediate_template_passes_the_door(self) -> None:
        """The door admits `entry_mode: "immediate"` as the arming door does;
        `entry_mode_unsupported` is the interpreter's refusal (spec 6.4), and
        PR 5 turns this assertion around."""
        admitted = admit(_example("immediate-plus-pullback"))
        self.assertEqual(admitted.intent.spec.entry_tiers[0].entry_mode, "immediate")

    def test_the_admitted_document_is_the_authors_plus_exactly_two_sentinels(self) -> None:
        author = _example()
        expected = copy.deepcopy(author)
        expected["intent_id"] = SENTINEL_INTENT_ID
        expected["meta"]["armed_ts"] = SENTINEL_ARMED_TS
        self.assertEqual(admit(author).document, expected)

    def test_a_stated_version_is_not_read(self) -> None:
        """Spec 4.3.1 classes both version paths out of scope: the codec and
        `validate_intent` stay version-blind, and so does this door. A v4
        document carrying something v3 cannot model is refused by the fixed
        point instead."""
        document = _example()
        document["meta"]["schema_version"] = "4"
        self.assertEqual(admit(document).intent.meta.schema_version, "4")

    def test_complete_does_not_mutate_its_input(self) -> None:
        author = _example()
        before = copy.deepcopy(author)
        complete(author)
        self.assertEqual(author, before)

    def test_complete_adds_exactly_the_two_sentinels_to_a_dated_document(self) -> None:
        author = _example()
        completed = complete(author)
        self.assertEqual(completed["intent_id"], SENTINEL_INTENT_ID)
        self.assertEqual(completed["meta"]["armed_ts"], SENTINEL_ARMED_TS)
        self.assertEqual(completed["meta"]["trade_date"], STATED_TRADE_DATE)
        stripped = copy.deepcopy(completed)
        del stripped["intent_id"]
        del stripped["meta"]["armed_ts"]
        self.assertEqual(stripped, author)


class CompletionTest(unittest.TestCase):
    def test_a_template_as_published_is_refused_for_its_missing_date(self) -> None:
        for name in TEMPLATE_NAMES:
            with self.subTest(template=name):
                reason, details = _refusal(self, functools.partial(admit, _template(name)))
                self.assertEqual(reason, "trade_date_required")
                self.assertEqual(details, {})

    def test_the_missing_date_message_names_the_edit(self) -> None:
        with self.assertRaises(DoorRefusalError) as caught:
            admit(_template("pullback-two-tiers"))
        self.assertIn('"meta"', caught.exception.message)
        self.assertIn('"trade_date"', caught.exception.message)

    def test_a_date_that_does_not_parse_is_refused(self) -> None:
        document = _example()
        document["meta"]["trade_date"] = "yesterday"
        reason, details = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "trade_date_malformed")
        self.assertEqual(details, {"value": "yesterday"})

    def test_a_date_that_parses_but_is_not_canonical_is_refused_as_the_door_refuses_it(
        self,
    ) -> None:
        """`date.fromisoformat` accepts the compact and the ISO-week spellings.
        The arming door writes the parsed date back in canonical form, so the
        author's spelling fails the fixed point there; this door does the same."""
        for spelling in ("20260923", "2026-W39-3"):
            with self.subTest(spelling=spelling):
                document = _example()
                document["meta"]["trade_date"] = spelling
                reason, details = _refusal(self, functools.partial(admit, document))
                self.assertEqual(reason, "key_discarded")
                self.assertEqual(details, {"paths": ["meta.trade_date"]})


class WireChecksTest(unittest.TestCase):
    def test_a_supplied_derived_field_is_refused_and_named(self) -> None:
        document = _example()
        document["intent_id"] = "KO:2026-09-23:manual"
        document["meta"]["armed_ts"] = "2026-09-23T14:00:00+00:00"
        document["spec"]["tp_tranches"][0]["r_multiple"] = 2.0
        reason, details = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "derived_field_supplied")
        self.assertEqual(
            details, {"paths": ["intent_id", "meta.armed_ts", "spec.tp_tranches[0].r_multiple"]}
        )

    def test_a_missing_required_object_is_a_schema_violation_at_its_path(self) -> None:
        document = _example()
        del document["spec"]["size"]
        reason, details = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "schema_violation")
        self.assertEqual(details["path"], "$.spec")

    def test_a_renamed_required_key_is_a_schema_violation_not_a_discarded_key(self) -> None:
        document = _example()
        tier = document["spec"]["entry_tiers"][0]
        tier["limit_pirce"] = tier.pop("limit_price")
        reason, details = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "schema_violation")
        self.assertEqual(details["path"], "$.spec.entry_tiers[0]")

    def test_a_boolean_generation_is_a_schema_violation(self) -> None:
        document = _example()
        document["meta"]["generation"] = True
        reason, details = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "schema_violation")
        self.assertEqual(details["path"], "$.meta.generation")

    def test_a_document_that_is_not_an_object_is_a_schema_violation_at_the_root(self) -> None:
        for document in ([], "abc", None, 1.5):
            with self.subTest(document=document):
                reason, details = _refusal(self, functools.partial(admit, document))
                self.assertEqual(reason, "schema_violation")
                self.assertEqual(details["path"], "$")

    def test_an_integral_float_generation_passes_the_schema_and_is_undecodable(self) -> None:
        document = _example()
        document["meta"]["generation"] = 1.0
        reason, details = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "undecodable")
        self.assertEqual(details, {})

    def test_an_extra_key_the_decoder_would_drop_is_refused_and_named(self) -> None:
        """The added-key form (`test_arm_cli.py` uses it too): `limit_price`
        stays, so the schema passes, and only the fixed point can see that the
        author's `limit_pirce` did not come back."""
        document = _example()
        document["spec"]["entry_tiers"][0]["limit_pirce"] = 999.0
        with self.assertLogs("broker_contract.trade_intent.codec", level="WARNING"):
            reason, details = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "key_discarded")
        self.assertEqual(details, {"paths": ["spec.entry_tiers[0].limit_pirce"]})

    def test_a_nan_price_reaches_validate_intent_and_is_refused_there(self) -> None:
        """NaN never equals itself, so a naive fixed point would report the
        price as discarded and name the wrong rule."""
        document = _example()
        document["spec"]["disaster_stop"] = float("nan")
        self.assertEqual(_invalid(self, lambda: admit(document)), "numeric_not_finite")

    def test_an_incoherent_document_is_refused_by_validate_intent_with_its_own_code(self) -> None:
        document = _example()
        document["spec"]["disaster_stop"] = 1000.0
        self.assertEqual(_invalid(self, lambda: admit(document)), "stop_above_entry")


class GateOrderTest(unittest.TestCase):
    def test_the_refusals_come_in_gate_order(self) -> None:
        """One document wrong in five ways; each fix reveals the next gate."""
        document = _template("pullback-two-tiers")
        document["intent_id"] = "supplied"
        del document["spec"]["size"]
        document["spec"]["entry_tiers"][0]["limit_pirce"] = 1.0
        document["spec"]["disaster_stop"] = 1000.0

        reason, _ = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "derived_field_supplied")

        del document["intent_id"]
        reason, _ = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "schema_violation")

        document["spec"]["size"] = {"notional_acct": 1500.0, "currency": "EUR"}
        reason, _ = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "trade_date_required")

        document["meta"]["trade_date"] = STATED_TRADE_DATE
        reason, _ = _refusal(self, lambda: admit(document))
        self.assertEqual(reason, "key_discarded")

        del document["spec"]["entry_tiers"][0]["limit_pirce"]
        self.assertEqual(_invalid(self, lambda: admit(document)), "stop_above_entry")


class VocabularyTest(unittest.TestCase):
    def test_the_door_reasons_are_the_six_of_the_spec(self) -> None:
        self.assertEqual(frozenset(DOOR_REASONS), EXPECTED_REASONS)

    def test_a_refusal_with_a_reason_outside_the_vocabulary_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            DoorRefusalError("made_up", "nothing")

    def test_the_sentinels_are_the_published_values(self) -> None:
        self.assertEqual(SENTINEL_INTENT_ID, "REPLAY")
        self.assertEqual(SENTINEL_ARMED_TS, "1970-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
