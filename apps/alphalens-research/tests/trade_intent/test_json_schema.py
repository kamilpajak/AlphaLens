"""The published JSON Schema for `TradeIntent` (#1405).

The schema describes the DOOR: what a producer must put on the wire for the
contract to accept it, checked BEFORE decoding. It is deliberately not a
description of the codec's input language — the codec still migrates a legacy
key the schema does not know, and that gap is pinned below rather than hidden.

Two properties matter more than the artefact itself:

* the schema must never ACCEPT a document the codec REFUSES on shape. A client
  that validates locally, gets a green light and is then refused at the door has
  been lied to. The reverse (schema stricter) is allowed and documented.
* the committed artefact must match a fresh generation, and a BREAKING drift
  must demand a version bump rather than a quiet recommit — this project has
  already renamed a wire field inside a major version once.

The sweep below is what gives the first property teeth: a hand-written table of
mutations can only refute what its author thought of, and when this was written
the sweep found two divergences the author's table had missed.
"""

from __future__ import annotations

import contextlib
import copy
import io
import itertools
import json
import math
import os
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
from broker_contract.trade_intent.codec import TradeIntentDecodeError, intent_from_jsonable
from broker_contract.trade_intent.json_schema import (
    SCHEMA_FILENAME,
    artefact_path,
    generate_schema,
    main,
    render_schema,
)
from broker_contract.trade_intent.schema import SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[4]
README = REPO_ROOT / "apps" / "alphalens-broker-contract" / "README.md"

REGENERATE = "python -m broker_contract.trade_intent.json_schema --write"


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(generate_schema())


def _accepts(document: Any) -> bool:
    return not list(_validator().iter_errors(document))


def _decodes(document: Any) -> bool:
    try:
        intent_from_jsonable(document)
    except TradeIntentDecodeError:
        return False
    return True


# --------------------------------------------------------------------------
# Fixtures. Every shape here was MEASURED in the 63 real journaled documents
# (2026-09-11): both schema versions, exit present and null, levels with and
# without a reaction plan, an immediate tier, and the five venues in use.
# Invented-but-plausible documents would prove nothing about production.
# --------------------------------------------------------------------------


def _brief_like() -> dict[str, Any]:
    return {
        "intent_id": "AMBA-2026-09-11",
        "account_id": "default",
        "instrument": {"ticker": "AMBA", "mic": "XNAS"},
        "spec": {
            "entry_tiers": [
                {"limit_price": 59.0, "alloc_pct": 60.0, "tag": "T1", "entry_mode": "pullback"},
                {"limit_price": 57.0, "alloc_pct": 40.0, "tag": "T2", "entry_mode": "pullback"},
            ],
            "disaster_stop": 55.0,
            "tp_tranches": [{"price": 70.0, "tranche_pct": 100.0, "r_multiple": 2.0, "tag": "TP1"}],
            "suggested_size_pct": 2.0,
            "order_ttl_days": 7,
            "side": "long",
            "schema_version": "2",
        },
        "meta": {
            "armed_ts": "2026-09-11T08:00:00+00:00",
            "trade_date": "2026-09-11",
            "schema_version": "2",
            "source": "brief",
            "generation": 1,
        },
        "exit": {
            "initial_levels": {"stop": 55.0, "tp": 70.0},
            "reaction_plan": [{"arm_trigger_r": 0.5, "trail_frac": 0.6, "kind": "trailing_stop"}],
        },
    }


def _manual_like() -> dict[str, Any]:
    """A manual pick: no exit at all, one rung, a non-US venue."""
    return {
        "intent_id": "KER-2026-09-08",
        "instrument": {"ticker": "KER", "mic": "XPAR"},
        "spec": {
            "entry_tiers": [{"limit_price": 210.0, "alloc_pct": 100.0}],
            "disaster_stop": 195.0,
            "tp_tranches": [],
            "suggested_size_pct": 1.5,
        },
        "meta": {"armed_ts": "2026-09-08T13:00:00+00:00", "trade_date": "2026-09-08"},
        "exit": None,
    }


def _reanchor_like() -> dict[str, Any]:
    """The historical brief shape: levels plus a re-anchor, and an immediate tier."""
    document = _brief_like()
    document["instrument"] = {"ticker": "QUBT", "mic": "XNYS"}
    document["spec"]["entry_tiers"][0]["entry_mode"] = "immediate"
    document["exit"]["reaction_plan"] = [
        {"k_atr": 1.5, "atr": 2.0, "ceiling_price": None, "kind": "reanchor_on_fill"}
    ]
    return document


def _legacy_v1() -> dict[str, Any]:
    """48 of the 63 real documents look like this: version 1, and the pre-rename
    date key the codec migrates."""
    document = _brief_like()
    document["spec"]["schema_version"] = "1"
    document["meta"] = {
        "armed_ts": "2026-08-20T08:00:00+00:00",
        "brief_date": "2026-08-20",
        "schema_version": "1",
    }
    return document


CURRENT_FIXTURES = (_brief_like, _manual_like, _reanchor_like)


class TestTheGeneratedShape(unittest.TestCase):
    def test_it_is_a_2020_12_schema_naming_its_version(self) -> None:
        schema = generate_schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn(SCHEMA_VERSION, schema["$id"])

    def test_a_literal_becomes_a_closed_enum(self) -> None:
        schema = generate_schema()
        self.assertEqual(schema["$defs"]["TradeSpec"]["properties"]["side"]["enum"], ["long"])
        self.assertEqual(
            schema["$defs"]["EntryTierSpec"]["properties"]["entry_mode"]["enum"],
            ["pullback", "immediate"],
        )

    def test_a_tuple_becomes_an_array_of_its_item(self) -> None:
        tiers = generate_schema()["$defs"]["TradeSpec"]["properties"]["entry_tiers"]
        self.assertEqual(tiers["type"], "array")
        self.assertEqual(tiers["items"], {"$ref": "#/$defs/EntryTierSpec"})

    def test_fields_without_a_default_are_required_and_the_rest_are_not(self) -> None:
        spec = generate_schema()["$defs"]["TradeSpec"]
        self.assertIn("disaster_stop", spec["required"])
        self.assertNotIn("order_ttl_days", spec["required"])
        self.assertEqual(spec["properties"]["order_ttl_days"]["default"], 7)

    def test_every_union_branch_requires_its_discriminator(self) -> None:
        """Measured: without this an empty object `{}` matches ModelPush — whose
        only field has a default — so `[{}]` passed the schema while the codec
        refused it for an unknown kind."""
        defs = generate_schema()["$defs"]
        for name in ("ReanchorOnFill", "TrailingStop", "ModelPush"):
            with self.subTest(primitive=name):
                self.assertIn("kind", defs[name]["required"])

    def test_a_meaning_becomes_the_published_description(self) -> None:
        alloc = generate_schema()["$defs"]["EntryTierSpec"]["properties"]["alloc_pct"]
        self.assertIn("PERCENTAGE", alloc["description"])

    def test_the_one_extra_keyword_reaches_the_schema(self) -> None:
        generation = generate_schema()["$defs"]["IntentMeta"]["properties"]["generation"]
        self.assertEqual(generation["minimum"], 1)

    def test_no_numeric_bound_leaks_in_from_validate_intent(self) -> None:
        """`validate_intent` owns the bounds. A second owner here is how the two
        drift apart, so the generator must not have grown any."""
        bounded = [
            f"{name}.{field}"
            for name, definition in generate_schema()["$defs"].items()
            for field, body in definition.get("properties", {}).items()
            if {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"} & set(body)
        ]
        self.assertEqual(bounded, ["IntentMeta.generation"])


class TestEveryDocumentWeEmitValidates(unittest.TestCase):
    def test_the_current_fixtures_pass(self) -> None:
        for build in CURRENT_FIXTURES:
            with self.subTest(document=build.__name__):
                errors = sorted(_validator().iter_errors(build()), key=lambda e: e.json_path)
                self.assertEqual(
                    [f"{e.json_path}: {e.message}" for e in errors],
                    [],
                )

    def test_a_re_encoded_document_validates_without_any_normalisation(self) -> None:
        """The property #1405 needs from the codec: what comes out of
        `intent_to_jsonable` is JSON-shaped, so it can go straight to a validator."""
        from broker_contract.trade_intent.codec import intent_to_jsonable

        for build in CURRENT_FIXTURES:
            with self.subTest(document=build.__name__):
                emitted = intent_to_jsonable(intent_from_jsonable(build()))
                self.assertTrue(_accepts(emitted), emitted)


# --------------------------------------------------------------------------
# The sweep.
# --------------------------------------------------------------------------

# Values chosen to break a shape rather than a bound: wrong JSON kind, wrong
# numeric kind, empty, absent, and the two shapes a reaction plan can take.
EDGE_VALUES: tuple[Any, ...] = (
    0,
    -1,
    1.0,
    0.0,
    "",
    "x",
    None,
    True,
    [],
    {},
    float("nan"),
    10**20,
    [{}],
    {"kind": "typo"},
)

# The ONE divergence JSON Schema cannot express, with the reason. Draft 2020-12
# defines "integer" as any number with zero fractional part, so `1.0` is a valid
# integer to every validator — while the codec refuses a float because the pick
# identity strings (crid, pick_key, journal join) are built from this field
# (#1371). `multipleOf` and `minimum` do not change this; measured 2026-09-11.
#
# A second entry here is not a second exception, it is a defect: fix the schema
# or fix the codec.
INEXPRESSIBLE = {("meta.generation", "1.0")}


def _leaf_and_container_paths(node: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(node, dict):
        yield prefix
        for key, value in node.items():
            yield from _leaf_and_container_paths(value, (*prefix, key))
    elif isinstance(node, list):
        yield prefix
        for index, value in enumerate(node):
            yield from _leaf_and_container_paths(value, (*prefix, index))
    else:
        yield prefix


def _replace(document: Any, path: tuple[Any, ...], value: Any) -> None:
    cursor = document
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value


def _remove(document: Any, path: tuple[Any, ...]) -> None:
    cursor = document
    for step in path[:-1]:
        cursor = cursor[step]
    del cursor[path[-1]]


class TestTheSchemaNeverOutrunsTheDoor(unittest.TestCase):
    """A document the schema accepts must be one the codec can decode.

    Swept rather than tabulated: the paths come from the documents themselves,
    so a field added tomorrow is swept without anyone remembering to add it.
    """

    def _divergences(self) -> tuple[set[tuple[str, str]], int]:
        found: set[tuple[str, str]] = set()
        swept = 0
        for build in (*CURRENT_FIXTURES, _reanchor_like):
            base = build()
            paths = sorted(set(_leaf_and_container_paths(base)), key=len)
            for path, value in itertools.product(paths, EDGE_VALUES):
                if not path:
                    continue
                mutated = copy.deepcopy(base)
                try:
                    _replace(mutated, path, value)
                except (KeyError, IndexError, TypeError):
                    continue
                swept += 1
                if _accepts(mutated) and not _decodes(mutated):
                    found.add((".".join(map(str, path)), repr(value)))
            for path in paths:
                if not path:
                    continue
                mutated = copy.deepcopy(base)
                try:
                    _remove(mutated, path)
                except (KeyError, IndexError, TypeError):
                    continue
                swept += 1
                if _accepts(mutated) and not _decodes(mutated):
                    found.add((f"DROP {'.'.join(map(str, path))}", ""))
        return found, swept

    def test_the_sweep_is_large_enough_to_be_a_sweep(self) -> None:
        """Positive control: an empty sweep would make the test below vacuous."""
        _, swept = self._divergences()
        self.assertGreater(swept, 500)

    def test_nothing_the_schema_accepts_is_refused_by_the_codec(self) -> None:
        found, _ = self._divergences()
        self.assertEqual(
            found - INEXPRESSIBLE,
            set(),
            "the schema accepts a document the codec refuses on shape — a client "
            "that validated locally would be refused at the door",
        )

    def test_the_declared_exception_is_real_and_still_needed(self) -> None:
        """Positive control for INEXPRESSIBLE: if the schema ever does refuse
        `generation: 1.0`, the exception must go rather than rot."""
        document = _brief_like()
        document["meta"]["generation"] = 1.0
        self.assertTrue(_accepts(document))
        self.assertFalse(_decodes(document))


class TestRefusals(unittest.TestCase):
    """Negative controls that can actually fail, each with its positive control."""

    def test_the_unmutated_documents_pass(self) -> None:
        for build in CURRENT_FIXTURES:
            with self.subTest(document=build.__name__):
                self.assertTrue(_accepts(build()))

    def test_a_price_sent_as_a_string_is_refused(self) -> None:
        document = _brief_like()
        document["spec"]["entry_tiers"][0]["limit_price"] = "59"
        self.assertFalse(_accepts(document))

    def test_a_short_is_refused(self) -> None:
        document = _brief_like()
        document["spec"]["side"] = "short"
        self.assertFalse(_accepts(document))

    def test_an_unknown_entry_mode_is_refused(self) -> None:
        document = _brief_like()
        document["spec"]["entry_tiers"][0]["entry_mode"] = "market"
        self.assertFalse(_accepts(document))

    def test_an_unknown_reaction_kind_is_refused(self) -> None:
        document = _brief_like()
        document["exit"]["reaction_plan"] = [{"kind": "typo"}]
        self.assertFalse(_accepts(document))

    def test_a_reaction_primitive_with_no_kind_is_refused(self) -> None:
        document = _brief_like()
        document["exit"]["reaction_plan"] = [{"k_atr": 1.5, "atr": 2.0}]
        self.assertFalse(_accepts(document))

    def test_an_empty_reaction_primitive_is_refused(self) -> None:
        """`{}` matched ModelPush before the discriminator became required."""
        document = _brief_like()
        document["exit"]["reaction_plan"] = [{}]
        self.assertFalse(_accepts(document))

    def test_a_boolean_generation_is_refused(self) -> None:
        document = _brief_like()
        document["meta"]["generation"] = True
        self.assertFalse(_accepts(document))

    def test_a_zero_generation_is_refused(self) -> None:
        document = _brief_like()
        document["meta"]["generation"] = 0
        self.assertFalse(_accepts(document))

    def test_a_ladder_sent_as_an_object_is_refused(self) -> None:
        document = _brief_like()
        document["spec"]["entry_tiers"] = {}
        self.assertFalse(_accepts(document))

    def test_a_document_with_no_trade_date_is_refused(self) -> None:
        document = _brief_like()
        del document["meta"]["trade_date"]
        self.assertFalse(_accepts(document))

    def test_an_exit_with_no_levels_is_accepted(self) -> None:
        """Positive control on the optional half of #1236: a declared trail with
        no bracket is a legitimate document, not a malformed one."""
        document = _brief_like()
        document["exit"].pop("initial_levels")
        self.assertTrue(_accepts(document))


class TestTheKnownDivergencesArePinned(unittest.TestCase):
    """Both gaps between schema and codec are decisions. A future reader who
    "fixes" one gets a red test naming whose decision it was."""

    def test_the_legacy_date_key_is_refused_by_the_schema_and_accepted_by_the_codec(
        self,
    ) -> None:
        legacy = _legacy_v1()
        self.assertFalse(
            _accepts(legacy),
            "the schema describes the DOOR, and a new producer must send trade_date",
        )
        self.assertTrue(
            _decodes(legacy),
            "the codec's brief_date shim serves the journal drain, which is a "
            "different entry point and is not schema-gated",
        )

    def test_no_gate_reads_the_schema_version_at_all(self) -> None:
        """Pins the fact the published README states, because the claim it
        replaced ("only an unknown future version would be refused") was copied
        from the issue and was false: nothing in this path reads the field.

        If version gating is ever added, this test goes red and the README
        paragraph must be rewritten in the same commit.
        """
        for version in ("1", "2", "99", "not-a-version"):
            document = _brief_like()
            document["meta"]["schema_version"] = version
            document["spec"]["schema_version"] = version
            with self.subTest(version=version):
                self.assertTrue(_accepts(document))
                self.assertTrue(_decodes(document))

    def test_an_old_schema_version_is_still_accepted(self) -> None:
        """Older is accepted, only unknown-and-future would be refused — and the
        version is not an enum, so v1 payloads pass on shape."""
        legacy = _legacy_v1()
        legacy["meta"]["trade_date"] = legacy["meta"].pop("brief_date")
        self.assertTrue(_accepts(legacy))


# --------------------------------------------------------------------------
# The committed artefact.
# --------------------------------------------------------------------------


def _objects(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every object definition in the schema, keyed by name."""
    return {"TradeIntent": schema, **schema.get("$defs", {})}


def _shape(node: dict[str, Any]) -> dict[str, Any]:
    """The part of a property that constrains a producer.

    `description` and `default` carry no constraint, so changing them is never a
    break. Everything else is compared literally — which is what catches a `$ref`
    retargeted at another definition, or an array whose `items` changed, neither
    of which shows up in the property's own `type`.
    """
    return {key: value for key, value in node.items() if key not in ("description", "default")}


def _as_set(node: dict[str, Any], keyword: str) -> frozenset[Any] | None:
    """A constraint as a set of admissible values; None means unconstrained."""
    if keyword not in node:
        return None
    value = node[keyword]
    return frozenset(value if isinstance(value, list) else [value])


def _is_widening(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when `new` admits everything `old` did, in the two ways we allow.

    Widening a type union or an enum is safe for a producer pinned to the old
    artefact: every document it could send still validates. Narrowing either, or
    changing anything else at all, is not.
    """
    if {key: value for key, value in _shape(old).items() if key not in ("type", "enum")} != {
        key: value for key, value in _shape(new).items() if key not in ("type", "enum")
    }:
        return False
    for keyword in ("type", "enum"):
        was, now = _as_set(old, keyword), _as_set(new, keyword)
        if was is None:
            continue  # was unconstrained; any constraint now is a narrowing
        if now is not None and was <= now:
            continue  # same constraint or a wider one
        if now is None:
            continue  # constraint dropped entirely: wider
        return False
    if _as_set(old, "type") is None and _as_set(new, "type") is not None:
        return False
    return _as_set(old, "enum") is not None or _as_set(new, "enum") is None


def breaking_changes(committed: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Differences that break a producer pinned to ``committed``.

    Additive — a new optional property, a new definition, a widened enum or type,
    a reworded description — returns nothing: regenerate and commit. Everything
    else needs a version bump, because the artefact for a version must not change
    meaning under a consumer's feet. This project has already renamed a wire field
    inside major version 2 without noticing, which is the case this exists for.

    Deliberately strict in one direction: a RELAXED numeric bound also reads as
    breaking here. There is exactly one bound in the whole schema and it is
    allowlisted, so paying for that precision is cheaper than a classifier with a
    second notion of "wider".
    """
    broken: list[str] = []
    old_objects, new_objects = _objects(committed), _objects(live)
    broken.extend(
        f"{name}: definition removed" for name in sorted(set(old_objects) - set(new_objects))
    )
    for name in sorted(set(old_objects) & set(new_objects)):
        old, new = old_objects[name], new_objects[name]
        old_props, new_props = old.get("properties", {}), new.get("properties", {})
        broken.extend(
            f"{name}.{field}: field removed or renamed"
            for field in sorted(set(old_props) - set(new_props))
        )
        broken.extend(
            f"{name}.{field}: newly required"
            for field in sorted(set(new.get("required", ())) - set(old.get("required", ())))
        )
        for field in sorted(set(old_props) & set(new_props)):
            was, now = _shape(old_props[field]), _shape(new_props[field])
            if was != now and not _is_widening(old_props[field], new_props[field]):
                broken.append(f"{name}.{field}: narrowed or retyped, {was} -> {now}")
    return broken


class TestDriftClassification(unittest.TestCase):
    """The classifier decides "recommit" vs "bump the version", so it is tested
    rather than trusted. Every case below except the last two was a MISS in the
    first implementation, found by probing it — a `$ref` retargeted at another
    definition and an array whose items changed both left the property's own
    `type` untouched and slipped straight through."""

    def _pair(self) -> tuple[dict[str, Any], dict[str, Any]]:
        base = {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "ref": {"$ref": "#/$defs/Leaf"},
                "list": {"type": "array", "items": {"$ref": "#/$defs/Leaf"}},
            },
            "required": ["a"],
            "$defs": {
                "Leaf": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "Other": {"type": "object", "properties": {}},
            },
        }
        return copy.deepcopy(base), copy.deepcopy(base)

    def test_an_unchanged_schema_breaks_nothing(self) -> None:
        old, new = self._pair()
        self.assertEqual(breaking_changes(old, new), [])

    def test_a_new_optional_field_is_additive(self) -> None:
        old, new = self._pair()
        new["properties"]["b"] = {"type": "string"}
        self.assertEqual(breaking_changes(old, new), [])

    def test_a_reworded_description_is_additive(self) -> None:
        old, new = self._pair()
        new["properties"]["a"]["description"] = "clearer wording"
        self.assertEqual(breaking_changes(old, new), [])

    def test_a_renamed_field_is_breaking(self) -> None:
        """The exact change this project made to `brief_date` without a bump."""
        old, new = self._pair()
        new["properties"]["renamed"] = new["properties"].pop("a")
        new["required"] = ["renamed"]
        self.assertIn("TradeIntent.a: field removed or renamed", breaking_changes(old, new))

    def test_a_newly_required_field_is_breaking(self) -> None:
        old, new = self._pair()
        new["properties"]["b"] = {"type": "string"}
        new["required"] = ["a", "b"]
        self.assertIn("TradeIntent.b: newly required", breaking_changes(old, new))

    def test_a_changed_type_is_breaking(self) -> None:
        old, new = self._pair()
        new["$defs"]["Leaf"]["properties"]["n"] = {"type": "string"}
        self.assertTrue(any(item.startswith("Leaf.n:") for item in breaking_changes(old, new)))

    def test_a_retargeted_ref_is_breaking(self) -> None:
        """Invisible to a classifier that only compares `type`: a $ref node has none."""
        old, new = self._pair()
        new["properties"]["ref"]["$ref"] = "#/$defs/Other"
        self.assertTrue(
            any(item.startswith("TradeIntent.ref:") for item in breaking_changes(old, new))
        )

    def test_a_retargeted_array_item_is_breaking(self) -> None:
        """Also invisible: the property stays `type: array` either way."""
        old, new = self._pair()
        new["properties"]["list"]["items"] = {"$ref": "#/$defs/Other"}
        self.assertTrue(
            any(item.startswith("TradeIntent.list:") for item in breaking_changes(old, new))
        )

    def test_a_dropped_union_branch_is_breaking(self) -> None:
        old, new = self._pair()
        old["properties"]["a"] = {"oneOf": [{"$ref": "#/$defs/Leaf"}, {"$ref": "#/$defs/Other"}]}
        new["properties"]["a"] = {"oneOf": [{"$ref": "#/$defs/Leaf"}]}
        self.assertTrue(
            any(item.startswith("TradeIntent.a:") for item in breaking_changes(old, new))
        )

    def test_a_new_bound_is_breaking(self) -> None:
        old, new = self._pair()
        new["$defs"]["Leaf"]["properties"]["n"]["minimum"] = 1
        self.assertTrue(any(item.startswith("Leaf.n:") for item in breaking_changes(old, new)))

    def test_losing_nullability_is_breaking_and_gaining_it_is_not(self) -> None:
        old, new = self._pair()
        old["properties"]["a"]["type"] = ["string", "null"]
        self.assertTrue(
            any(item.startswith("TradeIntent.a:") for item in breaking_changes(old, new))
        )
        old, new = self._pair()
        new["properties"]["a"]["type"] = ["string", "null"]
        self.assertEqual(breaking_changes(old, new), [])

    def test_a_narrowed_enum_is_breaking_and_a_widened_one_is_not(self) -> None:
        old, new = self._pair()
        old["properties"]["a"]["enum"] = ["x", "y"]
        new["properties"]["a"]["enum"] = ["x"]
        self.assertTrue(
            any(item.startswith("TradeIntent.a:") for item in breaking_changes(old, new))
        )
        old, new = self._pair()
        old["properties"]["a"]["enum"] = ["x"]
        new["properties"]["a"]["enum"] = ["x", "y"]
        self.assertEqual(breaking_changes(old, new), [])

    def test_a_first_enum_on_a_free_field_is_breaking(self) -> None:
        old, new = self._pair()
        new["properties"]["a"]["enum"] = ["x"]
        self.assertTrue(
            any(item.startswith("TradeIntent.a:") for item in breaking_changes(old, new))
        )

    def test_a_removed_definition_is_breaking(self) -> None:
        old, new = self._pair()
        del new["$defs"]["Leaf"]
        self.assertIn("Leaf: definition removed", breaking_changes(old, new))


class TestTheCommittedArtefact(unittest.TestCase):
    def test_the_artefact_for_the_current_version_exists(self) -> None:
        """A missing artefact FAILS. The Django precedent skips instead, which
        makes its gate disappear exactly when the artefact does."""
        self.assertTrue(
            artefact_path().exists(),
            f"{SCHEMA_FILENAME} is missing — generate it with `{REGENERATE}`",
        )

    def test_it_matches_a_fresh_generation(self) -> None:
        committed = json.loads(artefact_path().read_text())
        live = generate_schema()
        if committed == live:
            return
        broken = breaking_changes(committed, live)
        self.assertEqual(
            broken,
            [],
            "the contract changed in a way that breaks a consumer pinned to "
            f"v{SCHEMA_VERSION}. Bump SCHEMA_VERSION (which creates a new "
            "artefact and freezes this one) rather than recommitting this file",
        )
        self.fail(f"{SCHEMA_FILENAME} is stale (additive drift). Regenerate: `{REGENERATE}`")

    def test_the_file_is_byte_identical_to_the_renderer(self) -> None:
        self.assertEqual(artefact_path().read_text(), render_schema())

    def test_rendering_is_idempotent(self) -> None:
        self.assertEqual(render_schema(), render_schema())

    def test_the_readme_names_the_current_artefact(self) -> None:
        """Two artefacts sit side by side after a bump, and nothing else says
        which one a consumer should read."""
        self.assertIn(SCHEMA_FILENAME, README.read_text())


class TestAgainstARealJournal(unittest.TestCase):
    """Opt-in, like the L4 vendor probes: point it at a real `picks.jsonl` and
    it validates every intent that file carries.

        ALPHALENS_PICKS_JSONL=~/.alphalens/broker_orders/live/picks.jsonl \\
            .venv/bin/python -m unittest tests.trade_intent.test_json_schema

    Measured 2026-09-11 over 63 real documents (LIVE + SIM): 12 accepted, 51
    refused, every refusal for the legacy `brief_date` key and nothing else.
    """

    @unittest.skipUnless(os.environ.get("ALPHALENS_PICKS_JSONL"), "no journal given")
    def test_every_refusal_is_one_we_already_know_about(self) -> None:
        path = Path(os.path.expanduser(os.environ["ALPHALENS_PICKS_JSONL"]))
        documents = [
            row["intent"]
            for row in (json.loads(line) for line in path.read_text().splitlines() if line.strip())
            if row.get("intent")
        ]
        self.assertGreater(len(documents), 0, f"{path} carries no intents")
        unexpected: list[str] = []
        accepted = 0
        for document in documents:
            errors = list(_validator().iter_errors(document))
            if not errors:
                accepted += 1
                continue
            legacy_only = all(
                "trade_date" in error.message and "required" in error.message for error in errors
            )
            if not legacy_only:
                unexpected.append(f"{document['instrument']['ticker']}: {errors[0].message}")
        print(f"\n{path}: {len(documents)} intents, {accepted} accepted")
        self.assertEqual(unexpected, [])


class TestTheRegenerationCommand(unittest.TestCase):
    """`python -m broker_contract.trade_intent.json_schema` is the command the
    gate's failure message names, so it is exercised rather than assumed."""

    def test_bare_invocation_writes_the_schema_to_stdout(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            status = main([])
        self.assertEqual(status, 0)
        self.assertEqual(buffer.getvalue(), render_schema())

    def test_write_puts_it_where_the_gate_looks(self) -> None:
        original = artefact_path().read_text()
        self.addCleanup(artefact_path().write_text, original)
        artefact_path().write_text("{}\n")
        with contextlib.redirect_stderr(io.StringIO()):
            status = main(["--write"])
        self.assertEqual(status, 0)
        self.assertEqual(artefact_path().read_text(), render_schema())

    def test_an_unknown_argument_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main(["--bogus"])
        self.assertEqual(status, 2, "usage is exit 2 across this project's CLIs")
        self.assertIn("usage:", errors.getvalue())


class TestTheRendererIsStrict(unittest.TestCase):
    def test_the_rendered_text_parses_back_into_the_schema(self) -> None:
        self.assertEqual(json.loads(render_schema()), generate_schema())

    def test_it_ends_in_a_newline(self) -> None:
        self.assertTrue(render_schema().endswith("\n"))

    def test_the_schema_is_a_valid_schema(self) -> None:
        """A generated document that no validator can load would fail silently
        everywhere else in this file."""
        jsonschema.Draft202012Validator.check_schema(generate_schema())

    def test_no_non_finite_number_reaches_the_artefact(self) -> None:
        """`json.dumps` writes a bare NaN by default, which is not JSON."""
        for value in json.loads(render_schema()).get("$defs", {}).values():
            for body in value.get("properties", {}).values():
                default = body.get("default")
                if isinstance(default, float):
                    self.assertTrue(math.isfinite(default))


if __name__ == "__main__":
    unittest.main()
