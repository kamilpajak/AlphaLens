"""Pluggable fill-detection interface + the MVP polling implementation.

FillSource is the seam the control loop asks "what changed since last tick?".
The MVP ships PollingFillSource over the read-only reconcile_brackets engine
(the reconciliation floor); a StreamingFillSource (WebSocket push) is a phase-B
drop-in behind the SAME poll_tick interface with NO control-loop change —
deliberately NOT built here. poll_tick re-reads the journal each call (via
load_records) so a freshly-placed bracket is visible, recomputes every verdict,
and emits one Transition per entry the FIRST time its status enters a reportable
state (FILLED / PARTIALLY_FILLED / terminal). A plain WORKING entry — including
WORKING(PAST-TTL!) — emits nothing (TTL divergence is an alert, not a fill).
Downstream idempotency (a re-observed FILLED after restart must not double-place
a stop) is the position-manager's job; the prev-status map only suppresses
re-emitting the SAME state on a later tick.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from alphalens_pipeline.brokers.contract import Broker, OrderStatus
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict, reconcile_brackets

TransitionKind = Literal["FILLED", "TERMINAL", "PARTIAL"]

_FILLED = OrderStatus.FILLED.value
_PARTIAL = OrderStatus.PARTIALLY_FILLED.value
_TERMINAL_TOKENS = frozenset(
    {OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value}
)


@dataclass(frozen=True)
class Transition:
    order_id: str
    kind: TransitionKind
    ticker: str = ""
    filled_quantity: float = 0.0
    verdict: ReconcileVerdict | None = None


@runtime_checkable
class FillSource(Protocol):
    def poll_tick(self) -> list[Transition]: ...


# StreamingFillSource (phase-B, WebSocket push) is intentionally NOT defined
# here. It will implement this SAME FillSource Protocol and layer ABOVE the
# polling floor with no control-loop change; adding it also adds the
# PUT /streaming/ws/authorize reauth to the session-keeper.


def _classify(status: str) -> TransitionKind | None:
    if status == _FILLED:
        return "FILLED"
    if status == _PARTIAL:
        return "PARTIAL"
    if status in _TERMINAL_TOKENS:
        return "TERMINAL"
    return None


class PollingFillSource:
    """MVP fill-source: diff successive reconcile_brackets snapshots."""

    def __init__(
        self,
        broker: Broker,
        load_records: Callable[[], Iterable[Mapping[str, Any]]],
        *,
        today: dt.date | None = None,
    ):
        self._broker = broker
        self._load_records = load_records
        self._today = today
        self._prev_status: dict[str, str] = {}

    def poll_tick(self) -> list[Transition]:
        records = list(self._load_records())
        verdicts = reconcile_brackets(records, self._broker, today=self._today)
        transitions: list[Transition] = []
        for verdict in verdicts:
            order_id = verdict.entry_order_id
            if not order_id:
                continue
            previous = self._prev_status.get(order_id)
            current = verdict.status
            self._prev_status[order_id] = current
            if current == previous:
                continue
            kind = _classify(current)
            if kind is None:
                continue
            transitions.append(
                Transition(
                    order_id=order_id,
                    kind=kind,
                    ticker=verdict.ticker,
                    filled_quantity=float(verdict.details.get("filled_quantity") or 0.0),
                    verdict=verdict,
                )
            )
        return transitions


__all__ = ["FillSource", "PollingFillSource", "Transition", "TransitionKind"]
