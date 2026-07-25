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


def _make_client(
    *,
    token_provider: Any = None,
    subscriber: Any = None,
    session: Any = None,
    max_consecutive_failures: int = 6,
) -> tuple[SaxoStreamingClient, dict[str, list[Any]]]:
    events: dict[str, list[Any]] = {"trigger": [], "heartbeat": []}
    client = SaxoStreamingClient(
        token_provider or _SpyTokenProvider(),
        subscriber or _FakeSubscriber(),
        context_id=_CTX,
        on_trigger=lambda: events["trigger"].append(True),
        on_heartbeat=events["heartbeat"].append,
        session=session or _RecordingSession(),
        tuning=StreamTuning(max_consecutive_failures=max_consecutive_failures),
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
        )
        self.assertTrue(client.is_streaming)
        tripped = [client._register_failure() for _ in range(6)]
        # Only the 6th failure trips the breaker.
        self.assertEqual(tripped, [False, False, False, False, False, True])
        self.assertFalse(client.is_streaming)
        self.assertEqual(len(alerts), 1)
        # Breaker teardown DELETEs subscriptions so the streaming REST stops.
        self.assertEqual(sub.deleted, [_CTX])
        # A further failure never re-alerts (matches the overnight-spam fix).
        client._register_failure()
        self.assertEqual(len(alerts), 1)

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


class TestStreamPushToken(unittest.TestCase):
    def test_push_token_updates_current_token(self):
        client, _ = _make_client()
        client.push_token("abc")
        self.assertEqual(client._current_token, "abc")


if __name__ == "__main__":
    unittest.main()
