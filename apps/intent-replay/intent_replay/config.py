"""The STATED run configuration (spec sections 2.1, 4.1, 5.2 and 5.4).

Everything the replay needs is either read from the document or stated here by
the caller. Nothing reaches this module from a deployment drop-in, an
environment variable, a production constant or another module's default; the
allowed imports are pinned by a test, and no field of any class below carries
a default. A missing key is a refusal, never a default — a default that
happens to match today's deployment is the worst case, because it is correct
right up to the moment it quietly is not.

Two codes, one failure mode each, both with a closed reason vocabulary:

* ``config_incomplete`` — a required key the caller did NOT state
  (``missing_key``). ``details["keys"]`` lists every missing key, so the caller
  completes the block in one pass.
* ``config_invalid`` — a key the caller DID state, with a value nothing can
  use: the wrong type, a non-finite number, a non-positive distance or price, a
  negative cost, a unit other than the published one, an empty string, a key
  this block does not model, or ``oco: true``. Its own code rather than a
  reason under the first, on the same argument that gave ``bars_invalid`` its
  own code: a complete configuration can still carry a value nothing can use,
  and telling that caller "you did not state this" sends them to the wrong
  place.

Missing keys win. When keys are both missing and invalid, only the missing ones
are reported — every ``config_invalid`` violation, unknown keys included, is
held back until the block is complete, and the caller hears about values on
the next pass. A missing key is ONE fact, so no value rule runs on an absent
key.

``unknown_key`` is here because the contract's codec DROPS a key it does not
model with only a warning, which is why the arming door needs its fixed-point
gate: ``entry_trail_bp`` would otherwise switch entry trailing off in a run
whose author believes the distance was stated.

Three decisions taken with the PR 3 plan on 2026-09-25, each pinned by a test:

* ``entry_deadline`` is always the provenance object of section 5.1; a stated
  ``null`` is refused. Every decoded document carries ``spec.order_ttl_days``
  (the codec defaults it), so a null would translate a present path into
  nothing, with no ``formula`` a reader could check. A run with no deadline
  states one past its last bar and says so in ``formula``.
* ``oco`` is required and only ``false`` is accepted in v1. The walk models no
  OCO pair, and this block travels in the result, so an accepted ``true``
  would describe a policy the run did not apply.
* Epoch values carry no sign or size rule: a stated integer is accepted as
  stated (a ``walk_start`` the bars do not cover is refused downstream by
  ``window_too_short``), and an integer of any size is finite, so the finiteness
  check runs on floats only — ``math.isfinite`` on an int wider than a float
  raises rather than answers.

What this block cannot check, stated so it is not assumed: the currency
``costs.min_commission.unit`` must match is the INSTRUMENT's, which no document
path states; and a run whose instrument currency differs from
``spec.size.currency`` needs an FX rate this block does not carry. Both are
recorded on the epic and decided outside this module.

ENGINE module: stdlib and ``broker_contract`` only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from broker_contract.failure import ContractError

from intent_replay.refusal import refuse

__all__ = [
    "BPS",
    "CONFIG_INCOMPLETE_CODE",
    "CONFIG_INCOMPLETE_REASONS",
    "CONFIG_INVALID_CODE",
    "CONFIG_INVALID_REASONS",
    "CONFIG_KEYS",
    "EPOCH_MS_UTC",
    "FRACTION",
    "ConfigError",
    "Costs",
    "Quantity",
    "RunConfig",
    "Translated",
]

CONFIG_INCOMPLETE_CODE: Final = "config_incomplete"
CONFIG_INVALID_CODE: Final = "config_invalid"

# The published unit strings of spec section 5.2.
EPOCH_MS_UTC: Final = "epoch_ms_utc"
FRACTION: Final = "fraction"
BPS: Final = "bps"

# The block's keys in the order section 5.2 prints them; ``to_jsonable`` keeps it.
CONFIG_KEYS: Final = (
    "entry_deadline",
    "walk_start",
    "entry_trail_bps",
    "ceiling_price",
    "time_stop_t",
    "oco",
    "costs",
)
_TRANSLATED_KEYS: Final = ("kind", "value", "unit", "source", "formula")
_QUANTITY_KEYS: Final = ("value", "unit")
_COSTS_KEYS: Final = (
    "commission_rate",
    "min_commission",
    "min_commission_applies",
    "fx_applies",
    "exit_edge_min_bps",
)

CONFIG_INCOMPLETE_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {"missing_key": "A required key is absent. The replay states nothing on the caller's behalf."}
)

CONFIG_INVALID_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "unknown_key": (
            "A key this block does not model. It would otherwise be ignored, and the run "
            "would inherit nothing where the caller believed something was stated."
        ),
        "wrong_type": "The value is not of the stated key's type (a null where none is allowed included).",
        "numeric_not_finite": "A price or cost is NaN or infinite; every comparison on it would silently pass.",
        "not_positive": "A distance or a price that must be above zero is not.",
        "negative": "A cost below zero.",
        "unit_mismatch": "The unit is not the one the walk compares against.",
        "empty_string": "A provenance field or a currency code with no text.",
        "oco_unsupported": "v1 models no OCO pair; the key is stated false or the run is refused.",
    }
)


class ConfigError(ContractError):
    """The stated configuration cannot be used; nothing was computed."""


@dataclass(frozen=True, slots=True)
class Translated:
    """The section 5.1 provenance shape of a TRANSLATED document path (section 5.2).

    ``value`` is epoch milliseconds, UTC, as an int: that is what the walk
    compares against ``Bar.t``.
    """

    kind: str
    value: int
    unit: str
    source: str
    formula: str

    def to_jsonable(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in _TRANSLATED_KEYS}


@dataclass(frozen=True, slots=True)
class Quantity:
    """A number that carries its unit (the section 5.2 costs block)."""

    value: float
    unit: str

    def to_jsonable(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class Costs:
    """The threshold the take-profit cost gate compares against (section 8.1)."""

    commission_rate: Quantity
    min_commission: Quantity
    min_commission_applies: bool
    fx_applies: bool
    exit_edge_min_bps: Quantity

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "commission_rate": self.commission_rate.to_jsonable(),
            "min_commission": self.min_commission.to_jsonable(),
            "min_commission_applies": self.min_commission_applies,
            "fx_applies": self.fx_applies,
            "exit_edge_min_bps": self.exit_edge_min_bps.to_jsonable(),
        }


@dataclass(frozen=True, slots=True)
class RunConfig:
    """The seven stated keys of the section 5.2 config block."""

    walk_start: Translated
    entry_deadline: Translated
    entry_trail_bps: int | None
    ceiling_price: float | None
    time_stop_t: int | None
    oco: bool
    costs: Costs

    @classmethod
    def from_jsonable(cls, data: Mapping[str, Any]) -> RunConfig:
        """Parse a decoded JSON mapping, refusing anything not usable.

        Takes a mapping, not text: refusing a repeated key is the loader's job
        (``json.loads`` silently keeps the last), as it is at the arming door.
        """
        reader = _Reader()
        parsed = _read_block(reader, data)
        reader.raise_if_any()
        return cls(
            walk_start=_present(parsed.walk_start),
            entry_deadline=_present(parsed.entry_deadline),
            entry_trail_bps=parsed.entry_trail_bps,
            ceiling_price=parsed.ceiling_price,
            time_stop_t=parsed.time_stop_t,
            oco=_present(parsed.oco),
            costs=_present(parsed.costs),
        )

    def to_jsonable(self) -> dict[str, Any]:
        """EXACTLY the section 5.2 block, in its key order; the envelope carries it."""
        return {
            "entry_deadline": self.entry_deadline.to_jsonable(),
            "walk_start": self.walk_start.to_jsonable(),
            "entry_trail_bps": self.entry_trail_bps,
            "ceiling_price": self.ceiling_price,
            "time_stop_t": self.time_stop_t,
            "oco": self.oco,
            "costs": self.costs.to_jsonable(),
        }


# ---------------------------------------------------------------------------
# Reading. Every violation is collected; the refusal is raised once.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Violation:
    key: str
    reason: str
    message: str
    expected: Any = field(default=None)
    has_expected: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"key": self.key, "reason": self.reason}
        if self.has_expected:
            entry["expected"] = self.expected
        return entry


_UNSET: Final = object()


@dataclass(slots=True)
class _Reader:
    missing: list[str] = field(default_factory=list)
    invalid: list[_Violation] = field(default_factory=list)

    def miss(self, key: str) -> None:
        self.missing.append(key)

    def reject(self, key: str, reason: str, message: str, *, expected: Any = _UNSET) -> None:
        """Record a violation; ``expected`` (any value, ``False`` included) is
        published beside it when given."""
        if expected is _UNSET:
            self.invalid.append(_Violation(key, reason, message))
        else:
            self.invalid.append(_Violation(key, reason, message, expected, True))

    def raise_if_any(self) -> None:
        if self.missing:
            keys = sorted(self.missing)
            raise refuse(
                ConfigError,
                CONFIG_INCOMPLETE_CODE,
                f"required configuration key {keys[0]!r} is not stated",
                reasons=CONFIG_INCOMPLETE_REASONS,
                reason="missing_key",
                keys=keys,
                violations=[{"key": key, "reason": "missing_key"} for key in keys],
            )
        if self.invalid:
            first = self.invalid[0]
            raise refuse(
                ConfigError,
                CONFIG_INVALID_CODE,
                f"{first.key}: {first.message}",
                reasons=CONFIG_INVALID_REASONS,
                reason=first.reason,
                keys=sorted({violation.key for violation in self.invalid}),
                violations=[violation.to_jsonable() for violation in self.invalid],
            )


@dataclass(slots=True)
class _Parsed:
    walk_start: Translated | None = None
    entry_deadline: Translated | None = None
    entry_trail_bps: int | None = None
    ceiling_price: float | None = None
    time_stop_t: int | None = None
    oco: bool | None = None
    costs: Costs | None = None


def _present[T](value: T | None) -> T:
    if value is None:  # pragma: no cover - raise_if_any() ran first
        raise RuntimeError("a parsed value is absent after no violation was recorded")
    return value


def _path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _mapping(
    reader: _Reader, node: Any, path: str, keys: tuple[str, ...]
) -> Mapping[str, Any] | None:
    """The node as a mapping with exactly ``keys``, recording every unknown and
    missing key; ``None`` when the node is not a mapping at all."""
    if not isinstance(node, Mapping):
        reader.reject(path or "<root>", "wrong_type", "expected an object")
        return None
    for key in node:
        if key not in keys:
            reader.reject(_path(path, key), "unknown_key", "not a key of this block")
    for key in keys:
        if key not in node:
            reader.miss(_path(path, key))
    return node


def _string(reader: _Reader, node: Any, path: str) -> str | None:
    if not isinstance(node, str):
        reader.reject(path, "wrong_type", "expected a string")
        return None
    if not node:
        reader.reject(path, "empty_string", "must not be empty")
        return None
    return node


def _boolean(reader: _Reader, node: Any, path: str) -> bool | None:
    if not isinstance(node, bool):
        reader.reject(path, "wrong_type", "expected true or false")
        return None
    return node


def _integer(reader: _Reader, node: Any, path: str) -> int | None:
    # bool is a subclass of int; a stated true is not a stated integer.
    if isinstance(node, bool) or not isinstance(node, int):
        reader.reject(path, "wrong_type", "expected an integer")
        return None
    return node


def _number(reader: _Reader, node: Any, path: str) -> float | None:
    """A finite int or float. An int of any size is finite; ``math.isfinite``
    runs on floats only, because on an int wider than a float it raises."""
    if isinstance(node, bool) or not isinstance(node, int | float):
        reader.reject(path, "wrong_type", "expected a number")
        return None
    if isinstance(node, float) and not math.isfinite(node):
        reader.reject(path, "numeric_not_finite", "must be a finite number")
        return None
    return node


def _translated(reader: _Reader, node: Any, path: str) -> Translated | None:
    node = _mapping(reader, node, path, _TRANSLATED_KEYS)
    if node is None:
        return None
    texts = {
        key: _string(reader, node[key], _path(path, key))
        for key in ("kind", "source", "formula")
        if key in node
    }
    value = _integer(reader, node["value"], _path(path, "value")) if "value" in node else None
    unit = (
        _unit(reader, node.get("unit"), _path(path, "unit"), EPOCH_MS_UTC)
        if "unit" in node
        else None
    )
    if (
        len(texts) != 3
        or value is None
        or unit is None
        or any(text is None for text in texts.values())
    ):
        return None
    return Translated(
        kind=_present(texts["kind"]),
        value=value,
        unit=unit,
        source=_present(texts["source"]),
        formula=_present(texts["formula"]),
    )


def _unit(reader: _Reader, node: Any, path: str, expected: str | None) -> str | None:
    """A unit string: the published one when ``expected`` is given, any
    non-empty code otherwise (``min_commission`` is in a currency)."""
    unit = _string(reader, node, path)
    if unit is None:
        return None
    if expected is not None and unit != expected:
        reader.reject(path, "unit_mismatch", f"expected {expected!r}", expected=expected)
        return None
    return unit


def _quantity(reader: _Reader, node: Any, path: str, expected_unit: str | None) -> Quantity | None:
    node = _mapping(reader, node, path, _QUANTITY_KEYS)
    if node is None:
        return None
    value = _number(reader, node["value"], _path(path, "value")) if "value" in node else None
    if value is not None and value < 0:
        reader.reject(_path(path, "value"), "negative", "a cost cannot be below zero")
        value = None
    unit = (
        _unit(reader, node["unit"], _path(path, "unit"), expected_unit) if "unit" in node else None
    )
    if value is None or unit is None:
        return None
    return Quantity(value=value, unit=unit)


def _costs(reader: _Reader, node: Any, path: str) -> Costs | None:
    node = _mapping(reader, node, path, _COSTS_KEYS)
    if node is None:
        return None
    rate = (
        _quantity(reader, node["commission_rate"], _path(path, "commission_rate"), FRACTION)
        if "commission_rate" in node
        else None
    )
    minimum = (
        _quantity(reader, node["min_commission"], _path(path, "min_commission"), None)
        if "min_commission" in node
        else None
    )
    min_applies = (
        _boolean(reader, node["min_commission_applies"], _path(path, "min_commission_applies"))
        if "min_commission_applies" in node
        else None
    )
    fx_applies = (
        _boolean(reader, node["fx_applies"], _path(path, "fx_applies"))
        if "fx_applies" in node
        else None
    )
    edge = (
        _quantity(reader, node["exit_edge_min_bps"], _path(path, "exit_edge_min_bps"), BPS)
        if "exit_edge_min_bps" in node
        else None
    )
    if rate is None or minimum is None or min_applies is None or fx_applies is None or edge is None:
        return None
    return Costs(
        commission_rate=rate,
        min_commission=minimum,
        min_commission_applies=min_applies,
        fx_applies=fx_applies,
        exit_edge_min_bps=edge,
    )


def _trail_distance(reader: _Reader, node: Any) -> int | None:
    """``entry_trail_bps``: null is OFF; an int is a distance and must be >= 1.
    The live flag reads 0 as OFF, so a 0 here is ambiguous and is refused with
    the form to use instead."""
    if node is None:
        return None
    distance = _integer(reader, node, "entry_trail_bps")
    if distance is not None and distance < 1:
        reader.reject(
            "entry_trail_bps", "not_positive", "must be >= 1; entry trailing OFF is stated as null"
        )
        return None
    return distance


def _ceiling(reader: _Reader, node: Any) -> float | None:
    if node is None:
        return None
    price = _number(reader, node, "ceiling_price")
    if price is not None and price <= 0:
        reader.reject("ceiling_price", "not_positive", "a price must be above zero")
        return None
    return price


def _oco(reader: _Reader, node: Any) -> bool | None:
    oco = _boolean(reader, node, "oco")
    if oco:
        reader.reject(
            "oco", "oco_unsupported", "v1 models no OCO pair; state false", expected=False
        )
        return None
    return oco


def _read_block(reader: _Reader, data: Any) -> _Parsed:
    parsed = _Parsed()
    node = _mapping(reader, data, "", CONFIG_KEYS)
    if node is None:
        return parsed
    if "entry_deadline" in node:
        parsed.entry_deadline = _translated(reader, node["entry_deadline"], "entry_deadline")
    if "walk_start" in node:
        parsed.walk_start = _translated(reader, node["walk_start"], "walk_start")
    if "entry_trail_bps" in node:
        parsed.entry_trail_bps = _trail_distance(reader, node["entry_trail_bps"])
    if "ceiling_price" in node:
        parsed.ceiling_price = _ceiling(reader, node["ceiling_price"])
    if "time_stop_t" in node and node["time_stop_t"] is not None:
        parsed.time_stop_t = _integer(reader, node["time_stop_t"], "time_stop_t")
    if "oco" in node:
        parsed.oco = _oco(reader, node["oco"])
    if "costs" in node:
        parsed.costs = _costs(reader, node["costs"], "costs")
    return parsed
