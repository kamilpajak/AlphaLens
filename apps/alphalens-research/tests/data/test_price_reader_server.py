"""The cross-process price-reader server (#1172 PR-2).

Hermetic: a real UNIX socket in a tmpdir in front of a fake quote source. No
network, no Saxo, no threads left behind (each test stops the server).

What these tests are really pinning is the CONNECTION as the unit of identity:
one connection owns one latch accumulator and its own slice of the wire
subscription, and closing it releases both. Get that wrong and the two
broker-manager daemons either starve each other's touch evidence or leave
phantom subscriptions behind.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.data.alt_data import price_reader_server
from alphalens_pipeline.data.alt_data.price_reader_server import (
    PROTOCOL_VERSION,
    READER_STREAM_METRICS_JOB,
    SERVER_METRICS_JOB,
    PriceReaderAlreadyRunningError,
    PriceReaderServer,
)

_T0 = dt.datetime(2026, 8, 27, 14, 30, 0, tzinfo=dt.UTC)


class _FakeQuote:
    def __init__(self, uic: int, bid, ask, *, delayed: int | None = 0) -> None:
        self.uic = uic
        self.bid = bid
        self.ask = ask
        self.event_time = _T0
        self.delayed_by_minutes = delayed
        self.received_at = _T0


class _FakeStream:
    """A QuoteSource with the per-consumer latch surface, recording calls."""

    def __init__(self, quotes: dict[int, _FakeQuote] | None = None) -> None:
        self.quotes = quotes or {}
        self.subscribed: dict[str, set[int]] = {}
        self.latch_uics: dict[str, set[int]] = {}
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.lows: dict[tuple[str, int], float] = {}
        self.reseeds: list[tuple[str, int, float]] = []
        self.resolved: list[tuple[str, str]] = []
        self.raise_on_get = False

    # --- QuoteSource ---
    def get(self, uic: int):
        if self.raise_on_get:
            raise RuntimeError("boom")
        return self.quotes.get(uic)

    def drain_running_low(self, uic: int, *, consumer: str = "default"):
        return self.lows.pop((consumer, uic), None)

    def reseed_running_low(self, uic: int, low: float, *, consumer: str = "default") -> None:
        self.reseeds.append((consumer, uic, low))
        self.lows[(consumer, uic)] = low

    def live_uic_for(self, ticker: str, *, exchange_mic: str):
        self.resolved.append((ticker, exchange_mic))
        return 211 if ticker == "MRVI" else None

    def ensure_subscribed(self, uics, *, scope: str = "default") -> None:
        self.subscribed[scope] = set(uics)

    # --- latch consumer surface ---
    def register_latch_consumer(self, consumer: str) -> None:
        self.registered.append(consumer)

    def unregister_latch_consumer(self, consumer: str) -> None:
        self.unregistered.append(consumer)

    def set_latch_uics(self, consumer: str, uics) -> None:
        self.latch_uics[consumer] = set(uics)


class _Client:
    """Minimal line-protocol client — the PR-3 client in miniature."""

    def __init__(self, path: Path, *, timeout: float = 5.0) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(path))
        self._buf = b""

    def call(self, op: str, *, version: int = PROTOCOL_VERSION, **args):
        payload = {"v": version, "op": op, **args}
        self.send_raw(json.dumps(payload).encode() + b"\n")
        return self.read_response()

    def send_raw(self, raw: bytes) -> None:
        self._sock.sendall(raw)

    def read_response(self):
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise AssertionError("server closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def close(self) -> None:
        self._sock.close()


class PriceReaderServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sock" / "reader.sock"
        self.stream = _FakeStream()
        self.server = PriceReaderServer(self.stream, self.path)
        self.server.start()
        self.addCleanup(self.server.stop)

    def connect(self) -> _Client:
        client = _Client(self.path)
        self.addCleanup(client.close)
        return client


class TestQuoteOps(PriceReaderServerTestCase):
    def test_quote_round_trip_carries_every_field(self):
        self.stream.quotes[211] = _FakeQuote(211, 18.61, 18.62)
        reply = self.connect().call("quote", uic=211)
        self.assertTrue(reply["ok"])
        result = reply["result"]
        self.assertEqual(result["uic"], 211)
        self.assertEqual(result["bid"], 18.61)
        self.assertEqual(result["ask"], 18.62)
        self.assertEqual(result["delayed_by_minutes"], 0)
        self.assertEqual(result["event_time"], _T0.isoformat())
        self.assertEqual(result["received_at"], _T0.isoformat())

    def test_quote_for_an_uncached_uic_is_null_not_an_error(self):
        reply = self.connect().call("quote", uic=999)
        self.assertTrue(reply["ok"])
        self.assertIsNone(reply["result"])

    def test_a_half_blank_quote_passes_its_nulls_through(self):
        """The adapter's freshness gate is the layer that vetoes a missing
        side; the wire must not invent a number for it."""
        self.stream.quotes[211] = _FakeQuote(211, None, 18.62, delayed=None)
        result = self.connect().call("quote", uic=211)["result"]
        self.assertIsNone(result["bid"])
        self.assertIsNone(result["delayed_by_minutes"])

    def test_resolve_uic_delegates_to_the_stream(self):
        client = self.connect()
        self.assertEqual(
            client.call("resolve_uic", ticker="MRVI", exchange_mic="XNAS")["result"], 211
        )
        self.assertIsNone(client.call("resolve_uic", ticker="NOPE", exchange_mic="XNYS")["result"])
        self.assertEqual(self.stream.resolved, [("MRVI", "XNAS"), ("NOPE", "XNYS")])


class TestPerConnectionIsolation(PriceReaderServerTestCase):
    def test_two_connections_subscribe_under_distinct_wire_scopes(self):
        """SIM's 'exits' and LIVE's 'exits' must not overwrite each other — the
        wire set is the UNION across connections, not a last-writer-wins slot."""
        a, b = self.connect(), self.connect()
        a.call("subscribe", scope="exits", uics=[211])
        b.call("subscribe", scope="exits", uics=[212])
        self.assertEqual(len(self.stream.subscribed), 2)
        self.assertEqual(
            sorted(u for uics in self.stream.subscribed.values() for u in uics), [211, 212]
        )

    def test_subscribe_syncs_the_connections_latch_uic_set(self):
        """The latch set and the wire subscription MUST move together: a latch
        set narrower than the subscription accrues nothing for the missing
        uics, and touch detection goes dark with no error."""
        client = self.connect()
        client.call("subscribe", scope="exits", uics=[211])
        client.call("subscribe", scope="entry-watch", uics=[212, 213])
        consumer = self.stream.registered[-1]
        self.assertEqual(self.stream.latch_uics[consumer], {211, 212, 213})

    def test_a_scope_shrinking_shrinks_the_latch_set_too(self):
        client = self.connect()
        client.call("subscribe", scope="exits", uics=[211, 212])
        client.call("subscribe", scope="exits", uics=[211])
        consumer = self.stream.registered[-1]
        self.assertEqual(self.stream.latch_uics[consumer], {211})

    def test_drain_and_reseed_use_the_connections_own_consumer(self):
        a, b = self.connect(), self.connect()
        a.call("subscribe", scope="exits", uics=[211])
        b.call("subscribe", scope="exits", uics=[211])
        consumer_a, consumer_b = self.stream.registered[0], self.stream.registered[1]
        self.stream.lows[(consumer_a, 211)] = 18.61
        self.stream.lows[(consumer_b, 211)] = 18.55

        self.assertEqual(a.call("drain_low", uic=211)["result"], 18.61)
        self.assertEqual(b.call("drain_low", uic=211)["result"], 18.55)
        self.assertIsNone(a.call("drain_low", uic=211)["result"])

        a.call("reseed_low", uic=211, low=18.61)
        self.assertEqual(self.stream.reseeds, [(consumer_a, 211, 18.61)])

    def test_each_connection_gets_its_own_consumer_id(self):
        self.connect().call("hello", client="sim")
        self.connect().call("hello", client="sim")
        self.assertEqual(len(set(self.stream.registered)), 2)

    def test_closing_a_connection_releases_its_scopes_and_latch(self):
        """A crashed daemon must not leave a phantom subscription streaming
        uics nobody reads, nor an accumulator nobody drains."""
        client = _Client(self.path)
        client.call("subscribe", scope="exits", uics=[211])
        consumer = self.stream.registered[-1]
        client.close()
        self.server.wait_for_idle()
        self.assertEqual(self.stream.unregistered, [consumer])
        self.assertEqual(
            [uics for scope, uics in self.stream.subscribed.items() if scope.startswith(consumer)],
            [set()],
        )


class TestWireRobustness(PriceReaderServerTestCase):
    def test_a_non_finite_quote_side_is_nulled_rather_than_emitted_as_nan(self):
        """`QuoteCache` stores quote sides uncoerced, so a NaN can reach here.
        `json.dumps` would happily write bare `NaN`, which is not JSON — and a
        NaN that survives the wire is a number-shaped value the consumer's
        arithmetic would have to defend against. Null is the honest wire form
        for 'no usable price'."""
        self.stream.quotes[211] = _FakeQuote(211, float("nan"), float("inf"))
        raw = self.connect()
        raw.send_raw(
            json.dumps({"v": PROTOCOL_VERSION, "op": "quote", "uic": 211}).encode() + b"\n"
        )
        line = raw.read_response()
        self.assertIsNone(line["result"]["bid"])
        self.assertIsNone(line["result"]["ask"])

    def test_an_oversized_line_is_rejected_without_unbounded_buffering(self):
        """A client bug (or a corrupted frame) must not let one connection
        allocate without limit inside the reader both daemons depend on.

        The server answers once and drops THAT connection, so the oversized
        send can legitimately fail mid-write — which is the point: it stopped
        reading instead of buffering the rest. Any OSError counts: the errno
        depends on how far the drop got (EPIPE, ECONNRESET, or ENOTCONN once
        the server half-closes), and pinning one of them would make the test
        assert a platform detail rather than the behaviour."""
        client = self.connect()
        with contextlib.suppress(OSError):
            client.send_raw(b'{"v":1,"op":"quote","uic":' + b"9" * 200_000 + b"}\n")
        with contextlib.suppress(AssertionError, OSError):
            reply = client.read_response()
            self.assertFalse(reply["ok"])
            self.assertEqual(reply["error"], "bad_request")
        # Whatever the write raced to, the OTHER daemon's connection is fine.
        healthy = self.connect()
        self.assertTrue(healthy.call("hello", client="other")["ok"])

    def test_a_failed_subscribe_does_not_leave_the_scope_recorded(self):
        """If the stream call fails, the connection must not believe the scope
        is subscribed: its union would then diverge from the wire, and the
        release on close would hand back a set that was never taken."""
        client = self.connect()
        original = self.stream.ensure_subscribed

        def _boom(uics, *, scope="default"):
            raise RuntimeError("stream down")

        self.stream.ensure_subscribed = _boom
        self.assertEqual(client.call("subscribe", scope="exits", uics=[211])["error"], "internal")
        self.stream.ensure_subscribed = original
        client.call("subscribe", scope="exits", uics=[212])
        consumer = self.stream.registered[-1]
        self.assertEqual(self.stream.latch_uics[consumer], {212})


class TestIdleConnectionsSurvive(unittest.TestCase):
    """A client holds ONE connection open across ticks and is silent between
    them — the daemons poll every ~45 s.

    ``socket.settimeout`` bounds EVERY blocking call, reads included, so
    setting it on the accepted connection dropped any client that stayed quiet
    longer than the timeout. Measured on the VPS 2026-08-28: the SIM daemon had
    103 connect attempts (one per tick) instead of 1, the reader's journal was
    a stream of ``TimeoutError`` out of ``handle``'s ``readline``, and the
    ``clients`` gauge oscillated 0-2 so it said nothing about health. The write
    side still has to be bounded — a client that stops READING would otherwise
    pin a handler thread in ``sendall`` forever — so the bound moved to
    ``SO_SNDTIMEO``, which applies to sends only.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sock" / "reader.sock"
        self.stream = _FakeStream()
        # Short enough to keep the test fast, long enough that the round trip
        # below is not racing it. Read at call time by `setup()`.
        self._patch = mock.patch.object(price_reader_server, "_SEND_TIMEOUT_S", 0.3)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.server = PriceReaderServer(self.stream, self.path)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_a_client_silent_past_the_send_timeout_is_still_served(self):
        self.stream.quotes[211] = _FakeQuote(211, 18.61, 18.62)
        client = _Client(self.path)
        self.addCleanup(client.close)

        first = client.call("quote", uic=211)
        self.assertTrue(first["ok"])

        time.sleep(0.9)  # three times the bound, with no traffic at all

        second = client.call("quote", uic=211)
        self.assertTrue(
            second["ok"],
            "the reader dropped a connection that was merely idle — every "
            "daemon tick would then pay a reconnect",
        )
        self.assertEqual(second["result"]["bid"], 18.61)

    def test_the_send_bound_is_actually_installed_on_the_accepted_socket(self):
        """Pins the PRESENCE of the replacement, not only the absence of the
        old mechanism.

        Without this, deleting the setsockopt call would leave every other
        test green while a client that stopped reading could pin a handler
        thread forever — the same "no test can see it" shape that produced the
        defect this class fixes."""
        client = _Client(self.path)
        self.addCleanup(client.close)
        self.assertTrue(client.call("hello")["ok"])

        states = list(self.server._open.values())
        raw = states[0].sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, 32)
        seconds, microseconds = struct.unpack("ll", raw[: struct.calcsize("ll")])
        self.assertAlmostEqual(seconds + microseconds / 1_000_000, 0.3, places=6)

    def test_the_connection_is_not_left_in_timeout_mode(self):
        """Guards the mechanism, not just the symptom: a non-None
        ``gettimeout()`` on the accepted socket means some later edit put the
        blanket timeout back and idle reads are bounded again."""
        client = _Client(self.path)
        self.addCleanup(client.close)
        self.assertTrue(client.call("hello")["ok"])

        states = list(self.server._open.values())
        self.assertEqual(len(states), 1)
        self.assertIsNone(
            states[0].sock.gettimeout(),
            "the accepted socket must stay in blocking mode; bound the write "
            "side with SO_SNDTIMEO instead",
        )


class TestWriteTimeoutIsTreatedAsAVanishedClient(unittest.TestCase):
    """The write bound changed exception TYPE, and that had to be checked.

    ``settimeout`` raises ``TimeoutError``; ``SO_SNDTIMEO`` raises
    ``BlockingIOError`` (EAGAIN) instead — verified by filling a socketpair
    whose peer never reads. Both are ``OSError`` subclasses, so ``_reply``'s
    handler covers them, but nothing pinned that, and a write bound that
    escapes ``_reply`` would kill the handler thread mid-protocol instead of
    releasing the connection cleanly.
    """

    def _reply_result(self, error: Exception) -> bool:
        handler = object.__new__(price_reader_server._Handler)

        class _RaisingFile:
            def write(self, _data):
                raise error

            def flush(self):
                raise error

        handler.wfile = _RaisingFile()
        return handler._reply({"ok": True})

    def test_a_send_timeout_reports_the_client_as_gone(self):
        for error in (
            BlockingIOError(35, "Resource temporarily unavailable"),
            TimeoutError("timed out"),
            BrokenPipeError(32, "Broken pipe"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(
                    self._reply_result(error),
                    "a failed write must release the connection, never escape",
                )


class TestProtocolErrors(PriceReaderServerTestCase):
    def test_an_unknown_op_errors_without_dropping_the_connection(self):
        client = self.connect()
        reply = client.call("teleport", uic=1)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "unknown_op")
        self.stream.quotes[211] = _FakeQuote(211, 1.0, 1.1)
        self.assertTrue(client.call("quote", uic=211)["ok"])

    def test_a_wrong_protocol_version_is_refused(self):
        reply = self.connect().call("quote", uic=211, version=PROTOCOL_VERSION + 1)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "bad_version")

    def test_a_malformed_line_errors_without_dropping_the_connection(self):
        client = self.connect()
        client.send_raw(b"not json\n")
        reply = client.read_response()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "bad_request")
        self.stream.quotes[211] = _FakeQuote(211, 1.0, 1.1)
        self.assertTrue(client.call("quote", uic=211)["ok"])

    def test_missing_or_non_numeric_args_error_rather_than_crash(self):
        client = self.connect()
        self.assertEqual(client.call("quote")["error"], "bad_request")
        self.assertEqual(client.call("quote", uic="oops")["error"], "bad_request")
        self.assertEqual(
            client.call("subscribe", scope="exits", uics="all")["error"], "bad_request"
        )

    def test_bad_float_and_string_fields_error_rather_than_crash(self):
        client = self.connect()
        self.assertEqual(client.call("reseed_low", uic=211, low="oops")["error"], "bad_request")
        self.assertEqual(
            client.call("resolve_uic", ticker=123, exchange_mic="XNYS")["error"], "bad_request"
        )

    def test_an_exception_inside_the_stream_becomes_an_error_response(self):
        """The reader must survive a defect in the quote source: one client's
        failing call cannot take the process (and the other daemon) down."""
        client = self.connect()
        self.stream.raise_on_get = True
        reply = client.call("quote", uic=211)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "internal")
        self.stream.raise_on_get = False
        self.assertTrue(client.call("quote", uic=211)["ok"])


class TestMetrics(unittest.TestCase):
    """The reader is a new always-on unit: it needs its own liveness signal,
    and it must not clobber anyone else's textfile."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sock" / "reader.sock"
        self.emitted: list[tuple[str, dict[str, float]]] = []
        self.stream = _FakeStream()
        self.server = PriceReaderServer(
            self.stream,
            self.path,
            emit=lambda job, metrics: self.emitted.append((job, dict(metrics))),
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_gauges_report_up_and_the_live_client_count(self):
        client = _Client(self.path)
        self.addCleanup(client.close)
        client.call("hello", client="sim")
        self.server.emit_gauges()
        job, metrics = self.emitted[-1]
        self.assertEqual(job, SERVER_METRICS_JOB)
        self.assertEqual(metrics[f'alphalens_price_reader_up{{job="{SERVER_METRICS_JOB}"}}'], 1)
        self.assertEqual(
            metrics[f'alphalens_price_reader_clients{{job="{SERVER_METRICS_JOB}"}}'], 1
        )

    def test_stopping_emits_a_down_gauge(self):
        """`up=0` with the unit gone is the signature the alert pages on; a
        reader that just stops emitting looks identical to a healthy quiet
        one until the staleness rule fires much later."""
        self.server.stop()
        job, metrics = self.emitted[-1]
        self.assertEqual(job, SERVER_METRICS_JOB)
        self.assertEqual(metrics[f'alphalens_price_reader_up{{job="{SERVER_METRICS_JOB}"}}'], 0)

    def test_a_failing_emitter_never_breaks_a_request(self):
        """Same doctrine as the daemon heartbeat: a textfile-dir hiccup is not
        allowed to take prices down."""
        self.server._emit = lambda job, metrics: (_ for _ in ()).throw(OSError("no dir"))
        self.server.emit_gauges()  # must not raise
        client = _Client(self.path)
        self.addCleanup(client.close)
        self.assertTrue(client.call("hello", client="sim")["ok"])


class TestMetricsJobsDoNotCollide(unittest.TestCase):
    def test_the_reader_jobs_differ_from_every_per_env_price_stream_job(self):
        """`emit_domain_metrics` rewrites a whole per-job file, so two
        emitters sharing a job silently erase each other's series."""
        from alphalens_pipeline.brokers.automanager import state_paths

        jobs = {
            SERVER_METRICS_JOB,
            READER_STREAM_METRICS_JOB,
            state_paths.price_stream_metrics_job("sim"),
            state_paths.price_stream_metrics_job("live"),
        }
        self.assertEqual(len(jobs), 4)


class TestSocketLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sock" / "reader.sock"

    def test_the_socket_is_owner_only_and_its_directory_too(self):
        server = PriceReaderServer(_FakeStream(), self.path)
        server.start()
        self.addCleanup(server.stop)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)

    def test_a_stale_socket_file_is_replaced_on_start(self):
        """A hard kill leaves the socket file behind; bind() would fail with
        EADDRINUSE and the reader would never come back after a reboot."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")
        server = PriceReaderServer(_FakeStream(), self.path)
        server.start()
        self.addCleanup(server.stop)
        client = _Client(self.path)
        self.addCleanup(client.close)
        self.assertTrue(client.call("hello", client="t")["ok"])

    def test_stop_unlinks_the_socket(self):
        server = PriceReaderServer(_FakeStream(), self.path)
        server.start()
        server.stop()
        self.assertFalse(self.path.exists())

    def test_a_second_reader_refuses_to_take_over_a_live_socket(self):
        """MEASURED before this guard existed: the second reader unlinked the
        path, bound its own socket and served the next client — so two readers
        held two elevated Saxo sessions, which is the exact demotion this whole
        design exists to prevent. Unlinking removes the directory entry, not
        the first process's listening socket, so 'it bound fine' is not
        evidence that nobody else is serving."""
        first = PriceReaderServer(_FakeStream(), self.path)
        first.start()
        self.addCleanup(first.stop)
        second = PriceReaderServer(_FakeStream(), self.path)
        with self.assertRaises(PriceReaderAlreadyRunningError):
            second.start()
        # ... and the first one still owns the socket.
        client = _Client(self.path)
        self.addCleanup(client.close)
        self.assertTrue(client.call("hello", client="probe")["ok"])

    def test_a_stale_socket_left_by_a_dead_reader_is_replaced(self):
        """The mirror case: nothing is listening, so the leftover file must NOT
        block a restart (a hard kill leaves one behind)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")
        server = PriceReaderServer(_FakeStream(), self.path)
        server.start()
        self.addCleanup(server.stop)
        client = _Client(self.path)
        self.addCleanup(client.close)
        self.assertTrue(client.call("hello", client="probe")["ok"])

    def test_a_stop_requested_before_serving_is_honoured(self):
        """MEASURED before the durable flag existed: a SIGTERM arriving between
        installing the handler and binding was silently lost, and the unit then
        served forever — systemd would report a stop that never happened."""
        server = PriceReaderServer(_FakeStream(), self.path, heartbeat_interval_s=3600)
        server.request_stop()
        done = threading.Event()
        threading.Thread(target=lambda: (server.serve_forever(), done.set()), daemon=True).start()
        self.assertTrue(done.wait(5.0), "serve_forever ignored a stop requested before it started")


if __name__ == "__main__":
    unittest.main()
