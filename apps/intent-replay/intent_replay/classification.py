"""The path classes of spec section 4.3.1, and the gate that refuses a document
carrying a path in none of them.

Every path of a document belongs to exactly one of three classes, and the
classification is part of the DESIGN, not of a caller's input:

* **interpreted** — the interpreter reads it and its value changes what the
  replay does. NOT listed here: the interpreter proves that class at runtime
  by actually reading the path, and hands the gate the set of paths it read.
  Two interpreted paths are proved by gate 4 (``validate_intent``) refusing on
  them rather than by a read, and ``validate_intent`` reports nothing about
  what it examined, so those two are written down in :data:`REFUSED_BY_DOOR`.
  Each row there is pinned by a test that RUNS the refusal; lifting one makes
  that test red, and the row must leave.
* **translated** — the client resolves it (a calendar, a fee card, a fill) and
  states the result in the run configuration, which carries the rule as well
  as the value. Listed in :data:`TRANSLATED`.
* **out of scope** — a queue or deployment concern, an identity, or a label
  the envelope may echo but nothing reads. Listed in :data:`OUT_OF_SCOPE`.

The gate's universe is the COMPLETED wire document (a mapping, after the
adapter fills the section 6.4 sentinels), not the decoded dataclass: it is the
door's round-trip gate applied one level deeper, and the round trip counts wire
keys. A path is present when its key is present, whatever its value. List items
share one path (``spec.entry_tiers[].limit_price``); an array's item object has
no path of its own.

A container path (:data:`CONTAINERS`) counts as classified whether its value is
an object, a list, an empty list or ``null``: the gate asks "is this path
classified", not "was it read". The interpreter's handling of ``exit: null`` or
an empty take-profit ladder is proved by the walk properties of section 6.3,
not by this gate.

The three listed sets mirror the tables of section 4.3.1 and are pinned to them
in both directions by a test, so a row cannot appear here without an edit to
the spec — which is the property the section promises: "the set of things this
tool quietly does not honour cannot grow without someone writing it down".

ENGINE module: stdlib and ``broker_contract`` only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, Final

from broker_contract.failure import ContractError

from intent_replay.refusal import refuse

__all__ = [
    "CONTAINERS",
    "OUT_OF_SCOPE",
    "PATH_UNCLASSIFIED_CODE",
    "REFUSED_BY_DOOR",
    "TRANSLATED",
    "PathUnclassifiedError",
    "check_classified",
    "document_paths",
    "unclassified_paths",
]

PATH_UNCLASSIFIED_CODE: Final = "path_unclassified"


class PathUnclassifiedError(ContractError):
    """The document carries a path in none of the three classes; nothing was computed."""


# Every named object or array node of the input schema. A hand list, pinned
# equal to the schema-derived set by a test so it cannot drift.
CONTAINERS: Final[frozenset[str]] = frozenset(
    {
        "instrument",
        "spec",
        "spec.size",
        "spec.entry_tiers",
        "spec.tp_tranches",
        "meta",
        "exit",
        "exit.initial_levels",
        "exit.reaction_plan",
    }
)

# path -> the configuration key that carries its resolution.
TRANSLATED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "instrument.mic": "walk_start, entry_deadline (the venue's calendar)",
        "spec.order_ttl_days": "entry_deadline",
        "meta.source": "walk_start",
        "meta.trade_date": "walk_start",
    }
)

# path -> why the replay deliberately ignores it.
OUT_OF_SCOPE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "instrument.ticker": "identity; the walk is over the bars it is handed",
        "instrument.mic": "fee card and settlement currency; costs are a non-goal",
        "spec.size.currency": "a label on the cash answer",
        "spec.schema_version": "the door is the only gate that reads a version",
        "meta.schema_version": "the door is the only gate that reads a version",
        "meta.generation": "a queue concern",
        "meta.armed_ts": "an identity concern; the adapter fills a sentinel",
        "intent_id": "an identity concern; the adapter fills a sentinel",
        "spec.entry_tiers[].tag": "a rung label with no sizing semantics",
        "spec.tp_tranches[].tag": "a tranche label with no sizing semantics",
        "account_id": "a reserved tenant dimension; a deployment concern",
    }
)

# path -> the ``INTENT_INVALID_REASONS`` key gate 4 raises on it.
REFUSED_BY_DOOR: Final[Mapping[str, str]] = MappingProxyType(
    {
        "spec.side": "side_not_long",
        "exit.reaction_plan[].ceiling_price": "ceiling_price_unsupported",
    }
)


def document_paths(document: Mapping[str, Any]) -> frozenset[str]:
    """Every key path present in ``document``, containers included.

    List items are folded onto one path with ``[]``; a null value still yields
    its path; an empty list yields only its own path.
    """
    found: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                found.add(path)
                walk(value, path)
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix + "[]")

    walk(document, "")
    return frozenset(found)


def unclassified_paths(document: Mapping[str, Any], read: Iterable[str]) -> tuple[str, ...]:
    """The paths present in ``document`` that no class covers, sorted.

    ``read`` is the set of paths the interpreter proved by reading; the three
    listed classes and the gate-4 rows are subtracted alongside it.
    """
    classified = CONTAINERS | set(TRANSLATED) | set(OUT_OF_SCOPE) | set(REFUSED_BY_DOOR) | set(read)
    return tuple(sorted(document_paths(document) - classified))


def check_classified(document: Mapping[str, Any], read: Iterable[str]) -> None:
    """Refuse ``document`` unless every path it carries is classified (spec section 5.4)."""
    paths = unclassified_paths(document, read)
    if paths:
        raise refuse(
            PathUnclassifiedError,
            PATH_UNCLASSIFIED_CODE,
            f"the document carries {len(paths)} path(s) this tool does not classify: "
            + ", ".join(paths),
            paths=list(paths),
        )
