"""Unit tests for ``intent_replay/classification.py`` — the path classes of
spec section 4.3.1 and the gate that refuses a document carrying a path in
none of them.

Three sources are held together here, in both directions:

* the published input schema (``generate_schema("input")``), which says which
  paths exist;
* the two tables of spec section 4.3.1 (plus the optional-path table PR 3
  added), which say where each path lands — the spec is the record, so a row
  in the code with no row in the spec is red, and a stale spec row is red;
* the module's own listed classes, which the gate subtracts.

``interpreted`` is not a list in the module: the interpreter proves it at
runtime by reading. Until PR 5 exists, this file carries the expected set
READ OFF THE SPEC TABLES, and hands it to the gate as ``read`` — the coverage
record for review the spec describes, never the gate's source of truth.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

from broker_contract.trade_intent.codec import intent_from_jsonable
from broker_contract.trade_intent.json_schema import generate_schema
from broker_contract.trade_intent.schema import (
    ExitGeometrySpec,
    InitialLevels,
    ReanchorOnFill,
)
from broker_contract.trade_intent.validate import (
    INTENT_INVALID_REASONS,
    IntentInvalidError,
    validate_intent,
)
from intent_replay import classification
from intent_replay.classification import (
    CONTAINERS,
    OUT_OF_SCOPE,
    PATH_UNCLASSIFIED_CODE,
    REFUSED_BY_DOOR,
    TRANSLATED,
    PathUnclassifiedError,
    check_classified,
    document_paths,
    unclassified_paths,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SPEC = WORKSPACE_ROOT / "docs" / "superpowers" / "specs" / "2026-09-23-intent-replay-design.md"
EXAMPLES = WORKSPACE_ROOT / "apps" / "alphalens-broker-contract" / "examples" / "manual-pick"

# The completion of spec section 6.4: two out-of-scope sentinels and a stated
# trade date. Nothing else is filled — in particular no tags.
SENTINEL_INTENT_ID = "REPLAY"
SENTINEL_ARMED_TS = "1970-01-01T00:00:00+00:00"
STATED_TRADE_DATE = "2026-09-23"

# Every one of the input schema's paths, by hand from the schema: two
# reaction-plan items (one per kind), non-null initial levels, both tags,
# both schema versions, a generation, a TTL, a side, an account. Built by
# hand rather than re-rendered through the codec, because the codec emits
# ``spec.tp_tranches[].r_multiple`` (a derived field the completion does not
# fill) and renders only what a document declared.
MAXIMAL_DOCUMENT: Mapping[str, Any] = {
    "instrument": {"ticker": "NVO", "mic": "XNYS"},
    "spec": {
        "entry_tiers": [
            {"limit_price": 72.5, "alloc_pct": 60.0, "tag": "T1", "entry_mode": "pullback"},
            {"limit_price": 70.0, "alloc_pct": 40.0, "tag": "T2", "entry_mode": "pullback"},
        ],
        "disaster_stop": 66.0,
        "tp_tranches": [{"price": 80.0, "tranche_pct": 50.0, "tag": "TP1"}],
        "size": {"notional_acct": 1500.0, "currency": "USD"},
        "order_ttl_days": 7,
        "side": "long",
        "schema_version": "3",
    },
    "meta": {
        "trade_date": STATED_TRADE_DATE,
        "schema_version": "3",
        "source": "manual",
        "generation": 1,
    },
    "exit": {
        "initial_levels": {"stop": 65.0, "tp": 85.0},
        "reaction_plan": [
            {"kind": "reanchor_on_fill", "k_atr": 1.5, "atr": 1.2, "ceiling_price": None},
            {"kind": "trailing_stop", "arm_trigger_r": 0.5, "trail_frac": 0.6},
        ],
    },
    "account_id": "default",
}

# Descending every property and collecting the names listed in a ``required``
# array reaches seven more paths than descending only through required
# properties; they are recorded here so the difference is written down.
OPTIONAL_CONTAINER_REQUIRED_LEAVES = frozenset(
    {
        "exit.initial_levels.stop",
        "exit.initial_levels.tp",
        "exit.reaction_plan[].kind",
        "exit.reaction_plan[].atr",
        "exit.reaction_plan[].k_atr",
        "exit.reaction_plan[].arm_trigger_r",
        "exit.reaction_plan[].trail_frac",
    }
)


# ---------------------------------------------------------------------------
# Schema enumeration
# ---------------------------------------------------------------------------


def _resolve(schema: Mapping[str, Any], node: Mapping[str, Any]) -> Mapping[str, Any]:
    if "$ref" in node:
        return schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def _branches(schema: Mapping[str, Any], node: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """The node itself, or every non-null alternative of a union."""
    node = _resolve(schema, node)
    alternatives = node.get("anyOf", []) + node.get("oneOf", [])
    if not alternatives:
        yield node
        return
    for alternative in alternatives:
        resolved = _resolve(schema, alternative)
        if resolved.get("type") != "null":
            yield resolved


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def required_paths(schema: Mapping[str, Any]) -> frozenset[str]:
    """Convention A: descend ONLY through properties listed in ``required``;
    an array is descended as ``[]``. This is the convention under which the
    spec's count of sixteen holds; no anyOf/oneOf node is reachable this way,
    because the only unions sit under the optional ``exit``."""
    found: set[str] = set()

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        for branch in _branches(schema, node):
            if branch.get("type") == "array":
                walk(branch["items"], prefix + "[]")
                continue
            for key in branch.get("required", []):
                path = _join(prefix, key)
                found.add(path)
                walk(branch["properties"][key], path)

    walk(schema, "")
    return frozenset(found)


def required_names_everywhere(schema: Mapping[str, Any]) -> frozenset[str]:
    """Convention B: descend every property; collect a path when its name is
    in its parent's ``required`` list."""
    found: set[str] = set()

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        for branch in _branches(schema, node):
            if branch.get("type") == "array":
                walk(branch["items"], prefix + "[]")
                continue
            required = set(branch.get("required", []))
            for key, child in branch.get("properties", {}).items():
                path = _join(prefix, key)
                if key in required:
                    found.add(path)
                walk(child, path)

    walk(schema, "")
    return frozenset(found)


def all_paths(schema: Mapping[str, Any]) -> frozenset[str]:
    found: set[str] = set()

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        for branch in _branches(schema, node):
            if branch.get("type") == "array":
                walk(branch["items"], prefix + "[]")
                continue
            for key, child in branch.get("properties", {}).items():
                path = _join(prefix, key)
                found.add(path)
                walk(child, path)

    walk(schema, "")
    return frozenset(found)


def container_paths(schema: Mapping[str, Any]) -> frozenset[str]:
    """Every NAMED object or array node. An array's item object has no name of
    its own (``document_paths`` never yields ``a[]``), so it is not one."""
    found: set[str] = set()

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        for branch in _branches(schema, node):
            if branch.get("type") == "array":
                walk(branch["items"], prefix + "[]")
                continue
            for key, child in branch.get("properties", {}).items():
                path = _join(prefix, key)
                if any(
                    b.get("type") == "array" or "properties" in b for b in _branches(schema, child)
                ):
                    found.add(path)
                walk(child, path)

    walk(schema, "")
    return frozenset(found)


# ---------------------------------------------------------------------------
# Spec tables
# ---------------------------------------------------------------------------

_BACKTICK = re.compile(r"`([^`]+)`")
_CLASS_WORDS = ("container", "translated", "out of scope", "interpreted")


@dataclasses.dataclass(frozen=True)
class _SpecRow:
    paths: frozenset[str]
    classes: frozenset[str]
    gate4: bool


def _section_4_3_1() -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("### 4.3.1")
    end = text.index("### 4.4", start)
    return text[start:end]


def spec_tables() -> dict[str, list[_SpecRow]]:
    """The markdown tables of section 4.3.1, keyed by their first header cell."""
    tables: dict[str, list[_SpecRow]] = {}
    header: str | None = None
    for line in _section_4_3_1().splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            header = cells[0].lower()
            tables.setdefault(header, [])
            continue
        if set(cells[0]) <= {"-", " "}:
            continue
        class_text = cells[1].lower()
        classes = frozenset(word for word in _CLASS_WORDS if word in class_text)
        if not classes:
            # A row whose class column names no class would classify nothing
            # and silently pass every equality below; refuse to parse it.
            raise ValueError(f"section 4.3.1 row with no class word in column 2: {line!r}")
        tables[header].append(
            _SpecRow(
                paths=frozenset(_BACKTICK.findall(cells[0])),
                classes=classes,
                gate4="gate 4" in class_text,
            )
        )
    return tables


def spec_paths_classed(word: str, *, gate4: bool | None = None) -> frozenset[str]:
    found: set[str] = set()
    for rows in spec_tables().values():
        for row in rows:
            if word in row.classes and (gate4 is None or row.gate4 == gate4):
                found.update(row.paths)
    return frozenset(found)


def expected_interpreted() -> frozenset[str]:
    """The paths the interpreter must prove by reading: every row the spec
    classes as interpreted, minus the two gate 4 proves by refusing."""
    return spec_paths_classed("interpreted", gate4=False) - frozenset(REFUSED_BY_DOOR)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _completed(document: Mapping[str, Any]) -> dict[str, Any]:
    completed = copy.deepcopy(dict(document))
    completed["intent_id"] = SENTINEL_INTENT_ID
    completed["meta"] = {
        **completed["meta"],
        "armed_ts": SENTINEL_ARMED_TS,
        "trade_date": STATED_TRADE_DATE,
    }
    return completed


def _example(name: str) -> dict[str, Any]:
    return _completed(json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8")))


def _refusal(exc: PathUnclassifiedError) -> tuple[str, list[str]]:
    failure = exc.failure
    assert failure.retryable is False, "a refused document is never retryable"
    return failure.code, list(failure.details["paths"])


class SchemaCoverageTest(unittest.TestCase):
    """The schema-coverage check of the step-1 plan, promoted to a test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.input_schema = generate_schema("input")
        cls.stored_schema = generate_schema("stored")

    def test_the_required_paths_are_the_sixteen_of_the_spec_table(self) -> None:
        rows = spec_tables()["required path"]
        self.assertGreaterEqual(len(rows), 16, "the coverage table was not parsed")
        spec_required = frozenset().union(*(row.paths for row in rows))
        self.assertEqual(required_paths(self.input_schema), spec_required)
        self.assertEqual(len(spec_required), 16)
        # The other convention, written down rather than rediscovered.
        self.assertEqual(
            required_names_everywhere(self.input_schema),
            spec_required | OPTIONAL_CONTAINER_REQUIRED_LEAVES,
        )

    def test_every_path_of_the_input_schema_is_classified(self) -> None:
        # Not only the required ones: the runtime gate sees every path an
        # author writes, and a pick copied with the README jq recipe carries
        # tags, a side, versions and an account. Red on 2026-09-25 naming
        # ``spec.entry_tiers[].tag``, ``spec.tp_tranches[].tag`` and
        # ``account_id`` until section 4.3.1 gave them a class.
        classified = (
            CONTAINERS
            | frozenset(TRANSLATED)
            | frozenset(OUT_OF_SCOPE)
            | frozenset(REFUSED_BY_DOOR)
            | expected_interpreted()
        )
        paths = all_paths(self.input_schema)
        self.assertEqual(len(paths), 37)
        self.assertEqual(sorted(paths - classified), [])

    def test_the_listed_classes_match_the_spec_tables(self) -> None:
        # Both directions: a row in the code with no spec row is red (the spec
        # sentence "adding a row is an edit to this section", enforced), and a
        # spec row the code dropped is red.
        tables = spec_tables()
        self.assertGreaterEqual(sum(len(rows) for rows in tables.values()), 30, "tables not parsed")
        self.assertEqual(frozenset(TRANSLATED), spec_paths_classed("translated"))
        self.assertEqual(frozenset(OUT_OF_SCOPE), spec_paths_classed("out of scope"))
        self.assertEqual(frozenset(REFUSED_BY_DOOR), spec_paths_classed("interpreted", gate4=True))
        self.assertEqual(CONTAINERS, spec_paths_classed("container"))

    def test_containers_equal_the_schema_object_and_array_nodes(self) -> None:
        self.assertEqual(CONTAINERS, container_paths(self.input_schema))

    def test_every_listed_path_exists_in_a_published_schema(self) -> None:
        # A misspelt row classifies nothing (the "rule that resolves to
        # nothing" precedent of the dependency gate).
        published = all_paths(self.input_schema) | all_paths(self.stored_schema)
        for name, listed in {
            "CONTAINERS": CONTAINERS,
            "TRANSLATED": frozenset(TRANSLATED),
            "OUT_OF_SCOPE": frozenset(OUT_OF_SCOPE),
            "REFUSED_BY_DOOR": frozenset(REFUSED_BY_DOOR),
        }.items():
            with self.subTest(name):
                self.assertEqual(sorted(listed - published), [])

    def test_interpreted_is_disjoint_from_the_listed_classes(self) -> None:
        # ``instrument.mic`` sits in two LISTED classes by the spec's own row;
        # an interpreted path in a listed class would have two owners.
        listed = frozenset(TRANSLATED) | frozenset(OUT_OF_SCOPE) | CONTAINERS
        self.assertEqual(sorted(expected_interpreted() & listed), [])
        self.assertEqual(sorted(frozenset(REFUSED_BY_DOOR) & listed), [])


class RefusedByDoorReachabilityTest(unittest.TestCase):
    """Each ``REFUSED_BY_DOOR`` row names a refusal ``validate_intent`` still
    RAISES — membership of the reason in the vocabulary would stay green after
    the refusal was lifted, so the test builds the document and runs the gate.
    Lifting a refusal makes this red, and the row must then leave the list.
    """

    def _decoded(self) -> Any:
        return intent_from_jsonable(_example("pullback-two-tiers"))

    def test_the_completed_example_passes_the_door_gate(self) -> None:
        self.assertIsNone(validate_intent(self._decoded()))  # positive control

    def test_each_row_names_a_registered_reason(self) -> None:
        for path, reason in REFUSED_BY_DOOR.items():
            with self.subTest(path):
                self.assertIn(reason, INTENT_INVALID_REASONS)

    def test_a_short_side_is_still_refused(self) -> None:
        intent = self._decoded()
        short = dataclasses.replace(intent, spec=dataclasses.replace(intent.spec, side="short"))
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(short)
        self.assertEqual(ctx.exception.failure.details["reason"], REFUSED_BY_DOOR["spec.side"])

    def test_a_ceiling_price_is_still_refused(self) -> None:
        intent = self._decoded()
        capped = dataclasses.replace(
            intent,
            exit=ExitGeometrySpec(
                initial_levels=InitialLevels(stop=65.0, tp=85.0),
                reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0, ceiling_price=140.0),),
            ),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(capped)
        self.assertEqual(
            ctx.exception.failure.details["reason"],
            REFUSED_BY_DOOR["exit.reaction_plan[].ceiling_price"],
        )


class GateTest(unittest.TestCase):
    def test_the_published_examples_pass_the_gate_when_every_interpreted_path_is_read(self) -> None:
        # The positive control of spec section 6.3, run on COMPLETED examples.
        for name in ("pullback-two-tiers", "pullback-trailing-stop", "immediate-plus-pullback"):
            with self.subTest(name):
                self.assertEqual(unclassified_paths(_example(name), expected_interpreted()), ())
                self.assertIsNone(check_classified(_example(name), expected_interpreted()))

    def test_a_maximal_document_passes_and_each_listed_row_carries_weight(self) -> None:
        document = _completed(MAXIMAL_DOCUMENT)
        input_paths = all_paths(generate_schema("input"))
        self.assertEqual(document_paths(document), input_paths | {"intent_id", "meta.armed_ts"})
        self.assertEqual(unclassified_paths(document, expected_interpreted()), ())
        # Delete each listed row in turn: the gate must name exactly that path.
        for name, listed in (
            ("TRANSLATED", TRANSLATED),
            ("OUT_OF_SCOPE", OUT_OF_SCOPE),
            ("REFUSED_BY_DOOR", REFUSED_BY_DOOR),
        ):
            for path in listed:
                with self.subTest(f"{name}:{path}"):
                    patches = {
                        other: MappingProxyType({k: v for k, v in table.items() if k != path})
                        for other, table in (
                            ("TRANSLATED", TRANSLATED),
                            ("OUT_OF_SCOPE", OUT_OF_SCOPE),
                            ("REFUSED_BY_DOOR", REFUSED_BY_DOOR),
                        )
                    }
                    with mock.patch.multiple(classification, **patches):
                        self.assertEqual(
                            unclassified_paths(document, expected_interpreted()), (path,)
                        )

    def test_an_unread_unclassified_path_is_refused_and_named(self) -> None:
        document = _example("pullback-two-tiers")
        document["spec"]["foo"] = 1
        document["meta"]["bar"] = None
        with self.assertRaises(PathUnclassifiedError) as ctx:
            check_classified(document, expected_interpreted())
        self.assertEqual(
            _refusal(ctx.exception), (PATH_UNCLASSIFIED_CODE, ["meta.bar", "spec.foo"])
        )

    def test_an_unread_optional_path_is_refused(self) -> None:
        # Gate arithmetic on a hand-built ``read``: an interpreted path the
        # interpreter did not read is named. What PR 5 must supply is the
        # read set; this shows what happens when it does not.
        document = _example("pullback-trailing-stop")
        read = expected_interpreted() - {"exit.reaction_plan[].trail_frac"}
        self.assertEqual(unclassified_paths(document, read), ("exit.reaction_plan[].trail_frac",))

    def test_a_read_path_needs_no_class(self) -> None:
        document = _example("pullback-two-tiers")
        document["spec"]["foo"] = 1
        self.assertEqual(unclassified_paths(document, expected_interpreted() | {"spec.foo"}), ())


class DocumentPathsTest(unittest.TestCase):
    def test_document_paths_normalises_lists_and_keeps_null_leaves(self) -> None:
        cases = {
            "nested list items": ({"a": [{"b": 1}, {"c": None}]}, {"a", "a[].b", "a[].c"}),
            "null container": ({"exit": None}, {"exit"}),
            "empty list": ({"spec": {"tp_tranches": []}}, {"spec", "spec.tp_tranches"}),
            "scalar list": ({"xs": [1, 2]}, {"xs"}),
        }
        for label, (document, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(document_paths(document), frozenset(expected))


if __name__ == "__main__":
    unittest.main()
