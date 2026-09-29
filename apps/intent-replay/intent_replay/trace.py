"""The trace: the ordered events a bar walk emits (spec section 4.6).

ENGINE module: stdlib only.

Section 4.6 publishes SEVEN kinds and the result envelope carries them, so
:data:`KINDS` is a contract rather than an implementation detail. An eighth
kind would be an edit of a LOCKED spec, which is why three facts this walk
knows get no event of their own:

* a tranche the cost gate refused (the reader sees a touched level and no
  ``tp_fired``);
* a rung SKIPPED because the stop has moved above it (section 4.4);
* the quantity change behind ``tp-tranche-resize``, which section 4.6 lists as
  a ``stop_moved`` reason: firing a tranche patches the stop to the highest
  floor it has earned, so the LEVEL does not move and only the size does.

Each is a known limitation recorded with the change, not a silent omission.

``position_closed`` carries the sale for every reason EXCEPT ``tp_complete``.
On that reason the cash is the last ``tp_fired`` and this event is a marker
carrying zero units. The asymmetry is forced by the seven kinds: there is no
``stop_filled``, so a stop-out and a time-stop have nowhere else to report
what they sold, while a take-profit sweep has already reported it. Anything
summing cash out of the trace has to know which, so it is stated here instead
of being inferred from the field values.

Every reason vocabulary is closed and checked at construction — the shape
``bars.BARS_REASONS`` already uses. A reason outside its mapping is a
programming error, never a shipped event.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, ClassVar, Final

__all__ = [
    "CLOSE_REASONS",
    "EVENT_TYPES",
    "EXPIRY_REASONS",
    "KINDS",
    "LADDERS",
    "OUTCOMES",
    "STOP_MOVE_REASONS",
    "EntryExpired",
    "EntryFilled",
    "HorizonOpen",
    "PositionClosed",
    "StopMoved",
    "StopPlaced",
    "TpFired",
    "TraceEvent",
    "to_jsonable",
]

EXPIRY_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "deadline": (
            "The entry deadline passed with rungs unfilled. It is the ONLY cause: a rung "
            "below a stop that has moved is skipped on every such bar, never expired."
        ),
    }
)

STOP_MOVE_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "trail": "The trailing arm moved the stop, clamped on the observed price and ratcheted.",
        "reanchor-on-fill": "The re-anchor arm moved the stop to a new average fill's target.",
    }
)

CLOSE_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "stop": "The bar's low reached the resting stop.",
        "tp_complete": "The take-profit ladder sold the whole position; the cash is the last tp_fired.",
        "time_stop": "The stated time stop was reached with the position still open.",
    }
)

# The summary vocabularies the envelope publishes beside the trace, closed for
# the reason the reason mappings are: a value outside them is a word this tool
# never answers with.
OUTCOMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "no_fill": "No rung ever filled.",
        "closed_stop": "The position closed on the resting stop.",
        "closed_tp": "The take-profit ladder closed the position.",
        "closed_time_stop": "The stated time stop closed the position.",
        "open": "The bars ran out with the position still open.",
    }
)

LADDERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "initial_levels": "One tranche of 100% at exit.initial_levels.tp (spec section 5.1).",
        "tp_tranches": "The author's own spec.tp_tranches ladder.",
    }
)


def _check(value: str, vocabulary: Mapping[str, str], kind: str, field: str) -> None:
    """Refuse a word outside a published vocabulary, at construction."""
    if value not in vocabulary:
        raise ValueError(f"unregistered {kind} {field}: {value!r}")


@dataclass(frozen=True, slots=True)
class EntryFilled:
    """One rung filled. ``cash`` is ``units * price``, carried rather than
    recomputed so a reader never re-derives it at a different precision."""

    kind: ClassVar[str] = "entry_filled"
    t: int
    tier_index: int
    price: float
    units: float
    cash: float


@dataclass(frozen=True, slots=True)
class EntryExpired:
    """Every rung still unfilled when the deadline passed, in one event."""

    kind: ClassVar[str] = "entry_expired"
    t: int
    tiers: tuple[int, ...]
    reason: str

    def __post_init__(self) -> None:
        _check(self.reason, EXPIRY_REASONS, self.kind, "reason")


@dataclass(frozen=True, slots=True)
class StopPlaced:
    """The disaster stop resting after the first fill."""

    kind: ClassVar[str] = "stop_placed"
    t: int
    level: float


@dataclass(frozen=True, slots=True)
class StopMoved:
    """A stop decision returned a level DIFFERENT from the resting one.

    ``after`` may be BELOW ``before``: the re-anchor arm clamps against the
    brief floor and not against the standing level, so a later and lower rung
    fill re-anchors lower (spec section 6.3).
    """

    kind: ClassVar[str] = "stop_moved"
    t: int
    reason: str
    before: float
    after: float

    def __post_init__(self) -> None:
        _check(self.reason, STOP_MOVE_REASONS, self.kind, "reason")


@dataclass(frozen=True, slots=True)
class TpFired:
    """One take-profit tranche realised. ``ladder`` names which ladder the run
    used, because section 5.1 asks the TRACE to say so."""

    kind: ClassVar[str] = "tp_fired"
    t: int
    tranche_index: int
    price: float
    units: float
    proceeds: float
    ladder: str

    def __post_init__(self) -> None:
        _check(self.ladder, LADDERS, self.kind, "ladder")


@dataclass(frozen=True, slots=True)
class PositionClosed:
    """The position reached zero. Carries the sale unless the reason is
    ``tp_complete`` (see the module docstring)."""

    kind: ClassVar[str] = "position_closed"
    t: int
    reason: str
    price: float
    units: float

    def __post_init__(self) -> None:
        _check(self.reason, CLOSE_REASONS, self.kind, "reason")


@dataclass(frozen=True, slots=True)
class HorizonOpen:
    """The bars ran out with the position still open.

    ``price`` is the CLOSE of the last bar the walk saw, and the units are
    valued at it (section 4.6). A mark is a valuation and not a fill, so it
    pays no fee, takes no slippage, and the cost gate is NOT consulted for it.
    """

    kind: ClassVar[str] = "horizon_open"
    t: int
    units: float
    price: float


TraceEvent = (
    EntryFilled | EntryExpired | StopPlaced | StopMoved | TpFired | PositionClosed | HorizonOpen
)

EVENT_TYPES: Final = (
    EntryFilled,
    EntryExpired,
    StopPlaced,
    StopMoved,
    TpFired,
    PositionClosed,
    HorizonOpen,
)

KINDS: Final[tuple[str, ...]] = tuple(cls.kind for cls in EVENT_TYPES)


def to_jsonable(event: TraceEvent) -> dict[str, Any]:
    """``t`` first, then ``kind``, then the event's own fields in declaration
    order. ONE function over ``dataclasses.fields`` rather than a method per
    kind, so a field added to any event cannot be forgotten in the rendering."""
    values = {field.name: getattr(event, field.name) for field in fields(event)}
    return {"t": values.pop("t"), "kind": event.kind, **values}
