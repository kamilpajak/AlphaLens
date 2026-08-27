"""UNIX-socket server publishing ONE Saxo LIVE quote stream to several
processes (#1172).

Saxo grants exactly one elevated (``FullTradingAndChat``) session per LIVE
login; a second elevated consumer silently demotes BOTH sides to 15-minute
delayed quotes. ``get_shared_price_stream`` is a per-PROCESS singleton, so
before this module only one broker-manager daemon could hold real-time prices
and the other ran dark. Here one always-on reader process owns the elevated
session and both daemons read from it.

**The connection is the unit of identity.** A connection owns:

* its own slice of the wire subscription — its scopes are namespaced
  ``<conn-id>:<scope>`` so SIM's ``exits`` and LIVE's ``exits`` are different
  keys and the wire set stays the UNION rather than last-writer-wins; and
* its own touch-latch accumulator (``register_latch_consumer``), because the
  drain is a POP: a shared accumulator would let one daemon's drain destroy
  the other's evidence of a real touch.

Closing the connection releases both. That is what makes a crashed daemon
harmless: no phantom subscription streaming uics nobody reads, no accumulator
nobody drains. A reconnect starts from an empty window, which loses at most one
tick of latch accrual — the safe direction.

Every subscribe updates the latch set and the wire subscription TOGETHER (the
synchronisation obligation documented on ``QuoteCache.set_latch_uics``): a
latch set narrower than the subscription accrues nothing for the missing uics
and touch detection goes dark with no error.

Wire protocol v1 — newline-delimited JSON, one request per line, one response
per line, in order::

    -> {"v": 1, "op": "quote", "uic": 211}
    <- {"ok": true, "result": {...}}
    <- {"ok": false, "error": "bad_request"}

Error codes are stable and never renamed: ``bad_version``, ``bad_request``,
``unknown_op``, ``internal``. A bad request NEVER drops the connection — a bug
in one client must not disconnect the other daemon.

This module must not import ``brokers/`` (dependency direction); it talks to
the stream through the structural
:class:`~alphalens_pipeline.data.alt_data.quote_source.QuoteSource` surface.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
import json
import logging
import os
import socketserver
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from alphalens_pipeline.data.alt_data.quote_source import Quotelike, QuoteSource

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

SOCKET_PATH_ENV = "ALPHALENS_SAXO_PRICE_READER_SOCKET"

# Owner-only, like the Saxo token store: the socket hands out live market data
# and mutates a shared subscription, so it is not for other local users.
_SOCKET_MODE = 0o600
_SOCKET_DIR_MODE = 0o700

# Stable, machine-readable error codes (CLI doctrine: domain detail lives in
# the code, never in prose). NEVER rename one.
ERR_BAD_VERSION = "bad_version"
ERR_BAD_REQUEST = "bad_request"
ERR_UNKNOWN_OP = "unknown_op"
ERR_INTERNAL = "internal"

# Prometheus job labels. TWO of them, and both distinct from the per-env
# ``live-price-stream-<env>`` jobs the in-process daemons use: emit_domain_metrics
# rewrites a whole per-job file, so two emitters sharing a job silently erase
# each other's series. The stream the reader owns keeps the existing
# alphalens_live_price_stream_* family under its own job; the server's own
# liveness is a separate family under a separate job.
SERVER_METRICS_JOB = "price-reader"
READER_STREAM_METRICS_JOB = "live-price-stream-reader"

# Liveness cadence. Well under the AlphalensPriceReaderDown evaluation window,
# and cheap (one small file write).
_HEARTBEAT_INTERVAL_S = 30.0


def default_socket_path() -> Path:
    """``~/.alphalens/price_reader/reader.sock`` unless the env names another.

    Resolved at CALL time (never an import-time constant) so a test or an
    operator can point a process elsewhere, matching ``state_paths``' rule."""
    override = os.environ.get(SOCKET_PATH_ENV)
    if override:
        return Path(override)
    return Path.home() / ".alphalens" / "price_reader" / "reader.sock"


class ReaderStream(QuoteSource, Protocol):
    """The quote source a reader server needs: a :class:`QuoteSource` plus the
    per-consumer touch-latch surface (``SaxoPriceStream`` satisfies it)."""

    def register_latch_consumer(self, consumer: str) -> None: ...

    def unregister_latch_consumer(self, consumer: str) -> None: ...

    def set_latch_uics(self, consumer: str, uics: set[int] | list[int]) -> None: ...


class ProtocolError(Exception):
    """A malformed or unsupported request. Carries the stable wire code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _as_int(value: object, *, field: str) -> int:
    """Coerce a wire value to ``int`` or raise :class:`ProtocolError`.

    ``bool`` is rejected on purpose: ``isinstance(True, int)`` is True in
    Python, and a JSON ``true`` reaching a uic field means the caller is
    confused, not that uic 1 was requested."""
    if isinstance(value, bool) or not isinstance(value, int):
        logger.debug("price reader: bad %s field: %r", field, value)
        raise ProtocolError(ERR_BAD_REQUEST)
    return value


def _as_float(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        logger.debug("price reader: bad %s field: %r", field, value)
        raise ProtocolError(ERR_BAD_REQUEST)
    return float(value)


def _as_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        logger.debug("price reader: bad %s field: %r", field, value)
        raise ProtocolError(ERR_BAD_REQUEST)
    return value


def _as_uic_list(value: object) -> list[int]:
    if not isinstance(value, list):
        raise ProtocolError(ERR_BAD_REQUEST)
    return [_as_int(item, field="uics[]") for item in value]


def _default_emit(job: str, metrics: Mapping[str, float | int]) -> object:
    """Prometheus textfile emit, imported lazily so this module stays usable
    (and testable) without the observability package on the path."""
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    return emit_domain_metrics(job, metrics)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _quote_payload(quote: Quotelike | None) -> dict[str, Any] | None:
    """Serialise a quote for the wire, passing NULLS THROUGH.

    A missing side or an unknown delay is information the consumer's freshness
    gate acts on (it vetoes); inventing a number here would turn a veto into a
    tradeable price."""
    if quote is None:
        return None
    return {
        "uic": quote.uic,
        "bid": quote.bid,
        "ask": quote.ask,
        "event_time": _iso(quote.event_time),
        "delayed_by_minutes": quote.delayed_by_minutes,
        "received_at": _iso(quote.received_at),
    }


class _ConnectionState:
    """One client connection's identity: its consumer id and the scopes it has
    subscribed under (so both can be released when it closes)."""

    def __init__(self, consumer: str) -> None:
        self.consumer = consumer
        self.scope_uics: dict[str, set[int]] = {}

    def wire_scope(self, scope: str) -> str:
        return f"{self.consumer}:{scope}"

    def union(self) -> set[int]:
        return set().union(*self.scope_uics.values()) if self.scope_uics else set()


class _Handler(socketserver.StreamRequestHandler):
    """One connection, one thread, one latch consumer.

    NO read timeout on purpose: a client holds its connection open across
    ticks (that persistence is what gives it a stable accumulator), so long
    silences are the NORMAL state, not a wedge. A dead client is detected by
    its socket closing — which ends ``handle`` and releases everything — not
    by a timer.
    """

    @property
    def _server(self) -> PriceReaderServer:
        return self.server.reader  # type: ignore[attr-defined]

    def setup(self) -> None:
        super().setup()
        self._state = self._server.open_connection()

    def handle(self) -> None:
        for raw in self.rfile:
            response = self._server.dispatch(self._state, raw)
            try:
                self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")
                self.wfile.flush()
            except OSError:
                # The client vanished mid-reply. Its state is released in
                # finish(); nothing here is worth an alert.
                return

    def finish(self) -> None:
        with contextlib.suppress(Exception):
            self._server.close_connection(self._state)
        super().finish()


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    # A bound-but-unaccepted backlog beyond this means something is very wrong;
    # two daemons plus an operator CLI never need more.
    request_queue_size = 16

    def __init__(self, address: str, handler: type[socketserver.BaseRequestHandler], reader):
        self.reader = reader
        super().__init__(address, handler)

    def handle_error(self, request, client_address) -> None:  # pragma: no cover - defensive
        logger.warning("price reader: handler crashed", exc_info=True)


class PriceReaderServer:
    """Serves one :class:`ReaderStream` to local processes over a UNIX socket.

    The server does NOT own the stream's lifecycle beyond what it is told to
    do: the CLI command that constructs both is the composition root and is
    what stops the stream.
    """

    def __init__(
        self,
        stream: ReaderStream,
        socket_path: Path,
        *,
        metrics_job: str = SERVER_METRICS_JOB,
        emit: Callable[[str, Mapping[str, float | int]], object] | None = None,
        heartbeat_interval_s: float = _HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._stream = stream
        self._path = Path(socket_path)
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_interval_s = heartbeat_interval_s
        self._metrics_job = metrics_job
        self._emit = emit or _default_emit
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._open: dict[str, _ConnectionState] = {}
        self._requests = 0
        self._errors = 0
        self._last_request_ts = 0

    # ----- lifecycle -----

    @property
    def socket_path(self) -> Path:
        return self._path

    def start(self) -> None:
        """Bind, then serve in a background thread."""
        self._bind()
        self._thread = threading.Thread(target=self._serve, name="price-reader-server", daemon=True)
        self._thread.start()
        self._start_heartbeat()

    def _bind(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._path.parent, _SOCKET_DIR_MODE)
        # A hard kill leaves the socket file behind and bind() would then fail
        # with EADDRINUSE forever — the reader must come back after a crash or
        # a reboot without an operator deleting a file by hand.
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        self._server = _ThreadingUnixServer(str(self._path), _Handler, self)
        os.chmod(self._path, _SOCKET_MODE)

    def _serve(self) -> None:
        assert self._server is not None
        with contextlib.suppress(Exception):
            self._server.serve_forever(poll_interval=0.2)

    def serve_forever(self) -> None:
        """Bind and serve on the CALLING thread (the daemon entry point)."""
        self._bind()
        self._start_heartbeat()
        self._serve()

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()
        self.emit_gauges()
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="price-reader-heartbeat", daemon=True
        )
        self._heartbeat.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self._heartbeat_interval_s):
            self.emit_gauges()

    def emit_gauges(self, *, up: bool = True) -> None:
        """Publish liveness + traffic counters. Best-effort by contract: a
        textfile-dir hiccup must never take prices down (same doctrine as the
        daemon heartbeat)."""
        label = f'{{job="{self._metrics_job}"}}'
        with self._lock:
            clients = len(self._open)
            requests, errors, last_ts = self._requests, self._errors, self._last_request_ts
        try:
            self._emit(
                self._metrics_job,
                {
                    f"alphalens_price_reader_up{label}": int(up),
                    f"alphalens_price_reader_clients{label}": clients,
                    f"alphalens_price_reader_requests_total{label}": requests,
                    f"alphalens_price_reader_request_errors_total{label}": errors,
                    f"alphalens_price_reader_last_request_timestamp_seconds{label}": last_ts,
                },
            )
        except Exception:
            logger.warning("price reader: gauge emit failed", exc_info=True)

    def stop(self) -> None:
        """Shut the listener down, drop every connection's state, unlink the
        socket. Idempotent — a SIGTERM path may race an operator ``stop``."""
        self._heartbeat_stop.set()
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None:
            heartbeat.join(timeout=2.0)
        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(Exception):
                server.shutdown()
            with contextlib.suppress(Exception):
                server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        for state in self._drain_open_states():
            self._release(state)
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        # A reader that merely STOPS emitting looks identical to a healthy quiet
        # one until the staleness rule fires much later; an explicit up=0 is
        # what the down alert keys on.
        self.emit_gauges(up=False)

    def wait_for_idle(self, timeout: float = 2.0) -> None:
        """Block until every connection's teardown has been processed.

        Test seam ONLY: a client's ``close()`` returns before the server's
        handler thread has run ``finish()``, so an assertion on the release
        would otherwise race the handler."""
        step = 0.01
        waited = 0.0
        while waited < timeout:
            with self._lock:
                if not self._open:
                    return
            time.sleep(step)
            waited += step

    # ----- connection bookkeeping -----

    def open_connection(self) -> _ConnectionState:
        """Register a new connection: its own latch consumer, its own scopes.

        The id is server-assigned and never client-supplied — two connections
        from the same daemon must not share an accumulator."""
        with self._lock:
            consumer = f"conn-{next(self._ids)}"
            state = _ConnectionState(consumer)
            self._open[consumer] = state
        self._stream.register_latch_consumer(consumer)
        logger.info("price reader: client connected (%s)", consumer)
        return state

    def close_connection(self, state: _ConnectionState) -> None:
        with self._lock:
            if self._open.pop(state.consumer, None) is None:
                return  # already released (stop() raced the handler)
        self._release(state)

    def _drain_open_states(self) -> list[_ConnectionState]:
        with self._lock:
            states = list(self._open.values())
            self._open.clear()
        return states

    def _release(self, state: _ConnectionState) -> None:
        """Hand back everything the connection held: an EMPTY desired set for
        each of its wire scopes, then its accumulator. Best-effort per step —
        a failure to release one scope must not skip the others."""
        for scope in list(state.scope_uics):
            with contextlib.suppress(Exception):
                self._stream.ensure_subscribed([], scope=state.wire_scope(scope))
        with contextlib.suppress(Exception):
            self._stream.unregister_latch_consumer(state.consumer)
        logger.info("price reader: client disconnected (%s)", state.consumer)

    # ----- dispatch -----

    def dispatch(self, state: _ConnectionState, raw: bytes | str) -> dict[str, Any]:
        """Decode one request line and run it. NEVER raises: every failure
        becomes an error response, because dropping the connection would take
        the other daemon's prices down with this one's bug."""
        self._count_request()
        try:
            request = self._decode(raw)
            op = _as_str(request.get("op"), field="op")
            handler = self._OPS.get(op)
            if handler is None:
                raise ProtocolError(ERR_UNKNOWN_OP)
            return {"ok": True, "result": handler(self, state, request)}
        except ProtocolError as exc:
            self._count_error()
            return {"ok": False, "error": exc.code}
        # Deliberately broad: a defect in the quote source (or an unexpected
        # payload shape) must degrade to one error response, never kill the
        # reader process that both daemons depend on.
        except Exception:
            self._count_error()
            logger.warning("price reader: request failed", exc_info=True)
            return {"ok": False, "error": ERR_INTERNAL}

    def _count_request(self) -> None:
        with self._lock:
            self._requests += 1
            self._last_request_ts = int(time.time())

    def _count_error(self) -> None:
        with self._lock:
            self._errors += 1

    @staticmethod
    def _decode(raw: bytes | str) -> dict[str, Any]:
        try:
            request = json.loads(raw)
        except ValueError as exc:
            raise ProtocolError(ERR_BAD_REQUEST) from exc
        if not isinstance(request, dict):
            raise ProtocolError(ERR_BAD_REQUEST)
        if request.get("v") != PROTOCOL_VERSION:
            raise ProtocolError(ERR_BAD_VERSION)
        return request

    # ----- ops -----

    def _op_hello(self, state: _ConnectionState, request: dict[str, Any]) -> dict[str, Any]:
        """Informational handshake. Registration already happened on connect,
        so a client that skips ``hello`` still works — the id it would learn
        here is never used on the wire."""
        return {"pid": os.getpid(), "protocol": PROTOCOL_VERSION, "consumer": state.consumer}

    def _op_subscribe(self, state: _ConnectionState, request: dict[str, Any]) -> None:
        """Replace this connection's ``scope`` slice, then move the wire
        subscription and the latch set TOGETHER (see the module docstring)."""
        scope = _as_str(request.get("scope"), field="scope")
        uics = _as_uic_list(request.get("uics"))
        state.scope_uics[scope] = set(uics)
        self._stream.ensure_subscribed(sorted(set(uics)), scope=state.wire_scope(scope))
        self._stream.set_latch_uics(state.consumer, state.union())

    def _op_quote(self, state: _ConnectionState, request: dict[str, Any]) -> dict[str, Any] | None:
        return _quote_payload(self._stream.get(_as_int(request.get("uic"), field="uic")))

    def _op_drain_low(self, state: _ConnectionState, request: dict[str, Any]) -> float | None:
        uic = _as_int(request.get("uic"), field="uic")
        return self._stream.drain_running_low(uic, consumer=state.consumer)  # type: ignore[call-arg]

    def _op_reseed_low(self, state: _ConnectionState, request: dict[str, Any]) -> None:
        uic = _as_int(request.get("uic"), field="uic")
        low = _as_float(request.get("low"), field="low")
        self._stream.reseed_running_low(uic, low, consumer=state.consumer)  # type: ignore[call-arg]

    def _op_resolve_uic(self, state: _ConnectionState, request: dict[str, Any]) -> int | None:
        ticker = _as_str(request.get("ticker"), field="ticker")
        mic = _as_str(request.get("exchange_mic"), field="exchange_mic")
        return self._stream.live_uic_for(ticker, exchange_mic=mic)

    _OPS = {
        "hello": _op_hello,
        "subscribe": _op_subscribe,
        "quote": _op_quote,
        "drain_low": _op_drain_low,
        "reseed_low": _op_reseed_low,
        "resolve_uic": _op_resolve_uic,
    }
