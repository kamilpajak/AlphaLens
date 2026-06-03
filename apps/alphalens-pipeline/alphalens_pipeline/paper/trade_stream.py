"""Always-on Alpaca ``trade_updates`` daemon — sub-second fill detection.

The paper harness attaches the protective OCO ladder only on the next reconcile
poll AFTER the first entry fill is observed, and the poll runs every 30 min
(``exit_manager.py`` attach-once gate ``net_open_qty>0 and not has_exit_ladder``,
reached only inside ``reconcile_orders``). So a filled position can sit with no
stop for up to ~30 min — worst at the 13:30 ET opening cross. This daemon holds
one account-scoped ``trade_updates`` WebSocket and, on a fill, fires the SAME
``reconcile_orders`` the timer fires — within seconds instead of up to 30 min.

Honest ceiling: Alpaca cannot give broker-server-side atomic protection for our
multi-tier partial-filling entry ladder (bracket children stay held until the
parent FULLY fills), so this is "fast WS detection + WE attach", which depends
on this process being alive. The 30-min reconcile poll STAYS as the gap-recovery
and wall-clock backstop (TTL, 42-day time-stop, gross_guard, #404 desync). True
broker-server-side protection is the Saxo-live track, not Alpaca.

Design invariants:
  * SINGLE-WRITER — the daemon NEVER writes the ledger. It only calls
    ``reconcile_orders`` (the sole ledger writer). A single worker thread runs
    every reconcile, so two reconciles can never overlap on the same ledger.
  * NEVER-RAISE handler — a handler exception that escaped would (per alpaca-py
    0.43.4) leave the consumer loop wedged with no crash, so ``Restart=`` never
    fires. The handler is fully wrapped; a bad event is logged and dropped.
  * NON-BLOCKING handler — ``reconcile_orders`` is sync sqlite + N sync REST
    calls; running it inline would freeze the WS consumer and risk a ping
    timeout. The handler only sets a flag + wakes the worker thread.
  * RECONNECT RECOVERY — alpaca-py exposes no reconnect callback, so a fill
    landing during a reconnect gap is recovered by the worker's periodic safety
    resync (every ``RESYNC_INTERVAL_S``) and, as the hard guarantee, the 30-min
    poll. A full resync also runs once at connect, before subscribing.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphalens_pipeline.observability.textfile import emit_domain_metrics
from alphalens_pipeline.paper.reconciler import reconcile_orders

if TYPE_CHECKING:
    from alphalens_pipeline.paper.broker import BrokerClient

logger = logging.getLogger(__name__)

_METRIC_JOB = "paper-trade-stream"

# How often the worker runs a safety reconcile when no event woke it. Recovers
# reconnect-gap fills sub-minute (the 30-min poll is the hard backstop). Also
# advances the last-reconcile gauge so the staleness alert can tell a live loop
# from a dead one on a quiet (event-free) weekend.
RESYNC_INTERVAL_S = 75.0

# How long the worker waits for an in-flight reconcile to finish on shutdown.
_JOIN_TIMEOUT_S = 30.0

# Events that change protection state -> trigger a reconcile.
#   partial_fill -> sub-second SL attach on the filled qty + #401 cancel-unfilled
#                   (the headline win: the net_open_qty>0 gate fires on the FIRST partial)
#   fill         -> same + drives FILLED / exit-leg outcome transitions
#   canceled     -> entry CANCELED-with-zero-fills -> UNFILLED; exit-leg cancel changes coverage
#   expired      -> entry TTL/TIF expiry -> UNFILLED, do not wait 30 min
#   rejected     -> wash-trade / insufficient-BP surfaces fast; plan settles
#   replaced     -> Alpaca auto-adjusts an OCO stop qty; cumulative filled_qty changed
#   done_for_day -> cheap end-of-day settle of a still-open order
TRIGGER_EVENTS = frozenset(
    {"fill", "partial_fill", "canceled", "expired", "rejected", "replaced", "done_for_day"}
)

# Our cancel/replace did NOT take -> the ledger may believe an action happened
# that did not. Do NOT reconcile-storm; surface a counter so the 30-min poll +
# #404 phantom-guard catching the desync is at least observable.
ACTION_REJECTED_EVENTS = frozenset({"order_cancel_rejected", "order_replace_rejected"})

# Intermediate acks / status-only noise. Triggering a reconcile on these would
# burn a broker.get_order sweep with no state change (a reconcile-storm risk).
IGNORE_EVENTS = frozenset(
    {
        "new",
        "accepted",
        "accepted_for_bidding",
        "pending_new",
        "pending_cancel",
        "pending_replace",
        "stopped",
        "calculated",
        "suspended",
        "held",
        "restated",
    }
)


def classify_event(name: str) -> str:
    """Map an event name to one of: ``trigger`` / ``action_rejected`` / ``ignore`` / ``unknown``.

    Unknown (unrecognised) events fall through to ``unknown`` and are NOT
    triggered — fail-safe: never burn a reconcile on an event we do not model.
    """
    if name in TRIGGER_EVENTS:
        return "trigger"
    if name in ACTION_REJECTED_EVENTS:
        return "action_rejected"
    if name in IGNORE_EVENTS:
        return "ignore"
    return "unknown"


def _event_name(data: Any) -> str:
    """Normalise a TradeUpdate's ``event`` (enum or raw string) to a lowercase str."""
    raw = getattr(data, "event", data)
    raw = getattr(raw, "value", raw)  # unwrap a TradeEvent enum
    return str(raw).lower()


class PaperTradeStreamDaemon:
    """Hold one ``trade_updates`` connection; trigger ``reconcile_orders`` on fills."""

    def __init__(
        self,
        *,
        stream: Any,
        broker: BrokerClient,
        ledger_path: Path,
        account: str = "test",
        reconcile: Callable[..., Any] = reconcile_orders,
        emit: Callable[[str, Any], Any] = emit_domain_metrics,
        resync_interval_s: float = RESYNC_INTERVAL_S,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._stream = stream
        self._broker = broker
        self._ledger_path = ledger_path
        self._account = account
        self._reconcile = reconcile
        self._emit = emit
        self._resync_interval_s = resync_interval_s
        self._now = now

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._pending = False
        self._worker: threading.Thread | None = None

        self._connected = 0
        self._last_event_ts: float | None = None
        self._last_reconcile_ts: float | None = None
        self._action_rejected = 0
        self._unknown_events = 0

    # -- event handling -------------------------------------------------

    async def _on_trade_update(self, data: Any) -> None:
        """alpaca-py async handler. NEVER raises (a raise wedges the consumer)."""
        try:
            name = _event_name(data)
            self._last_event_ts = self._now()
            kind = classify_event(name)
            if kind == "trigger":
                self._request_reconcile()
            elif kind == "action_rejected":
                self._action_rejected += 1
                logger.warning(
                    "trade_updates %s for order %s — our cancel/replace did not take; "
                    "leaving the desync for the poll + #404 phantom-guard",
                    name,
                    getattr(getattr(data, "order", None), "id", "?"),
                )
            elif kind == "unknown":
                self._unknown_events += 1
                logger.warning("trade_updates unknown event %r — ignored (fail-safe)", name)
            # ignore: liveness already recorded via _last_event_ts
        except Exception:
            logger.exception("trade_updates handler swallowed an error")

    def _request_reconcile(self) -> None:
        """Flag a reconcile + wake the worker. Non-blocking; never touches sqlite."""
        with self._lock:
            self._pending = True
        self._wake.set()

    # -- single-writer worker -------------------------------------------

    def _run_reconcile_once(self) -> None:
        """Call the SOLE ledger writer. Only ever invoked from one thread."""
        try:
            self._reconcile(
                ledger_path=self._ledger_path, broker=self._broker, account=self._account
            )
            self._last_reconcile_ts = self._now()
        except Exception:
            logger.exception("trade-stream reconcile failed; will retry next tick / poll")
        self._emit_health()

    def _tick(self) -> None:
        """One worker iteration: wait for a wake or the resync timeout, then reconcile.

        Either path reconciles: a wake = event-triggered (bursts coalesce to one
        because pending is cleared before the run); a timeout = periodic safety
        resync. Both advance the last-reconcile gauge.
        """
        self._wake.wait(self._resync_interval_s)
        self._wake.clear()
        with self._lock:
            self._pending = False
        self._run_reconcile_once()

    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
        # Stop the SDK stream from HERE — the worker is a non-loop thread, so
        # stream.stop() (which does run_coroutine_threadsafe(...).result()) can
        # complete. Calling it from _on_signal would run on the loop thread and
        # deadlock on .result() (the loop waiting on itself).
        self._stop_stream()

    # -- observability --------------------------------------------------

    def _emit_health(self) -> None:
        """Emit daemon-health gauges. Never raises (mirrors the cache emitters)."""
        acct = self._account
        metrics: dict[str, float | int] = {
            f'alphalens_paper_trade_stream_connected{{account="{acct}"}}': self._connected,
            f'alphalens_paper_trade_stream_heartbeat_timestamp_seconds{{account="{acct}"}}': self._now(),
            f'alphalens_paper_trade_stream_action_rejected_total{{account="{acct}"}}': self._action_rejected,
        }
        if self._last_reconcile_ts is not None:
            metrics[
                f'alphalens_paper_trade_stream_last_reconcile_timestamp_seconds{{account="{acct}"}}'
            ] = self._last_reconcile_ts
        try:
            self._emit(_METRIC_JOB, metrics)
        except Exception:
            logger.exception("trade-stream health emit failed")

    # -- lifecycle ------------------------------------------------------

    def run(self) -> None:
        """Blocking entry point (systemd ``ExecStart``). Returns only on shutdown."""
        self._install_signal_handlers()
        self._connected = 1
        # Connect-time full resync BEFORE subscribing: recover any fill that
        # landed while we were disconnected, before the event stream opens.
        self._run_reconcile_once()
        self._stream.subscribe_trade_updates(self._on_trade_update)
        self._worker = threading.Thread(
            target=self._reconcile_loop, name="trade-stream-reconcile", daemon=True
        )
        self._worker.start()
        try:
            # Race guard: a signal during the connect window already set _stop;
            # skip the blocking run() (nothing would stop it) and go to cleanup.
            if not self._stop.is_set():
                self._stream.run()  # blocking; owns its own asyncio loop
        finally:
            self._shutdown()

    def _install_signal_handlers(self) -> None:
        # alpaca-py's run() does NOT install SIGTERM handling — we own it so a
        # systemd stop drains cleanly. Best-effort: not all threads/platforms
        # allow signal registration.
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):  # not main thread / unsupported
                logger.debug("could not install handler for %s", sig)

    def _on_signal(self, _signum: int, _frame: Any) -> None:
        # Signal handlers run on the MAIN (loop) thread. Only set flags here:
        # the worker thread stops the SDK stream (see _reconcile_loop), because
        # stream.stop() blocks on the loop and would deadlock if called here.
        self._stop.set()
        self._wake.set()

    def _stop_stream(self) -> None:
        # Guard the pre-subscribe window where the SDK loop is not yet running:
        # calling stop() then would raise.
        loop = getattr(self._stream, "_loop", None)
        if loop is not None and getattr(loop, "is_running", lambda: False)():
            try:
                self._stream.stop()
            except Exception:
                logger.exception("error stopping trade stream")

    def _shutdown(self) -> None:
        self._stop.set()
        self._wake.set()  # release a worker blocked on the resync wait
        if self._worker is not None:
            self._worker.join(timeout=_JOIN_TIMEOUT_S)  # let an in-flight reconcile finish
        self._connected = 0
        self._emit_health()


__all__ = [
    "ACTION_REJECTED_EVENTS",
    "IGNORE_EVENTS",
    "RESYNC_INTERVAL_S",
    "TRIGGER_EVENTS",
    "PaperTradeStreamDaemon",
    "classify_event",
]
