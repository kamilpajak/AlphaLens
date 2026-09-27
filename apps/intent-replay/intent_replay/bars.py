"""The price-input contract of the replay (spec sections 4, 4.5 and 5.4).

ENGINE module: stdlib and ``broker_contract`` only.

A :class:`Bar` is the one price shape the walk reads. Two checks accompany it,
each a pure function of the bar sequence and, for the window, ONE timestamp:

* :func:`validate_sequence` refuses an empty sequence and any sequence whose
  ``t`` is not strictly increasing. It never sorts: the existing ``/edge``
  replay reorders a caller's input silently, and spec section 4.5 deviates
  from that on purpose, because a research tool that repairs its input
  produces a wrong answer that leaves no trace.
* :func:`check_window_covers` refuses a sequence that does not cover the
  stated ``walk_start``. It takes the timestamp as an argument and does not
  read the run configuration, so it can exist before the configuration
  model does.

Every refusal is a :class:`BarsError` carrying a ``Failure`` whose ``code``
is one of the four engine-owned bar codes and whose ``details["reason"]``
names the rule, on the project's rule of one code per failure mode with a
closed reason vocabulary (the shape ``broker_contract.trade_intent.validate``
already uses for ``intent_invalid``). The builder that enforces the vocabulary
at the raise site is shared with the other engine modules (``refusal.py``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Final

from broker_contract.failure import ContractError

from intent_replay.refusal import refuse

BARS_EMPTY_CODE: Final = "bars_empty"
BARS_UNORDERED_CODE: Final = "bars_unordered"
BARS_INVALID_CODE: Final = "bars_invalid"
WINDOW_TOO_SHORT_CODE: Final = "window_too_short"

# The closed reason vocabulary. A raise with a reason outside this mapping is a
# programming error (``_refuse`` asserts it), never a published refusal.
BARS_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "decreasing": "A bar's t is lower than the bar before it.",
        "duplicate": "Two consecutive bars carry the same t.",
        "numeric_not_finite": "A price is NaN or infinite; every comparison on it would silently pass.",
        "ends_before_walk_start": "The last bar precedes walk_start; there is nothing to walk.",
        "begins_after_walk_start": (
            "The first bar follows walk_start; the entry ladder was live over an "
            "interval the tape does not cover."
        ),
        "not_a_list": "The bar input is not one JSON array of bar objects.",
        "wrong_type": (
            "A bar is not an object, or one of its fields is not the type the published "
            "shape names."
        ),
        "missing_key": "A bar does not carry one of t, open, high, low, close.",
        "unknown_key": (
            "A bar carries a key the shape does not model. Ignoring it would replay a bar "
            "its author did not send."
        ),
    }
)

_PRICE_FIELDS: Final = ("open", "high", "low", "close")
# The published bar shape, in the order the README prints it.
_BAR_KEYS: Final = ("t", *_PRICE_FIELDS)


class BarsError(ContractError):
    """The supplied bars cannot be walked; nothing was computed."""


def _refuse(code: str, message: str, **details: Any) -> BarsError:
    return refuse(BarsError, code, message, reasons=BARS_REASONS, **details)


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLC bar.

    ``t`` is epoch milliseconds, UTC, and is taken to be the bar's OPEN time —
    an assumption this module states rather than a fact the spec settles. No
    volume: queue position is not modelled, so it would be a field nobody
    reads.

    A non-finite price is refused at construction. ``json.loads`` accepts a
    bare ``NaN`` and a NaN answers False to every ordering comparison, so a
    bar carrying one would pass every check downstream and describe a fill
    that never happened; the value type is where it stops.
    """

    t: int
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name not in _PRICE_FIELDS:
                continue
            value = getattr(self, field.name)
            if not math.isfinite(value):
                raise _refuse(
                    BARS_INVALID_CODE,
                    f"bar {field.name} must be a finite number, got {value!r}",
                    reason="numeric_not_finite",
                    field=field.name,
                    t=self.t,
                )


def parse_bars(data: Any) -> tuple[Bar, ...]:
    """Turn the PARSED bar input into bars, or refuse (spec section 5.4).

    The published shape is one JSON array of ``{t, open, high, low, close}``
    objects. The ENGINE owns that shape; the CLI only reads the file, so bytes
    that are not JSON at all are a CLI concern (``bars_malformed``) and never
    reach here — the same split ``config_malformed`` has against
    ``config_invalid``.

    Every refusal WIDENS ``bars_invalid`` instead of adding a code: a bar that
    is not a list entry, is not an object, lacks a field or carries a field the
    shape does not model all say "this is not a usable bar", which is the
    failure mode that code already names.

    Order and emptiness are NOT judged here. ``bars_empty`` and
    ``bars_unordered`` belong to :func:`validate_sequence`, and answering them
    in two places would let the caller's order of checks decide the code.
    """
    if not isinstance(data, list):
        raise _refuse(
            BARS_INVALID_CODE,
            f"the bars must be one JSON array of bar objects, got {type(data).__name__}",
            reason="not_a_list",
        )
    return tuple(_parse_bar(entry, index) for index, entry in enumerate(data))


def _number(entry: Mapping[str, Any], field: str, index: int) -> float:
    """One price, as a float. ``bool`` is refused explicitly because it is an
    ``int`` subclass, so a bare numeric check would read ``true`` as 1.0."""
    value = entry[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _refuse(
            BARS_INVALID_CODE,
            f"bar {index} field {field} must be a number, got {value!r}",
            reason="wrong_type",
            index=index,
            field=field,
        )
    return float(value)


def _parse_bar(entry: Any, index: int) -> Bar:
    if not isinstance(entry, Mapping):
        raise _refuse(
            BARS_INVALID_CODE,
            f"bar {index} must be an object, got {type(entry).__name__}",
            reason="wrong_type",
            index=index,
        )
    missing = [key for key in _BAR_KEYS if key not in entry]
    if missing:
        raise _refuse(
            BARS_INVALID_CODE,
            f"bar {index} is missing {', '.join(missing)}",
            reason="missing_key",
            index=index,
            keys=missing,
        )
    unknown = sorted(set(entry) - set(_BAR_KEYS))
    if unknown:
        raise _refuse(
            BARS_INVALID_CODE,
            f"bar {index} carries {', '.join(unknown)}, which a bar does not model",
            reason="unknown_key",
            index=index,
            keys=unknown,
        )
    t = entry["t"]
    if isinstance(t, bool) or not isinstance(t, int):
        raise _refuse(
            BARS_INVALID_CODE,
            f"bar {index} field t must be an integer, got {t!r}",
            reason="wrong_type",
            index=index,
            field="t",
        )
    return Bar(t=t, **{field: _number(entry, field, index) for field in _PRICE_FIELDS})


def validate_sequence(bars: Iterable[Bar]) -> tuple[Bar, ...]:
    """Return ``bars`` as a tuple if non-empty and strictly increasing in ``t``.

    Refuses rather than repairs (spec section 4.5). ``details["index"]`` is the
    position of the first bar that breaks the order.
    """
    sequence = tuple(bars)
    if not sequence:
        raise _refuse(BARS_EMPTY_CODE, "no bars were supplied")
    for index in range(1, len(sequence)):
        previous, current = sequence[index - 1].t, sequence[index].t
        if current == previous:
            raise _refuse(
                BARS_UNORDERED_CODE,
                f"bar {index} repeats t={current} of bar {index - 1}",
                reason="duplicate",
                index=index,
                t=current,
            )
        if current < previous:
            raise _refuse(
                BARS_UNORDERED_CODE,
                f"bar {index} has t={current}, earlier than bar {index - 1} at t={previous}",
                reason="decreasing",
                index=index,
                t=current,
            )
    return sequence


def check_window_covers(bars: Iterable[Bar], walk_start: int) -> None:
    """Refuse unless the bars COVER ``walk_start`` (spec section 5.4).

    A bar whose ``t`` equals ``walk_start`` covers it on either side. The
    sequence is assumed ordered (see :func:`validate_sequence`); an empty one
    is refused here too, so the answer does not depend on which check the
    caller ran first.
    """
    sequence = tuple(bars)
    if not sequence:
        raise _refuse(BARS_EMPTY_CODE, "no bars were supplied")
    first, last = sequence[0].t, sequence[-1].t
    if last < walk_start:
        raise _refuse(
            WINDOW_TOO_SHORT_CODE,
            f"the last bar (t={last}) precedes walk_start={walk_start}; there is nothing to walk",
            reason="ends_before_walk_start",
            first_t=first,
            last_t=last,
            walk_start=walk_start,
        )
    if first > walk_start:
        raise _refuse(
            WINDOW_TOO_SHORT_CODE,
            f"the first bar (t={first}) follows walk_start={walk_start}; "
            "the tape does not cover the interval the entry ladder was live over",
            reason="begins_after_walk_start",
            first_t=first,
            last_t=last,
            walk_start=walk_start,
        )
