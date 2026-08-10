"""Saxo LIVE price stream: one long-lived WebSocket thread + a quote cache.

Price subscriptions stream DELTAS - an unchanged field is OMITTED. This differs
from the positions/orders stream, whose reader deliberately never merges and
re-reads full REST state instead. There is no cheap full re-read for a quote, so
this cache MUST merge: a message carrying only a Bid must leave the Ask intact.

The socket loop only decodes and applies. Every decision about whether a cached
quote may drive an order lives in the feed adapter's freshness gate.

:class:`SaxoPriceStream`'s socket loop is thin glue around
:func:`alphalens_pipeline.data.alt_data.saxo_stream_envelope.parse_stream_frames`
and the SIM streaming reader's reconnect tuning (``max_consecutive_failures=6``,
exponential backoff 1s -> 30s ceiling, see
``alphalens_pipeline.brokers.saxo.streaming.StreamTuning``) - reused as VALUES
only, not by importing that module (this package must not import ``brokers/``).
Its correctness is exercised by the Task 8 live probe, not by mocking a
WebSocket here; only :class:`QuoteCache` above is unit-tested.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
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

    def get(self, uic: int) -> Quote | None:
        with self._lock:
            return self._quotes.get(uic)

    def forget(self, uic: int) -> None:
        with self._lock:
            self._quotes.pop(uic, None)

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
    ) -> None:
        self._client = client
        self._token_provider = token_provider
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

        self._subscribed_uics: set[int] = set()
        self._consecutive_failures = 0
        self._live_uic_cache: dict[tuple[str, str], int] = {}

        self._stop = False
        self._thread: threading.Thread | None = None

    # ----- public API (caller thread) -----

    def get(self, uic: int) -> Quote | None:
        return self.cache.get(uic)

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
        """Diff the requested uic set against the subscribed one; no-op when
        unchanged, otherwise DELETE + recreate the single price subscription
        with the new set (Saxo has no partial-update verb for a subscription's
        uic list)."""
        requested = set(uics)
        if requested == self._subscribed_uics:
            return
        if self._subscribed_uics:
            with contextlib.suppress(Exception):
                self._client.delete_price_subscription(self._context_id, self._reference_id)
        if requested:
            self._client.create_price_subscription(
                context_id=self._context_id,
                reference_id=self._reference_id,
                uics=sorted(requested),
                refresh_rate_ms=self._refresh_rate_ms,
            )
        self._subscribed_uics = requested

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

    async def _supervise(self) -> None:  # pragma: no cover - exercised by live probe
        import asyncio

        async_sleep = self._async_sleep or asyncio.sleep
        while not self._stop:
            try:
                await self._run_one_connection()
            except Exception:
                logger.warning("saxo price stream session failed", exc_info=True)
            if self._stop:
                return
            self._consecutive_failures += 1
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

    async def _run_one_connection(self) -> None:  # pragma: no cover - exercised by live probe
        token = self._token_provider.access_token()
        url = f"{LIVE_STREAM_URL}?contextId={self._context_id}"
        conn = await self._ws_connect(url, {"Authorization": f"Bearer {token}"})
        try:
            while not self._stop:
                frame = await conn.recv()
                self._apply_frame(frame)
                self._consecutive_failures = 0  # a delivered frame proves the connection is live
        finally:
            with contextlib.suppress(Exception):
                await conn.close()

    def _apply_frame(self, frame: bytes | str) -> None:
        if isinstance(frame, str):
            frame = frame.encode("utf-8")
        now = self._clock()
        for msg in parse_stream_frames(frame):
            if msg.reference_id.startswith(self._CONTROL_REF_PREFIX):
                continue  # heartbeat / reset / disconnect: liveness only, never a quote row
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


def get_shared_price_stream() -> SaxoPriceStream:
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
    """
    global _shared_stream  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _shared_stream is None or not _shared_stream.is_running():
        with _shared_stream_lock:
            if _shared_stream is None or not _shared_stream.is_running():
                cfg = LiveAuthConfig.from_env()
                token_provider = LiveTokenProvider(cfg)
                client = SaxoMarketDataClient(token_provider=token_provider)
                stream = SaxoPriceStream(client, token_provider)
                stream.start()
                _shared_stream = stream
    return _shared_stream
