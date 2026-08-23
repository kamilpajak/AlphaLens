"""Stream<->loop glue for the dark Saxo streaming reader (ADR 0014, SIM-only).

Design memo: ``docs/research/saxo_streaming_design_2026_07_24.md`` (LOCKED),
sections "Trigger wiring (streaming_trigger.py)" + "Thread model".

:class:`StreamTrigger` is the ONLY thing bridging the background streaming reader
and the single-threaded protection loop. It owns exactly four cross-thread
surfaces and NOTHING safety-critical:

* a ``threading.Event`` (``wake_event``) — the reader's ONLY visible effect on
  the main loop is making this Event fire, which turns a blocking backstop wait
  into an EARLY wake. Multiple setters (stream thread + the trailing-debounce
  timer), single clearer (main thread) — stdlib-thread-safe, idempotent.
* ``last_message_epoch`` — a single monotonic float, SINGLE-WRITER = the stream
  thread (``on_trigger`` / ``on_heartbeat``). The main thread only READS it via
  :meth:`seconds_since_last_message` for its own staleness alert; a torn read is
  benign (it only nudges a threshold). The trailing timer NEVER writes it.
  ONE narrow exception: :meth:`reset_liveness` is a main-thread write, legal
  only while ``is_running()`` has just returned False — the reader thread is
  dead at that instant, so no concurrent writer exists (rearm design memo
  ``saxo_stream_breaker_rearm_design_2026_08_22.md`` §4.6).
* the ``_current_token`` holder — SINGLE-WRITER = the main thread via
  :meth:`push_token` (forwarded to the client). The reader only ever READS it.
* the streaming client's thread lifecycle (``start`` / ``stop``).

It NEVER runs reconcile, opens a journal, or touches throttle / executor /
oco_lag_counts / TickReport — the main thread stays the SOLE executor of
everything that decides or mutates protection, so the concurrency races a naive
second-executor design would create are impossible by construction.

Trailing-edge debounce (adversary-1 fix): a burst of stream frames must not spin
the main loop through one ``run_once`` per frame, but the LAST burst member must
never be swallowed (a leading-edge-only debounce would drop the final fill until
the ~45s poll backstop). So the first member of a burst wakes IMMEDIATELY (the
sub-second latency win) and, once the stream goes quiet for
``STREAM_DEBOUNCE_S``, one coalesced trailing wake guarantees the final REST
state is reconciled. The debounce lives entirely off the main loop's wait path —
the loop only ever waits on the plain Event.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from alphalens_pipeline.brokers.saxo.streaming import (
    SIM_STREAMING_BASE_URL,
    SaxoStreamingClient,
    StreamTuning,
)

if TYPE_CHECKING:
    from alphalens_pipeline.brokers.saxo.client import SaxoClient
    from alphalens_pipeline.brokers.saxo.tokens import TokenProvider

# Trailing-edge debounce window (seconds). The first burst member wakes
# immediately; the last yields a coalesced wake within this window of the stream
# going quiet. Configurable via ALPHALENS_BROKER_STREAM_DEBOUNCE_S (read in the
# control loop, not here).
DEFAULT_STREAM_DEBOUNCE_S = 1.0

# Default streaming-client staleness before it forces a reconnect. Kept <=
# poll_seconds so the main-thread stale alert never lags the already-covered
# protection. Configurable via ALPHALENS_BROKER_STREAM_STALE_S.
DEFAULT_STREAM_STALE_S = 45.0


# A timer handle only needs to start and cancel — ``threading.Timer`` satisfies
# it, and tests inject a deterministic fake.
_TimerFactory = Callable[[float, Callable[[], None]], Any]
_ClientFactory = Callable[..., Any]


def default_context_id_factory() -> str:
    """Mint a fresh streaming contextId — <=50 chars, ``[a-zA-Z0-9-]`` (the Saxo
    constraint). ONE home for the format (moved out of
    ``control_loop._build_stream_handles``, rearm design memo §4.3) so the
    initial context and every :meth:`StreamTrigger.rearm` rotation stay
    consistent. Rotation is mandatory on rearm: the trip-time subscription
    DELETE is best-effort and most likely to have failed during the outage, and
    the price-stream reliability contract mandates a fresh contextId per
    connection after the 2026-08-10 incident."""
    return f"almgr-{os.getpid()}-{int(time.time())}"


def _default_client_factory(**kwargs: Any) -> SaxoStreamingClient:
    """Build the real SIM-only streaming client. Its constructor runs the SIM
    rail, so a live streaming host is refused at the trigger boundary too."""
    return SaxoStreamingClient(**kwargs)


class StreamTrigger:
    """The single stream<->loop glue: owns the wake Event, the single-writer
    liveness epoch, the trailing-edge debounce, and the streaming client's
    lifecycle. Constructs (but does not start) the client in ``__init__``; the
    reader thread is spawned only by :meth:`start`."""

    def __init__(
        self,
        *,
        token_provider: TokenProvider,
        subscriber: SaxoClient,
        context_id: str,
        streaming_base_url: str = SIM_STREAMING_BASE_URL,
        debounce_s: float = DEFAULT_STREAM_DEBOUNCE_S,
        client_stale_after_s: float = DEFAULT_STREAM_STALE_S,
        monotonic: Callable[[], float] = time.monotonic,
        timer_factory: _TimerFactory = threading.Timer,
        client_factory: _ClientFactory | None = None,
        context_id_factory: Callable[[], str] = default_context_id_factory,
    ) -> None:
        self._monotonic = monotonic
        self._debounce_s = debounce_s
        self._timer_factory = timer_factory
        self._context_id_factory = context_id_factory

        self._wake_event = threading.Event()

        # SINGLE-WRITER = stream thread (on_trigger / on_heartbeat). None until
        # the first message. The main thread only reads it.
        self._last_message_epoch: float | None = None

        # Debounce state, guarded by ``_lock`` — touched ONLY by the stream
        # thread (on_trigger) and the trailing-timer thread, NEVER by the main
        # protective path, so this lock can never stall reconcile.
        self._lock = threading.Lock()
        self._last_trigger_mono: float = float("-inf")
        self._trailing_deadline: float | None = None
        self._trailing_timer: Any | None = None

        factory = client_factory or _default_client_factory
        self._client = factory(
            token_provider=token_provider,
            subscriber=subscriber,
            context_id=context_id,
            on_trigger=self.on_trigger,
            on_heartbeat=self.on_heartbeat,
            streaming_base_url=streaming_base_url,
            tuning=StreamTuning(stale_after_s=client_stale_after_s),
        )

    # ----- surfaces the main loop reads -----

    @property
    def wake_event(self) -> threading.Event:
        """The plain Event the loop waits on. The debounce never sits on this
        path — it only ever calls ``set()``."""
        return self._wake_event

    def seconds_since_last_message(self) -> float | None:
        """Elapsed monotonic seconds since the last stream message, or ``None``
        before any message. Main-thread read for the staleness alert; clamped at
        0 so a benign torn/backward read never reports negative."""
        epoch = self._last_message_epoch
        if epoch is None:
            return None
        return max(0.0, self._monotonic() - epoch)

    @property
    def is_streaming(self) -> bool:
        """False once the client's circuit breaker has shut the stream to
        poll-only."""
        return self._client.is_streaming

    @property
    def is_started(self) -> bool:
        return self._client.is_started

    def is_running(self) -> bool:
        """True while the reader THREAD is alive (rearm design memo §4.2). The
        truthful liveness read the tick's episode machine keys on —
        ``is_streaming`` alone misses a reader that crashed without tripping."""
        return self._client.is_running()

    @property
    def frames_delivered(self) -> int:
        """Monotonic count of real server frames — the delivery proof. The
        liveness epoch is NOT delivery evidence (a bare subscribe dispatch
        stamps it, memo §7.7); this counter is."""
        return self._client.frames_delivered

    @property
    def trips_total(self) -> int:
        """Monotonic breaker-trip count — a trip whose whole lifetime falls
        between two ticks is still countable (memo §7.1)."""
        return self._client.trips_total

    @property
    def consecutive_failures(self) -> int:
        """Current reconnect-failure streak, for the gauge."""
        return self._client.consecutive_failures

    # ----- callbacks the streaming reader fires (stream thread) -----

    def on_trigger(self) -> None:
        """A positions/orders DATA frame arrived. Stamp liveness and, via the
        trailing-edge debounce, wake the loop: immediately on the first member of
        a burst (sub-second win), then once more when the burst goes quiet so the
        LAST member is never swallowed."""
        now = self._monotonic()
        self._last_message_epoch = now
        fire_leading = False
        with self._lock:
            in_burst = (now - self._last_trigger_mono) < self._debounce_s
            self._last_trigger_mono = now
            if in_burst:
                # Extend the coalescing deadline; ensure exactly one live timer.
                self._trailing_deadline = now + self._debounce_s
                if self._trailing_timer is None:
                    self._schedule_trailing_locked(self._debounce_s)
            else:
                fire_leading = True
        if fire_leading:
            self._wake_event.set()

    def on_heartbeat(self, ts: float) -> None:
        """A heartbeat / control-liveness frame arrived — update liveness ONLY,
        never a wake (heartbeats are not fills)."""
        self._last_message_epoch = ts

    # ----- trailing-edge debounce internals (stream + timer threads) -----

    def _schedule_trailing_locked(self, delay: float) -> None:
        """Arm the single trailing timer. Caller holds ``_lock``."""
        timer = self._timer_factory(delay, self._on_trailing_fire)
        timer.daemon = True
        self._trailing_timer = timer
        timer.start()

    def _on_trailing_fire(self) -> None:
        """Timer-thread callback. If the debounce deadline moved out (more burst
        members arrived), reschedule and wait; otherwise emit the ONE coalesced
        trailing wake. It only ever sets the Event — it NEVER writes the
        single-writer liveness epoch (thread-partition invariant)."""
        with self._lock:
            self._trailing_timer = None
            deadline = self._trailing_deadline
            if deadline is None:
                return
            remaining = deadline - self._monotonic()
            if remaining > 0:
                self._schedule_trailing_locked(remaining)
                return
            self._trailing_deadline = None
        self._wake_event.set()

    # ----- lifecycle + token push (main thread) -----

    def push_token(self, token: str) -> None:
        """Main thread hands the current bearer to the reader (never pulled by
        the reader itself, so it can never stall on the token flock)."""
        self._client.push_token(token)

    def start(self) -> bool:
        """Spawn the reader daemon thread. Returns False (poll-only) when the
        client refuses to start — e.g. under a static token provider."""
        return self._client.start()

    def rearm(self) -> bool:
        """MAIN THREAD ONLY (single caller: run_daemon's tick — rearm design
        memo §4.3). Mint a FRESH contextId via the injected factory and delegate
        to the client's spawn-guarded ``rearm()``. Returns True iff a new reader
        thread is running."""
        return self._client.rearm(self._context_id_factory())

    def reset_liveness(self) -> None:
        """MAIN-THREAD write clearing the liveness epoch at a re-arm, so an
        hours-old epoch cannot fire the throttled 'stream-dead' alert against a
        fresh trial (rearm design memo §4.6/§7.2). The epoch is documented
        single-writer = stream thread; this second writer is legal ONLY because
        the tick calls it while ``is_running()`` has just returned False — the
        reader thread is dead at that instant, so no concurrent writer exists."""
        self._last_message_epoch = None

    def stop(self, *, timeout: float = 5.0) -> None:
        """Cancel any pending trailing timer and stop the streaming client
        (DELETE subs + join)."""
        with self._lock:
            timer = self._trailing_timer
            self._trailing_timer = None
            self._trailing_deadline = None
        if timer is not None:
            timer.cancel()
        self._client.stop(timeout=timeout)


__all__ = [
    "DEFAULT_STREAM_DEBOUNCE_S",
    "DEFAULT_STREAM_STALE_S",
    "StreamTrigger",
    "default_context_id_factory",
]
