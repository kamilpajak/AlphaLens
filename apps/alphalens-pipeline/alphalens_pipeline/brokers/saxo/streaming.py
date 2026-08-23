"""Saxo SIM WebSocket streaming reader (dark, SIM-only — ADR 0014).

Design memo: ``docs/research/saxo_streaming_design_2026_07_24.md`` (LOCKED).

Streaming is a pure LATENCY win with a provable never-worse-than-poll floor:
this reader runs on ONE background daemon thread whose only cross-thread action
is calling ``on_trigger()`` (which the ``StreamTrigger`` turns into an early
``wake_event.set()``). It never runs reconcile, never places orders, never
touches the protection journals/throttle. A total streaming failure (never
started, disconnected, silently dead, thread-crashed, circuit-broken) leaves the
main loop's absolute-deadline poll backstop untouched — exactly today's
behaviour.

Two responsibilities used to live here; one has since moved:

1. The PURE binary-envelope parser (:func:`parse_stream_frames`,
   :class:`StreamMessage`, :class:`SaxoStreamProtocolError`) relocated to
   ``alphalens_pipeline.data.alt_data.saxo_stream_envelope`` (2026-08-07) — it
   is a wire-protocol decoder, not broker logic, and the LIVE price stream
   (which must live under ``data/alt_data/``, never ``brokers/``) needs the
   same decoder. Re-imported here unchanged so every existing call site keeps
   working through the same names.
2. :class:`SaxoStreamingClient` — connect / subscribe / DUMB route / reconnect
   with ``&messageid`` replay / exponential backoff + circuit breaker / PUT
   token re-authorize on a MAIN-pushed token change. All the DECISION logic is
   in synchronous helpers (hermetically tested); only the socket recv loop runs
   inside ``asyncio.run`` on the daemon thread.

Routing is deliberately DUMB — any positions/orders DATA frame fires the trigger
and the main thread re-reads FULL REST state (deltas are never merged, so the
partial-merge bug surface does not exist).
"""

from __future__ import annotations

import contextlib
import enum
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests

from alphalens_pipeline.brokers.saxo.errors import (
    SaxoError,
    SaxoLiveEnvironmentBlockedError,
)
from alphalens_pipeline.brokers.saxo.tokens import StaticTokenProvider, TokenProvider
from alphalens_pipeline.data.alt_data.saxo_stream_envelope import (
    SaxoStreamProtocolError,
    StreamMessage,
    parse_stream_frames,
)

if TYPE_CHECKING:
    from alphalens_pipeline.brokers.saxo.client import SaxoClient

logger = logging.getLogger(__name__)

# CONFIRMED PROTOCOL (exploratory SIM probe, 2026-07-24). The other documented
# plain-websocket host convention does NOT resolve on SIM (DNS fail) — this
# host is the winner and the ONLY one we connect to.
SIM_STREAMING_BASE_URL = "wss://sim-streaming.saxobank.com/sim/oapi/streaming/ws/connect"

# LIVE streaming host markers — named ONLY so the SIM rail can refuse them
# explicitly. Kept WITHOUT the ``.net`` suffix on the auth host so this string
# never matches the trading-gateway rail's live auth marker nor the
# ``test_no_raw_saxo_http`` URL-fragment list.
_LIVE_STREAMING_MARKERS = ("live-streaming", "live.logonvalidation")

# Retired-context deque bound (rearm design memo §5): 5 rungs to the tick-side
# cooldown-ladder saturation plus slack, so a permanent outage cannot grow the
# deque without limit. Client-side because the deque lives on the client.
_STREAM_RETIRED_CONTEXT_CAP = 8

# Control-message reference ids (Saxo reserves the ``_`` prefix for control).
_HEARTBEAT_REF = "_heartbeat"
_RESET_REF = "_resetsubscriptions"
_DISCONNECT_REF = "_disconnect"


class SaxoStreamError(SaxoError):
    """Non-transient streaming failure (bad host, no token, protocol violation)."""


class StreamAction(enum.Enum):
    """What the recv loop should do after routing a frame."""

    CONTINUE = "continue"
    RECONNECT = "reconnect"


@dataclass(frozen=True)
class ReconnectStep:
    """The synchronous DECISION for what the supervisor does after a connection
    ENDED (a connect/subscribe exception OR a clean RECONNECT-return — both
    counted identically). ``give_up`` means the breaker tripped and streaming
    shuts to poll-only; otherwise ``backoff_s`` is the sleep before the next
    attempt. Extracted so the reconnect-storm discipline is hermetically
    testable, not buried in the async ``_supervise`` loop (finding #3)."""

    give_up: bool
    backoff_s: float


def _refuse_non_sim_streaming(streaming_base_url: str) -> None:
    """SIM-only structural rail — mirrors ``SaxoClient``'s equality-to-SIM guard.

    Refuses any LIVE marker AND any host that is not exactly the SIM streaming
    host, so a typo'd / proxied LIVE URL is refused too. LIVE is unreachable by
    construction (unchanged by the ADR 0015/0017 order-rail unlocks — LIVE
    streaming would need its own future ADR).
    """
    lowered = streaming_base_url.lower()
    if any(marker in lowered for marker in _LIVE_STREAMING_MARKERS):
        raise SaxoLiveEnvironmentBlockedError(
            f"streaming host {streaming_base_url!r} names a LIVE marker "
            "— SIM-only (ADR 0014; unchanged by ADR 0015/0017 — LIVE "
            "streaming needs its own future ADR)."
        )
    if streaming_base_url != SIM_STREAMING_BASE_URL:
        raise SaxoLiveEnvironmentBlockedError(
            f"SaxoStreamingClient is SIM-only: streaming_base_url must be "
            f"{SIM_STREAMING_BASE_URL!r}, got {streaming_base_url!r}."
        )


_SUBSCRIPTION_CREATED_STATUS = 201


def _require_subscription_created(status_code: int, reference_id: str) -> None:
    """Assert a subscription POST returned 201 (finding #2). A silently-rejected
    subscription (e.g. wrong ClientKey scope) would leave the recv loop waiting
    for data that never arrives -> a ~60s stale reconnect loop; raising here
    makes the supervisor count it as a connection failure (backoff + breaker)."""
    if status_code != _SUBSCRIPTION_CREATED_STATUS:
        raise SaxoStreamError(
            f"subscription {reference_id!r} POST returned {status_code}, expected "
            f"{_SUBSCRIPTION_CREATED_STATUS} — treated as a connection failure"
        )


async def _default_ws_connect(url: str, headers: dict[str, str]) -> Any:
    """Open a WS connection with the venv's ``websockets`` (asyncio), confined to
    the stream thread. ``websockets>=12`` uses ``additional_headers=`` (older
    ``extra_headers=``) — try both. Imported lazily so importing this module
    never requires ``websockets`` at import time."""
    from websockets.asyncio.client import connect

    try:
        return await connect(url, additional_headers=headers)
    except TypeError:
        return await connect(url, extra_headers=headers)


@dataclass(frozen=True)
class StreamTuning:
    """Reconnect / backoff / staleness policy for the streaming reader.

    Grouped into one immutable object so the client constructor stays within a
    sane parameter count and the retry discipline reads as one cohesive, testable
    unit. Defaults are the SIM values locked in the design memo; production keeps
    them all except ``stale_after_s`` (passed through by :class:`StreamTrigger`).

    ``frozen=True`` is load-bearing: it is what makes a single shared instance
    safe as the constructor's default argument (no mutable-default aliasing trap).
    Keep it frozen and keep every field immutable."""

    stale_after_s: float = 45.0
    recv_timeout_s: float = 30.0
    max_consecutive_failures: int = 6
    backoff_floor_s: float = 1.0
    backoff_ceiling_s: float = 30.0
    # Delivery-life gate (rearm design memo §4.2/§5): a frame clears the failure
    # streak only once the CURRENT connection has been alive this long. 10x
    # SaxoClient._MIN_REQUEST_INTERVAL_S (0.5s, what bounds one reconnect+
    # resubscribe cycle) and far below the 20-30s heartbeat cadence, so it is
    # transparent on a healthy connection.
    min_connection_life_s: float = 5.0


class SaxoStreamingClient:
    """SIM-only Saxo WebSocket reader. DI-clean, mirrors ``SaxoClient``.

    Shares the process-wide OAuth provider + the single default ``SaxoClient``
    singleton (subscription REST goes through it, so there is ONE coordinated
    throttle and ``test_no_raw_saxo_http`` stays green). The reader NEVER pulls
    the token itself — the main thread pushes the current bearer via
    :meth:`push_token`, and re-authorization is a PUT in place.
    """

    def __init__(
        self,
        token_provider: TokenProvider,
        subscriber: SaxoClient,
        *,
        context_id: str,
        on_trigger: Callable[[], None],
        on_heartbeat: Callable[[float], None],
        streaming_base_url: str = SIM_STREAMING_BASE_URL,
        tuning: StreamTuning = StreamTuning(),
        session: requests.Session | None = None,
        ws_connect: Callable[[str, dict[str, str]], Awaitable[Any]] | None = None,
        async_sleep: Callable[[float], Awaitable[None]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        alert: Callable[[str], None] | None = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ):
        _refuse_non_sim_streaming(streaming_base_url)
        self._token_provider = token_provider
        self._subscriber = subscriber
        self._context_id = context_id
        self._on_trigger = on_trigger
        self._on_heartbeat = on_heartbeat
        self._streaming_base_url = streaming_base_url
        self._stale_after_s = tuning.stale_after_s
        self._recv_timeout_s = tuning.recv_timeout_s
        self._max_consecutive_failures = tuning.max_consecutive_failures
        self._backoff_floor_s = tuning.backoff_floor_s
        self._backoff_ceiling_s = tuning.backoff_ceiling_s
        self._min_connection_life_s = tuning.min_connection_life_s
        self._session = session or requests.Session()
        self._ws_connect = ws_connect or _default_ws_connect
        self._async_sleep = async_sleep
        self._sleep = sleep
        self._monotonic = monotonic
        self._alert = alert if alert is not None else logger.warning
        self._thread_factory = thread_factory
        # Context ids retired by rearm(); drained (best-effort DELETE) on the
        # next healthy _subscribe and by stop(). Main thread only ever appends
        # while the reader is provably dead; deque ops are atomic (memo §4.3).
        self._retired_context_ids: deque[str] = deque(maxlen=_STREAM_RETIRED_CONTEXT_CAP)

        self._timeout = 30.0
        # Cross-thread: the bearer the MAIN loop pushes (atomic str assignment).
        self._current_token: str | None = None
        self._last_authorized_token: str | None = None
        # Retained for reconnect replay (single-writer = stream thread).
        self._last_message_id: int | None = None
        self._last_recv_mono: float = 0.0
        # Stamped in _run_one_connection beside _last_recv_mono; gates WHICH
        # delivery may clear the failure streak (rearm design memo §4.2).
        self._connection_started_mono: float = 0.0
        self._subscription_generation = 0

        self._consecutive_failures = 0
        self._frames_delivered = 0
        self._trips_total = 0
        self._is_streaming = True
        self._breaker_alerted = False

        self._stop = False
        self._started = False
        self._thread: threading.Thread | None = None

    # ----- public API (main thread) -----

    @property
    def is_streaming(self) -> bool:
        """False once the circuit breaker has shut the stream to poll-only."""
        return self._is_streaming

    @property
    def is_started(self) -> bool:
        return self._started

    def is_running(self) -> bool:
        """True once ``start()`` has launched the reader thread and it is still
        alive. False before the first ``start()`` call, or after the reconnect
        circuit breaker has tripped and ``_supervise`` has returned, exiting the
        thread — the signal the daemon tick uses to know the reader is dark and
        needs re-arming rather than being trusted dead-but-healthy-looking.
        Verbatim mirror of ``SaxoPriceStream.is_running``."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def frames_delivered(self) -> int:
        """Monotonic count of REAL server frames, incremented ONLY in
        :meth:`_mark_delivered` — the delivery proof. A bare subscribe dispatch
        stamps the liveness epoch without any server frame (memo §7.7), so the
        epoch must never be used as delivery evidence; this counter is."""
        return self._frames_delivered

    @property
    def trips_total(self) -> int:
        """Monotonic count of breaker trips — lets the main-thread tick count a
        trip whose whole lifetime falls between two ticks."""
        return self._trips_total

    @property
    def consecutive_failures(self) -> int:
        """Current failure streak, for the gauge — makes the streak composition
        recoverable, which the 2026-08-22 incident journal could not do."""
        return self._consecutive_failures

    def push_token(self, token: str) -> None:
        """Main thread hands the reader the current bearer (never pulled here)."""
        self._current_token = token

    def start(self) -> bool:
        """Spawn the reader daemon thread. Returns False (poll-only) under a
        :class:`StaticTokenProvider` — a fixed 24h token cannot be
        PUT-reauthorized in place, so streaming refuses to start and logs once."""
        if isinstance(self._token_provider, StaticTokenProvider):
            logger.warning(
                "Saxo streaming requires the OAuth provider (a static 24h token "
                "cannot be re-authorized in place) — running poll-only"
            )
            return False
        if self._thread is not None:
            return True
        self._stop = False
        self._thread = self._thread_factory(
            target=self._thread_main, name="saxo-stream", daemon=True
        )
        self._thread.start()
        self._started = True
        return True

    def rearm(self, context_id: str) -> bool:
        """MAIN THREAD ONLY. Re-open a tripped-or-crashed reader on a FRESH
        context. Returns True iff a new reader thread is running.

        Thread-safety: every field written here is written only after
        ``is_running()`` has confirmed the previous reader thread is dead, and
        the new thread is spawned after those writes — so no field ever has two
        live writers. Attribute assignment is atomic under the GIL, so no lock
        is taken and the protective loop can never block here.

        Deliberately NOT reset (rearm design memo §4.3): ``_consecutive_failures``
        (keeping the streak IS the half-open mechanism — the trial gets exactly
        one connect and only a delivered frame restores the budget),
        ``_current_token`` (clearing it would unbound the startup
        ``token_missing`` exemption), ``_subscription_generation`` (monotonic —
        guarantees fresh pos-N/ord-N ReferenceIds).

        SINGLE-CALLER CONTRACT: ``start()`` sets ``self._stop = False``, so a
        rearm() racing stop() could resurrect a reader mid-shutdown. Both are
        main-thread-only (run_daemon's tick and the CLI ``finally`` are the same
        thread). Do not call this from anywhere else."""
        if self._stop:
            return False  # shutdown latch — never resurrect
        if self.is_running():
            return False  # old reader still unwinding asyncio.run
        if self._current_token is None:
            return False  # never trial without a bearer (memo §4.4)
        # Rollback (below) restores ONLY _thread/_is_streaming — the context
        # rotation and cleared _last_message_id/_breaker_alerted deliberately
        # stay: the next trial reuses the rotated id, and the superseded id is
        # already in the retired drain deque (DELETE on a dead context no-ops).
        prior_thread, prior_streaming = self._thread, self._is_streaming
        self._retired_context_ids.append(self._context_id)
        self._context_id = context_id  # rotate — trip-time DELETE is best-effort
        self._last_message_id = None  # explicit COLD connect (INC-0 probed cold only)
        self._breaker_alerted = False  # so a second trip logs again
        self._thread = None  # else start() silently no-ops
        self._is_streaming = True
        try:
            started = self.start()
        except Exception:
            # Roll back so the next tick retries — a naive swallow would leave
            # _thread=None with _is_streaming=True: a dead stream every
            # instrument reports as healthy (memo §7.5).
            self._thread, self._is_streaming = prior_thread, prior_streaming
            raise
        if not started:  # StaticTokenProvider refusal
            self._thread, self._is_streaming = prior_thread, prior_streaming
            return False
        return True

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the reader to stop, best-effort DELETE the subs (current
        context AND any rearm-retired ones), join."""
        self._stop = True
        with contextlib.suppress(Exception):
            self._subscriber.delete_all_subscriptions(self._context_id)
        self._drain_retired_contexts()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ----- routing (synchronous, hermetically tested) -----

    def _dispatch_frame(self, buf: bytes) -> StreamAction:
        """Parse + route every message in one WS frame; retain the last msgId for
        reconnect replay. Any message demanding RECONNECT wins."""
        action = StreamAction.CONTINUE
        for msg in parse_stream_frames(buf):
            self._last_message_id = msg.message_id
            if self._route_message(msg) is StreamAction.RECONNECT:
                action = StreamAction.RECONNECT
        return action

    def _route_message(self, msg: StreamMessage) -> StreamAction:
        ref = msg.reference_id
        if ref == _HEARTBEAT_REF:
            self._mark_delivered()
            self._on_heartbeat(self._monotonic())
            return StreamAction.CONTINUE
        if ref == _RESET_REF:
            self._handle_reset()
            return StreamAction.CONTINUE
        if ref == _DISCONNECT_REF:
            # A WS auth drop never flows through the HTTP 401 seam, so invalidate
            # ourselves before the supervisor reconnects fresh. NOT a delivery —
            # this is a teardown signal, so it never clears the failure streak.
            self._token_provider.invalidate()
            return StreamAction.RECONNECT
        if ref.startswith("_"):
            # Unknown control message — liveness only, never a trigger.
            self._mark_delivered()
            self._on_heartbeat(self._monotonic())
            return StreamAction.CONTINUE
        # Data (positions / orders) — fire the trigger; reconcile re-reads REST.
        self._mark_delivered()
        self._on_trigger()
        return StreamAction.CONTINUE

    def _mark_delivered(self) -> None:
        """A real server frame arrived. Always counts as delivery evidence
        (``frames_delivered``). Clears the failure streak only once the CURRENT
        connection has been alive for ``min_connection_life_s``: a connection
        that delivers one frame and dies inside that window has not demonstrated
        it can carry the stream, and clearing on it lets a
        one-heartbeat-then-drop gateway spin under the breaker forever. Delivery
        stays the ONLY reset trigger (finding #1) — this narrows WHICH delivery
        counts, never widens it."""
        self._frames_delivered += 1
        if self._monotonic() - self._connection_started_mono >= self._min_connection_life_s:
            self._reset_failures()

    # ----- subscriptions (synchronous, through the shared SaxoClient) -----

    def _subscribe(self, *, delete_first: bool) -> None:
        """Create the positions + orders subscriptions and fire ONE snapshot
        trigger (so a fill that happened while disconnected self-reconciles).
        ``delete_first`` tears down the old context subs first (reconnect / reset).
        """
        if delete_first:
            self._subscriber.delete_all_subscriptions(self._context_id)
        self._create_subscriptions()
        self._on_trigger()
        # REST is demonstrably healthy here (two 201s) — the best moment to
        # drain contexts retired by rearm() whose trip-time DELETE likely failed
        # during the outage (memo §4.3/§7.13).
        self._drain_retired_contexts()

    def _drain_retired_contexts(self) -> None:
        """Best-effort DELETE every rearm-retired context's subscriptions.
        Bounded to one pass over the ids present at entry; a failed DELETE
        re-appends its id for the next drain. ``deque.popleft``/``append`` are
        atomic, so the reader thread (healthy ``_subscribe``) and the main
        thread (``stop()``) never corrupt it."""
        for _ in range(len(self._retired_context_ids)):
            try:
                retired = self._retired_context_ids.popleft()
            except IndexError:  # pragma: no cover - concurrent drain emptied it
                return
            try:
                self._subscriber.delete_all_subscriptions(retired)
            except Exception:
                self._retired_context_ids.append(retired)

    def _handle_reset(self) -> None:
        """``_resetsubscriptions``: DELETE all subs on the context + recreate with
        NEW ReferenceIds + fresh snapshot + trigger (a bare socket reconnect is
        insufficient and is never used for a reset)."""
        self._subscribe(delete_first=True)

    def _create_subscriptions(self) -> None:
        client_key = self._subscriber.get_client_info()["ClientKey"]
        generation = self._subscription_generation
        self._subscription_generation += 1
        pos_ref = "pos" if generation == 0 else f"pos-{generation}"
        ord_ref = "ord" if generation == 0 else f"ord-{generation}"
        pos_status, _ = self._subscriber.create_positions_subscription(
            context_id=self._context_id, reference_id=pos_ref, client_key=client_key
        )
        _require_subscription_created(pos_status, pos_ref)
        ord_status, _ = self._subscriber.create_orders_subscription(
            context_id=self._context_id, reference_id=ord_ref, client_key=client_key
        )
        _require_subscription_created(ord_status, ord_ref)

    # ----- reconnect + circuit breaker (synchronous) -----

    def _build_connect_url(self, last_message_id: int | None) -> str:
        url = f"{self._streaming_base_url}?contextId={self._context_id}"
        if last_message_id is not None:
            url += f"&messageid={last_message_id}"
        return url

    def _compute_backoff(self, consecutive_failures: int) -> float:
        """Exponential 1s -> 30s cap."""
        exponent = max(0, consecutive_failures - 1)
        return min(self._backoff_floor_s * (2**exponent), self._backoff_ceiling_s)

    def _register_failure(self) -> bool:
        """Increment the consecutive-failure counter; trip the breaker (shut to
        poll-only + alert once) on the Nth. Returns True when tripped."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_consecutive_failures:
            self._trip_breaker()
            return True
        return False

    def _reset_failures(self) -> None:
        self._consecutive_failures = 0

    def _plan_reconnect_step(self, *, token_missing: bool = False) -> ReconnectStep:
        """DECIDE what to do after a connection ENDED — a connect/subscribe
        exception OR a clean RECONNECT-return (socket EOF, ``_disconnect``,
        stale-timeout, reauth non-202). Both are counted identically: register
        the failure, trip the breaker on the Nth (``give_up``), otherwise return
        the exponential backoff to sleep before the next attempt. The streak is
        reset ONLY by :meth:`_mark_delivered` (a real frame), so a
        connect-then-drop storm that never delivers still trips the breaker
        instead of spinning resubscribe REST at full speed (finding #1).

        ``token_missing`` marks the pre-first-token STARTUP window: the reader
        thread is spawned before the main loop pushes the first bearer, so the
        first connect(s) can find no token. That is "not ready yet", NOT a
        connection failure — it must NOT count toward the breaker (else a slow
        first tick, e.g. a Saxo 503 on the initial reconcile, burns the whole
        6-attempt budget and shuts streaming to poll-only for the entire session
        — the 2026-07-27 incident). ``_current_token`` is only ever set, never
        re-cleared to None, so this exemption is self-limiting to startup: it
        just waits the floor backoff and re-checks, never eroding the real
        failure budget."""
        if token_missing:
            return ReconnectStep(give_up=False, backoff_s=self._backoff_floor_s)
        if self._register_failure():
            return ReconnectStep(give_up=True, backoff_s=0.0)
        return ReconnectStep(
            give_up=False, backoff_s=self._compute_backoff(self._consecutive_failures)
        )

    def _trip_breaker(self) -> None:
        self._trips_total += 1
        self._is_streaming = False
        with contextlib.suppress(Exception):
            self._subscriber.delete_all_subscriptions(self._context_id)
        if not self._breaker_alerted:
            self._breaker_alerted = True
            try:
                self._alert(
                    f"saxo stream circuit breaker tripped after "
                    f"{self._consecutive_failures} consecutive failures — running poll-only"
                )
            except Exception:  # pragma: no cover - alert must never crash the reader
                logger.warning("saxo stream breaker alert failed", exc_info=True)

    # ----- token re-authorize (synchronous, off the hot path) -----

    def _authorize_url(self) -> str:
        # The SIM rail (_refuse_non_sim_streaming, enforced in __init__) guarantees a
        # wss:// base, so the authorize endpoint is its https:// sibling. A plaintext
        # ws:// base can never reach here, so no insecure-scheme normalization exists.
        base = self._streaming_base_url
        if base.startswith("wss://"):
            base = "https://" + base[len("wss://") :]
        if base.endswith("/connect"):
            base = base[: -len("/connect")] + "/authorize"
        return f"{base}?contextid={self._context_id}"

    def _maybe_reauthorize(self) -> StreamAction:
        """PUT the MAIN-pushed token in place when it has rotated (no reconnect).
        On a non-202 the token is stale/rejected -> invalidate + reconnect. The
        reader NEVER calls ``get_access_token`` — it authorizes off the pushed
        token only (adversary-2 fix #4)."""
        token = self._current_token
        if token is None or token == self._last_authorized_token:
            return StreamAction.CONTINUE
        # Intentional blocking ``requests`` PUT on the stream thread's asyncio
        # loop (finding #4): the loop is single-purpose (no sibling coroutines to
        # starve) and never blocks the MAIN protective thread. Worst case a hung
        # streaming host stalls only this reader's own recv for up to
        # ``self._timeout`` — the poll backstop still covers protection.
        resp = self._session.put(
            self._authorize_url(),
            headers={"Authorization": f"BEARER {token}"},
            timeout=self._timeout,
        )
        if resp.status_code == 202:
            self._last_authorized_token = token
            return StreamAction.CONTINUE
        self._token_provider.invalidate()
        return StreamAction.RECONNECT

    # ----- async I/O glue (stream thread only; covered by the live probe) -----

    def _thread_main(self) -> None:
        import asyncio

        try:
            asyncio.run(self._supervise())
        except Exception:  # pragma: no cover - a reader crash must not touch main
            logger.warning("saxo stream reader thread crashed", exc_info=True)

    def _log_session_failure(self, exc: Exception) -> None:
        """Log a connection that ended, at the level its cause warrants.

        Before the main loop has pushed the first bearer (``_current_token is
        None``) a failed connect is the expected startup-window wait, NOT a real
        failure: the reader thread is spawned before the first tick, and that
        first tick can lag ~15s behind a full reconcile, so the pre-token
        connect attempts raise "no bearer token pushed before connect" once a
        second until the token lands (live 2026-08-02: 16 such lines over 16s,
        then a silent, healthy stream). ``_current_token`` is set-once (never
        re-cleared), so once a token has been pushed this branch never recurs.
        DEBUG for that wait keeps a self-healing startup from reading as a 1/s
        WARNING crisis; the same #918 exemption keeps it off the breaker. Any
        failure AFTER a token is present is a genuine session failure -> WARNING.
        """
        if self._current_token is None:
            logger.debug("saxo stream connect deferred (startup, no bearer yet): %s", exc)
        else:
            logger.warning("saxo stream session failed: %s", exc)

    async def _supervise(self) -> None:  # pragma: no cover - exercised by live probe
        import asyncio

        async_sleep = self._async_sleep or asyncio.sleep
        is_reconnect = False
        while not self._stop and self._is_streaming:
            try:
                await self._run_one_connection(is_reconnect=is_reconnect)
            except Exception as exc:
                self._log_session_failure(exc)
            is_reconnect = True
            # A clean RECONNECT-return and an exception are counted identically
            # (finding #1): both mean the connection ended and must be throttled.
            # A stop / already-tripped breaker exit needs no failure registration.
            if self._stop or not self._is_streaming:
                return
            # A connect that could not even start because the main loop has not
            # pushed the first token yet is a startup-window wait, not a failure
            # (2026-07-27 breaker-trip incident) — see _plan_reconnect_step. The
            # wait cannot spin unbounded: this reader is a daemon thread of the
            # main loop, which pushes a token every tick — if the main loop dies,
            # the process exits and takes this thread with it.
            step = self._plan_reconnect_step(token_missing=self._current_token is None)
            if step.give_up:
                return
            await async_sleep(step.backoff_s)

    async def _run_one_connection(self, *, is_reconnect: bool) -> None:  # pragma: no cover
        token = self._current_token
        if token is None:
            raise SaxoStreamError("no bearer token pushed before connect")
        url = self._build_connect_url(self._last_message_id if is_reconnect else None)
        conn = await self._ws_connect(url, {"Authorization": f"BEARER {token}"})
        self._last_authorized_token = token
        # One stamp serves two clocks: staleness (_last_recv_mono) and the
        # delivery-life gate (_connection_started_mono, memo §4.2).
        self._connection_started_mono = self._last_recv_mono = self._monotonic()
        try:
            # NOTE: the failure streak is NOT reset here. A dispatched subscribe
            # is no proof the connection delivers — the reset happens only when a
            # real frame arrives (:meth:`_mark_delivered`), so a connect ->
            # subscribe -> immediate-drop storm still trips the breaker.
            self._subscribe(delete_first=is_reconnect)
            while not self._stop:
                if await self._recv_and_route(conn) is StreamAction.RECONNECT:
                    return
        finally:
            with contextlib.suppress(Exception):
                await conn.close()

    async def _recv_and_route(self, conn: Any) -> StreamAction:  # pragma: no cover
        import asyncio

        try:
            frame = await asyncio.wait_for(conn.recv(), timeout=self._recv_timeout_s)
        except TimeoutError:
            if self._monotonic() - self._last_recv_mono > self._stale_after_s:
                return StreamAction.RECONNECT
            return self._maybe_reauthorize()
        except Exception:
            # Socket EOF/RST -> reconnect fresh (the poll backstop covers the gap).
            return StreamAction.RECONNECT
        self._last_recv_mono = self._monotonic()
        if isinstance(frame, str):
            frame = frame.encode("utf-8")
        action = self._dispatch_frame(frame)
        if action is StreamAction.CONTINUE:
            action = self._maybe_reauthorize()
        return action


__all__ = [
    "SIM_STREAMING_BASE_URL",
    "ReconnectStep",
    "SaxoStreamError",
    "SaxoStreamProtocolError",
    "SaxoStreamingClient",
    "StreamAction",
    "StreamMessage",
    "parse_stream_frames",
]
