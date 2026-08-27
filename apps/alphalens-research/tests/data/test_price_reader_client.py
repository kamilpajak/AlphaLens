"""The cross-process price-reader CLIENT (#1172 PR-3).

End-to-end over a REAL socket against the real server in front of a fake quote
source — the seam being proven is that a socket proxy is interchangeable with
the in-process stream, so a stubbed transport would prove nothing.

The through-line of these tests is VETO, NEVER RAISE. ``latest()`` runs in the
middle of a daemon tick, outside the construction boundary that catches broad
in ``_build_live_exits_feed``; an exception there would abort the tick and with
it the never-naked protection pass that runs after it. Every transport failure
must therefore look exactly like "no price".
"""

from __future__ import annotations

import contextlib
import datetime as dt
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from alphalens_pipeline.data.alt_data.price_reader_client import RemoteQuoteSource
from alphalens_pipeline.data.alt_data.price_reader_server import PriceReaderServer
from alphalens_pipeline.data.alt_data.quote_source import QuoteSource

_T0 = dt.datetime(2026, 8, 27, 14, 30, 0, tzinfo=dt.UTC)


class _FakeQuote:
    def __init__(self, uic: int, bid, ask, *, delayed: int | None = 0, event_time=_T0) -> None:
        self.uic = uic
        self.bid = bid
        self.ask = ask
        self.event_time = event_time
        self.delayed_by_minutes = delayed
        self.received_at = _T0


class _FakeStream:
    def __init__(self) -> None:
        self.quotes: dict[int, _FakeQuote] = {}
        self.subscribed: dict[str, set[int]] = {}
        self.latch_uics: dict[str, set[int]] = {}
        self.lows: dict[tuple[str, int], float] = {}
        self.reseeds: list[tuple[str, int, float]] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self.registered: list[str] = []

    def get(self, uic: int):
        return self.quotes.get(uic)

    def drain_running_low(self, uic: int, *, consumer: str = "default"):
        return self.lows.pop((consumer, uic), None)

    def reseed_running_low(self, uic: int, low: float, *, consumer: str = "default") -> None:
        self.reseeds.append((consumer, uic, low))

    def live_uic_for(self, ticker: str, *, exchange_mic: str):
        self.resolve_calls.append((ticker, exchange_mic))
        return {"MRVI": 211, "PSNL": 212}.get(ticker)

    def ensure_subscribed(self, uics, *, scope: str = "default") -> None:
        self.subscribed[scope] = set(uics)

    def register_latch_consumer(self, consumer: str) -> None:
        self.registered.append(consumer)

    def unregister_latch_consumer(self, consumer: str) -> None:
        return None

    def set_latch_uics(self, consumer: str, uics) -> None:
        self.latch_uics[consumer] = set(uics)


class _ServedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sock" / "reader.sock"
        self.stream = _FakeStream()
        self.server = PriceReaderServer(self.stream, self.path, heartbeat_interval_s=3600)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = RemoteQuoteSource(self.path, timeout_s=5.0)
        self.addCleanup(self.client.close)


class TestRoundTrip(_ServedTestCase):
    def test_the_client_satisfies_the_quote_source_contract(self):
        """The whole point of PR-1's Protocol: the socket proxy stands in for
        the in-process stream without the adapter knowing."""
        self.assertIsInstance(self.client, QuoteSource)

    def test_a_quote_survives_the_wire_intact(self):
        self.stream.quotes[211] = _FakeQuote(211, 18.61, 18.62)
        quote = self.client.get(211)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.uic, 211)
        self.assertEqual(quote.bid, 18.61)
        self.assertEqual(quote.ask, 18.62)
        self.assertEqual(quote.delayed_by_minutes, 0)
        self.assertEqual(quote.event_time, _T0)
        self.assertEqual(quote.received_at, _T0)

    def test_an_absent_quote_is_none_not_an_error(self):
        self.assertIsNone(self.client.get(999))

    def test_a_half_blank_quote_keeps_its_nulls(self):
        """The adapter vetoes on a missing side; the client must not invent one
        to make the shape tidy."""
        self.stream.quotes[211] = _FakeQuote(211, None, 18.62, delayed=None)
        quote = self.client.get(211)
        assert quote is not None
        self.assertIsNone(quote.bid)
        self.assertIsNone(quote.delayed_by_minutes)

    def test_a_quote_without_an_event_time_stays_without_one(self):
        """``is_fresh`` vetoes a None event_time. Defaulting it to 'now' here
        would turn a veto into a tradeable price — the exact dishonesty the
        price-feed contract forbids."""
        self.stream.quotes[211] = _FakeQuote(211, 18.61, 18.62, event_time=None)
        quote = self.client.get(211)
        assert quote is not None
        self.assertIsNone(quote.event_time)

    def test_subscribe_reaches_the_stream_under_the_callers_scope(self):
        self.client.ensure_subscribed([211, 212], scope="entry-watch")
        self.assertEqual(len(self.stream.subscribed), 1)
        scope, uics = next(iter(self.stream.subscribed.items()))
        self.assertTrue(scope.endswith(":entry-watch"))
        self.assertEqual(uics, {211, 212})

    def test_drain_and_reseed_round_trip(self):
        self.client.ensure_subscribed([211], scope="entry-watch")
        consumer = self.stream.registered[-1]
        self.stream.lows[(consumer, 211)] = 18.55
        self.assertEqual(self.client.drain_running_low(211), 18.55)
        self.assertIsNone(self.client.drain_running_low(211))
        self.client.reseed_running_low(211, 18.55)
        self.assertEqual(self.stream.reseeds, [(consumer, 211, 18.55)])

    def test_uic_resolution_is_cached_on_success_only(self):
        """Mirrors ``SaxoPriceStream.live_uic_for``: a miss is retried on the
        next tick rather than vetoed for the process lifetime."""
        self.assertEqual(self.client.live_uic_for("MRVI", exchange_mic="XNAS"), 211)
        self.assertEqual(self.client.live_uic_for("MRVI", exchange_mic="XNAS"), 211)
        self.assertIsNone(self.client.live_uic_for("NOPE", exchange_mic="XNYS"))
        self.assertIsNone(self.client.live_uic_for("NOPE", exchange_mic="XNYS"))
        self.assertEqual(
            self.stream.resolve_calls,
            [("MRVI", "XNAS"), ("NOPE", "XNYS"), ("NOPE", "XNYS")],
        )

    def test_one_connection_serves_many_calls(self):
        """The accumulator lives on the CONNECTION, so a client that
        reconnected per call would drain a fresh, always-empty window."""
        self.client.ensure_subscribed([211], scope="entry-watch")
        for _ in range(5):
            self.client.get(211)
        self.assertEqual(len(set(self.stream.registered)), 1)


class TestEveryFailureIsAVeto(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sock" / "reader.sock"

    def _client(self) -> RemoteQuoteSource:
        client = RemoteQuoteSource(self.path, timeout_s=1.0)
        self.addCleanup(client.close)
        return client

    def test_no_reader_running_vetoes_every_call(self):
        """The reader unit being down must look exactly like the OFF flag: no
        prices, no crash, the daemon keeps reconciling and keeps managing the
        standalone disaster stop."""
        client = self._client()
        self.assertIsNone(client.get(211))
        self.assertIsNone(client.drain_running_low(211))
        self.assertIsNone(client.live_uic_for("MRVI", exchange_mic="XNAS"))
        client.reseed_running_low(211, 1.0)  # must not raise
        client.ensure_subscribed([211], scope="exits")  # must not raise

    def test_a_reader_that_dies_mid_session_vetoes_rather_than_raises(self):
        stream = _FakeStream()
        stream.quotes[211] = _FakeQuote(211, 18.61, 18.62)
        server = PriceReaderServer(stream, self.path, heartbeat_interval_s=3600)
        server.start()
        client = self._client()
        self.assertIsNotNone(client.get(211))

        server.stop()
        self.assertIsNone(client.get(211))
        self.assertIsNone(client.drain_running_low(211))

    def test_a_server_error_response_vetoes(self):
        """An `ok:false` is distinct from `result:null` on the wire, and both
        have to arrive at the caller as 'no price' — but only the error may be
        counted as a failure."""
        stream = _FakeStream()

        def _boom(uic):
            raise RuntimeError("stream defect")

        stream.get = _boom  # type: ignore[method-assign]
        server = PriceReaderServer(stream, self.path, heartbeat_interval_s=3600)
        server.start()
        self.addCleanup(server.stop)
        client = self._client()
        self.assertIsNone(client.get(211))

    def test_a_wedged_reader_times_out_instead_of_stalling_the_tick(self):
        """A reader that accepts and never answers must not pin the daemon: the
        tick has a protection pass after it."""
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        listener.bind(str(self.path))
        listener.listen(1)
        self.addCleanup(listener.close)
        accepted: list[socket.socket] = []

        def _accept_and_ignore() -> None:
            conn, _ = listener.accept()
            accepted.append(conn)  # keep it open, never reply

        threading.Thread(target=_accept_and_ignore, daemon=True).start()

        client = self._client()
        started = dt.datetime.now(dt.UTC)
        self.assertIsNone(client.get(211))
        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
        self.assertLess(elapsed, 5.0, "the client must give up on its own timeout")
        for conn in accepted:
            conn.close()

    def test_an_endless_reply_is_bounded_rather_than_buffered_forever(self):
        """The server bounds REQUEST lines; the client must bound REPLIES. A
        reader that streams without ever sending a newline would otherwise grow
        the daemon's memory unbounded — the per-read timeout does not help,
        because every read succeeds."""
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        listener.bind(str(self.path))
        listener.listen(1)
        self.addCleanup(listener.close)

        def _flood() -> None:
            conn, _ = listener.accept()
            with contextlib.suppress(OSError):
                conn.recv(4096)
                while True:
                    conn.sendall(b"x" * 8192)  # never a newline

        threading.Thread(target=_flood, daemon=True).start()
        self.assertIsNone(self._client().get(211))

    def test_garbage_on_the_wire_vetoes(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        listener.bind(str(self.path))
        listener.listen(1)
        self.addCleanup(listener.close)

        def _reply_garbage() -> None:
            conn, _ = listener.accept()
            conn.recv(4096)
            conn.sendall(b"not json at all\n")
            conn.close()

        threading.Thread(target=_reply_garbage, daemon=True).start()
        self.assertIsNone(self._client().get(211))


class TestReconnect(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sock" / "reader.sock"
        self.stream = _FakeStream()

    def test_the_client_recovers_after_the_reader_restarts(self):
        """The reader is a unit an operator restarts. The daemon must recover
        on its own — an operator restarting the reader should not have to
        remember to restart two daemons as well."""
        server = PriceReaderServer(self.stream, self.path, heartbeat_interval_s=3600)
        server.start()
        client = RemoteQuoteSource(self.path, timeout_s=5.0, reconnect_backoff_s=0.0)
        self.addCleanup(client.close)
        self.stream.quotes[211] = _FakeQuote(211, 18.61, 18.62)
        self.assertIsNotNone(client.get(211))

        server.stop()
        self.assertIsNone(client.get(211))

        restarted = PriceReaderServer(self.stream, self.path, heartbeat_interval_s=3600)
        restarted.start()
        self.addCleanup(restarted.stop)
        self.assertIsNotNone(client.get(211), "the client must reconnect to the new reader")

    def test_a_reseed_that_lands_on_a_fresh_connection_cannot_become_a_phantom_touch(self):
        """The cross-PR interaction worth proving rather than assuming.

        A reconnect gives the client a NEW server-side consumer with an empty
        accumulator. A reseed issued right after (the client re-hands a low it
        drained before the drop) therefore lands on a consumer that is not yet
        watching that uic. PR-1's clear-on-ENTER rule is what stops that from
        waiting there and firing a touch the watch never saw, once the uic is
        subscribed."""
        from alphalens_pipeline.data.alt_data.saxo_price_stream import SaxoPriceStream

        class _Client:
            def access_token(self):  # pragma: no cover - not reached
                return "t"

        stream = SaxoPriceStream(object(), _Client())
        server = PriceReaderServer(stream, self.path, heartbeat_interval_s=3600)
        server.start()
        self.addCleanup(server.stop)
        client = RemoteQuoteSource(self.path, timeout_s=5.0, reconnect_backoff_s=0.0)
        self.addCleanup(client.close)

        # A low handed back BEFORE this connection watches the uic ...
        client.reseed_running_low(211, 18.61)
        # ... then the watch arms.
        client.ensure_subscribed([211], scope="entry-watch")
        self.assertIsNone(
            client.drain_running_low(211),
            "a low from before the watch existed must not survive into it",
        )

    def test_a_dead_connection_is_retried_within_the_call_at_production_defaults(self):
        """MEASURED as broken before this fix, and the earlier test missed it
        because it configured ``reconnect_backoff_s=0.0`` — it proved a shape
        we do not ship.

        With the production backoff, a failed exchange marked the client down
        immediately, so the in-call retry found itself inside the cooldown and
        returned nothing. A daemon whose reader restarted therefore stayed
        priceless for a full cooldown instead of recovering on the next call.
        A connection that WAS open and died is the case that deserves the
        retry; a fresh connect failure still enters the cooldown at once."""
        server = PriceReaderServer(self.stream, self.path, heartbeat_interval_s=3600)
        server.start()
        client = RemoteQuoteSource(self.path)  # production defaults
        self.addCleanup(client.close)
        self.stream.quotes[211] = _FakeQuote(211, 18.61, 18.62)
        self.assertIsNotNone(client.get(211))

        server.stop()
        restarted = PriceReaderServer(self.stream, self.path, heartbeat_interval_s=3600)
        restarted.start()
        self.addCleanup(restarted.stop)

        self.assertIsNotNone(
            client.get(211),
            "a call whose established connection died must reconnect within the call",
        )

    def test_a_fresh_connect_failure_still_enters_the_cooldown_at_once(self):
        """The other half: with no reader at all there is nothing to retry, so
        the call must not burn a second connect attempt."""
        client = RemoteQuoteSource(self.path)  # production defaults, no server
        self.addCleanup(client.close)
        self.assertIsNone(client.get(211))
        self.assertEqual(client.connect_attempts, 1)

    def test_reconnect_attempts_are_throttled(self):
        """~30 uics per tick against a down reader must not become 30 connect
        storms; the first failure marks it down for a cooldown."""
        client = RemoteQuoteSource(self.path, timeout_s=1.0, reconnect_backoff_s=60.0)
        self.addCleanup(client.close)
        self.assertIsNone(client.get(211))
        attempts_after_first = client.connect_attempts
        for _ in range(10):
            client.get(211)
        self.assertEqual(client.connect_attempts, attempts_after_first)


if __name__ == "__main__":
    unittest.main()
