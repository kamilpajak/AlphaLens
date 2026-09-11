"""Generate the published JSON Schema for :class:`TradeIntent` (#1405).

The contract has lived as dataclasses plus prose in the package README, which a
third-party producer cannot read mechanically and which nothing turns red when
the types move. This module walks ``schema.py`` and emits the machine-readable
form; the artefact it produces is committed next door under ``docs/`` and a CI
gate fails when the two disagree. Hand-writing that file was the alternative and
was rejected for the obvious reason: it drifts on the first new field, silently.

**What the schema is.** A description of the DOOR — what a producer must put on
the wire for the contract to accept it, checked BEFORE decoding. It is NOT a
description of the codec's input language: the codec still migrates the
pre-#1290 ``meta.brief_date`` key, and 51 of the 63 documents in the live
journals (2026-09-11) carry it. Those reach the daemon through the journal
drain, which is a different entry point and is not schema-gated.

**What the schema is not.** A verdict. It pins shape and closed vocabularies and
nothing else: a schema-valid document can still be refused by
``validate_intent`` (allocations that do not sum to 100, a stop above the entry
ladder, a NaN). One rule, one owner — mirroring those bounds here would give
each of them two owners and let the two drift. The single exception is
``IntentMeta.generation``'s ``minimum: 1``, which mirrors a CODEC rule (#1371)
rather than a validator one: without it the schema would hand a client a green
light the door then refuses, which is the one direction of divergence that
breaks the schema's promise.

Stdlib only, like the rest of the package (``dependencies = []`` on purpose) —
the ``jsonschema`` library appears in the tests, never here.

Regenerate the artefact with::

    python -m broker_contract.trade_intent.json_schema --write
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
import typing
from pathlib import Path
from typing import Any, Final

from broker_contract.trade_intent.schema import SCHEMA_VERSION, TradeIntent

__all__ = [
    "SCHEMA_FILENAME",
    "artefact_path",
    "generate_schema",
    "render_schema",
]

SCHEMA_FILENAME: Final = f"trade-intent-v{SCHEMA_VERSION}.schema.json"

# A URN rather than a URL: nothing serves this document over HTTP, and an $id
# that looks fetchable and is not would be a promise the project cannot keep.
SCHEMA_ID: Final = f"urn:alphalens:contract:trade-intent:{SCHEMA_VERSION}"

_JSON_TYPES: Final[dict[type, str]] = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
}


def artefact_path() -> Path:
    """Where the committed schema lives: inside the package it describes.

    Beside the package rather than at the repo root, so the artefact travels
    with ``broker_contract`` if it is ever extracted as a standalone
    distribution — the reason this package carries no dependencies at all.
    """
    return Path(__file__).resolve().parents[2] / "docs" / SCHEMA_FILENAME


def _reference(cls: type) -> dict[str, Any]:
    return {"$ref": f"#/$defs/{cls.__name__}"}


def _nullable(node: dict[str, Any]) -> dict[str, Any]:
    """Admit null beside an existing node, however that node is expressed."""
    if "type" in node and isinstance(node["type"], str):
        return {**node, "type": [node["type"], "null"]}
    return {"anyOf": [node, {"type": "null"}]}


def _literal_node(annotation: Any) -> dict[str, Any]:
    values = list(typing.get_args(annotation))
    kinds = {_JSON_TYPES[type(value)] for value in values}
    node: dict[str, Any] = {"enum": values}
    if len(kinds) == 1:
        node = {"type": kinds.pop(), **node}
    return node


def _union_node(annotation: Any, definitions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    arguments = typing.get_args(annotation)
    members = [argument for argument in arguments if argument is not type(None)]
    nullable = len(members) != len(arguments)
    if len(members) == 1:
        node = _node(members[0], definitions)
        return _nullable(node) if nullable else node
    # A discriminated union (today: the reaction-plan primitives). Every branch
    # must REQUIRE its tag, or a branch whose fields all carry defaults matches
    # an empty object: measured 2026-09-11, `[{}]` passed the schema as a
    # ModelPush while the codec refused it for an unknown kind.
    for member in members:
        _define(member, definitions)
        _require_discriminator(definitions[member.__name__])
    node = {"oneOf": [_reference(member) for member in members]}
    return _nullable(node) if nullable else node


def _require_discriminator(definition: dict[str, Any]) -> None:
    tags = sorted(
        name
        for name, body in definition.get("properties", {}).items()
        if len(body.get("enum", ())) == 1
    )
    if tags:
        definition["required"] = sorted({*definition.get("required", ()), *tags})


def _node(annotation: Any, definitions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    origin = typing.get_origin(annotation)
    if annotation in _JSON_TYPES:
        return {"type": _JSON_TYPES[annotation]}
    if origin is typing.Literal:
        return _literal_node(annotation)
    if origin in (types.UnionType, typing.Union):
        return _union_node(annotation, definitions)
    if origin is tuple:
        return {"type": "array", "items": _node(typing.get_args(annotation)[0], definitions)}
    if dataclasses.is_dataclass(annotation) and isinstance(annotation, type):
        _define(annotation, definitions)
        return _reference(annotation)
    raise TypeError(f"no JSON Schema mapping for {annotation!r}")


def _property_node(
    field: dataclasses.Field[Any],
    annotation: Any,
    definitions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    node = _node(annotation, definitions)
    meaning = field.metadata.get("meaning")
    if meaning:
        node["description"] = str(meaning)
    node.update(field.metadata.get("json_schema") or {})
    if field.default is not dataclasses.MISSING:
        node["default"] = _default_value(field.default)
    elif field.default_factory is not dataclasses.MISSING:  # pragma: no cover - none today
        node["default"] = _default_value(field.default_factory())
    return node


def _default_value(value: Any) -> Any:
    if isinstance(value, tuple | list):
        return [_default_value(item) for item in value]
    return value


def _define(cls: type, definitions: dict[str, dict[str, Any]]) -> None:
    if cls.__name__ in definitions:
        return
    definitions[cls.__name__] = {}  # placeholder: breaks reference cycles
    hints = typing.get_type_hints(cls)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in dataclasses.fields(cls):
        properties[field.name] = _property_node(field, hints[field.name], definitions)
        if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            required.append(field.name)
    definition: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        definition["required"] = required
    definitions[cls.__name__] = definition


def generate_schema() -> dict[str, Any]:
    """Build the schema for :class:`TradeIntent` from the dataclasses."""
    definitions: dict[str, dict[str, Any]] = {}
    _define(TradeIntent, definitions)
    root = definitions.pop(TradeIntent.__name__)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "TradeIntent",
        "description": (
            "One client-armed pick handed to the broker-manager. Generated from "
            "broker_contract.trade_intent.schema — edit the dataclasses, never this file. "
            "The schema pins SHAPE; a schema-valid document can still be refused by the "
            "door's semantic rules (see intent_invalid in the package README). Within a "
            "major version only optional fields are added: a field is never renamed, "
            "retyped, or given a new unit. NOTE: this schema does not constrain "
            "schema_version and neither does the codec — the DOOR does (broker "
            "arm-intent refuses a stated version other than its own), while the journal "
            "drain stays ungated so older documents keep decoding there."
        ),
        **root,
        "$defs": definitions,
    }


def render_schema() -> str:
    """The exact bytes of the committed artefact."""
    return json.dumps(generate_schema(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in ([], ["--write"]):
        print(f"usage: python -m {__spec__.name} [--write]", file=sys.stderr)
        return 2
    if arguments == ["--write"]:
        path = artefact_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_schema())
        print(f"wrote {path}", file=sys.stderr)
        return 0
    print(render_schema(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
