"""Position-manager — the "act" half of the auto-manager loop.

Pure decision function: one reconcile verdict + a per-tick BrokerView (the
control loop assembles it from the broker + the standalone-stop journal) -> the
single Action to take. No I/O — the control loop executes the returned Action.

MVP action set (design memo Components §9):
  entry FILLED, position open, no protective stop yet
      -> PlaceStandaloneStop(realized filled qty, journaled disaster stop)
  round-trip closed / CANCELLED / REJECTED / EXPIRED -> CancelRemaining
  PAST-TTL / divergence / UNRESOLVED -> AlertOnly(reason) (never auto-cancel)
  else (still WORKING, or already protected) -> NoOp

Realized-qty rule (Risk 2): the stop MUST size to the REALIZED entry fill
(verdict.details['filled_quantity']), NEVER planned verdict.qty — a planned-qty
stop over-hedges and can flip short after a partial fill.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from alphalens_pipeline.brokers.contract import OrderStatus
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

# Exact reconcile note string this module keys on (brokers/reconcile.py
# _reconcile_filled). Pinned as a constant so a reconcile-side wording change
# fails these tests loudly rather than silently mis-classifying a live position.
_NOTE_ROUND_TRIP_CLOSED = "round trip closed (FIFO pair)"

_TERMINAL_NON_FILLED = frozenset(
    {OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value}
)


@dataclass(frozen=True)
class DisasterStop:
    uic: int
    side: str  # "SELL" for a long position's protective stop
    stop_price: float


@dataclass(frozen=True)
class BrokerView:
    protected_request_ids: frozenset[str]  # entries already carrying a live standalone stop
    disaster_stops: Mapping[
        str, DisasterStop
    ]  # journaled disaster stop per entry client_request_id
    working_children: Mapping[str, tuple[str, ...]]  # request_id -> still-working exit order ids


@dataclass(frozen=True)
class PlaceStandaloneStop:
    qty: float
    stop_price: float


@dataclass(frozen=True)
class CancelRemaining:
    pass


@dataclass(frozen=True)
class AlertOnly:
    reason: str


@dataclass(frozen=True)
class NoOp:
    pass


Action = PlaceStandaloneStop | CancelRemaining | AlertOnly | NoOp


def advance(verdict: ReconcileVerdict, broker_view: BrokerView) -> Action:
    """One verdict -> the single MVP Action (pure; no side effects)."""
    request_id = str(verdict.details.get("client_request_id") or "")
    if verdict.divergence:
        return AlertOnly(verdict.reason or f"{verdict.ticker}: divergence — {verdict.verdict}")
    if verdict.unresolved:
        return AlertOnly(verdict.reason or f"{verdict.ticker}: {verdict.verdict}")
    if verdict.status == OrderStatus.FILLED.value:
        return _advance_filled(verdict, broker_view, request_id)
    if verdict.status in _TERMINAL_NON_FILLED:
        return CancelRemaining()
    if verdict.status == OrderStatus.PARTIALLY_FILLED.value:
        # Risk 2: the entry took a partial fill, so the position is open with NO
        # standalone stop yet (the MVP places the stop only on a FULL fill;
        # resize-on-partial is phase B). Alert rather than sit silent — this is
        # an unprotected live position.
        filled = verdict.details.get("filled_quantity")
        return AlertOnly(
            f"{verdict.ticker}: entry PARTIALLY_FILLED (order {verdict.entry_order_id}, "
            f"filled {filled!r}) — position open with no standalone stop yet"
        )
    return NoOp()  # still WORKING, not past TTL


def _advance_filled(verdict: ReconcileVerdict, broker_view: BrokerView, request_id: str) -> Action:
    if verdict.note == _NOTE_ROUND_TRIP_CLOSED:
        return CancelRemaining()
    if request_id in broker_view.protected_request_ids:
        return NoOp()  # standalone stop already placed on a prior tick
    filled = verdict.details.get("filled_quantity")
    try:
        realized_qty = float(filled)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        realized_qty = 0.0
    if realized_qty <= 0:
        return AlertOnly(
            f"{verdict.ticker}: entry FILLED but realized fill qty is unknown "
            f"({filled!r}) — refusing to size a standalone stop"
        )
    disaster = broker_view.disaster_stops.get(request_id)
    if disaster is None:
        return AlertOnly(
            f"{verdict.ticker}: entry FILLED but no journaled disaster stop for "
            f"request {request_id!r} — cannot protect the position"
        )
    return PlaceStandaloneStop(qty=realized_qty, stop_price=disaster.stop_price)


__all__ = [
    "Action",
    "AlertOnly",
    "BrokerView",
    "CancelRemaining",
    "DisasterStop",
    "NoOp",
    "PlaceStandaloneStop",
    "advance",
]
