"""Socket proxy onto the shared Saxo price reader (#1172).

Stands in for the in-process :class:`SaxoPriceStream` wherever a daemon builds
its price feed, so both broker-manager instances read the ONE elevated Saxo
session the reader process holds. Satisfies
:class:`~alphalens_pipeline.data.alt_data.quote_source.QuoteSource`, which is
what lets the price-feed adapter stay unaware of which side of a process
boundary its quotes come from.

**VETO, NEVER RAISE.** Every method returns the no-data answer on ANY transport
doubt — no socket, refused, timed out, short read, unparseable reply, an
``ok:false`` from the server, a protocol mismatch. This is not defensive
habit: ``latest()`` runs in the MIDDLE of a daemon tick, outside the
construction boundary that catches broad in ``control_loop._build_live_exits_feed``,
and an exception there would abort the tick along with the never-naked
protection pass that follows it. A reader that is down must look exactly like
the feature flag being off.

**One connection, held across ticks.** The reader keys each client's touch-latch
accumulator by CONNECTION, so a client that reconnected per call would drain a
fresh, always-empty window and never see a touch. The connection is opened
lazily, kept, and re-established after a failure — with a cooldown, so a tick
that asks about ~30 uics against a down reader makes ONE connect attempt rather
than thirty.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Wire contract v1 and the socket-path resolver come from the SERVER module
# rather than being duplicated here, so the two halves cannot drift.
from alphalens_pipeline.data.alt_data.price_reader_server import (
    PROTOCOL_VERSION,
    default_socket_path,
)

logger = logging.getLogger(__name__)

# A local UNIX round trip is microseconds; this only bounds a wedged reader
# (one that accepted the connection and then stopped answering). Well under the
# daemon's ~45 s tick so a stall cannot eat the protection pass behind it.
_DEFAULT_TIMEOUT_S = 3.0

# After a failure, wait this long before trying to connect again. Sized so one
# tick's worth of calls costs at most one connect attempt.
_DEFAULT_RECONNECT_BACKOFF_S = 30.0

_RECV_CHUNK = 65536

# Reply-line ceiling, mirroring the server's request ceiling. Every legitimate
# reply is a short JSON object; anything larger is a broken reader, and an
# unbounded buffer inside the trading daemon is not an option.
_MAX_REPLY_BYTES = 64 * 1024


@dataclass(frozen=True)
class RemoteQuote:
    """The wire form of a quote, shaped like the in-process ``Quote``.

    ``bid``/``ask`` are DECLARED ``float | None`` and carry whatever the reader
    sent, including ``None``: a missing side is information the freshness gate
    acts on, and inventing a number here would turn a veto into a tradeable
    price."""

    uic: int
    bid: float | None
    ask: float | None
    event_time: dt.datetime | None
    delayed_by_minutes: int | None
    received_at: dt.datetime


def _parse_dt(raw: object) -> dt.datetime | None:
    """ISO-8601 back to an aware datetime, or ``None`` on any doubt.

    A naive result is refused rather than assumed UTC: ``is_fresh`` subtracts
    it from an aware ``now``, and a wrong guess about the zone is a wrong
    answer about freshness."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class RemoteQuoteSource:
    """A :class:`QuoteSource` served over a UNIX socket by the price reader."""

    def __init__(
        self,
        socket_path: Path | str | None = None,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        reconnect_backoff_s: float = _DEFAULT_RECONNECT_BACKOFF_S,
        monotonic=time.monotonic,
    ) -> None:
        self._path = Path(socket_path) if socket_path is not None else default_socket_path()
        self._timeout_s = timeout_s
        self._reconnect_backoff_s = reconnect_backoff_s
        self._monotonic = monotonic
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_attempt_at = 0.0
        self._uics: dict[tuple[str, str], int] = {}
        # Counters the composition root publishes as gauges, and the reconnect
        # test asserts on.
        self.connect_attempts = 0
        self.failures = 0
        self._down_logged = False

    # ----- QuoteSource -----

    def get(self, uic: int) -> RemoteQuote | None:
        payload = self._call("quote", uic=uic)
        if not isinstance(payload, dict):
            return None
        try:
            return RemoteQuote(
                uic=int(payload["uic"]),
                bid=payload.get("bid"),
                ask=payload.get("ask"),
                event_time=_parse_dt(payload.get("event_time")),
                delayed_by_minutes=payload.get("delayed_by_minutes"),
                received_at=_parse_dt(payload.get("received_at")) or dt.datetime.now(dt.UTC),
            )
        except (KeyError, TypeError, ValueError):
            # A shape change on the wire is a doubt like any other.
            logger.warning("price-reader client: unusable quote payload: %r", payload)
            return None

    def drain_running_low(self, uic: int) -> float | None:
        low = self._call("drain_low", uic=uic)
        return float(low) if isinstance(low, (int, float)) and not isinstance(low, bool) else None

    def reseed_running_low(self, uic: int, low: float) -> None:
        self._call("reseed_low", uic=uic, low=low)

    def live_uic_for(self, ticker: str, *, exchange_mic: str) -> int | None:
        """Resolve and cache on SUCCESS ONLY, mirroring
        ``SaxoPriceStream.live_uic_for``: caching a miss would veto the ticker
        for the process lifetime over one bad tick."""
        key = (ticker.upper(), exchange_mic.upper())
        cached = self._uics.get(key)
        if cached is not None:
            return cached
        resolved = self._call("resolve_uic", ticker=ticker, exchange_mic=exchange_mic)
        if isinstance(resolved, int) and not isinstance(resolved, bool):
            self._uics[key] = resolved
            return resolved
        return None

    def ensure_subscribed(self, uics: set[int] | list[int], *, scope: str = "default") -> None:
        self._call("subscribe", scope=scope, uics=sorted(set(uics)))

    # ----- transport -----

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._buf = b""
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def _call(self, op: str, **args: Any) -> Any:
        """One request/response. Returns the result, or ``None`` on ANY doubt.

        One retry at most, and ONLY when the connection was already open: a
        reader restarted between ticks leaves a stale socket whose first write
        fails, and that case deserves a second try. A fresh connect failure has
        nothing to retry, so it enters the cooldown at once.

        The ordering here is load-bearing and was measured wrong before: marking
        the client down BEFORE the retry put attempt 2 inside its own cooldown,
        so the retry never ran at the production backoff and a daemon whose
        reader restarted stayed priceless for the whole cooldown."""
        had_connection = self._sock is not None
        for attempt in (1, 2):
            sock = self._connection()
            if sock is None:
                return None
            try:
                return self._exchange(sock, op, args)
            # Deliberately broad: every transport doubt is a veto, never a raise
            # into the tick (see the module docstring).
            except Exception as exc:
                self.close()  # drop the socket AND its half-read buffer
                if attempt == 2 or not had_connection:
                    self._mark_down(f"{op} failed: {exc}")
                    return None
        return None

    def _exchange(self, sock: socket.socket, op: str, args: dict[str, Any]) -> Any:
        request = json.dumps({"v": PROTOCOL_VERSION, "op": op, **args}).encode("utf-8") + b"\n"
        sock.sendall(request)
        line = self._read_line(sock)
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError("reply is not an object")
        if not reply.get("ok"):
            # A refusal is a FAILURE (it means the reader could not answer),
            # unlike `ok: true, result: null`, which is a legitimate no-data.
            raise ValueError(f"reader refused {op}: {reply.get('error')}")
        return reply.get("result")

    def _read_line(self, sock: socket.socket) -> bytes:
        """Read one newline-terminated reply, BOUNDED.

        The per-read timeout does not bound a reader that keeps sending without
        ever ending the line — every read succeeds, and the buffer grows inside
        the daemon. The server bounds request lines for the same reason."""
        while b"\n" not in self._buf:
            chunk = sock.recv(_RECV_CHUNK)
            if not chunk:
                raise ConnectionError("reader closed the connection")
            self._buf += chunk
            if len(self._buf) > _MAX_REPLY_BYTES:
                raise ConnectionError("reader reply exceeded the line ceiling")
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def _connection(self) -> socket.socket | None:
        if self._sock is not None:
            return self._sock
        now = self._monotonic()
        if now < self._next_attempt_at:
            return None  # still cooling down from the last failure
        self.connect_attempts += 1
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout_s)
        try:
            sock.connect(str(self._path))
        except OSError as exc:
            sock.close()
            self._mark_down(f"connect to {self._path} failed: {exc}")
            return None
        self._sock = sock
        self._buf = b""
        if self._down_logged:
            logger.info("price-reader client: reconnected to %s", self._path)
            self._down_logged = False
        return sock

    def _mark_down(self, detail: str) -> None:
        self.failures += 1
        self._next_attempt_at = self._monotonic() + self._reconnect_backoff_s
        if not self._down_logged:
            self._down_logged = True
            logger.warning(
                "price-reader client: no prices from %s — %s. Vetoing every quote until it "
                "answers again.",
                self._path,
                detail,
            )

    @property
    def is_connected(self) -> bool:
        return self._sock is not None
