"""One formatter for the trade alerts the daemon sends to the operator (#1621).

Call sites build a :class:`TradeEvent` from facts they already hold and pass it
to :func:`render`; they never write the text themselves. Before this module the
entry-fill, stop-fill and take-profit alerts were three f-strings at three call
sites, and they drifted apart (no ticker on the entry fill, no way to tell a
trailed stop from the plan stop, ``sold`` for a SELL that was only submitted).

The text has one fixed order — side, ticker, label, quantity, ``@ price``,
reason, outcome, ``(order id)`` — and a field that is not known is left out,
never rendered as ``None``. The order id stays because it is the only link from
the message to the order in the broker's app.

Since part 3 the same slots also carry the protective stop's own STATE: it was
placed, or its level was raised. Those two are not fills, so they fill the
reason slot with the move rather than with an exit reason — a raise to 62.42
must not read like a fill at 62.42.

Pure: no I/O and no import from ``control_loop``, so the formatter can be tested
without a daemon.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SEPARATOR = " - "
# Below one unit of currency two decimals hide the move; four keep it readable.
_SMALL_PRICE = 1.0


class EventKind(enum.Enum):
    """What happened to an order. The side follows from it: the executor is
    long-only, so an entry is always a BUY and an exit always a SELL."""

    ENTRY_FILLED = "entry_filled"
    EXIT_FILLED = "exit_filled"
    # The SELL was sent, and no fill was read back yet (a take-profit market
    # sell: the live-exit engine does not read the fill price).
    EXIT_SUBMITTED = "exit_submitted"
    # Neither of the two below is a fill. They are the protective stop's own
    # state: it now rests at the venue, or its level moved UP. A long's
    # protective stop is a resting SELL, which is why they share the side.
    STOP_PLACED = "stop_placed"
    STOP_RAISED = "stop_raised"


_SIDE_BY_KIND: Mapping[EventKind, str] = {
    EventKind.ENTRY_FILLED: "BUY",
    EventKind.EXIT_FILLED: "SELL",
    EventKind.EXIT_SUBMITTED: "SELL",
    EventKind.STOP_PLACED: "SELL",
    EventKind.STOP_RAISED: "SELL",
}

# The kinds whose middle slot describes a stop MOVE rather than a fill reason.
# ``ExitReason`` is shared with the fill kinds, so without this split a raise to
# 62.42 would render the same words a FILL at 62.42 does.
_STOP_MOVE_KINDS = frozenset({EventKind.STOP_PLACED, EventKind.STOP_RAISED})


class ExitReason(enum.Enum):
    """Why an exit happened, in words the operator reads."""

    TAKE_PROFIT = "take-profit"
    PLAN_STOP = "plan stop"
    TRAILED_STOP = "trailed stop"
    REANCHORED_STOP = "re-anchored stop"
    # The fallback when the journal names a stop move this module does not
    # know. The alert still goes out; only the detail is lost.
    STOP = "stop"


# One entry per reaction-plan kind of the TradeIntent contract
# (``broker_contract.trade_intent.codec._REACTION_BY_KIND``): the journal marker
# the kind's executor writes when it moves the resting stop, or ``None`` when
# the kind moves no stop. A test compares the keys with the contract's registry,
# so a new kind cannot reach a stop-fill alert without being named here.
STOP_MOVE_MARKER_BY_REACTION_KIND: Mapping[str, str | None] = {
    "trailing_stop": "trailed",
    "reanchor_on_fill": "reanchored",
    # Reserves a tag only; no executor acts on it (schema.py ``ModelPush``).
    "model": None,
}

REASON_BY_STOP_MARKER: Mapping[str, ExitReason] = {
    "trailed": ExitReason.TRAILED_STOP,
    "reanchored": ExitReason.REANCHORED_STOP,
}


@dataclass(frozen=True)
class TradeEvent:
    """The facts behind one trade alert. Every optional field may be ``None``
    when the call site does not know it; :func:`render` leaves it out."""

    kind: EventKind
    ticker: str
    label: str | None
    # ``None`` only where no quantity is a FACT at the call site. A stop RAISE
    # is such a site: it is a level change, and the only quantity the pure
    # managed-exit build holds is the ladder's reference qty, which is not the
    # resting stop's size. A plausible number there would read as the size of
    # the protected position.
    qty: float | None = None
    price: float | None = None
    reason: ExitReason | None = None
    # The stop or target level behind ``reason`` (for example the trailed
    # level); for the stop-move kinds, the level the stop now sits at.
    level: float | None = None
    # Where a raised stop came FROM. Only the stop-move kinds carry it.
    previous_level: float | None = None
    order_id: str | None = None
    position_closed: bool | None = None


def stop_reason(marker_kind: str | None) -> ExitReason:
    """The reason for a stop fill, from the newest marker that moved THIS stop.

    ``None`` (nothing moved it) is the plan stop. A marker this module does not
    know becomes the generic :attr:`ExitReason.STOP` with one WARNING: a missing
    detail must never cost the operator the alert."""
    if marker_kind is None:
        return ExitReason.PLAN_STOP
    reason = REASON_BY_STOP_MARKER.get(marker_kind)
    if reason is None:
        logger.warning(
            "trade alert: no stop reason for journal marker %r — rendered as a plain stop",
            marker_kind,
        )
        return ExitReason.STOP
    return reason


def format_price(price: float) -> str:
    """Two decimals at or above 1.00, four below it."""
    return f"{price:.2f}" if abs(price) >= _SMALL_PRICE else f"{price:.4f}"


def _with_level(text: str, level: float | None) -> str:
    """``text`` followed by ``level``, or ``text`` alone when it is not known."""
    return text if level is None else f"{text} {format_price(level)}"


def _stop_move_text(event: TradeEvent) -> str | None:
    """The middle slot for a stop-state kind: what happened to the protective
    stop, and at which level(s)."""
    if event.kind is EventKind.STOP_PLACED:
        return _with_level("stop placed", event.level)
    # A raise names its cause when the call site knows it (which journaled floor
    # moved the stop) and BOTH levels, because the MOVE is the fact the operator
    # is missing: the new level alone does not say how much it locked in.
    moved = f"{event.reason.value} raised" if event.reason is not None else "stop raised"
    if event.previous_level is None or event.level is None:
        # A half-known move is still worth saying; the arrow form needs both.
        return _with_level(moved, event.level)
    return f"{moved} {format_price(event.previous_level)} -> {format_price(event.level)}"


def _reason_text(event: TradeEvent) -> str | None:
    if event.kind in _STOP_MOVE_KINDS:
        return _stop_move_text(event)
    if event.reason is None:
        return None
    text = event.reason.value
    if event.reason is ExitReason.TAKE_PROFIT and event.kind is EventKind.EXIT_SUBMITTED:
        return f"{text}, market sell sent"
    if event.level is not None:
        return f"{text} {format_price(event.level)}"
    return text


def _outcome_text(event: TradeEvent) -> str | None:
    if event.position_closed is None:
        return None
    # "still open" covers both a take-profit tranche that sells part of the
    # position by design and a stop that filled only in part; the quantity says
    # how much went.
    return "position closed" if event.position_closed else "position still open"


def render(event: TradeEvent) -> str:
    """The one line the operator reads for ``event``."""
    head = [_SIDE_BY_KIND[event.kind], event.ticker]
    if event.label:
        head.append(event.label)
    if event.qty is not None:
        head.append(f"{event.qty:g}")
    if event.price is not None:
        head.append(f"@ {format_price(event.price)}")
    parts = [" ".join(head)]
    parts.extend(text for text in (_reason_text(event), _outcome_text(event)) if text)
    message = _SEPARATOR.join(parts)
    if event.order_id:
        message = f"{message} (order {event.order_id})"
    return message


__all__ = [
    "REASON_BY_STOP_MARKER",
    "STOP_MOVE_MARKER_BY_REACTION_KIND",
    "EventKind",
    "ExitReason",
    "TradeEvent",
    "format_price",
    "render",
    "stop_reason",
]
