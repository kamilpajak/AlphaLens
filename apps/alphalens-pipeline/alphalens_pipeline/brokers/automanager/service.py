"""ManagerService — the formal client<->manager Protocol boundary (PR-8).

Broker-manager extraction memo, section 5.1: the three file/side-effect faces
the auto-manager already exposes — the pick queue (``picks.jsonl``), the
reconcile projection (``build_protection_view`` + ``reconcile_brackets``), and
the alert/report/verdict sinks — become three formal methods:

    submit_intent(TradeIntent)      -> IntentAck{intent_id, status, reason?}
    query_state(intent_ids?)        -> list[PositionState]
    stream_events()                 -> Iterator[ManagerEvent]

This is the client<->manager boundary the extraction epic swaps transports
behind (memo section 6, "Then (separate project)"): once ``brokers/`` moves
into its own repo, a real service (HTTP/gRPC/queue) implements the SAME
:class:`ManagerService` Protocol, and every client-side caller (this module's
:class:`InProcessManagerService`, the acceptance ``ManagerWorld``, a future
CLI shim) is unaffected by the swap.

``InProcessManagerService`` below is the "file journal / direct-call"
transport — it drives the REAL ``control_loop.run_once`` in-process, with NO
network hop. The persistence layer the epic eventually reimplements behind
this same Protocol (an ordered/atomic channel replacing ``O_APPEND``
latest-line-wins, sequence/version vectors) is a GENUINE build, not a
file-for-socket swap (memo section 5.2's honesty note) — that reimplementation
is explicitly OUT OF SCOPE for this module and this PR.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from broker_contract.trade_intent.schema import TradeIntent

from alphalens_pipeline.brokers.automanager import control_loop
from alphalens_pipeline.brokers.automanager.control_loop import LoopDeps, TickReport
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

# Resting SELL leg order types that count as protective stop coverage — mirrors
# the acceptance world's ``_resting_stop_qty`` (Stop / StopIfTraded /
# TrailingStopIfTraded). An OCO Limit take-profit leg does NOT count toward
# stop coverage on its own.
_STOP_ORDER_TYPES = frozenset({"Stop", "StopIfTraded", "TrailingStopIfTraded"})


# --- Value types (memo section 5.1) -------------------------------------------


@dataclass(frozen=True)
class IntentAck:
    """``submit_intent``'s synchronous reply.

    Arming never refuses at submit time (mirrors ``picks.arm_pick``: "Places
    nothing itself; the daemon drains") — a capacity/cap refusal surfaces
    later, via a ``stream_events`` alert on a subsequent drain cycle. The
    ``"refused"`` status is reserved for a future transport that CAN validate
    synchronously (e.g. malformed intent, unknown instrument).
    """

    intent_id: str
    status: Literal["armed", "refused"]
    reason: str | None = None


@dataclass(frozen=True)
class PositionState:
    """``query_state``'s read projection.

    A PURE function of live broker truth, computed fresh on every call — no
    stored status field (memo section 5.1: "State is a pure function computed
    on read from live broker truth... a dropped connection loses nothing; the
    next tick re-derives"). ``terminal`` is always ``False`` here: this
    projection only ever iterates currently-held long positions (see
    :meth:`InProcessManagerService.query_state`), so a genuinely terminal
    (flat, no resting legs) position simply does not appear in the result —
    there is no journaled "closed" flag to read.
    """

    uic: int
    symbol: str
    owned_qty: float
    covered_qty: float
    stop_price: float | None
    tp_price: float | None
    terminal: bool


# --- ManagerEvent — a typed union over the three alert/report/verdict sinks --


@dataclass(frozen=True)
class AlertEvent:
    """A human-facing alert fired this cycle (a degrade, a refusal, a
    KILL/chain transition, ...). Every ``deps.alert`` / ``deps.alert_throttled``
    call the tick actually SENT is mirrored here — this is what makes
    guarantee #5 (never-silent) a direct assertion on the event stream
    (memo section 5.3)."""

    message: str = ""
    kind: Literal["alert"] = "alert"


@dataclass(frozen=True)
class TickReportEvent:
    """The tick-level summary counters (picks placed, exits placed, cancels,
    alerts, orphans, verdict_count, actions) for one ``run_cycle``."""

    report: TickReport
    kind: Literal["tick_report"] = "tick_report"


@dataclass(frozen=True)
class OrderOutcomeEvent:
    """One reconcile verdict surfaced this cycle (FILLED / CANCELLED / a
    divergence / ...).

    RESERVED — see :meth:`InProcessManagerService.run_cycle`'s docstring:
    ``control_loop.run_once`` does not currently return the verdicts it
    computed internally, and this PR does not modify ``run_once`` (hard
    guardrail) to expose them. No ``run_cycle`` call emits this event yet;
    the dataclass exists so the discriminated union's shape is locked in for
    a later PR that either reads verdicts back out of a widened ``TickReport``
    or re-derives them from ``deps.verdicts_fn`` directly.
    """

    verdict: ReconcileVerdict
    kind: Literal["order_outcome"] = "order_outcome"


@dataclass(frozen=True)
class LivenessEvent:
    """Heartbeat/kill/chain liveness, emitted once per ``run_cycle`` alongside
    the ``TickReportEvent`` — the ``stream_events`` analogue of the daemon's
    Prometheus heartbeat + KILL-active gauges."""

    heartbeat_ts: float = 0.0
    kill_active: bool = False
    chain_alive: bool = True
    kind: Literal["liveness"] = "liveness"


ManagerEvent = AlertEvent | TickReportEvent | OrderOutcomeEvent | LivenessEvent


# --- The Protocol (memo section 5.1) ------------------------------------------


@runtime_checkable
class ManagerService(Protocol):
    """The client<->manager boundary (broker-manager extraction memo, section
    5.1). This is the ONE surface a client (the acceptance ``ManagerWorld``
    today; a future CLI shim after the extraction epic) is allowed to depend
    on — never ``control_loop.LoopDeps`` / ``run_once`` directly.

    ``InProcessManagerService`` (below) is the in-process "file journal /
    direct-call" transport implementing this Protocol today. A later,
    network-crossing implementation (the extraction epic) reimplements the
    PERSISTENCE behind these same three methods — not the Protocol itself
    (memo section 5.2): the file journals' ``O_APPEND`` line-atomicity and
    latest-line-per-key semantics are local-filesystem properties that do not
    survive a socket, so that reimplementation is a genuine build, out of
    scope for this module.
    """

    def submit_intent(self, intent: TradeIntent) -> IntentAck: ...

    def query_state(self, intent_ids: Sequence[str] | None = None) -> list[PositionState]: ...

    def stream_events(self) -> Iterator[ManagerEvent]: ...


# A ``deps_factory`` builds the real, fully-wired ``LoopDeps`` for one cycle,
# given (in order): the event-capturing alert sink, the event-capturing
# alert_throttled sink, and the SERVICE's own internal pick queue. The caller
# (``ManagerWorld`` today; ``build_default_deps``'s composition-root
# equivalent for a future prod caller) owns composing the two given sinks with
# whatever REAL underlying delivery it wants (journald, the throttle, a
# Telegram NotificationPort, or — in the acceptance world's case — the plain
# ``self.alerts`` list) — see ``InProcessManagerService``'s class docstring
# for the tee contract.
DepsFactory = Callable[
    [Callable[[str], None], Callable[[str, str], bool], list[TradeIntent]],
    LoopDeps,
]


class InProcessManagerService:
    """The in-process transport implementation of :class:`ManagerService`.

    Owns two pieces of daemon-lifetime state: an internal pick queue (the
    in-memory analogue of ``picks.jsonl``) and an event buffer (the in-memory
    analogue of the alert/report/verdict sinks). Every cycle it asks its
    ``deps_factory`` for a fresh, fully-wired :class:`LoopDeps` — closing over
    the SAME internal pick queue and a pair of event-capturing alert sinks —
    and drives the REAL ``control_loop.run_once`` against it. Nothing about
    the tick logic, safety rails, or protection pass is stubbed; only the
    transport carrying picks in and events out is this module's concern.

    **The alert-capturing tee.** This service builds two tiny callables
    (``_tee_alert`` / ``_tee_alert_throttled``) that do nothing but append an
    :class:`AlertEvent` to the internal buffer, and hands them to
    ``deps_factory``. The CALLER's ``deps_factory`` is expected to wrap those
    with whatever real underlying delivery it wants — e.g. ``deps.alert =
    lambda msg: (real_sink(msg), tee_alert(msg))`` — so that every alert the
    tick actually sends is BOTH delivered for real AND recorded on the event
    stream. That is what turns guarantee #5 (never-silent) into a direct
    assertion on ``stream_events()`` instead of scraping a log (memo section
    5.3).

    **Gotcha for the ``_tee_alert_throttled`` seam:** ``deps.alert_throttled``
    is only ONE of two paths a throttled alert can take. The protection-pass
    executor (``control_loop._make_protection_executor``) is handed the
    SHARED ``_AlertThrottle`` instance directly and calls ``throttle.emit(...)``
    on it for ``PlaceStop`` / ``UpgradeToOco`` / ``AmendStop`` / the executor's
    own ``AlertOnly`` — entirely bypassing the ``deps.alert_throttled`` field.
    A caller that only wraps the ``alert_throttled`` FIELD misses every
    protection-pass degrade. The correct seam is the ``_AlertThrottle``'s own
    BASE sink (the callable passed to ``_AlertThrottle(base_sink)`` at
    construction) — tee-ing there catches a throttled send regardless of
    which of the two call paths triggered it, since both share the one
    instance. See ``tests/brokers/automanager/test_service.py``'s
    ``_ServiceHarness`` and ``acceptance/world.py`` for the reference wiring.
    """

    def __init__(self, deps_factory: DepsFactory) -> None:
        self._deps_factory = deps_factory
        self._picks: list[TradeIntent] = []
        self._events: list[ManagerEvent] = []

    # ==== ManagerService ========================================================

    def submit_intent(self, intent: TradeIntent) -> IntentAck:
        """Append to the internal pick queue; never refuses at submit time
        (mirrors ``picks.arm_pick`` — "places nothing itself; the daemon
        drains"). A capacity/cap refusal surfaces later as an ``AlertEvent``
        on a subsequent ``run_cycle``."""
        self._picks.append(intent)
        return IntentAck(intent_id=intent.intent_id, status="armed")

    def query_state(self, intent_ids: Sequence[str] | None = None) -> list[PositionState]:
        """A pure read: build fresh deps (no I/O beyond the broker reads
        ``build_protection_view`` already performs), project every currently
        long position, and return — never places, cancels, or amends
        anything.

        ``intent_ids`` filtering is BEST-EFFORT: this service already knows
        the ticker for every intent it has been handed (``submit_intent``
        records the full :class:`TradeIntent`), so the filter narrows by
        ticker match. It is ticker-level, not exact-intent-level — a
        ``PositionState`` carries no ``intent_id`` of its own (state is
        derived purely from live broker truth, memo section 5.1), so two
        distinct intents on the same ticker are indistinguishable here. An
        unknown id in ``intent_ids`` simply contributes no symbol (silently
        narrows to nothing for that id, never raises)."""
        deps = self._build_deps()
        records = deps.read_records()
        # read-only projection: never reconciles, so the unbound (inert) exit_policy is intentional
        view = control_loop.build_protection_view(deps.broker, records)

        wanted_symbols: set[str] | None = None
        if intent_ids is not None:
            symbol_by_intent_id = {
                intent.intent_id: intent.instrument.ticker.upper() for intent in self._picks
            }
            wanted_symbols = {
                symbol_by_intent_id[intent_id]
                for intent_id in intent_ids
                if intent_id in symbol_by_intent_id
            }

        states: list[PositionState] = []
        for uic, position in view.long_positions.items():
            symbol = position.instrument.ticker.upper()
            if wanted_symbols is not None and symbol not in wanted_symbols:
                continue
            legs = view.sell_legs_by_uic.get(uic, ())
            covered_qty = sum(
                (leg.amount or 0.0) for leg in legs if leg.order_type in _STOP_ORDER_TYPES
            )
            planned = view.planned_by_uic.get(uic)
            states.append(
                PositionState(
                    uic=uic,
                    symbol=symbol,
                    owned_qty=position.quantity,
                    covered_qty=covered_qty,
                    stop_price=None if planned is None else planned.stop_price,
                    tp_price=None if planned is None else planned.tp_price,
                    terminal=False,
                )
            )
        return states

    def stream_events(self) -> Iterator[ManagerEvent]:
        """Drain the buffered events in FIFO order and CLEAR the buffer. A
        healthy quiet cycle yields only a ``TickReportEvent`` + a
        ``LivenessEvent`` — no ``AlertEvent``. A second call before the next
        ``run_cycle`` yields nothing (the buffer was already drained)."""
        drained, self._events = self._events, []
        yield from drained

    # ==== the in-process transport's own driving method =========================

    def run_cycle(self) -> TickReport:
        """Drive ONE real management tick (``control_loop.run_once``) and
        buffer its observable outcome as events.

        Always appends a ``TickReportEvent`` (the tick's summary counters)
        and a ``LivenessEvent`` (kill/chain/heartbeat). ``AlertEvent``s
        accumulate as a side effect of the tick itself, via the tee'd sinks
        the ``deps_factory`` was handed (see the class docstring) — never
        appended directly here.

        Does NOT emit ``OrderOutcomeEvent`` — see that dataclass's docstring:
        ``run_once`` does not return the verdicts it computed internally, and
        this PR does not modify ``run_once`` to expose them (hard guardrail).
        RESERVED for a later PR."""
        deps = self._build_deps()
        report = control_loop.run_once(deps)
        self._events.append(TickReportEvent(report=report))
        chain = deps.ensure_alive()
        self._events.append(
            LivenessEvent(
                heartbeat_ts=time.time(),
                # D3 (ADR 0016): the per-instance KILL OR the GLOBAL kill —
                # same verdict run_once uses to gate placement (control_loop.
                # _kill_active is the single source of truth so this cannot
                # drift from the gating/heartbeat sites).
                kill_active=control_loop._kill_active(deps),
                chain_alive=bool(getattr(chain, "alive", False)),
            )
        )
        return report

    # ==== internals ==============================================================

    def _build_deps(self) -> LoopDeps:
        def _tee_alert(message: str) -> None:
            self._events.append(AlertEvent(message=message))

        def _tee_alert_throttled(message: str, reason: str) -> bool:
            self._events.append(AlertEvent(message=message))
            return True

        return self._deps_factory(_tee_alert, _tee_alert_throttled, self._picks)


__all__ = [
    "AlertEvent",
    "DepsFactory",
    "InProcessManagerService",
    "IntentAck",
    "LivenessEvent",
    "ManagerEvent",
    "ManagerService",
    "OrderOutcomeEvent",
    "PositionState",
    "TickReportEvent",
]
