"""The replay door: template completion, then the gates of spec section 4.3.

The replay refuses what the arming door refuses, so a document that could not
be armed is never given a number. The gates run in the door's order and raise
the door's reasons:

  0. derived fields   the author sent `intent_id`, `meta.armed_ts` or an
                      `r_multiple`, which a door computes (`derived_field_supplied`)
  1. the shape        the published INPUT JSON Schema, on the wire, on the
                      author's document (`schema_violation`)
  2. completion       section 6.4: two sentinels, and a stated `meta.trade_date`
                      normalised the way the door normalises it
                      (`trade_date_required`, `trade_date_malformed`)
  3. the codec        (`undecodable`)
  4. the fixed point  every key the author sent survives decode + re-render
                      (`key_discarded`); a non-canonical date spelling lands here
  5. `validate_intent`  the document is coherent (`intent_invalid`, raised by the
                      contract as :class:`IntentInvalidError` and passed through)

Not checked, and stated here rather than left to be inferred: the venue, the
pick key and the generation (section 4.3 — queue and deployment concerns), and
`meta.schema_version` (section 4.3.1 classes both version paths out of scope:
the codec and `validate_intent` are version-blind on purpose, and so is this
door; a document of a later version that carries something this contract
cannot model is refused by the fixed point instead).

The derived-field check refuses the PRESENCE of a field this module fills; it
reads no value, so it is not an interpretation of a path section 4.3.1 classes
out of scope.

Completion supplies exactly what section 6.4 lists: a fixed `intent_id` and a
fixed `meta.armed_ts` (out of scope, any value is as good as any other), and
nothing else. `meta.trade_date` is stated by the author: the arming door fills
it from its clock and its venue calendar, and this leaf has neither, so a
missing date is a refusal that names the one-line edit. Tags, `generation` and
`r_multiple` are not filled; the gate of section 4.3.1 runs over the completed
document and a filled tag would be a path the author did not write.

This module raises :class:`DoorRefusalError` with a REASON and never a code.
`intent_malformed` is a CLI-owned code (section 5.4), and the CLI maps the
reason onto it.

ADAPTER module: stdlib, ``broker_contract`` and ``jsonschema`` (gate 1).
"""

from __future__ import annotations

import copy
import datetime as dt
import functools
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

import jsonschema
from broker_contract.trade_intent.codec import (
    TradeIntentDecodeError,
    discarded_paths,
    intent_from_jsonable,
    intent_to_jsonable,
    supplied_derived_paths,
)
from broker_contract.trade_intent.json_schema import generate_schema
from broker_contract.trade_intent.schema import TradeIntent
from broker_contract.trade_intent.validate import validate_intent

__all__ = [
    "DOOR_REASONS",
    "SENTINEL_ARMED_TS",
    "SENTINEL_INTENT_ID",
    "Admitted",
    "DoorRefusalError",
    "admit",
    "complete",
]

SENTINEL_INTENT_ID: Final = "REPLAY"
SENTINEL_ARMED_TS: Final = "1970-01-01T00:00:00+00:00"

DOOR_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "derived_field_supplied": "the document carries `intent_id`, `meta.armed_ts` or a "
        "`r_multiple`, which a door computes; `details.paths` lists them",
        "schema_violation": "the document fails the published input JSON Schema; "
        "`details.path` locates it",
        "trade_date_required": "no `meta.trade_date`: the replay has no clock and no calendar "
        "to fill it from, so the author states it",
        "trade_date_malformed": "`meta.trade_date` is not a date",
        "undecodable": "the shape passes but the decoder refuses it (e.g. `generation: 1.0`)",
        "key_discarded": "a key the decoder would DROP, so the replay would not carry what "
        "was sent; `details.paths` lists them",
    }
)


class DoorRefusalError(Exception):
    """A document the replay will not admit.

    ``reason`` is one of :data:`DOOR_REASONS`; a reason outside it is a
    programming error surfaced at construction, never a published refusal.
    """

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        if reason not in DOOR_REASONS:
            raise ValueError(f"unregistered door reason: {reason!r}")
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details: Mapping[str, Any] = details


@dataclass(frozen=True, slots=True)
class Admitted:
    """What the door lets through: the decoded intent and the completed wire
    document, which is the universe of the classification gate (4.3.1)."""

    intent: TradeIntent
    document: Mapping[str, Any]


def complete(document: Mapping[str, Any]) -> dict[str, Any]:
    """The author's document with the two section 6.4 sentinels in place and
    its stated ``meta.trade_date`` in canonical form. The input is left untouched."""
    meta = document.get("meta")
    if not isinstance(meta, Mapping) or "trade_date" not in meta:
        raise DoorRefusalError(
            "trade_date_required",
            "meta.trade_date is not stated - the replay has no clock to fill it from; "
            'add it to the document: "meta": {"source": "manual", "trade_date": "YYYY-MM-DD"}',
        )
    raw = meta["trade_date"]
    try:
        parsed = dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise DoorRefusalError(
            "trade_date_malformed", f"meta.trade_date {raw!r} is not a YYYY-MM-DD date", value=raw
        ) from exc
    completed = copy.deepcopy(dict(document))
    completed["intent_id"] = SENTINEL_INTENT_ID
    completed["meta"] = {
        **completed["meta"],
        "armed_ts": SENTINEL_ARMED_TS,
        "trade_date": parsed.isoformat(),
    }
    return completed


@functools.cache
def _validator() -> jsonschema.Draft202012Validator:
    # Generated in-process, as the arming door does; CI pins that the committed
    # artefact equals what this call produces.
    return jsonschema.Draft202012Validator(generate_schema("input"))


def _refuse_derived_fields(document: Any) -> None:
    paths = supplied_derived_paths(document)
    if paths:
        raise DoorRefusalError(
            "derived_field_supplied",
            f"{', '.join(paths)} - a door computes these; remove them from the document",
            paths=paths,
        )


def _assert_published_shape(document: Any) -> None:
    errors = sorted(_validator().iter_errors(document), key=lambda error: error.json_path)
    if errors:
        first = errors[0]
        raise DoorRefusalError(
            "schema_violation", f"{first.json_path}: {first.message}", path=first.json_path
        )


def _decoded(completed: Mapping[str, Any]) -> TradeIntent:
    try:
        return intent_from_jsonable(completed)
    except TradeIntentDecodeError as exc:
        raise DoorRefusalError("undecodable", str(exc)) from exc


def _assert_fixed_point(document: Mapping[str, Any], intent: TradeIntent) -> None:
    discarded = discarded_paths(document, intent_to_jsonable(intent))
    if discarded:
        raise DoorRefusalError(
            "key_discarded",
            f"the decoder would discard {', '.join(discarded)} - the replay would not carry "
            "what was sent",
            paths=discarded,
        )


def admit(document: Any) -> Admitted:
    """Run the gates in the door's order and return what passed them.

    Raises :class:`DoorRefusalError` for gates 0-4 and lets the contract's
    :class:`~broker_contract.trade_intent.validate.IntentInvalidError` through
    from gate 5.
    """
    _refuse_derived_fields(document)
    _assert_published_shape(document)
    completed = complete(document)
    intent = _decoded(completed)
    _assert_fixed_point(document, intent)
    validate_intent(intent)
    return Admitted(intent=intent, document=completed)
