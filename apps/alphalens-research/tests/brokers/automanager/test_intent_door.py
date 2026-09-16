"""The arming door derives identity and labels (#1468).

Pure tests of `intent_door`: no Typer, no journal file, a frozen clock. The CLI
tests in `test_arm_intent_cli.py` drive the same rules end to end.
"""

from __future__ import annotations

import copy
import datetime as dt
import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager import intent_door
from alphalens_pipeline.brokers.automanager.picks import (
    STATUS_ARMED,
    STATUS_DISARMED,
    PickRecord,
)
from broker_contract.trade_intent.codec import intent_from_jsonable

AFTER_NY_CLOSE = dt.datetime(2026, 9, 17, 1, 30, tzinfo=dt.UTC)
NY_SESSION = dt.datetime(2026, 9, 16, 15, 0, tzinfo=dt.UTC)


def _author(**meta: Any) -> dict[str, Any]:
    """The minimal document an author writes: nothing the door can compute."""
    return {
        "instrument": {"ticker": "KO", "mic": "XNYS"},
        "spec": {
            "entry_tiers": [
                {"limit_price": 60.0, "alloc_pct": 75.0},
                {"limit_price": 58.0, "alloc_pct": 25.0},
            ],
            "disaster_stop": 55.0,
            "tp_tranches": [{"price": 66.0, "tranche_pct": 50.0}],
            "size": {"notional_acct": 1500.0, "currency": "EUR"},
        },
        "meta": {"source": "manual", **meta},
    }


def _record(
    *,
    ticker: str = "KO",
    trade_date: str = "2026-09-16",
    generation: int = 1,
    status: str = STATUS_ARMED,
    mic: str | None = "XNYS",
    armed_ts: str = "2026-09-16T14:00:00+00:00",
) -> PickRecord:
    intent = {"instrument": {"ticker": ticker, "mic": mic}} if mic is not None else {}
    return PickRecord(
        ticker=ticker,
        trade_date=dt.date.fromisoformat(trade_date),
        status=status,
        record={"ticker": ticker, "armed_ts": armed_ts, "status": status, "intent": intent},
        generation=generation,
    )


def _token(trade_date: str, generation: int = 1) -> str:
    return trade_date if generation == 1 else f"{trade_date}-g{generation}"


def _complete(document: dict[str, Any], *, now: dt.datetime = NY_SESSION, records=(), placed=()):
    return intent_door.complete(
        document, now_utc=now, records=list(records), placed_keys=set(placed), env="sim"
    )


class DerivedFieldsAreRefusedOnInput(unittest.TestCase):
    def test_a_minimal_document_supplies_none(self) -> None:
        self.assertEqual(intent_door.supplied_derived_paths(_author()), [])

    def test_each_derived_field_is_named_where_it_was_sent(self) -> None:
        document = _author(armed_ts="2026-09-16T00:00:00+00:00")
        document["intent_id"] = "KO:2026-09-16:manual"
        document["spec"]["tp_tranches"][0]["r_multiple"] = 2.0
        self.assertEqual(
            intent_door.supplied_derived_paths(document),
            ["intent_id", "meta.armed_ts", "spec.tp_tranches[0].r_multiple"],
        )

    def test_a_wrong_container_is_left_to_the_schema(self) -> None:
        document = _author()
        document["meta"] = "manual"
        document["spec"]["tp_tranches"] = {"price": 1}
        self.assertEqual(intent_door.supplied_derived_paths(document), [])
        self.assertEqual(intent_door.supplied_derived_paths(["not", "an", "object"]), [])


class TheWalkerCoversEveryDerivedField(unittest.TestCase):
    """`supplied_derived_paths` names its fields by hand. A field newly marked
    `door="derived"` in the contract would be absent from the input schema and
    silently accepted by the door, so the two lists are pinned together."""

    WALKED = {
        ("TradeIntent", "intent_id"),
        ("IntentMeta", "armed_ts"),
        ("TpTrancheSpec", "r_multiple"),
    }

    def test_the_contract_marks_exactly_the_fields_the_walker_checks(self) -> None:
        import dataclasses

        from broker_contract.trade_intent import schema as contract_schema

        derived = {
            (name, field.name)
            for name, cls in vars(contract_schema).items()
            if isinstance(cls, type) and dataclasses.is_dataclass(cls)
            for field in dataclasses.fields(cls)
            if field.metadata.get("door") == "derived"
        }
        self.assertEqual(derived, self.WALKED)


class IdentityShapesAreCheckedBeforeAnythingIsDerived(unittest.TestCase):
    def test_a_float_generation_is_refused_as_undecodable(self) -> None:
        with self.assertRaises(intent_door.GenerationMalformedError):
            intent_door.check_identity_shapes(_author(generation=2.0))

    def test_a_boolean_generation_is_refused(self) -> None:
        with self.assertRaises(intent_door.GenerationMalformedError):
            intent_door.check_identity_shapes(_author(generation=True))

    def test_a_trade_date_that_is_not_a_date_is_refused(self) -> None:
        with self.assertRaises(intent_door.TradeDateMalformedError):
            intent_door.check_identity_shapes(_author(trade_date="09/17/2026"))

    def test_the_minimal_document_passes(self) -> None:
        intent_door.check_identity_shapes(_author())
        intent_door.check_identity_shapes(_author(trade_date="2026-09-17", generation=3))


class TheTradeDateIsTheSessionThatHasNotClosed(unittest.TestCase):
    def test_after_the_new_york_close_it_is_the_next_session(self) -> None:
        completion = _complete(_author(), now=AFTER_NY_CLOSE)
        self.assertEqual(completion.document["meta"]["trade_date"], "2026-09-17")

    def test_the_day1_gate_defers_rather_than_passes_for_that_pick(self) -> None:
        """The reason the date is not "today at the exchange": 2026-09-16 would
        be a closed session and the gate would wave the pick through."""
        from alphalens_pipeline.brokers.automanager.control_loop import _day1_gap_gate_decision

        completion = _complete(_author(), now=AFTER_NY_CLOSE)
        verdict = _day1_gap_gate_decision(
            AFTER_NY_CLOSE,
            dt.date.fromisoformat(completion.document["meta"]["trade_date"]),
            60.0,
            61.0,
            "XNYS",
            source="manual",
        )
        self.assertEqual(verdict, "defer_preopen")

    def test_a_stated_date_is_kept(self) -> None:
        completion = _complete(_author(trade_date="2026-09-18"), now=AFTER_NY_CLOSE)
        self.assertEqual(completion.document["meta"]["trade_date"], "2026-09-18")

    def test_a_brief_document_must_state_its_date(self) -> None:
        with self.assertRaises(intent_door.TradeDateRequiredError):
            _complete(_author(source="brief"))

    def test_a_brief_document_with_its_date_is_accepted(self) -> None:
        completion = _complete(_author(source="brief", trade_date="2026-09-15"))
        self.assertEqual(completion.document["intent_id"], "KO:2026-09-15")


class IdentityAndLabelsAreDerived(unittest.TestCase):
    def test_a_first_manual_pick(self) -> None:
        completion = _complete(_author())
        document = completion.document
        self.assertEqual(document["intent_id"], "KO:2026-09-16:manual")
        self.assertEqual(document["meta"]["armed_ts"], "2026-09-16T15:00:00+00:00")
        self.assertEqual(document["meta"]["generation"], 1)
        self.assertEqual([t["tag"] for t in document["spec"]["entry_tiers"]], ["T1", "T2"])
        self.assertEqual(document["spec"]["tp_tranches"][0]["tag"], "TP1")
        self.assertIsNone(completion.replaces)

    def test_the_author_document_is_not_mutated(self) -> None:
        document = _author()
        before = copy.deepcopy(document)
        _complete(document)
        self.assertEqual(document, before)

    def test_a_stated_tag_is_kept_even_when_empty(self) -> None:
        document = _author()
        document["spec"]["entry_tiers"][0]["tag"] = "swing-low"
        document["spec"]["entry_tiers"][1]["tag"] = ""
        tags = [t["tag"] for t in _complete(document).document["spec"]["entry_tiers"]]
        self.assertEqual(tags, ["swing-low", ""])

    def test_the_next_free_generation_skips_spent_ones(self) -> None:
        records = [
            _record(generation=1, status=STATUS_DISARMED),
            _record(generation=2, status="refused"),
        ]
        document = _complete(_author(), records=records).document
        self.assertEqual(document["meta"]["generation"], 3)
        self.assertEqual(document["intent_id"], "KO:2026-09-16:manual-g3")

    def test_a_brief_identity_has_no_manual_marker(self) -> None:
        document = _author(source="brief", trade_date="2026-09-15", generation=2)
        self.assertEqual(_complete(document).document["intent_id"], "KO:2026-09-15-g2")

    def test_the_result_decodes(self) -> None:
        intent = intent_from_jsonable(_complete(_author()).document)
        self.assertEqual(intent.meta.source, "manual")


class ARetryCannotBecomeASecondLivePick(unittest.TestCase):
    def test_no_generation_while_one_is_armed_and_unplaced(self) -> None:
        with self.assertRaises(intent_door.PickAlreadyArmedError) as caught:
            _complete(_author(), records=[_record()])
        self.assertIn(
            "alphalens broker disarm KO --date 2026-09-16 --env sim", str(caught.exception)
        )
        self.assertEqual(caught.exception.details["armed_generation"], 1)
        self.assertEqual(caught.exception.details["armed_trade_date"], "2026-09-16")

    def test_the_same_retry_after_midnight_derives_another_date_and_is_still_refused(self) -> None:
        with self.assertRaises(intent_door.PickAlreadyArmedError):
            _complete(_author(), now=AFTER_NY_CLOSE, records=[_record()])

    def test_generation_one_across_the_date_boundary_is_refused_too(self) -> None:
        with self.assertRaises(intent_door.PickAlreadyArmedError):
            _complete(_author(generation=1), now=AFTER_NY_CLOSE, records=[_record()])

    def test_no_generation_beside_a_placed_pick_of_the_same_day_is_refused(self) -> None:
        """Today's `arm-manual` rule: a placed pick may still have a live watch."""
        with self.assertRaises(intent_door.PickAlreadyArmedError):
            _complete(_author(), records=[_record()], placed=[("KO", "2026-09-16")])

    def test_a_placed_pick_of_an_earlier_day_does_not_block(self) -> None:
        records = [_record(trade_date="2026-09-10")]
        completion = _complete(_author(), records=records, placed=[("KO", "2026-09-10")])
        self.assertEqual(completion.document["meta"]["generation"], 1)

    def test_us_venues_are_one_venue(self) -> None:
        """Routing probes XNYS, XNAS and XASE together, and the brief producer stamps
        every brief pick XNYS, so a manual XNAS pick would trade the same instrument."""
        document = _author()
        document["instrument"]["mic"] = "XNAS"
        with self.assertRaises(intent_door.PickAlreadyArmedError):
            _complete(document, records=[_record(trade_date="2026-09-15")])

    def test_another_venue_does_not_block(self) -> None:
        document = _author()
        document["instrument"]["mic"] = "XWAR"
        warsaw_session = dt.datetime(2026, 9, 16, 10, 0, tzinfo=dt.UTC)
        completion = _complete(
            document, now=warsaw_session, records=[_record(trade_date="2026-09-15")]
        )
        self.assertEqual(completion.document["intent_id"], "KO:2026-09-16:manual")

    def test_a_record_without_a_venue_blocks_rather_than_guesses(self) -> None:
        document = _author()
        document["instrument"]["mic"] = "XWAR"
        with self.assertRaises(intent_door.PickAlreadyArmedError):
            _complete(document, records=[_record(trade_date="2026-09-15", mic=None)])

    def test_another_ticker_does_not_block(self) -> None:
        completion = _complete(_author(), records=[_record(ticker="PEP")])
        self.assertEqual(completion.document["meta"]["generation"], 1)

    def test_a_different_live_generation_is_refused(self) -> None:
        with self.assertRaises(intent_door.PickAlreadyArmedError):
            _complete(_author(generation=2), records=[_record()])


class AStatedGenerationFollowsTheWritabilityRules(unittest.TestCase):
    def test_a_replace_keeps_the_armed_ts_of_the_line_it_replaces(self) -> None:
        completion = _complete(_author(generation=1, trade_date="2026-09-16"), records=[_record()])
        self.assertEqual(completion.document["meta"]["armed_ts"], "2026-09-16T14:00:00+00:00")
        self.assertIsNotNone(completion.replaces)

    def test_a_replace_without_a_stated_date_on_the_same_session(self) -> None:
        completion = _complete(_author(generation=1), records=[_record()])
        self.assertEqual(completion.document["meta"]["armed_ts"], "2026-09-16T14:00:00+00:00")

    def test_a_spent_generation_is_refused(self) -> None:
        with self.assertRaises(intent_door.GenerationSpentError):
            _complete(_author(generation=1), records=[_record(status=STATUS_DISARMED)])

    def test_a_placed_generation_is_refused(self) -> None:
        with self.assertRaises(intent_door.AlreadyPlacedError):
            _complete(
                _author(generation=1), records=[_record()], placed=[("KO", _token("2026-09-16"))]
            )

    def test_the_next_generation_after_a_disarm_is_accepted(self) -> None:
        completion = _complete(_author(generation=2), records=[_record(status=STATUS_DISARMED)])
        self.assertEqual(completion.document["intent_id"], "KO:2026-09-16:manual-g2")
        self.assertIsNone(completion.replaces)


class RMultiplesAreDerivedAfterValidation(unittest.TestCase):
    def test_a_hand_worked_two_tier_example(self) -> None:
        """Blend = 0.75 * 60 + 0.25 * 58 = 59.5; 1R = 59.5 - 55 = 4.5;
        TP 66 is (66 - 59.5) / 4.5 = 1.4444… R."""
        intent = intent_from_jsonable(_complete(_author()).document)
        derived = intent_door.with_r_multiples(intent)
        self.assertAlmostEqual(derived.spec.tp_tranches[0].r_multiple, 6.5 / 4.5)
        self.assertEqual(intent.spec.tp_tranches[0].r_multiple, 0.0)

    def test_tier_amounts_are_the_notional_split_by_allocation(self) -> None:
        intent = intent_from_jsonable(_complete(_author()).document)
        self.assertEqual(intent_door.tier_amounts(intent), [1125.0, 375.0])


class EveryReasonIsNamed(unittest.TestCase):
    def test_each_refusal_class_names_its_reason(self) -> None:
        self.assertEqual(
            {cls.reason for cls in intent_door.REFUSALS if cls.reason},
            {
                "derived_field_supplied",
                "trade_date_required",
                "undecodable",
                "trade_date_malformed",
                "generation_spent",
                "already_placed",
            },
        )


if __name__ == "__main__":
    unittest.main()


class TheInputSchemaNeverOutrunsTheDoor(unittest.TestCase):
    """A document the INPUT schema accepts must not be refused by the door for a
    SHAPE reason, nor crash it. The stored-shape analogue lives in
    `tests/trade_intent/test_json_schema.py`; this one runs the door's own
    derivation, with an empty journal, a frozen clock and the venue gate inside
    the harness (a swapped MIC must not reach a calendar).
    """

    # What JSON Schema cannot express, with the refusal the door gives instead.
    # A new entry is a defect in the schema or the door, not a new exception.
    INEXPRESSIBLE = {
        ("meta.generation", "1.0"),  # an integer to JSON Schema, not to identity strings
        ("DROP meta.trade_date", "brief"),  # required only when source is "brief"
        ("meta.trade_date", "''"),  # a string to the schema, not a date
        ("meta.trade_date", "'x'"),
    }

    EDGE_VALUES = (0, -1, 1.0, "", "x", None, True, [], {}, float("nan"), 10**20, [{}])

    def _fixtures(self) -> dict[str, dict[str, Any]]:
        brief = _author(source="brief", trade_date="2026-09-15", generation=2)
        brief["spec"]["entry_tiers"][0].update(tag="swing-low", entry_mode="immediate")
        brief["exit"] = {
            "initial_levels": {"stop": 55.0, "tp": 66.0},
            "reaction_plan": [{"arm_trigger_r": 0.5, "trail_frac": 0.6, "kind": "trailing_stop"}],
        }
        return {"manual": _author(), "brief": brief}

    def _paths(self, node: Any, prefix: tuple[Any, ...] = ()):
        yield prefix
        if isinstance(node, dict):
            for key, value in node.items():
                yield from self._paths(value, (*prefix, key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from self._paths(value, (*prefix, index))

    def _door_verdict(self, document: Any) -> str | None:
        """None when the door takes it or refuses it for a non-shape reason;
        otherwise a short label of the shape refusal or the crash."""
        import jsonschema
        from alphalens_pipeline.brokers.automanager.manual_intent import (
            UnsupportedVenueError,
            ensure_supported_venue,
        )
        from broker_contract.trade_intent.codec import TradeIntentDecodeError

        if intent_door.supplied_derived_paths(document):
            return None
        if list(jsonschema.Draft202012Validator(self.schema).iter_errors(document)):
            return None
        try:
            ensure_supported_venue(document["instrument"]["mic"])
        except UnsupportedVenueError:
            return None
        try:
            intent_door.check_identity_shapes(document)
            completion = _complete(document)
            intent_from_jsonable(completion.document)
        except (intent_door.GenerationMalformedError, intent_door.TradeDateMalformedError):
            return "identity"
        except intent_door.TradeDateRequiredError:
            return "brief"
        except TradeIntentDecodeError:
            return "undecodable"
        except Exception as exc:  # a crash is exactly what this sweep hunts
            return f"crash {type(exc).__name__}: {exc}"
        return None

    def setUp(self) -> None:
        from broker_contract.trade_intent.json_schema import generate_schema

        self.schema = generate_schema("input")

    def _divergences(self) -> tuple[set[tuple[str, str]], int]:
        found: set[tuple[str, str]] = set()
        swept = 0
        for name, base in self._fixtures().items():
            for path in {p for p in self._paths(base) if p}:
                where = ".".join(map(str, path))
                for value in self.EDGE_VALUES:
                    mutated = copy.deepcopy(base)
                    cursor = mutated
                    for step in path[:-1]:
                        cursor = cursor[step]
                    cursor[path[-1]] = value
                    swept += 1
                    outcome = self._door_verdict(mutated)
                    if outcome is not None:
                        found.add((where, repr(value)) if outcome != "brief" else (where, name))
                mutated = copy.deepcopy(base)
                cursor = mutated
                for step in path[:-1]:
                    cursor = cursor[step]
                del cursor[path[-1]]
                swept += 1
                outcome = self._door_verdict(mutated)
                if outcome is not None:
                    found.add((f"DROP {where}", name if outcome == "brief" else outcome))
        return found, swept

    def test_the_sweep_is_large_enough_to_be_a_sweep(self) -> None:
        _, swept = self._divergences()
        self.assertGreater(swept, 300)

    def test_nothing_the_input_schema_accepts_trips_the_door_on_shape(self) -> None:
        found, _ = self._divergences()
        self.assertEqual(found - self.INEXPRESSIBLE, set())

    def test_every_declared_exception_is_real(self) -> None:
        """Positive control: an entry the sweep no longer finds must go."""
        found, _ = self._divergences()
        self.assertEqual(self.INEXPRESSIBLE - found, set())
