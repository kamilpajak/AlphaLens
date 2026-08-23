"""Hermetic tests for the Saxo SIM streaming client.

Everything the reader DECIDES lives in pure / synchronous helpers so the whole
state machine is testable without a socket or an event loop:

- ``parse_stream_frames`` — the pure binary-envelope parser (confirmed protocol,
  design memo ``saxo_streaming_design_2026_07_24.md`` §"CONFIRMED PROTOCOL");
- ``_route_message`` / ``_dispatch_frame`` — dumb routing (data -> trigger,
  ``_heartbeat`` -> liveness, ``_resetsubscriptions`` -> delete+recreate+snapshot,
  ``_disconnect`` -> invalidate+reconnect);
- ``_build_connect_url`` / ``_compute_backoff`` / ``_register_failure`` /
  ``_maybe_reauthorize`` — reconnect + circuit-breaker + token re-auth.

The async I/O glue (``_supervise`` / recv loop / real ``websockets`` connect) is
exercised by the opt-in ``tests/live/test_saxo_stream_live.py`` probe, not here.
"""

from __future__ import annotations

import logging
import unittest
from typing import Any

from alphalens_pipeline.brokers.saxo.streaming import (
    SIM_STREAMING_BASE_URL,
    SaxoStreamError,
    SaxoStreamingClient,
    SaxoStreamProtocolError,
    StreamAction,
    StreamMessage,
    StreamTuning,
    parse_stream_frames,
)

# ---------------------------------------------------------------------------
# Frame builder — the EXACT confirmed envelope, so the parser is tested against
# the real byte layout, not a paraphrase of it.
# ---------------------------------------------------------------------------


def build_message(
    message_id: int,
    reference_id: str,
    payload: bytes = b"{}",
    *,
    fmt: int = 0,
) -> bytes:
    ref = reference_id.encode("ascii")
    return (
        message_id.to_bytes(8, "little")
        + b"\x00\x00"  # bytes[8:10] reserved
        + bytes([len(ref)])
        + ref
        + bytes([fmt])  # 0 = JSON, 1 = protobuf (rejected)
        + len(payload).to_bytes(4, "little")
        + payload
    )


class TestStreamFrameParser(unittest.TestCase):
    def test_single_message_round_trips(self):
        buf = build_message(7, "pos", b'{"PositionId":"P-1"}')
        (msg,) = parse_stream_frames(buf)
        self.assertEqual(msg.message_id, 7)
        self.assertEqual(msg.reference_id, "pos")
        self.assertEqual(msg.payload, b'{"PositionId":"P-1"}')

    def test_multi_message_frame_unpacked_sequentially(self):
        buf = (
            build_message(1, "pos", b'{"a":1}')
            + build_message(2, "ord", b'{"b":2}')
            + build_message(3, "_heartbeat", b"[]")
        )
        msgs = parse_stream_frames(buf)
        self.assertEqual([m.message_id for m in msgs], [1, 2, 3])
        self.assertEqual([m.reference_id for m in msgs], ["pos", "ord", "_heartbeat"])
        self.assertEqual(msgs[1].payload, b'{"b":2}')

    def test_json_format_asserted_protobuf_byte_rejected(self):
        buf = build_message(1, "pos", b"\x00\x01", fmt=1)
        with self.assertRaises(SaxoStreamProtocolError):
            parse_stream_frames(buf)

    def test_reference_id_routes_data_vs_control(self):
        data = parse_stream_frames(build_message(1, "pos"))[0]
        control = parse_stream_frames(build_message(2, "_heartbeat"))[0]
        self.assertFalse(data.reference_id.startswith("_"))
        self.assertTrue(control.reference_id.startswith("_"))

    def test_truncated_buffer_raises_not_partial_route(self):
        full = build_message(1, "pos", b'{"big":"payload"}')
        # Chop the last byte of the payload: a partial message must RAISE, never
        # silently drop / route a half-decoded frame.
        with self.assertRaises(SaxoStreamProtocolError):
            parse_stream_frames(full[:-1])

    def test_truncated_header_raises(self):
        with self.assertRaises(SaxoStreamProtocolError):
            parse_stream_frames(b"\x01\x02\x03")

    def test_empty_buffer_is_empty_list(self):
        self.assertEqual(parse_stream_frames(b""), [])

    def test_message_id_retained_for_reconnect_replay(self):
        buf = build_message(4242, "pos")
        (msg,) = parse_stream_frames(buf)
        self.assertEqual(msg.message_id, 4242)

    def test_message_id_is_u64_little_endian(self):
        # 0x0102 stored LE -> first byte 0x02, second 0x01.
        buf = build_message(0x0102, "pos")
        self.assertEqual(buf[0], 0x02)
        self.assertEqual(buf[1], 0x01)
        self.assertEqual(parse_stream_frames(buf)[0].message_id, 0x0102)


# ---------------------------------------------------------------------------
# Fakes for the client (no socket, no event loop).
# ---------------------------------------------------------------------------


class _SpyTokenProvider:
    def __init__(self, token: str = "seed"):
        self._token = token
        self.get_calls = 0
        self.invalidations = 0

    def get_access_token(self) -> str:
        self.get_calls += 1
        return self._token

    def invalidate(self) -> None:
        self.invalidations += 1


class _FakeSubscriber:
    """Stand-in for the shared SaxoClient — records subscription REST."""

    def __init__(self, client_key: str = "CK-1"):
        self._client_key = client_key
        self.created: list[tuple[str, str]] = []  # (kind, reference_id)
        self.deleted: list[str] = []

    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": self._client_key}

    def create_positions_subscription(
        self, *, context_id: str, reference_id: str, client_key: str
    ) -> tuple[int, dict[str, Any]]:
        self.created.append(("positions", reference_id))
        return 201, {"Snapshot": {"Data": []}}

    def create_orders_subscription(
        self, *, context_id: str, reference_id: str, client_key: str
    ) -> tuple[int, dict[str, Any]]:
        self.created.append(("orders", reference_id))
        return 201, {"Snapshot": {"Data": []}}

    def delete_all_subscriptions(self, context_id: str) -> list[tuple[int, dict[str, Any]]]:
        self.deleted.append(context_id)
        return [(202, {}), (202, {})]


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _RecordingSession:
    def __init__(self, status_code: int = 202):
        self._status_code = status_code
        self.puts: list[dict[str, Any]] = []

    def put(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.puts.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        return _FakeResponse(self._status_code)


_CTX = "almgr-123-456"
_NEW_CTX = "almgr-123-789"


class _FakeClock:
    """Hand-advanced monotonic clock so the delivery-life gate is deterministic."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class _FakeThread:
    """Records thread lifecycle WITHOUT running the target — spawn/rearm tests
    must never run the real reader loop (a bare ``start()`` on the real loop
    would dial ``sim-streaming.saxobank.com``)."""

    def __init__(self, target: Any = None):
        self.target = target
        self.start_calls = 0
        self.alive = False

    def start(self) -> None:
        self.start_calls += 1
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.alive = False


class _RecordingThreadFactory:
    """thread_factory seam: hands out ``_FakeThread``s and can raise once, to
    script a ``Thread.start()``-style spawn failure (thread exhaustion)."""

    def __init__(self):
        self.threads: list[_FakeThread] = []
        self.raise_next = False

    def __call__(
        self, *, target: Any, name: str | None = None, daemon: bool | None = None
    ) -> _FakeThread:
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("can't start new thread")
        thread = _FakeThread(target=target)
        self.threads.append(thread)
        return thread


def _make_client(
    *,
    token_provider: Any = None,
    subscriber: Any = None,
    session: Any = None,
    max_consecutive_failures: int = 6,
    monotonic: Any = None,
    thread_factory: Any = None,
    alert: Any = None,
    ws_connect: Any = None,
    async_sleep: Any = None,
) -> tuple[SaxoStreamingClient, dict[str, list[Any]]]:
    events: dict[str, list[Any]] = {"trigger": [], "heartbeat": []}
    extra: dict[str, Any] = {}
    if monotonic is not None:
        extra["monotonic"] = monotonic
    if thread_factory is not None:
        extra["thread_factory"] = thread_factory
    if alert is not None:
        extra["alert"] = alert
    if ws_connect is not None:
        extra["ws_connect"] = ws_connect
    if async_sleep is not None:
        extra["async_sleep"] = async_sleep
    client = SaxoStreamingClient(
        token_provider or _SpyTokenProvider(),
        subscriber or _FakeSubscriber(),
        context_id=_CTX,
        on_trigger=lambda: events["trigger"].append(True),
        on_heartbeat=events["heartbeat"].append,
        session=session or _RecordingSession(),
        tuning=StreamTuning(max_consecutive_failures=max_consecutive_failures),
        **extra,
    )
    return client, events


class TestStreamTuningWiring(unittest.TestCase):
    """The StreamTuning policy object threads into the client's internal knobs
    (defaults used everywhere except stale_after_s, which StreamTrigger passes)."""

    def test_defaults_match_the_documented_sim_policy(self):
        client, _ = _make_client()
        self.assertEqual(client._stale_after_s, 45.0)
        self.assertEqual(client._recv_timeout_s, 30.0)
        self.assertEqual(client._max_consecutive_failures, 6)
        self.assertEqual(client._backoff_floor_s, 1.0)
        self.assertEqual(client._backoff_ceiling_s, 30.0)

    def test_custom_tuning_overrides_flow_to_the_client(self):
        client = SaxoStreamingClient(
            _SpyTokenProvider(),
            _FakeSubscriber(),
            context_id=_CTX,
            on_trigger=lambda: None,
            on_heartbeat=lambda ts: None,
            session=_RecordingSession(),
            tuning=StreamTuning(
                stale_after_s=12.0,
                recv_timeout_s=7.0,
                max_consecutive_failures=3,
                backoff_floor_s=0.5,
                backoff_ceiling_s=8.0,
            ),
        )
        self.assertEqual(client._stale_after_s, 12.0)
        self.assertEqual(client._recv_timeout_s, 7.0)
        self.assertEqual(client._max_consecutive_failures, 3)
        self.assertEqual(client._backoff_floor_s, 0.5)
        self.assertEqual(client._backoff_ceiling_s, 8.0)

    def test_min_connection_life_default_and_override_reach_the_client(self):
        # Default: 10x SaxoClient._MIN_REQUEST_INTERVAL_S (0.5s) — a gateway that
        # cannot keep a connection alive for 5s cannot clear the failure streak.
        client, _ = _make_client()
        self.assertEqual(client._min_connection_life_s, 5.0)
        override = SaxoStreamingClient(
            _SpyTokenProvider(),
            _FakeSubscriber(),
            context_id=_CTX,
            on_trigger=lambda: None,
            on_heartbeat=lambda ts: None,
            session=_RecordingSession(),
            tuning=StreamTuning(min_connection_life_s=9.0),
        )
        self.assertEqual(override._min_connection_life_s, 9.0)


class TestStreamControlMessages(unittest.TestCase):
    def test_heartbeat_stamps_liveness_without_trigger(self):
        client, events = _make_client()
        action = client._route_message(StreamMessage(1, "_heartbeat", b"[]"))
        self.assertIs(action, StreamAction.CONTINUE)
        self.assertEqual(len(events["heartbeat"]), 1)
        self.assertEqual(events["trigger"], [])

    def test_data_message_fires_trigger(self):
        client, events = _make_client()
        action = client._route_message(StreamMessage(1, "pos", b"{}"))
        self.assertIs(action, StreamAction.CONTINUE)
        self.assertEqual(len(events["trigger"]), 1)

    def test_order_data_message_fires_trigger(self):
        client, events = _make_client()
        client._route_message(StreamMessage(1, "ord", b"{}"))
        self.assertEqual(len(events["trigger"]), 1)

    def test_resetsubscriptions_forces_delete_recreate_snapshot_trigger(self):
        sub = _FakeSubscriber()
        client, events = _make_client(subscriber=sub)
        # First subscribe (generation 0) so the reset's new refs are observably
        # different from the originals.
        client._subscribe(delete_first=False)
        first_refs = list(sub.created)
        events["trigger"].clear()

        action = client._route_message(StreamMessage(9, "_resetsubscriptions", b"[]"))

        self.assertIs(action, StreamAction.CONTINUE)
        self.assertEqual(sub.deleted, [_CTX])  # DELETE all subs on the context
        new_refs = sub.created[len(first_refs) :]
        self.assertEqual(len(new_refs), 2)
        # NEW ReferenceIds — a bare socket reconnect is insufficient.
        self.assertNotEqual(
            {r for _, r in new_refs},
            {r for _, r in first_refs},
        )
        self.assertEqual(len(events["trigger"]), 1)  # fresh snapshot self-reconcile

    def test_disconnect_invalidates_token_and_reconnects(self):
        provider = _SpyTokenProvider()
        client, _ = _make_client(token_provider=provider)
        action = client._route_message(StreamMessage(3, "_disconnect", b"[]"))
        self.assertIs(action, StreamAction.RECONNECT)
        self.assertEqual(provider.invalidations, 1)

    def test_unknown_control_message_is_liveness_only(self):
        client, events = _make_client()
        action = client._route_message(StreamMessage(1, "_somethingnew", b"[]"))
        self.assertIs(action, StreamAction.CONTINUE)
        self.assertEqual(events["trigger"], [])

    def test_dispatch_frame_retains_last_message_id(self):
        client, _ = _make_client()
        buf = build_message(11, "pos") + build_message(22, "ord")
        client._dispatch_frame(buf)
        self.assertEqual(client._last_message_id, 22)

    def test_dispatch_frame_aggregates_reconnect_action(self):
        client, _ = _make_client()
        buf = build_message(1, "pos") + build_message(2, "_disconnect")
        action = client._dispatch_frame(buf)
        self.assertIs(action, StreamAction.RECONNECT)


class TestStreamSubscribe(unittest.TestCase):
    def test_first_subscribe_uses_base_refs_and_triggers(self):
        sub = _FakeSubscriber()
        client, events = _make_client(subscriber=sub)
        client._subscribe(delete_first=False)
        self.assertEqual(sub.created, [("positions", "pos"), ("orders", "ord")])
        self.assertEqual(sub.deleted, [])
        self.assertEqual(len(events["trigger"]), 1)

    def test_resubscribe_deletes_first_and_triggers(self):
        sub = _FakeSubscriber()
        client, events = _make_client(subscriber=sub)
        client._subscribe(delete_first=True)
        self.assertEqual(sub.deleted, [_CTX])
        self.assertEqual(len(events["trigger"]), 1)


class TestStreamReconnect(unittest.TestCase):
    def test_connect_url_carries_context_id_without_message_id_on_first_connect(self):
        client, _ = _make_client()
        url = client._build_connect_url(None)
        self.assertEqual(url, f"{SIM_STREAMING_BASE_URL}?contextId={_CTX}")

    def test_reconnect_uses_same_context_id_and_last_message_id(self):
        client, _ = _make_client()
        client._last_message_id = 987
        url = client._build_connect_url(client._last_message_id)
        self.assertIn(f"contextId={_CTX}", url)
        self.assertIn("&messageid=987", url)

    def test_every_reconnect_ends_in_snapshot_trigger(self):
        # _subscribe (called on every (re)connect) always ends in on_trigger.
        sub = _FakeSubscriber()
        client, events = _make_client(subscriber=sub)
        client._subscribe(delete_first=True)
        self.assertEqual(len(events["trigger"]), 1)


class TestStreamCircuitBreaker(unittest.TestCase):
    def test_backoff_capped_1s_to_30s(self):
        client, _ = _make_client()
        self.assertEqual(client._compute_backoff(1), 1.0)
        self.assertEqual(client._compute_backoff(2), 2.0)
        self.assertEqual(client._compute_backoff(3), 4.0)
        self.assertEqual(client._compute_backoff(4), 8.0)
        self.assertEqual(client._compute_backoff(5), 16.0)
        # Capped at 30s from here on.
        self.assertEqual(client._compute_backoff(6), 30.0)
        self.assertEqual(client._compute_backoff(50), 30.0)

    def test_consecutive_failures_shut_stream_to_poll_only_and_alert_once(self):
        alerts: list[str] = []
        sub = _FakeSubscriber()
        client = SaxoStreamingClient(
            _SpyTokenProvider(),
            sub,
            context_id=_CTX,
            on_trigger=lambda: None,
            on_heartbeat=lambda ts: None,
            session=_RecordingSession(),
            tuning=StreamTuning(max_consecutive_failures=6),
            alert=alerts.append,
            # rearm() below must spawn a fake thread, never the real loop.
            thread_factory=_RecordingThreadFactory(),
        )
        self.assertTrue(client.is_streaming)
        tripped = [client._register_failure() for _ in range(6)]
        # Only the 6th failure trips the breaker.
        self.assertEqual(tripped, [False, False, False, False, False, True])
        self.assertFalse(client.is_streaming)
        self.assertEqual(len(alerts), 1)
        # Breaker teardown DELETEs subscriptions so the streaming REST stops.
        self.assertEqual(sub.deleted, [_CTX])
        # Alert-once is PER EPISODE, no longer per process (rearm design memo
        # 2026-08-22 §4.5 — the record of the decision that consciously
        # contradicts the old "a further failure never re-alerts" permanence
        # pin). Within the episode further failures stay silent...
        client._register_failure()
        self.assertEqual(len(alerts), 1)
        # ...and rearm() clears the latch, so the NEXT episode's trip logs again
        # (one journald line per episode instead of one per process life).
        client.push_token("bearer")
        self.assertTrue(client.rearm(_NEW_CTX))
        client._register_failure()
        self.assertEqual(len(alerts), 2)

    def test_successful_connect_resets_failure_streak(self):
        client, _ = _make_client()
        client._register_failure()
        client._register_failure()
        self.assertEqual(client._consecutive_failures, 2)
        client._reset_failures()
        self.assertEqual(client._consecutive_failures, 0)


class TestStreamReconnectStormDiscipline(unittest.TestCase):
    """The RECONNECT class (socket EOF, ``_disconnect``, stale-timeout, reauth
    non-202) must be counted, backed off, and breaker-tripped exactly like a
    connect/subscribe exception — otherwise a connect-then-drop gateway becomes
    an unthrottled reconnect+resubscribe storm through the shared SaxoClient
    (adversary-2 fix #2). The failure streak resets ONLY on evidence the
    connection is actually delivering a frame, never on a mere subscribe
    dispatch."""

    def test_undelivering_reconnect_storm_trips_breaker_with_backoff(self):
        # Every connection ends WITHOUT delivering a frame (e.g. an immediate
        # ``_disconnect``). Each end is planned as a reconnect step: it must
        # count toward the streak, hand back a positive backoff, and trip the
        # breaker on the Nth — never spin at full speed.
        client, _ = _make_client(max_consecutive_failures=6)
        backoffs: list[float] = []
        trips = 0
        for _ in range(6):
            step = client._plan_reconnect_step()
            if step.give_up:
                trips += 1
                break
            self.assertGreater(step.backoff_s, 0.0)
            backoffs.append(step.backoff_s)
        self.assertEqual(trips, 1)
        self.assertFalse(client.is_streaming)
        # Exponential backoff was applied between every reconnect (never zero).
        self.assertEqual(backoffs, [1.0, 2.0, 4.0, 8.0, 16.0])

    def test_delivery_resets_streak_so_healthy_drop_backs_off_minimally(self):
        # A connection that delivered a real frame (heartbeat) then dropped is
        # NOT a storm: the streak resets on delivery, so the next reconnect step
        # uses the floor backoff, not a grown one.
        client, _ = _make_client()
        for _ in range(4):
            client._plan_reconnect_step()
        self.assertEqual(client._consecutive_failures, 4)
        client._route_message(StreamMessage(1, "_heartbeat", b"[]"))
        self.assertEqual(client._consecutive_failures, 0)
        step = client._plan_reconnect_step()
        self.assertFalse(step.give_up)
        self.assertEqual(step.backoff_s, 1.0)

    def test_data_frame_also_resets_streak(self):
        client, _ = _make_client()
        for _ in range(3):
            client._plan_reconnect_step()
        client._route_message(StreamMessage(1, "pos", b"{}"))
        self.assertEqual(client._consecutive_failures, 0)

    def test_subscribe_dispatch_alone_does_not_reset_streak(self):
        # The exact hole finding #1 flagged: a successful subscribe is NOT proof
        # the connection delivers, so it must NOT clear the streak — otherwise a
        # connect -> subscribe -> immediate ``_disconnect`` loop never trips.
        sub = _FakeSubscriber()
        client, _ = _make_client(subscriber=sub)
        client._register_failure()
        client._register_failure()
        client._subscribe(delete_first=False)
        self.assertEqual(client._consecutive_failures, 2)

    def test_disconnect_control_frame_does_not_reset_streak(self):
        client, _ = _make_client()
        client._register_failure()
        client._register_failure()
        client._route_message(StreamMessage(1, "_disconnect", b"[]"))
        self.assertEqual(client._consecutive_failures, 2)

    def test_plan_reconnect_step_reports_breaker_trip(self):
        client, _ = _make_client(max_consecutive_failures=2)
        first = client._plan_reconnect_step()
        self.assertFalse(first.give_up)
        second = client._plan_reconnect_step()
        self.assertTrue(second.give_up)
        self.assertFalse(client.is_streaming)


class TestStreamStartupTokenRace(unittest.TestCase):
    """The reader thread is spawned before the main loop pushes the first bearer
    token, so its first connect attempts can find ``_current_token is None`` and
    raise "no bearer token pushed before connect". That is "not ready yet", NOT a
    connection failure: waiting for the token must NEVER count toward the breaker.
    Otherwise a slow first tick (e.g. a Saxo 503 on the initial reconcile) burns
    the whole 6-attempt budget and shuts streaming to poll-only for the entire
    session — the exact 2026-07-27 incident. ``_current_token`` is only ever set
    (never re-cleared to None) once the main loop pushes, so ``token_missing`` is
    an exact, self-limiting flag for the pre-first-token startup window."""

    def test_missing_token_wait_never_counts_toward_breaker(self):
        client, _ = _make_client(max_consecutive_failures=6)
        self.assertIsNone(client._current_token)  # no token pushed yet
        for _ in range(20):  # far past the 6-failure breaker budget
            step = client._plan_reconnect_step(token_missing=True)
            self.assertFalse(step.give_up)
            self.assertEqual(step.backoff_s, client._backoff_floor_s)
        self.assertEqual(client._consecutive_failures, 0)
        self.assertTrue(client.is_streaming)  # breaker never tripped

    def test_real_failures_still_count_once_token_present(self):
        # The exemption is scoped to the tokenless startup window: once the token
        # is present a genuine connection failure counts and still trips the Nth.
        client, _ = _make_client(max_consecutive_failures=6)
        client.push_token("bearer")
        trips = 0
        for _ in range(6):
            step = client._plan_reconnect_step(token_missing=False)
            if step.give_up:
                trips += 1
                break
        self.assertEqual(trips, 1)
        self.assertFalse(client.is_streaming)

    def test_tokenless_waits_do_not_erode_a_later_real_failure_budget(self):
        # A stretch of tokenless waits must leave the full failure budget intact
        # for real failures that come after the token finally arrives.
        client, _ = _make_client(max_consecutive_failures=6)
        for _ in range(10):
            client._plan_reconnect_step(token_missing=True)
        client.push_token("bearer")
        self.assertEqual(client._consecutive_failures, 0)
        trips = [client._plan_reconnect_step().give_up for _ in range(6)]
        self.assertEqual(trips, [False, False, False, False, False, True])

    def test_startup_window_session_failure_logs_debug_not_warning(self):
        # Same startup window, now on the LOG side. Before the main loop pushes
        # the first bearer (_current_token is None) a failed connect is the
        # expected "not ready yet" wait — the reader is spawned before the first
        # tick and that tick can lag ~15s behind a full reconcile. It must log
        # at DEBUG, not WARNING: at 1/s it reads as a crisis but is the exact
        # self-healing wait #918 already exempts from the breaker (live 2026-08-02:
        # 16 WARNING/s for 16s, then a silent, healthy stream once the token landed).
        client, _ = _make_client()
        self.assertIsNone(client._current_token)  # startup window
        exc = SaxoStreamError("no bearer token pushed before connect")
        with self.assertLogs(
            "alphalens_pipeline.brokers.saxo.streaming", level="DEBUG"
        ) as captured:
            client._log_session_failure(exc)
        self.assertTrue(
            any("no bearer token" in r.getMessage() for r in captured.records),
            "the deferred-connect wait must still be logged (troubleshooting)",
        )
        self.assertTrue(
            all(r.levelno < logging.WARNING for r in captured.records),
            "startup-window connect wait must not log at WARNING or above",
        )

    def test_session_failure_after_token_present_logs_warning(self):
        # Once a token has been pushed (_current_token set-once), any connection
        # that ends is a genuine session failure -> WARNING, unchanged.
        client, _ = _make_client()
        client.push_token("bearer")
        exc = SaxoStreamError("websocket closed unexpectedly")
        with self.assertLogs(
            "alphalens_pipeline.brokers.saxo.streaming", level="WARNING"
        ) as captured:
            client._log_session_failure(exc)
        self.assertTrue(
            any("websocket closed" in r.getMessage() for r in captured.records),
            "a real post-startup session failure must log at WARNING",
        )


class TestStreamSubscriptionAcceptance(unittest.TestCase):
    """A subscription POST that does not return 201 is a dead subscription: the
    recv loop would then wait for data that never arrives. It must raise so the
    supervisor counts it as a connection failure (backoff + breaker), rather
    than resetting the streak and silently looping every ~60s (finding #2)."""

    def test_non_201_positions_subscription_raises(self):
        class _RejectingSubscriber(_FakeSubscriber):
            def create_positions_subscription(self, **kwargs: Any):
                self.created.append(("positions", kwargs["reference_id"]))
                return 400, {"Message": "invalid ClientKey scope"}

        client, _ = _make_client(subscriber=_RejectingSubscriber())
        with self.assertRaises(SaxoStreamError):
            client._subscribe(delete_first=False)

    def test_non_201_orders_subscription_raises(self):
        class _RejectingSubscriber(_FakeSubscriber):
            def create_orders_subscription(self, **kwargs: Any):
                self.created.append(("orders", kwargs["reference_id"]))
                return 500, {}

        client, _ = _make_client(subscriber=_RejectingSubscriber())
        with self.assertRaises(SaxoStreamError):
            client._subscribe(delete_first=False)

    def test_201_subscription_succeeds(self):
        sub = _FakeSubscriber()
        client, events = _make_client(subscriber=sub)
        client._subscribe(delete_first=False)  # _FakeSubscriber returns 201
        self.assertEqual(len(events["trigger"]), 1)


class TestStreamTokenReauth(unittest.TestCase):
    def test_pushed_token_change_sends_put_authorize_not_reconnect(self):
        provider = _SpyTokenProvider()
        session = _RecordingSession(status_code=202)
        client, _ = _make_client(token_provider=provider, session=session)
        # Simulate an established connection authorized with the seed token.
        client.push_token("t1")
        client._last_authorized_token = "t1"
        # Main thread rotates the bearer.
        client.push_token("t2")

        action = client._maybe_reauthorize()

        self.assertIs(action, StreamAction.CONTINUE)
        (put,) = session.puts
        self.assertTrue(put["url"].endswith(f"/authorize?contextid={_CTX}"))
        self.assertTrue(put["url"].startswith("https://"))
        self.assertEqual(put["headers"]["Authorization"], "BEARER t2")
        self.assertEqual(client._last_authorized_token, "t2")
        self.assertEqual(provider.invalidations, 0)

    def test_no_put_when_token_unchanged(self):
        session = _RecordingSession(status_code=202)
        client, _ = _make_client(session=session)
        client.push_token("t1")
        client._last_authorized_token = "t1"
        action = client._maybe_reauthorize()
        self.assertIs(action, StreamAction.CONTINUE)
        self.assertEqual(session.puts, [])

    def test_put_authorize_non_202_invalidates_and_reconnects(self):
        provider = _SpyTokenProvider()
        session = _RecordingSession(status_code=401)
        client, _ = _make_client(token_provider=provider, session=session)
        client.push_token("t1")
        client._last_authorized_token = "t1"
        client.push_token("t2")

        action = client._maybe_reauthorize()

        self.assertIs(action, StreamAction.RECONNECT)
        self.assertEqual(provider.invalidations, 1)
        # The rejected token is NOT recorded as authorized.
        self.assertEqual(client._last_authorized_token, "t1")

    def test_reader_never_calls_get_access_token(self):
        """The reader authorizes off the MAIN-pushed token, never by pulling the
        provider inline (adversary-2 fix #4 — no flock/network stall on the hot
        path). Drive a full route + reauth cycle and assert zero pulls."""
        provider = _SpyTokenProvider()
        session = _RecordingSession(status_code=202)
        client, _ = _make_client(token_provider=provider, session=session)
        client.push_token("t1")
        client._last_authorized_token = "t1"

        client._build_connect_url(None)
        client._dispatch_frame(build_message(1, "pos"))
        client._dispatch_frame(build_message(2, "_heartbeat"))
        client.push_token("t2")
        client._maybe_reauthorize()

        self.assertEqual(provider.get_calls, 0)


class TestStreamDeliveryEvidence(unittest.TestCase):
    """INC-1 read surface + delivery-life gate (rearm design memo §4.2).

    ``frames_delivered`` is the only real delivery proof (a bare subscribe
    dispatch stamps the liveness epoch without any server frame — probed §7.7);
    ``is_running()`` mirrors ``SaxoPriceStream.is_running``; the delivery-life
    gate narrows WHICH delivery clears the failure streak (a frame on a
    connection younger than ``min_connection_life_s`` is delivery evidence but
    not proof the connection can carry the stream), so a one-heartbeat-then-drop
    gateway now trips the breaker instead of spinning under it forever."""

    def test_is_running_is_false_before_start_and_after_the_reader_thread_ends(self):
        import threading as _threading

        reached_sleep = _threading.Event()
        release = _threading.Event()

        async def failing_ws_connect(url: str, headers: dict[str, str]) -> Any:
            raise SaxoStreamError("scripted connect failure")

        async def gated_sleep(delay: float) -> None:
            reached_sleep.set()
            release.wait(timeout=5.0)

        client, _ = _make_client(
            max_consecutive_failures=2,
            ws_connect=failing_ws_connect,
            async_sleep=gated_sleep,
            alert=lambda msg: None,
        )
        client.push_token("bearer")
        self.assertFalse(client.is_running())  # before the first start()

        with self.assertLogs("alphalens_pipeline.brokers.saxo.streaming", level="WARNING"):
            self.assertTrue(client.start())
            self.assertTrue(reached_sleep.wait(timeout=5.0))
            self.assertTrue(client.is_running())  # reader thread alive mid-backoff
            release.set()
            client._thread.join(timeout=5.0)
        # Breaker tripped on the 2nd failure -> _supervise returned -> thread ended.
        self.assertFalse(client.is_running())
        self.assertFalse(client.is_streaming)

    def test_frames_delivered_increments_on_every_real_frame(self):
        client, _ = _make_client()
        client._route_message(StreamMessage(1, "_heartbeat", b"[]"))
        client._route_message(StreamMessage(2, "pos", b"{}"))
        client._route_message(StreamMessage(3, "_somethingnew", b"[]"))
        self.assertEqual(client.frames_delivered, 3)

    def test_frames_delivered_does_not_move_on_a_bare_subscribe_dispatch(self):
        # (probed, memo §7.7): _subscribe stamps the liveness epoch via
        # on_trigger with ZERO server frames — two 201 POSTs must never read
        # as "delivering".
        client, _ = _make_client()
        client._subscribe(delete_first=False)
        self.assertEqual(client.frames_delivered, 0)

    def test_trips_total_increments_on_every_breaker_trip(self):
        client, _ = _make_client(max_consecutive_failures=2, alert=lambda msg: None)
        client._register_failure()
        client._register_failure()
        self.assertEqual(client.trips_total, 1)
        # A further failure past the threshold trips again — the counter is
        # monotonic so a trip whose whole lifetime falls between two daemon
        # ticks is still countable from the main thread.
        client._register_failure()
        self.assertEqual(client.trips_total, 2)

    def test_a_frame_inside_the_min_connection_life_does_not_clear_the_streak(self):
        clock = _FakeClock()
        client, _ = _make_client(monotonic=clock)
        client._register_failure()
        client._register_failure()
        client._connection_started_mono = clock.now  # stamped at connect
        clock.advance(2.0)  # inside the 5s connection-life window
        client._route_message(StreamMessage(1, "_heartbeat", b"[]"))
        self.assertEqual(client.consecutive_failures, 2)
        self.assertEqual(client.frames_delivered, 1)  # still delivery EVIDENCE

    def test_a_frame_after_the_min_connection_life_clears_the_streak(self):
        clock = _FakeClock()
        client, _ = _make_client(monotonic=clock)
        client._register_failure()
        client._register_failure()
        client._connection_started_mono = clock.now
        clock.advance(5.0)  # gate is >=, so exactly the life opens it
        client._route_message(StreamMessage(1, "_heartbeat", b"[]"))
        self.assertEqual(client.consecutive_failures, 0)

    def test_one_frame_per_connection_storm_still_trips_the_breaker(self):
        # The exploit the gate closes: a gateway that accepts the socket, sends
        # ONE heartbeat, then drops. Pre-gate the streak reset on that frame and
        # the breaker never tripped — a silent ~2s reconnect spin forever.
        clock = _FakeClock()
        client, _ = _make_client(
            monotonic=clock, max_consecutive_failures=6, alert=lambda msg: None
        )
        client.push_token("bearer")
        tripped = False
        for _ in range(6):
            client._connection_started_mono = clock.now  # fresh connection
            clock.advance(2.0)  # heartbeat lands inside the life window
            client._route_message(StreamMessage(1, "_heartbeat", b"[]"))
            step = client._plan_reconnect_step()
            if step.give_up:
                tripped = True
                break
        self.assertTrue(tripped)
        self.assertFalse(client.is_streaming)
        self.assertEqual(client.trips_total, 1)


def _tripped_client(
    **kwargs: Any,
) -> tuple[SaxoStreamingClient, _RecordingThreadFactory, dict[str, list[Any]]]:
    """A client that started (fake thread), got a bearer, and tripped its
    breaker — the state every rearm scenario begins from. The fake reader
    thread is marked dead, as the real ``_supervise`` exit would leave it."""
    factory = _RecordingThreadFactory()
    client, events = _make_client(thread_factory=factory, alert=lambda msg: None, **kwargs)
    client.push_token("bearer")
    client.start()
    for _ in range(client._max_consecutive_failures):
        client._register_failure()
    factory.threads[-1].alive = False  # supervisor returned after the trip
    return client, factory, events


class TestStreamRearm(unittest.TestCase):
    """INC-2: ``rearm()`` — spawn-guarded, context-rotating, cold (memo §4.3).

    Half-open by construction: the failure streak is deliberately NOT reset, so
    the re-armed reader's next failure re-trips immediately (one trial connect)
    while a delivered frame restores the full budget. The context is rotated to
    a FRESH id (the trip-time DELETE is best-effort and is the call most likely
    to have failed during the outage — INC-0 probed the rotation recipe live on
    2026-08-23), and retired contexts are drained on the next healthy subscribe
    rather than orphaned."""

    def test_start_after_a_trip_is_a_silent_no_op_returning_true(self):
        # The trap rearm() exists to fix: _thread is never cleared, so start()
        # after a trip reports success while doing nothing (root cause 2.1).
        client, factory, _ = _tripped_client()
        self.assertTrue(client.start())
        self.assertEqual(len(factory.threads), 1)  # no new thread spawned
        self.assertFalse(client.is_streaming)  # and the breaker stayed shut

    def test_rearm_spawns_a_new_reader_thread_and_reopens_the_breaker(self):
        client, factory, _ = _tripped_client()
        self.assertTrue(client.rearm(_NEW_CTX))
        self.assertEqual(len(factory.threads), 2)
        self.assertEqual(factory.threads[-1].start_calls, 1)
        self.assertTrue(client.is_running())
        self.assertTrue(client.is_streaming)

    def test_rearm_keeps_the_failure_streak_so_the_trial_gets_exactly_one_connect(self):
        # (probed, memo §3 Q2): the streak sits at the threshold, so the very
        # next failure trips again — a one-connect trial budget, zero new state.
        client, _, _ = _tripped_client()
        self.assertTrue(client.rearm(_NEW_CTX))
        self.assertEqual(client.consecutive_failures, client._max_consecutive_failures)
        step = client._plan_reconnect_step()
        self.assertTrue(step.give_up)
        self.assertFalse(client.is_streaming)
        self.assertEqual(client.trips_total, 2)

    def test_a_delivered_frame_after_rearm_restores_the_full_six_attempt_budget(self):
        clock = _FakeClock()
        client, _, _ = _tripped_client(monotonic=clock)
        self.assertTrue(client.rearm(_NEW_CTX))
        client._connection_started_mono = clock.now  # trial connection opens
        clock.advance(6.0)  # past the delivery-life gate
        client._route_message(StreamMessage(1, "_heartbeat", b"[]"))
        self.assertEqual(client.consecutive_failures, 0)
        trips = [client._plan_reconnect_step().give_up for _ in range(6)]
        self.assertEqual(trips, [False, False, False, False, False, True])

    def test_rearm_rotates_the_context_id_and_retires_the_old_one(self):
        client, _, _ = _tripped_client()
        self.assertTrue(client.rearm(_NEW_CTX))
        self.assertEqual(client._context_id, _NEW_CTX)
        self.assertIn(_CTX, client._retired_context_ids)

    def test_rearm_clears_last_message_id_so_the_trial_connects_cold(self):
        # INC-0 established the COLD path only — warm messageid replay onto a
        # fresh context is unproven and never attempted (memo §8.1).
        client, _, _ = _tripped_client()
        client._last_message_id = 987
        self.assertTrue(client.rearm(_NEW_CTX))
        self.assertIsNone(client._last_message_id)

    def test_rearm_clears_the_breaker_alert_latch_so_a_second_trip_logs_again(self):
        alerts: list[str] = []
        factory = _RecordingThreadFactory()
        client, _ = _make_client(thread_factory=factory, alert=alerts.append)
        client.push_token("bearer")
        client.start()
        for _ in range(6):
            client._register_failure()
        factory.threads[-1].alive = False
        self.assertEqual(len(alerts), 1)
        self.assertTrue(client.rearm(_NEW_CTX))
        client._register_failure()  # streak kept -> immediate re-trip
        self.assertEqual(len(alerts), 2)

    def test_rearm_refuses_while_the_old_reader_thread_is_still_alive(self):
        client, factory, _ = _tripped_client()
        factory.threads[-1].alive = True  # old reader still unwinding asyncio.run
        self.assertFalse(client.rearm(_NEW_CTX))
        self.assertEqual(client._context_id, _CTX)  # nothing rotated
        self.assertEqual(len(factory.threads), 1)

    def test_rearm_refuses_after_stop_so_shutdown_is_never_resurrected(self):
        client, _, _ = _tripped_client()
        client.stop()
        self.assertFalse(client.rearm(_NEW_CTX))
        self.assertFalse(client.is_running())

    def test_rearm_refuses_before_any_bearer_has_been_pushed(self):
        # Never trial without a bearer (memo §4.4/§7.4): the tick pushes the
        # token BEFORE any re-arm decision, so a refusal here means the trial
        # can never burn itself on a guaranteed 401.
        factory = _RecordingThreadFactory()
        client, _ = _make_client(thread_factory=factory, alert=lambda msg: None)
        client.start()
        for _ in range(6):
            client._register_failure()
        factory.threads[-1].alive = False
        self.assertIsNone(client._current_token)
        self.assertFalse(client.rearm(_NEW_CTX))
        self.assertEqual(len(factory.threads), 1)

    def test_a_raising_thread_spawn_leaves_is_streaming_false_so_the_next_tick_retries(self):
        # A RuntimeError from thread spawn (thread exhaustion) must roll back
        # _thread/_is_streaming: a naive swallow would leave _thread=None with
        # _is_streaming=True — a dead stream every instrument reports healthy
        # (memo §7.5).
        client, factory, _ = _tripped_client()
        factory.raise_next = True
        with self.assertRaises(RuntimeError):
            client.rearm(_NEW_CTX)
        self.assertFalse(client.is_streaming)
        self.assertFalse(client.is_running())
        self.assertIs(client._thread, factory.threads[0])  # prior thread restored

    def test_rearm_does_not_clear_the_pushed_token_so_the_startup_exemption_stays_bounded(self):
        # Clearing _current_token would turn the bounded startup token_missing
        # exemption (the 2026-07-27 incident fix) into an unbounded free-spin.
        client, _, _ = _tripped_client()
        self.assertTrue(client.rearm(_NEW_CTX))
        self.assertEqual(client._current_token, "bearer")

    def test_a_retired_context_is_deleted_on_the_next_healthy_subscribe(self):
        sub = _FakeSubscriber()
        client, _, _ = _tripped_client(subscriber=sub)
        self.assertTrue(client.rearm(_NEW_CTX))
        sub.deleted.clear()
        client._subscribe(delete_first=False)  # REST demonstrably healthy here
        self.assertIn(_CTX, sub.deleted)
        self.assertEqual(len(client._retired_context_ids), 0)

    def test_retired_context_deque_is_capped(self):
        # A permanent outage must not grow the deque without bound (cap 8 =
        # 5 rungs to ladder saturation plus slack, memo §5).
        client, _, _ = _tripped_client()
        for i in range(12):
            self.assertTrue(client.rearm(f"almgr-123-r{i}"))
            client._thread.alive = False  # trial died; next tick rearms again
        self.assertEqual(len(client._retired_context_ids), 8)
        self.assertNotIn(_CTX, client._retired_context_ids)  # oldest evicted


class TestStreamPushToken(unittest.TestCase):
    def test_push_token_updates_current_token(self):
        client, _ = _make_client()
        client.push_token("abc")
        self.assertEqual(client._current_token, "abc")


if __name__ == "__main__":
    unittest.main()
