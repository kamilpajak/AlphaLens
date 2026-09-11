"""What this contract still carries for history's sake, and when it can go.

A compatibility shim is written for a reason that expires, and the expiry is
never noticed. Two live in this package right now: the codec migrates a journal
key renamed in #1252, and a dataclass field has been refused outright since
#1236. Both are correct today. The failure mode is a year from now, when neither
the reason nor the exit condition is recoverable from the code, so nobody dares
delete either one.

Each allowance therefore records three things — what it is, why it is still
here, and the OBSERVATION that retires it. Not a date: a date is a wish, and a
wish cannot be checked. ``still_needed`` is that observation as a predicate over
one wire document, so "can I delete this yet" is a command
(``tests/trade_intent/test_legacy_register.py``, opt-in on a real journal)
rather than a judgement call.

Code sites carry ``# LEGACY(<key>)``. The marker is what turns removal into a
grep, and a bidirectional gate keeps the two halves honest: a marker with no
entry is red, and an entry with no marker is red.

Stdlib only, like the rest of the package (``dependencies = []`` on purpose).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "LEGACY_ALLOWANCES",
    "MARKER_PREFIX",
    "LegacyAllowance",
]

# The literal that precedes an allowance key at a code site. Held here so the
# gate builds its pattern from the same string the sites use, and so this file
# is the only one that spells it out.
MARKER_PREFIX: Final = "# LEGACY("


@dataclass(frozen=True, slots=True)
class LegacyAllowance:
    """One thing kept for backwards reasons, with its exit condition."""

    what: str
    why: str
    retires_when: str
    still_needed: Callable[[Mapping[str, Any]], bool]
    """Does THIS wire document still rely on the allowance? The census counts it."""

    def __post_init__(self) -> None:
        for name in ("what", "why", "retires_when"):
            if not str(getattr(self, name)).strip():
                raise ValueError(
                    f"a legacy allowance must state its {name!r} — a blank one satisfies "
                    "the anti-rot gate while telling a later reader nothing"
                )


def _carries_brief_date(document: Mapping[str, Any]) -> bool:
    meta = document.get("meta")
    return isinstance(meta, Mapping) and "brief_date" in meta


def _carries_reanchor_ceiling(document: Mapping[str, Any]) -> bool:
    exit_spec = document.get("exit")
    if not isinstance(exit_spec, Mapping):
        return False
    plan = exit_spec.get("reaction_plan") or ()
    return any(
        isinstance(entry, Mapping) and entry.get("ceiling_price") is not None for entry in plan
    )


LEGACY_ALLOWANCES: Final[Mapping[str, LegacyAllowance]] = MappingProxyType(
    {
        "brief_date_key": LegacyAllowance(
            what="`intent_from_jsonable` renames a document's `meta.brief_date` to "
            "`meta.trade_date` while decoding.",
            why="#1252 renamed the key. Journals are append-only, so every line written "
            "before that rename still carries the old name and must keep decoding. The "
            "published JSON Schema deliberately does NOT know the old key: the drain "
            "reads history, the door does not (#1405).",
            retires_when="No line of any picks journal carries `meta.brief_date`. Since "
            "journals are never rewritten, in practice this means the oldest line still "
            "read is newer than #1252 — measured 51 of 63 on 2026-09-11.",
            still_needed=_carries_brief_date,
        ),
        "reanchor_ceiling_price": LegacyAllowance(
            what="`ReanchorOnFill.ceiling_price` — the field on the dataclass, its node in "
            "the published schema, and the `ceiling_price_unsupported` rule that refuses "
            "any non-null value.",
            why="Every brief-armed document written before #1236 carries it. The field "
            "caps a take-profit, which is a placement instruction this contract does not "
            "carry, so `validate_intent` refuses it rather than discarding it silently. "
            "The current brief path stopped declaring it, but the refusal must outlive "
            "the producer that needed it.",
            retires_when="No line of any picks journal carries a non-null `ceiling_price` "
            "in `exit.reaction_plan` — measured 45 of 63 on 2026-09-11. Removing the field "
            "is a BREAKING schema change: it bumps `SCHEMA_VERSION`.",
            still_needed=_carries_reanchor_ceiling,
        ),
    }
)
