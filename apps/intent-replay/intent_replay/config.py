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
  this block does not model, a currency code that is not one, a sizing buffer
  outside ``[0, 100)``, or ``oco: true``. Its own code rather than a
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

Both things this block could not check before #1592 are now checked, and the
key set that made them checkable is section 5.2.1's. ``fx.instrument_currency``
states the settlement currency no document path carries, so
``costs.min_commission.unit`` is compared against it instead of being a label
nothing reads; and ``fx.mid_rate`` states the conversion, so a cross-currency
run is PRICED rather than refused. Two consequences shape the reading pass:

* **The fx key set is CONDITIONAL on a value inside it.** One key when the
  stated instrument code equals ``account_currency``, four when they differ. So
  a magnitude stated on a same-currency run is ``unknown_key`` and one absent
  on a cross-currency run is ``missing_key`` — refused either way, never
  accepted and left inert.
* **``fx_applies`` is DERIVED and is no longer a key.** A caller could state
  ``false`` on a cross-currency document, and section 8.1 measures what that
  cost. A value derived from the two codes cannot contradict them.

``account_currency`` is a REQUIRED keyword rather than a block key, because it
is the document's (``spec.size.currency``) and not the caller's: the block
cannot state it without being able to disagree with the document it replays.
A required keyword cannot be forgotten at a call site; a free function can.

ENGINE module: stdlib and ``broker_contract`` only.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from broker_contract.failure import ContractError

from intent_replay.fx import Fx
from intent_replay.refusal import refuse
from intent_replay.units import (
    BPS,
    EPOCH_MS_UTC,
    FRACTION,
    ISO_4217,
    PERCENT,
    QUANTITY_KEYS,
    TRANSLATED_KEYS,
    Quantity,
    Translated,
)

__all__ = [
    "CONFIG_INCOMPLETE_CODE",
    "CONFIG_INCOMPLETE_REASONS",
    "CONFIG_INVALID_CODE",
    "CONFIG_INVALID_REASONS",
    "CONFIG_KEYS",
    "ConfigError",
    "Costs",
    "FxFacts",
    "RunConfig",
]

CONFIG_INCOMPLETE_CODE: Final = "config_incomplete"
CONFIG_INVALID_CODE: Final = "config_invalid"

# The block's keys in the order section 5.2 prints them; ``to_jsonable`` keeps it.
CONFIG_KEYS: Final = (
    "entry_deadline",
    "walk_start",
    "entry_trail_bps",
    "ceiling_price",
    "time_stop_t",
    "oco",
    "fx",
    "costs",
)
_COSTS_KEYS: Final = (
    "commission_rate",
    "min_commission",
    "min_commission_applies",
    "exit_edge_min_bps",
)

# The conditional key set of section 5.2.1. ``instrument_currency`` is always
# required; the three magnitudes are required exactly when the codes differ.
_FX_SAME_KEYS: Final = ("instrument_currency",)
_FX_CROSS_KEYS: Final = (
    "instrument_currency",
    "mid_rate",
    "round_trip_cost_rate",
    "sizing_buffer_pct",
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
        "numeric_not_finite": (
            "A stated number the walk's arithmetic cannot carry: NaN or infinite, where every "
            "comparison on it would silently pass, or an integer too wide to convert to a float."
        ),
        "not_positive": "A distance or a price that must be above zero is not.",
        "negative": "A cost below zero.",
        "unit_mismatch": "The unit is not the one the walk compares against.",
        "empty_string": "A provenance field or a currency code with no text.",
        "oco_unsupported": "v1 models no OCO pair; the key is stated false or the run is refused.",
        "not_a_currency_code": (
            "A stated currency is not a three-letter uppercase ISO 4217 code. The door holds "
            "the document's own code to that shape, and the two are compared."
        ),
        "buffer_out_of_range": (
            "A sizing buffer at or above 100 per cent. At 100 the budget is zero and the walk "
            "divides by it; above 100 it is negative, and the run books a profit on a short "
            "the document never declared."
        ),
    }
)


class ConfigError(ContractError):
    """The stated configuration cannot be used; nothing was computed."""


@dataclass(frozen=True, slots=True)
class FxFacts:
    """The stated conversion of section 5.2.1, with the provenance it travels in.

    This is the ECHO half: the four fields are what the caller wrote, and they
    ride in the result so a reader can check the rate's as-of time against the
    run. The ARITHMETIC half is :class:`~intent_replay.fx.Fx`, carried by
    :class:`Costs`. Both are built once, from the same parsed values, in
    :func:`_read_block` — so neither can drift from the other, and a test pins
    every pair.

    ``mid_rate`` carries the section 5.1 provenance shape although it translates
    no document path. The reason is stated in section 5.2.1 rather than being a
    quiet stretch of the rule: it is the one value in the block that NOTHING in
    the run can check, so its ``source`` and the as-of time inside ``formula``
    are the only audit a later reader has.
    """

    instrument_currency: Translated[str]
    mid_rate: Translated[float] | None
    round_trip_cost_rate: Quantity | None
    sizing_buffer_pct: Quantity | None

    def to_jsonable(self) -> dict[str, Any]:
        """Only the keys the caller STATED, so the block round-trips.

        The derived flag and the derived notional are published by the result
        envelope and never here: a key this parser would refuse on input cannot
        appear in an echo it has to be able to re-read.
        """
        block: dict[str, Any] = {"instrument_currency": self.instrument_currency.to_jsonable()}
        if self.mid_rate is None:
            return block
        block["mid_rate"] = self.mid_rate.to_jsonable()
        block["round_trip_cost_rate"] = _present(self.round_trip_cost_rate).to_jsonable()
        block["sizing_buffer_pct"] = _present(self.sizing_buffer_pct).to_jsonable()
        return block


@dataclass(frozen=True, slots=True)
class Costs:
    """The threshold the take-profit cost gate compares against (section 8.1).

    ``fx`` is the conversion the gate applies, not a cost of its own: the gate
    compares ``min_commission`` — an INSTRUMENT-currency magnitude — against a
    notional the walk holds in the ACCOUNT's, so it needs the rate to make the
    two commensurable, and the stated round-trip rate to price the conversion
    leg the daemon prices. It is the same object the top-level ``fx`` block
    describes, carried here so the gate keeps one argument for all its costs.
    """

    commission_rate: Quantity
    min_commission: Quantity
    min_commission_applies: bool
    exit_edge_min_bps: Quantity
    fx: Fx

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "commission_rate": self.commission_rate.to_jsonable(),
            "min_commission": self.min_commission.to_jsonable(),
            "min_commission_applies": self.min_commission_applies,
            "exit_edge_min_bps": self.exit_edge_min_bps.to_jsonable(),
        }


@dataclass(frozen=True, slots=True)
class RunConfig:
    """The eight stated keys of the section 5.2 config block."""

    walk_start: Translated[int]
    entry_deadline: Translated[int]
    entry_trail_bps: int | None
    ceiling_price: float | None
    time_stop_t: int | None
    oco: bool
    fx: FxFacts
    costs: Costs

    @classmethod
    def from_jsonable(cls, data: Mapping[str, Any], *, account_currency: str) -> RunConfig:
        """Parse a decoded JSON mapping, refusing anything not usable.

        Takes a mapping, not text: refusing a repeated key is the loader's job
        (``json.loads`` silently keeps the last), as it is at the arming door.

        ``account_currency`` is the document's ``spec.size.currency``, already
        held to a three-letter uppercase code by the door. It decides the fx key
        set and the derived ``applies``, so parsing cannot be document-blind —
        and a second ``bind()`` pass would raise a SECOND refusal after the
        first succeeded, breaking this module's promise that one pass names
        every missing key.
        """
        reader = _Reader()
        parsed = _read_block(reader, data, account_currency)
        reader.raise_if_any()
        return cls(
            walk_start=_present(parsed.walk_start),
            entry_deadline=_present(parsed.entry_deadline),
            entry_trail_bps=parsed.entry_trail_bps,
            ceiling_price=parsed.ceiling_price,
            time_stop_t=parsed.time_stop_t,
            oco=_present(parsed.oco),
            fx=_present(parsed.fx),
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
            "fx": self.fx.to_jsonable(),
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
    walk_start: Translated[int] | None = None
    entry_deadline: Translated[int] | None = None
    entry_trail_bps: int | None = None
    ceiling_price: float | None = None
    time_stop_t: int | None = None
    oco: bool | None = None
    fx: FxFacts | None = None
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


def _translated[T](
    reader: _Reader,
    node: Any,
    path: str,
    *,
    value_of: Callable[[_Reader, Any, str], T | None],
    expected_unit: str,
) -> Translated[T] | None:
    """The section 5.1 provenance shape, over any value type.

    ``value_of`` and ``expected_unit`` are parameters rather than the epoch pair
    this used to hardcode: section 5.2.1 adds a translated STRING (the
    settlement currency, unit ``iso_4217``) and a translated FLOAT (the mid
    rate, whose unit spells the direction of the pair), and all three carry the
    same five fields with the same rules on the three text ones.
    """
    node = _mapping(reader, node, path, TRANSLATED_KEYS)
    if node is None:
        return None
    texts = {
        key: _string(reader, node[key], _path(path, key))
        for key in ("kind", "source", "formula")
        if key in node
    }
    value = value_of(reader, node["value"], _path(path, "value")) if "value" in node else None
    unit = (
        _unit(reader, node.get("unit"), _path(path, "unit"), expected_unit)
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
    non-empty code otherwise.

    ``expected is None`` survives for ONE case: ``min_commission.unit`` while
    the instrument's currency is itself missing or malformed. The refusal that
    case raises is the one about the currency, so a second violation on the
    minimum would name a key the caller has not reached yet.
    """
    unit = _string(reader, node, path)
    if unit is None:
        return None
    if expected is not None and unit != expected:
        reader.reject(path, "unit_mismatch", f"expected {expected!r}", expected=expected)
        return None
    return unit


def _quantity(reader: _Reader, node: Any, path: str, expected_unit: str | None) -> Quantity | None:
    node = _mapping(reader, node, path, QUANTITY_KEYS)
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


def _is_currency_code(value: Any) -> bool:
    """A three-letter uppercase ASCII code, the shape the door holds the
    document's own ``spec.size.currency`` to.

    ``isalpha`` and ``isupper`` both answer True for non-ASCII letters, so
    ``isascii`` is what makes this the same predicate as the door's.
    """
    return (
        isinstance(value, str)
        and len(value) == 3
        and value.isascii()
        and value.isalpha()
        and value.isupper()
    )


def _currency_code(reader: _Reader, node: Any, path: str) -> str | None:
    code = _string(reader, node, path)
    if code is None:
        return None
    if not _is_currency_code(code):
        reader.reject(
            path, "not_a_currency_code", "expected a three-letter uppercase ISO 4217 code"
        )
        return None
    return code


def _rate(reader: _Reader, node: Any, path: str) -> float | None:
    """The mid rate: finite and above zero.

    Positive does NOT close the inverse. 3.70 where 0.27027 was meant is
    positive, plausible, and above the fee card's knee leaves the gate's verdict
    bit-identical (section 8.1) — the only real check on the direction is the
    unit token, by string equality.
    """
    value = _number(reader, node, path)
    if value is None:
        return None
    if value <= 0.0:
        reader.reject(path, "not_positive", "a rate must be above zero")
        return None
    return value


def _buffer(reader: _Reader, node: Any, path: str) -> Quantity | None:
    """``fx.sizing_buffer_pct``, bounded to ``[0, 100)``.

    The bound is restated here rather than borrowed: the contract has none,
    because the daemon's buffer is an operator-locked constant
    (``execution.py``). Measured on the published fixture, at 100 the budget is
    ``0.0``, a rung's units are ``0.0`` and the walk divides by them; at 150 the
    budget is ``-750.0`` and the run reports ``notional_spent -450.0``,
    ``pnl_cash +450.0`` and ``outcome: no_fill`` — a booked profit on a short the
    document never declared. The reason is the SIGN of the units, not an
    overflow. Zero is a stated policy (withhold nothing) and is accepted.
    """
    quantity = _quantity(reader, node, path, PERCENT)
    if quantity is None:
        return None
    if quantity.value >= 100.0:
        reader.reject(
            _path(path, "value"),
            "buffer_out_of_range",
            "must be below 100; at 100 the budget is zero and above it the budget is negative",
            expected="[0, 100)",
        )
        return None
    return quantity


def _fx(reader: _Reader, node: Any, path: str, *, cross: bool, pair_unit: str) -> FxFacts | None:
    """The section 5.2.1 block, over the key set the stated code selected.

    ``cross`` and ``pair_unit`` are decided by :func:`_stated_code` from the
    code inside this very block, which is why they arrive as arguments: the key
    set is conditional on one of its own values, and the unit of ``mid_rate``
    is built from that value and the account's.
    """
    node = _mapping(reader, node, path, _FX_CROSS_KEYS if cross else _FX_SAME_KEYS)
    if node is None:
        return None
    instrument = (
        _translated(
            reader,
            node["instrument_currency"],
            _path(path, "instrument_currency"),
            value_of=_currency_code,
            expected_unit=ISO_4217,
        )
        if "instrument_currency" in node
        else None
    )
    rate = (
        _translated(
            reader,
            node["mid_rate"],
            _path(path, "mid_rate"),
            value_of=_rate,
            expected_unit=pair_unit,
        )
        if cross and "mid_rate" in node
        else None
    )
    cost = (
        _quantity(
            reader, node["round_trip_cost_rate"], _path(path, "round_trip_cost_rate"), FRACTION
        )
        if cross and "round_trip_cost_rate" in node
        else None
    )
    buffer_pct = (
        _buffer(reader, node["sizing_buffer_pct"], _path(path, "sizing_buffer_pct"))
        if cross and "sizing_buffer_pct" in node
        else None
    )
    if instrument is None or (cross and (rate is None or cost is None or buffer_pct is None)):
        return None
    return FxFacts(
        instrument_currency=instrument,
        mid_rate=rate,
        round_trip_cost_rate=cost,
        sizing_buffer_pct=buffer_pct,
    )


def _costs(reader: _Reader, node: Any, path: str, *, fx: Fx | None) -> Costs | None:
    node = _mapping(reader, node, path, _COSTS_KEYS)
    if node is None:
        return None
    rate = (
        _quantity(reader, node["commission_rate"], _path(path, "commission_rate"), FRACTION)
        if "commission_rate" in node
        else None
    )
    minimum = (
        _quantity(
            reader,
            node["min_commission"],
            _path(path, "min_commission"),
            None if fx is None else fx.instrument_currency,
        )
        if "min_commission" in node
        else None
    )
    min_applies = (
        _boolean(reader, node["min_commission_applies"], _path(path, "min_commission_applies"))
        if "min_commission_applies" in node
        else None
    )
    edge = (
        _quantity(reader, node["exit_edge_min_bps"], _path(path, "exit_edge_min_bps"), BPS)
        if "exit_edge_min_bps" in node
        else None
    )
    if rate is None or minimum is None or min_applies is None or edge is None or fx is None:
        return None
    return Costs(
        commission_rate=rate,
        min_commission=minimum,
        min_commission_applies=min_applies,
        exit_edge_min_bps=edge,
        fx=fx,
    )


def _trail_distance(reader: _Reader, node: Any) -> int | None:
    """``entry_trail_bps``: null is OFF; an int is a distance and must be >= 1.
    The live flag reads 0 as OFF, so a 0 here is ambiguous and is refused with
    the form to use instead.

    No UPPER bound, deliberately. The live rail caps the flag at 150 and a
    value outside ``[0, 150]`` makes the deployment's own reader fall back to
    0 - the three-limit ladder, which is the OPPOSITE policy - but that is a
    deployment rail, not a property of the document, and section 5.2 publishes
    the key as an integer >= 1. The README says what no deployment will run.

    "No upper bound" is a claim about the POLICY, not about arithmetic. The walk
    multiplies the distance by a price, and an integer wider than a float raises
    on that conversion, so a value the schema permits would leave a traceback
    where the tool owes a refusal. That one is refused here instead, as a number
    nothing can use - the same mode as NaN, not a new rule.
    """
    if node is None:
        return None
    distance = _integer(reader, node, "entry_trail_bps")
    if distance is None:
        return None
    if distance < 1:
        reader.reject(
            "entry_trail_bps", "not_positive", "must be >= 1; entry trailing OFF is stated as null"
        )
        return None
    try:
        float(distance)
    except OverflowError:
        reader.reject(
            "entry_trail_bps",
            "numeric_not_finite",
            "too wide to convert to a float; the walk multiplies it by a price",
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


def _stated_code(node: Any) -> str | None:
    """The instrument code the fx block states, WITHOUT validating it.

    A peek, and it has to be one: the fx key set is conditional on a value
    inside the fx block, so the set cannot be chosen after the block is read
    against a set. Anything but a non-empty string answers ``None``, and the
    same-currency set is then used — the refusal that case raises is the one
    about the code itself.
    """
    if not isinstance(node, Mapping):
        return None
    stated = node.get("fx")
    if not isinstance(stated, Mapping):
        return None
    currency = stated.get("instrument_currency")
    if not isinstance(currency, Mapping):
        return None
    value = currency.get("value")
    return value if isinstance(value, str) and value else None


def _engine_fx(facts: FxFacts, account_currency: str) -> Fx:
    """The ARITHMETIC half of the stated facts (``intent_replay.fx.Fx``).

    Built here and nowhere else, from the same parsed values the echo carries,
    so the two representations of one fact cannot disagree.
    """
    return Fx(
        account_currency=account_currency,
        instrument_currency=facts.instrument_currency.value,
        rate=None if facts.mid_rate is None else facts.mid_rate.value,
        round_trip_cost_rate=(
            None if facts.round_trip_cost_rate is None else facts.round_trip_cost_rate.value
        ),
        sizing_buffer_pct=(
            None if facts.sizing_buffer_pct is None else facts.sizing_buffer_pct.value
        ),
    )


def _read_block(reader: _Reader, data: Any, account_currency: str) -> _Parsed:
    parsed = _Parsed()
    node = _mapping(reader, data, "", CONFIG_KEYS)
    if node is None:
        return parsed
    if "entry_deadline" in node:
        parsed.entry_deadline = _translated(
            reader,
            node["entry_deadline"],
            "entry_deadline",
            value_of=_integer,
            expected_unit=EPOCH_MS_UTC,
        )
    if "walk_start" in node:
        parsed.walk_start = _translated(
            reader,
            node["walk_start"],
            "walk_start",
            value_of=_integer,
            expected_unit=EPOCH_MS_UTC,
        )
    if "entry_trail_bps" in node:
        parsed.entry_trail_bps = _trail_distance(reader, node["entry_trail_bps"])
    if "ceiling_price" in node:
        parsed.ceiling_price = _ceiling(reader, node["ceiling_price"])
    if "time_stop_t" in node and node["time_stop_t"] is not None:
        parsed.time_stop_t = _integer(reader, node["time_stop_t"], "time_stop_t")
    if "oco" in node:
        parsed.oco = _oco(reader, node["oco"])
    # The fx block is read BEFORE ``costs``: it names the currency
    # ``min_commission.unit`` is compared against, which is the half of
    # section 5.2.1 this module could not check before #1592.
    code = _stated_code(node)
    cross = code is not None and code != account_currency
    if "fx" in node:
        parsed.fx = _fx(
            reader,
            node["fx"],
            "fx",
            cross=cross,
            # Only the cross arm reads it. Built conditionally so a malformed
            # block cannot carry a ``None_per_PLN`` into a later edit that
            # reads the argument unconditionally; measured 2026-10-02, no
            # refusal publishes such a token today.
            pair_unit=f"{code}_per_{account_currency}" if cross else "",
        )
    fx = None if parsed.fx is None else _engine_fx(parsed.fx, account_currency)
    if "costs" in node:
        parsed.costs = _costs(reader, node["costs"], "costs", fx=fx)
    return parsed
