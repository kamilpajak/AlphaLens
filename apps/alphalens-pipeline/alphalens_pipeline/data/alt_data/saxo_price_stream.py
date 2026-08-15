"""Saxo LIVE price stream: one long-lived WebSocket thread + a quote cache.

Price subscriptions stream DELTAS - an unchanged field is OMITTED. This differs
from the positions/orders stream, whose reader deliberately never merges and
re-reads full REST state instead. There is no cheap full re-read for a quote, so
this cache MUST merge: a message carrying only a Bid must leave the Ask intact.

The socket loop only decodes and applies. Every decision about whether a cached
quote may drive an order lives in the feed adapter's freshness gate.

The ONE exception is the 1 Hz running-low accumulator (touch-latch,
entry_trailing_design_2026_08_12.md §5): a per-tick running minimum bid that
provably CANNOT live in the 45s feed adapter — it needs every 1 Hz frame the
adapter never sees. ``apply`` therefore folds each latchable quote into
``QuoteCache._running_low`` here, gated by a local latchable check (undelayed,
un-crossed, sane spread, both sides finite positive). This gate is a
NECESSARY-condition prefilter, not the order-decision gate: the feed adapter
still decides, at DRAIN time, whether a drained low may drive a touch (the
concurrent point-sample must itself be fresh/trusted). The accumulator is
exception-safe by construction so a payload-shape quirk can never raise out of
the reader thread and trip the reconnect breaker.

:class:`SaxoPriceStream`'s socket loop is thin glue around
:func:`alphalens_pipeline.data.alt_data.saxo_stream_envelope.parse_stream_frames`
and the SIM streaming reader's reconnect tuning (``max_consecutive_failures=6``,
exponential backoff 1s -> 30s ceiling, bounded ``recv`` per the SIM stale
pattern, see ``alphalens_pipeline.brokers.saxo.streaming.StreamTuning``) -
reused as VALUES only, not by importing that module (this package must not
import ``brokers/``).

Reliability contract (2026-08-10 incident — idle WS killed by Saxo, reconnects
died into a subscription-less context, breaker tripped):

* the reader owns ALL subscription REST traffic: every connection gets a
  FRESH ``contextId`` and re-creates the price subscription server-side, so a
  reconnect can never resume into a context whose subscription Saxo dropped;
  ``ensure_subscribed`` (caller thread) only mutates the DESIRED set;
* with ZERO desired uics the reader holds NO WebSocket open (an idle
  connection is exactly what the venue kills);
* ``recv`` is bounded (``_RECV_TIMEOUT_S``) so a half-open socket becomes a
  counted failure + reconnect, never a permanent wedge;
* the ``_resetsubscriptions`` control frame recreates the subscription on the
  same connection;
* lifecycle + freshness are exported as Prometheus textfile gauges
  (``alphalens_live_price_stream_*``) so a dark feed is distinguishable from
  a quiet one.

The socket loop is unit-tested hermetically via the injected ``ws_connect`` /
``async_sleep`` seams (tests/data/test_saxo_price_stream.py); the Task 8 live
probe remains the end-to-end check against the real venue.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import math
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import LiveAuthConfig, LiveTokenProvider
from alphalens_pipeline.data.alt_data.saxo_marketdata_client import SaxoMarketDataClient
from alphalens_pipeline.data.alt_data.saxo_stream_envelope import parse_stream_frames
from alphalens_pipeline.data.alt_data.session_reclaim import ReclaimLimiter

logger = logging.getLogger(__name__)

LIVE_STREAM_URL = "wss://live-streaming.saxobank.com/oapi/streaming/ws/connect"

# Reconnect / backoff policy - same VALUES as the SIM streaming reader's
# StreamTuning defaults (design memo docs/research/saxo_streaming_design_2026_07_24.md),
# not the same class: this module must not import brokers/.
_MAX_CONSECUTIVE_FAILURES = 6
_BACKOFF_FLOOR_S = 1.0
_BACKOFF_CEILING_S = 30.0

# Bounded recv (SIM stale pattern, stale_s=45): Saxo heartbeats every ~20-30s,
# so 45s of TOTAL silence on an open socket means half-open — reconnect.
_RECV_TIMEOUT_S = 45.0

# With zero desired uics the reader holds no WebSocket (idle connections get
# killed by the venue and turn into failure storms); poll the desired set at
# this cadence instead.
_IDLE_POLL_S = 1.0

# Saxo control frame announcing the server dropped this context's
# subscriptions — the one control ref that demands ACTION (recreate), not
# just liveness bookkeeping.
_RESET_SUBSCRIPTIONS_REF = "_resetsubscriptions"

# Prometheus textfile gauges (emit_domain_metrics job + throttle). Emitted
# best-effort from the reader thread: on supervise start/stop, on every
# counted failure, and throttled on the frame path. The job label is a
# constructor parameter (ADR 0016 D5), not a module constant: a future LIVE
# broker-manager instance runs its OWN price stream and must not share a
# Prometheus job (and thus textfile) with the SIM instance's stream. This
# module must not import brokers/ (dependency direction) — the composition
# root (control_loop.py's default price-feed factory) injects the resolved
# ``state_paths.price_stream_metrics_job()`` value; the default below keeps
# standalone/test construction working unchanged.
_DEFAULT_GAUGE_JOB = "live-price-stream"
_GAUGE_MIN_INTERVAL_S = 15.0

# Touch-latch spread ceiling for the 1 Hz running-low accumulator
# (entry_trailing_design_2026_08_12.md §5 mitigation path). MIRRORS
# ``broker_contract.price_feed.DEFAULT_MAX_RELATIVE_SPREAD`` (0.02) as a VALUE,
# never by importing that decision-layer module (this data package must not
# import broker_contract; same "reused as VALUES only" idiom as the reconnect
# tuning above). Pinned equal by a test so the copy cannot silently drift.
# Applied as (ask-bid)/bid, which is strictly TIGHTER than is_fresh's
# (ask-bid)/mid (bid < mid), so the latch never admits a quote is_fresh would
# reject on spread — an intentional at-least-as-strict second gate, not a
# competing definition.
_LATCH_MAX_RELATIVE_SPREAD = 0.02


def _latchable_side(value: object) -> float | None:
    """Coerce a raw JSON quote side to a finite, strictly-positive float, or
    ``None`` on ANY doubt (non-numeric, non-finite, non-positive).

    Veto-not-raise is mandatory: ``QuoteCache.apply`` runs on the WebSocket
    reader thread and stores bid/ask uncoerced (``Quote.bid``/``ask`` are
    ``float | None`` but a live payload can carry a string or an explicit null,
    e.g. a one-sided market or a halt). A bare ``math.isfinite`` / comparison /
    division on that value would raise ``TypeError`` inside ``apply``; an
    uncaught exception there is counted as a connection failure by ``_supervise``
    and, after ``_MAX_CONSECUTIVE_FAILURES`` such frames, trips the reconnect
    circuit breaker and darkens the whole live feed (entries AND live exits).
    So no arithmetic may touch a side until it is proven a finite number —
    mirrors ``broker_contract.price_feed._is_finite_number``."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _parse_utc(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A timestamp string with no offset (e.g. Saxo omitting the "Z") parses
        # to a NAIVE datetime. Letting that flow into Quote.event_time would
        # later crash is_fresh's (now - event_time) subtraction against an
        # aware `now` with a TypeError - a doubt about which instant this
        # names must be a veto here, not a crash three modules downstream.
        return None
    return parsed


@dataclass(frozen=True)
class Quote:
    """``bid``/``ask`` may legitimately be ``None`` even after ``apply`` has
    run: a delta for a uic that never received a full snapshot (first
    contact, or any delta for a never-before-seen uic - the same code path)
    produces a half-blank Quote with the untouched side still ``None``. That
    is acceptable ONLY because every consumer MUST treat a missing side as
    no-price, never as a stale-but-valid price - the freshness gate that
    enforces this lives in the feed adapter, not here."""

    uic: int
    bid: float | None
    ask: float | None
    event_time: dt.datetime | None
    delayed_by_minutes: int | None
    received_at: dt.datetime


class QuoteCache:
    """Thread-safe per-uic quote state with delta merging."""

    def __init__(self) -> None:
        self._quotes: dict[int, Quote] = {}
        # 1 Hz running-LOW accumulator per uic (touch-latch,
        # entry_trailing_design §5): the minimum latchable bid since the last
        # drain. Bounded by the live subscription set — ``forget`` (on
        # unsubscribe) pops it alongside the quote. Written under ``_lock`` on
        # the reader thread, drained under ``_lock`` on the caller thread. This
        # is the ONE accumulation that provably cannot live in the 45s feed
        # adapter (it needs every 1 Hz frame), so it is gated HERE by exception
        # for the reader thread's sake; the freshness gate that decides whether
        # a drained low may drive an order still lives in the feed adapter.
        self._running_low: dict[int, float] = {}
        self._lock = threading.Lock()

    def apply(self, row: dict[str, Any], *, received_at: dt.datetime) -> None:
        raw_uic = row.get("Uic")
        if raw_uic is None:
            return
        try:
            uic = int(raw_uic)
        except (TypeError, ValueError):
            # A malformed Uic must degrade to "skip this row", not raise out
            # of the WebSocket reader thread: an uncaught exception here is
            # counted as a connection failure by _supervise, and after
            # _MAX_CONSECUTIVE_FAILURES such frames the reconnect circuit
            # breaker trips and the stream goes permanently dark over what
            # may just be a payload-shape change.
            logger.warning("saxo price stream: dropping row with non-numeric Uic: %r", raw_uic)
            return
        event_time = _parse_utc(row.get("LastUpdated"))
        quote_block = row.get("Quote") or {}
        with self._lock:
            prev = self._quotes.get(uic)
            # Sequence regression: an older quote never overwrites a newer one.
            # Strictly LESS-THAN is deliberate, not an oversight: Saxo's
            # observed timestamps carry only second resolution, so two updates
            # sharing a LastUpdated during active trading are common, not a
            # corner case - using "<=" here would freeze the price for the
            # rest of every second. Do not change this to "<=".
            #
            # DelayedByMinutes is the ONE exception to "drop the whole row":
            # dropping price/event_time on a regression is conservative (it
            # never resurrects a stale price), but dropping a newly-reported
            # delay is NOT conservative - it would leave a demoted session
            # looking healthy and let the feed serve 15-minute-old prices to
            # an order decision. So a regressive row still applies the flag
            # (same key-presence semantics as below) while leaving bid / ask /
            # event_time untouched.
            if prev is not None and prev.event_time and event_time and event_time < prev.event_time:
                if "DelayedByMinutes" in quote_block:
                    self._quotes[uic] = replace(
                        prev, delayed_by_minutes=quote_block.get("DelayedByMinutes")
                    )
                return
            # Bid / Ask / DelayedByMinutes each apply the SAME distinction,
            # by dict.get()'s own KEY-PRESENCE semantics (not value
            # truthiness): a field OMITTED from Quote means "unchanged" and
            # falls back to the previous value; a field PRESENT with value
            # null means "reported unknown right now" (e.g. a one-sided
            # market or a halt) and propagates as None, overwriting whatever
            # was cached. This is why plain ``.get(key, default)`` is correct
            # here and a `quote_block.get(key) or default`-style rewrite
            # would be wrong: it would treat an explicit null the same as an
            # omitted key.
            merged = Quote(
                uic=uic,
                bid=quote_block.get("Bid", prev.bid if prev else None),
                ask=quote_block.get("Ask", prev.ask if prev else None),
                event_time=event_time or (prev.event_time if prev else None),
                delayed_by_minutes=quote_block.get(
                    "DelayedByMinutes", prev.delayed_by_minutes if prev else None
                ),
                received_at=received_at,
            )
            self._quotes[uic] = merged
            self._update_running_low(uic, merged)

    def _update_running_low(self, uic: int, quote: Quote) -> None:
        """Fold ``quote.bid`` into the per-uic running low, but ONLY for a
        latchable quote. MUST be called with ``self._lock`` held (it is, from
        ``apply``) — it never re-acquires (``_lock`` is non-reentrant).

        Latchable = undelayed (``DelayedByMinutes`` strictly ``0``; None/absent
        rejected) AND both sides finite positive numbers AND not crossed
        (``ask >= bid``) AND relative spread within ``_LATCH_MAX_RELATIVE_SPREAD``.
        Any doubt is a veto (no arithmetic on a side until proven finite): a
        single transient crossed / delayed / garbage tick must never plant a
        phantom low that later drives a false touch."""
        if quote.delayed_by_minutes != 0:
            return
        bid = _latchable_side(quote.bid)
        ask = _latchable_side(quote.ask)
        if bid is None or ask is None:
            return
        if ask < bid:  # crossed market
            return
        if (ask - bid) / bid > _LATCH_MAX_RELATIVE_SPREAD:
            return
        prev = self._running_low.get(uic)
        self._running_low[uic] = bid if prev is None else min(prev, bid)

    def drain_running_low(self, uic: int) -> float | None:
        """Pop-and-reset the accumulated running low for ``uic`` (read once,
        then gone), or ``None`` when nothing latchable accrued since the last
        drain. The reset is what bounds the accumulation window to one tick —
        the caller drains each watched uic EXACTLY once per decision tick, so a
        laddered pick's deeper tiers all share the same drained low rather than
        the first tier consuming it."""
        with self._lock:
            return self._running_low.pop(uic, None)

    def get(self, uic: int) -> Quote | None:
        with self._lock:
            return self._quotes.get(uic)

    def forget(self, uic: int) -> None:
        with self._lock:
            self._quotes.pop(uic, None)
            self._running_low.pop(uic, None)

    def any_delayed(self) -> bool:
        """True once ANY cached quote reports a positive ``DelayedByMinutes``
        - the only signal that the session was demoted, e.g. by an operator
        logging into SaxoTraderGO and evicting the API session's elevated
        capability."""
        with self._lock:
            return any(
                q.delayed_by_minutes is not None and q.delayed_by_minutes > 0
                for q in self._quotes.values()
            )


async def _default_ws_connect(url: str, headers: dict[str, str]) -> Any:
    """Open a WS connection with the venv's ``websockets`` (asyncio), confined to
    the stream thread. ``websockets>=12`` uses ``additional_headers=`` (older
    ``extra_headers=``) - try both. Imported lazily so importing this module
    never requires ``websockets`` at import time."""
    from websockets.asyncio.client import connect

    try:
        return await connect(url, additional_headers=headers)
    except TypeError:
        return await connect(url, extra_headers=headers)


class SaxoPriceStream:
    """One long-lived WebSocket thread streaming Saxo LIVE prices into a
    :class:`QuoteCache`.

    Owns the cache, the read-only :class:`SaxoMarketDataClient`, the
    ``contextId``/``referenceId`` pair the price subscription is created
    under, and the subscribed uic set. ``ensure_subscribed`` is the only entry
    point that talks REST; the daemon thread only decodes frames off the
    socket and applies them to the cache - it never decides whether a quote is
    fresh enough to drive an order (that lives in the feed adapter).
    """

    _CONTROL_REF_PREFIX = "_"

    def __init__(
        self,
        client: SaxoMarketDataClient,
        token_provider: LiveTokenProvider,
        *,
        context_id: str | None = None,
        reference_id: str = "px",
        refresh_rate_ms: int = 1000,
        cache: QuoteCache | None = None,
        ws_connect: Callable[[str, dict[str, str]], Awaitable[Any]] | None = None,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        async_sleep: Callable[[float], Awaitable[None]] | None = None,
        reclaim_limiter: ReclaimLimiter | None = None,
        metrics_job: str = _DEFAULT_GAUGE_JOB,
    ) -> None:
        self._client = client
        self._token_provider = token_provider
        # The Prometheus job label for every gauge this reader emits (ADR 0016
        # D5). Injected by the composition root so a future LIVE instance's
        # stream never shares a job/textfile with the SIM instance's.
        self._gauge_job = metrics_job
        # A fixed default would make a rebuild after a dead reader thread
        # (nothing here calls stop()) re-POST the SAME ContextId+ReferenceId,
        # and two processes on the same LIVE login would collide too. Mirror
        # the SIM auto-manager's per-process-unique convention
        # (control_loop.py's f"almgr-{os.getpid()}-{int(time.time())}"), with
        # a "px" marker so the two are distinguishable. Still injectable for
        # tests via the explicit context_id parameter.
        self._context_id = (
            context_id if context_id is not None else f"almgr-px-{os.getpid()}-{int(time.time())}"
        )
        self._reference_id = reference_id
        self._refresh_rate_ms = refresh_rate_ms
        self.cache = cache or QuoteCache()
        self._ws_connect = ws_connect or _default_ws_connect
        self._clock = clock
        self._async_sleep = async_sleep
        self._reclaim_limiter = reclaim_limiter or ReclaimLimiter(clock=self._clock)
        self._was_delayed = False

        # DESIRED uic set (caller thread writes via ensure_subscribed, reader
        # snapshots) + the dirty flag telling the reader to (re)create the
        # server-side subscription. The reader owns ALL subscription REST.
        self._subscribed_uics: set[int] = set()
        self._sub_lock = threading.Lock()
        self._sub_dirty = threading.Event()
        self._ctx_seq = 0

        self._consecutive_failures = 0
        self._live_uic_cache: dict[tuple[str, str], int] = {}
        self._last_frame_ts = 0
        self._last_gauge_emit = 0.0

        self._stop = False
        self._thread: threading.Thread | None = None

    # ----- public API (caller thread) -----

    def get(self, uic: int) -> Quote | None:
        return self.cache.get(uic)

    def drain_running_low(self, uic: int) -> float | None:
        """Pop-and-reset the cache's 1 Hz running low for ``uic`` (touch-latch).
        The feed adapter drains this once per watched uic per decision tick."""
        return self.cache.drain_running_low(uic)

    def is_running(self) -> bool:
        """True once ``start()`` has launched the reader thread and it is
        still alive. False before the first ``start()`` call, or after the
        reconnect circuit breaker has tripped (``_MAX_CONSECUTIVE_FAILURES``)
        and ``_supervise`` has returned, exiting the thread - the signal
        ``get_shared_price_stream`` uses to know its singleton needs
        rebuilding rather than being handed back dead."""
        return self._thread is not None and self._thread.is_alive()

    def live_uic_for(self, ticker: str, *, exchange_mic: str) -> int | None:
        """(ticker, venue) -> LIVE uic, cached for the process lifetime.

        Delegates to ``SaxoMarketDataClient.resolve_uic``, which refuses an
        ambiguous match rather than guessing (see its docstring) - the venue is
        load-bearing there too. ``resolve_uic`` returns ``None`` only for a
        COMPLETED HTTP response that is a non-200, has no matching symbol, or
        is ambiguous. Only a SUCCESSFUL resolution is cached: that ``None`` is
        retried on the next call instead of being vetoed for the rest of the
        process, mirroring
        ``brokers.saxo.broker.SaxoBroker.resolve_instrument``'s cache-on-
        success-only pattern. A socket-level transport exception (DNS
        failure, connection refused, timeout) is a DIFFERENT case: it is not
        caught inside ``resolve_uic`` and propagates straight out of this
        method too — the caller's construction boundary
        (``_build_live_exits_feed`` in ``control_loop.py``) treats that as a
        doubt over the WHOLE feed for that tick, not just this one ticker.
        """
        key = (ticker.upper(), exchange_mic.upper())
        cached = self._live_uic_cache.get(key)
        if cached is not None:
            return cached
        live_uic = self._client.resolve_uic(ticker, exchange_mic=exchange_mic)
        if live_uic is not None:
            self._live_uic_cache[key] = live_uic
        return live_uic

    def ensure_subscribed(self, uics: set[int] | list[int]) -> None:
        """Record the DESIRED uic set; the reader thread owns the REST.

        No-op when unchanged. On change: quotes for removed uics are forgotten
        (a stale delayed quote must not pin ``any_delayed`` — and thus disable
        session reclaim — forever) and the dirty flag tells the reader to
        (re)create the single server-side subscription (Saxo has no
        partial-update verb for a subscription's uic list). This method never
        talks REST itself: subscription create/delete happens ONLY on the
        reader thread (per connection, and on the dirty flag), which kills the
        caller/reader race on the shared HTTP session."""
        requested = set(uics)
        with self._sub_lock:
            if requested == self._subscribed_uics:
                return
            removed = self._subscribed_uics - requested
            self._subscribed_uics = requested
        for uic in removed:
            self.cache.forget(uic)
        self._sub_dirty.set()

    def _desired_uics(self) -> set[int]:
        with self._sub_lock:
            return set(self._subscribed_uics)

    def _rotate_context(self) -> None:
        """Fresh ``contextId`` for every connection attempt: a reconnect must
        never resume into a context whose subscription the server already
        dropped (the 2026-08-10 failure loop). Keeps the per-process-unique
        ``almgr-px-`` convention; the sequence suffix disambiguates rotations
        within one second."""
        self._ctx_seq += 1
        self._context_id = f"almgr-px-{os.getpid()}-{int(time.time())}-{self._ctx_seq}"

    def _recreate_subscription(self) -> None:
        """(Reader thread) DELETE best-effort + CREATE for the current desired
        set on the CURRENT context. Clears the dirty flag first so a
        concurrent ``ensure_subscribed`` re-arms it rather than being lost."""
        self._sub_dirty.clear()
        desired = self._desired_uics()
        with contextlib.suppress(Exception):
            self._client.delete_price_subscription(self._context_id, self._reference_id)
        if desired:
            self._client.create_price_subscription(
                context_id=self._context_id,
                reference_id=self._reference_id,
                uics=sorted(desired),
                refresh_rate_ms=self._refresh_rate_ms,
            )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop = False
        self._thread = threading.Thread(
            target=self._thread_main, name="saxo-price-stream", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the reader to stop, best-effort DELETE the subscription, join."""
        self._stop = True
        if self._subscribed_uics:
            with contextlib.suppress(Exception):
                self._client.delete_price_subscription(self._context_id, self._reference_id)
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ----- socket loop (stream thread only; covered by the Task 8 live probe) -----

    def _thread_main(self) -> None:
        import asyncio

        try:
            asyncio.run(self._supervise())
        except Exception:  # pragma: no cover - a reader crash must not touch the caller
            logger.warning("saxo price stream reader thread crashed", exc_info=True)

    async def _supervise(self) -> None:
        import asyncio

        async_sleep = self._async_sleep or asyncio.sleep
        self._emit_stream_gauge(reader_up=True, force=True)
        try:
            while not self._stop:
                if not self._desired_uics():
                    # Zero desired uics -> hold NO WebSocket: an idle,
                    # subscription-less connection is exactly what the venue
                    # kills, and each kill would burn the failure budget.
                    await async_sleep(_IDLE_POLL_S)
                    continue
                try:
                    await self._run_one_connection()
                except Exception:
                    logger.warning("saxo price stream session failed", exc_info=True)
                if self._stop:
                    return
                self._consecutive_failures += 1
                self._emit_stream_gauge(reader_up=True, force=True)
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "saxo price stream circuit breaker tripped after %d consecutive "
                        "failures - stopping the reader",
                        self._consecutive_failures,
                    )
                    return
                exponent = max(0, self._consecutive_failures - 1)
                backoff = min(_BACKOFF_FLOOR_S * (2**exponent), _BACKOFF_CEILING_S)
                await async_sleep(backoff)
        finally:
            # The reader is down from here — on clean stop, breaker trip, or
            # crash alike. reader_up=0 with subscribed_uics>0 is the
            # Prometheus-visible "dark feed" signature.
            self._emit_stream_gauge(reader_up=False, force=True)

    async def _run_one_connection(self) -> None:
        import asyncio

        token = self._token_provider.access_token()
        self._rotate_context()
        url = f"{LIVE_STREAM_URL}?contextId={self._context_id}"
        conn = await self._ws_connect(url, {"Authorization": f"Bearer {token}"})
        try:
            # Fresh context -> the subscription MUST be (re)created before any
            # delta can flow. Runs on the reader thread by design (single
            # owner of subscription REST).
            self._recreate_subscription()
            while not self._stop:
                frame = await asyncio.wait_for(conn.recv(), timeout=_RECV_TIMEOUT_S)
                self._apply_frame(frame)
                self._consecutive_failures = 0  # a delivered frame proves the connection is live
                self._last_frame_ts = int(time.time())
                self._emit_stream_gauge(reader_up=True)
                if self._sub_dirty.is_set():
                    # ensure_subscribed changed the desired set, or the server
                    # sent _resetsubscriptions — recreate on THIS connection.
                    self._recreate_subscription()
        finally:
            with contextlib.suppress(Exception):
                await conn.close()

    def _emit_stream_gauge(self, *, reader_up: bool, force: bool = False) -> None:
        """Best-effort Prometheus textfile emit — a textfile-dir hiccup must
        never crash the reader (same doctrine as the daemon heartbeat)."""
        now = time.monotonic()
        if not force and now - self._last_gauge_emit < _GAUGE_MIN_INTERVAL_S:
            return
        self._last_gauge_emit = now
        from alphalens_pipeline.observability.textfile import emit_domain_metrics

        label = f'{{job="{self._gauge_job}"}}'
        try:
            emit_domain_metrics(
                self._gauge_job,
                {
                    f"alphalens_live_price_stream_reader_up{label}": int(reader_up),
                    f"alphalens_live_price_stream_last_frame_timestamp_seconds{label}": (
                        self._last_frame_ts
                    ),
                    f"alphalens_live_price_stream_consecutive_failures{label}": (
                        self._consecutive_failures
                    ),
                    f"alphalens_live_price_stream_subscribed_uics{label}": len(
                        self._desired_uics()
                    ),
                },
            )
        except OSError:
            logger.warning("live price stream gauge emit failed", exc_info=True)

    def _apply_frame(self, frame: bytes | str) -> None:
        if isinstance(frame, str):
            frame = frame.encode("utf-8")
        now = self._clock()
        for msg in parse_stream_frames(frame):
            if msg.reference_id == _RESET_SUBSCRIPTIONS_REF:
                # The server dropped this context's subscriptions — arm the
                # dirty flag so the connection loop recreates in-place instead
                # of waiting for the recv timeout to force a full reconnect.
                logger.warning("saxo price stream: server reset subscriptions — recreating")
                self._sub_dirty.set()
                continue
            if msg.reference_id.startswith(self._CONTROL_REF_PREFIX):
                continue  # heartbeat / disconnect: liveness only, never a quote row
            try:
                payload = json.loads(msg.payload)
            except ValueError:
                # Covers json.JSONDecodeError AND UnicodeDecodeError — both
                # derive from ValueError.
                logger.warning(
                    "saxo price stream: undecodable JSON payload for refId %r",
                    msg.reference_id,
                )
                continue
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if isinstance(row, dict):
                    self.cache.apply(row, received_at=now)
                else:
                    # A live shape mismatch (Saxo sending something other
                    # than a row object) is dropped, never applied - DEBUG
                    # only, so it leaves a trace without paging on a shape
                    # that may be benign (e.g. a stray scalar).
                    logger.debug(
                        "saxo price stream: dropping non-dict row for refId %r: %r",
                        msg.reference_id,
                        row,
                    )
        self._maybe_reclaim()

    def _maybe_reclaim(self) -> None:
        """Fire the reclaim on a TRANSITION into the delayed state, not once
        per message - a 1 Hz stream would otherwise burn the whole hourly
        budget in seconds. On ``"budget-exhausted"`` there is no bypass: the
        feed adapter's freshness gate already vetoes delayed quotes, so doing
        nothing and waiting for the budget to refill is the safe outcome."""
        is_delayed = self.cache.any_delayed()
        if is_delayed and not self._was_delayed:
            outcome = self._reclaim_limiter.try_reclaim(self._client.elevate_session)
            if outcome == "budget-exhausted":
                logger.warning(
                    "saxo price stream: session reclaim budget exhausted - "
                    "quotes stay delayed until the budget refills"
                )
        self._was_delayed = is_delayed


_shared_stream: SaxoPriceStream | None = None
_shared_stream_lock = threading.Lock()


def get_shared_price_stream(*, metrics_job: str = _DEFAULT_GAUGE_JOB) -> SaxoPriceStream:
    """Module-level singleton, started on first call.

    A WebSocket must outlive a single daemon tick, but the feed factory that
    wants one is called EVERY tick. Creating a fresh ``SaxoPriceStream`` per
    call would open (and never close) a new connection every ~45s. Instead the
    first caller in the process pays for construction + ``start()``; every
    later call reuses the same stream and its live cache, and the caller is
    expected to only call ``ensure_subscribed`` to reconcile the subscription
    set for that tick.

    Rebuilds the singleton when the existing stream's reader thread has died
    (the reconnect circuit breaker tripped after
    ``_MAX_CONSECUTIVE_FAILURES``) - otherwise a dead stream would sit here
    silently serving no fresh quotes for the rest of the process, with only a
    log warning as a trace. The construction-then-``start()``-then-assign
    order is unchanged either way: ``_shared_stream`` is only ever rebound
    AFTER ``start()`` returns.

    ``metrics_job`` (ADR 0016 D5) is threaded into the constructed stream ONLY
    on the FIRST call that actually builds it (or a rebuild after the reader
    died) — a call that reuses the still-live singleton keeps that stream's
    already-resolved job label. The composition root
    (``control_loop._default_live_exits_feed_factory``) passes
    ``state_paths.price_stream_metrics_job()`` every tick; the default here
    keeps standalone/test construction unchanged.
    """
    global _shared_stream  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _shared_stream is None or not _shared_stream.is_running():
        with _shared_stream_lock:
            if _shared_stream is None or not _shared_stream.is_running():
                cfg = LiveAuthConfig.from_env()
                token_provider = LiveTokenProvider(cfg)
                client = SaxoMarketDataClient(token_provider=token_provider)
                stream = SaxoPriceStream(client, token_provider, metrics_job=metrics_job)
                stream.start()
                _shared_stream = stream
    return _shared_stream
