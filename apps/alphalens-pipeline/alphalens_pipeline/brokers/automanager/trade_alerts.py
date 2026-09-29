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


_SIDE_BY_KIND: Mapping[EventKind, str] = {
    EventKind.ENTRY_FILLED: "BUY",
    EventKind.EXIT_FILLED: "SELL",
    EventKind.EXIT_SUBMITTED: "SELL",
}


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
    qty: float
    price: float | None = None
    reason: ExitReason | None = None
    # The stop or target level behind ``reason`` (for example the trailed level).
    level: float | None = None
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


def _reason_text(event: TradeEvent) -> str | None:
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
