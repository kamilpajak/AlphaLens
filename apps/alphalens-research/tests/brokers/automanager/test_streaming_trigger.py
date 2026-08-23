"""Hermetic tests for :class:`StreamTrigger` — the ONLY stream<->loop glue.

The trigger owns a ``threading.Event`` (wake), a single-writer
``last_message_epoch`` (stamped by the stream thread), a trailing-edge debounce,
the ``_current_token`` holder (forwarded to the streaming client), and the
stream-thread lifecycle. It NEVER runs reconcile, opens a journal, or touches
throttle/executor/oco_lag_counts — every one of these tests pins that the glue
stays a pure early-wake with a provable never-worse-than-poll floor.

Design memo: ``docs/research/saxo_streaming_design_2026_07_24.md`` (LOCKED),
sections "Trigger wiring (streaming_trigger.py)" + "Thread model".
"""

from __future__ import annotations

import threading
import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.streaming_trigger import (
    DEFAULT_STREAM_DEBOUNCE_S,
    StreamTrigger,
)
from alphalens_pipeline.brokers.saxo.errors import SaxoLiveEnvironmentBlockedError
from alphalens_pipeline.brokers.saxo.streaming import SIM_STREAMING_BASE_URL
from alphalens_pipeline.brokers.saxo.tokens import StaticTokenProvider


class _FakeClock:
    """Deterministic monotonic clock the test advances by hand."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeTimer:
    """Records a scheduled trailing callback without a real background thread.

    ``fire()`` invokes the captured function so the test drives the debounce
    deadline-recheck deterministically (single live timer at a time)."""

    def __init__(self, interval: float, function: Any) -> None:
        self.interval = interval
        self.function = function
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.function()


class _TimerRecorder:
    """Timer factory that keeps every timer it hands out so a test can fire the
    currently-live one (always the last appended)."""

    def __init__(self) -> None:
        self.timers: list[_FakeTimer] = []

    def __call__(self, interval: float, function: Any) -> _FakeTimer:
        timer = _FakeTimer(interval, function)
        self.timers.append(timer)
        return timer

    def fire_last(self) -> None:
        self.timers[-1].fire()


class _StubClient:
    """Stands in for :class:`SaxoStreamingClient` — captures the wired callbacks
    and records lifecycle calls WITHOUT opening a socket or spawning a thread.
    Carries the full rearm-era read surface (rearm design memo §4.2/§4.3) so the
    trigger's read-only delegations and ``rearm()`` are pinned against it."""

    def __init__(self, *, start_result: bool = True, rearm_result: bool = True) -> None:
        self.on_trigger: Any = None
        self.on_heartbeat: Any = None
        self.pushed: list[str] = []
        self.started = False
        self.stopped = False
        self.stop_timeout: float | None = None
        self._start_result = start_result
        self._rearm_result = rearm_result
        self.is_streaming = True
        self.is_started = False
        self.running = False
        self.frames_delivered = 0
        self.trips_total = 0
        self.consecutive_failures = 0
        self.rearm_ids: list[str] = []

    def push_token(self, token: str) -> None:
        self.pushed.append(token)

    def start(self) -> bool:
        self.started = True
        self.is_started = self._start_result
        return self._start_result

    def is_running(self) -> bool:
        return self.running

    def rearm(self, context_id: str) -> bool:
        self.rearm_ids.append(context_id)
        return self._rearm_result

    def stop(self, *, timeout: float = 5.0) -> None:
        self.stopped = True
        self.stop_timeout = timeout


def _capturing_factory(stub: _StubClient) -> Any:
    def factory(**kwargs: Any) -> _StubClient:
        stub.on_trigger = kwargs["on_trigger"]
        stub.on_heartbeat = kwargs["on_heartbeat"]
        return stub

    return factory


def _make_trigger(
    stub: _StubClient,
    clock: _FakeClock,
    recorder: _TimerRecorder,
    *,
    debounce_s: float = 1.0,
    context_id_factory: Any = None,
) -> StreamTrigger:
    kwargs: dict[str, Any] = {}
    if context_id_factory is not None:
        kwargs["context_id_factory"] = context_id_factory
    return StreamTrigger(
        token_provider=object(),
        subscriber=object(),
        context_id="almgr-1-2",
        debounce_s=debounce_s,
        monotonic=clock,
        timer_factory=recorder,
        client_factory=_capturing_factory(stub),
        **kwargs,
    )


class TestStreamTriggerLeadingWake(unittest.TestCase):
    def test_on_trigger_sets_wake_event(self) -> None:
        clock, rec, stub = _FakeClock(), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec)

        self.assertFalse(trigger.wake_event.is_set())
        trigger.on_trigger()
        self.assertTrue(trigger.wake_event.is_set())

    def test_client_is_wired_to_trigger_callbacks(self) -> None:
        clock, rec, stub = _FakeClock(), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec)

        # The streaming client fires exactly these bound methods — the glue.
        self.assertEqual(stub.on_trigger, trigger.on_trigger)
        self.assertEqual(stub.on_heartbeat, trigger.on_heartbeat)


class TestStreamTriggerHeartbeat(unittest.TestCase):
    def test_on_heartbeat_updates_liveness_without_wake(self) -> None:
        clock, rec, stub = _FakeClock(100.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec)

        trigger.on_heartbeat(100.0)
        self.assertFalse(trigger.wake_event.is_set())
        self.assertEqual(trigger.seconds_since_last_message(), 0.0)

        clock.advance(5.0)
        self.assertEqual(trigger.seconds_since_last_message(), 5.0)

    def test_seconds_since_last_message_is_none_before_any_message(self) -> None:
        clock, rec, stub = _FakeClock(), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec)
        self.assertIsNone(trigger.seconds_since_last_message())

    def test_on_trigger_also_stamps_liveness(self) -> None:
        clock, rec, stub = _FakeClock(50.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec)
        trigger.on_trigger()
        self.assertEqual(trigger.seconds_since_last_message(), 0.0)


class TestStreamTriggerTrailingDebounce(unittest.TestCase):
    def test_trailing_edge_debounce_last_burst_member_always_wakes(self) -> None:
        clock, rec, stub = _FakeClock(0.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec, debounce_s=1.0)

        # Leading edge — the first burst member wakes immediately (sub-second win).
        trigger.on_trigger()
        self.assertTrue(trigger.wake_event.is_set())
        trigger.wake_event.clear()

        # Two more members inside the debounce window are COALESCED (no wake yet).
        clock.t = 0.5
        trigger.on_trigger()
        clock.t = 0.8
        trigger.on_trigger()
        self.assertFalse(trigger.wake_event.is_set())

        # The pending timer fires at its original deadline (t=1.5) but the window
        # extended to t=1.8 (last member at 0.8) — so it reschedules, no wake yet.
        clock.t = 1.5
        rec.fire_last()
        self.assertFalse(trigger.wake_event.is_set())

        # At the extended deadline the LAST burst member finally yields its wake.
        clock.t = 1.8
        rec.fire_last()
        self.assertTrue(trigger.wake_event.is_set())

        # Coalesced: the whole burst produced ONE trailing timer chain, not one
        # timer per member (initial schedule + a single deadline-extension reschedule).
        self.assertEqual(len(rec.timers), 2)

    def test_single_member_burst_after_gap_is_a_fresh_leading_wake(self) -> None:
        clock, rec, stub = _FakeClock(0.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec, debounce_s=1.0)

        trigger.on_trigger()  # leading at t=0
        trigger.wake_event.clear()

        # A trigger AFTER the debounce window is an independent event -> leading wake,
        # never coalesced, no trailing timer armed.
        clock.t = 5.0
        trigger.on_trigger()
        self.assertTrue(trigger.wake_event.is_set())
        self.assertEqual(len(rec.timers), 0)


class TestStreamTriggerWaitPath(unittest.TestCase):
    def test_debounce_never_touches_wait_path(self) -> None:
        clock, rec, stub = _FakeClock(0.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec, debounce_s=1.0)

        # The wait surface is a plain Event — the loop waits on it directly and the
        # debounce logic lives entirely off that path.
        self.assertIsInstance(trigger.wake_event, threading.Event)

        # Waiting before any trigger returns immediately (never blocked by debounce).
        self.assertFalse(trigger.wake_event.wait(0.0))

        trigger.on_trigger()  # leading
        self.assertTrue(trigger.wake_event.wait(0.0))
        trigger.wake_event.clear()

        # A within-window member coalesces WITHOUT setting the event and without
        # blocking on_trigger — the wait path stays clear until the trailing deadline.
        clock.t = 0.1
        trigger.on_trigger()
        self.assertFalse(trigger.wake_event.is_set())
        self.assertFalse(trigger.wake_event.wait(0.0))


class TestStreamTriggerSingleWriterPartition(unittest.TestCase):
    def test_trailing_timer_never_writes_liveness_epoch(self) -> None:
        clock, rec, stub = _FakeClock(0.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec, debounce_s=1.0)

        trigger.on_trigger()  # leading, epoch=0
        clock.t = 0.5
        trigger.on_trigger()  # within window, epoch=0.5, arms trailing (deadline 1.5)
        self.assertEqual(trigger._last_message_epoch, 0.5)

        # The trailing timer fires on the TIMER thread — it may only set the Event,
        # never write the single-writer liveness epoch.
        clock.t = 1.5
        rec.fire_last()
        self.assertTrue(trigger.wake_event.is_set())
        self.assertEqual(trigger._last_message_epoch, 0.5)

    def test_push_token_forwards_to_client_without_touching_liveness(self) -> None:
        clock, rec, stub = _FakeClock(0.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec)
        trigger.on_heartbeat(0.0)

        trigger.push_token("tok-1")
        trigger.push_token("tok-2")
        self.assertEqual(stub.pushed, ["tok-1", "tok-2"])
        self.assertEqual(trigger._last_message_epoch, 0.0)


class TestStreamTriggerLifecycle(unittest.TestCase):
    def test_start_forwards_client_result(self) -> None:
        clock, rec = _FakeClock(), _TimerRecorder()
        started = _StubClient(start_result=True)
        self.assertTrue(_make_trigger(started, clock, rec).start())
        self.assertTrue(started.started)

        refused = _StubClient(start_result=False)
        self.assertFalse(_make_trigger(refused, clock, rec).start())

    def test_stop_cancels_pending_trailing_timer_and_stops_client(self) -> None:
        clock, rec, stub = _FakeClock(0.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec, debounce_s=1.0)

        trigger.on_trigger()
        clock.t = 0.5
        trigger.on_trigger()  # arms a trailing timer
        pending = rec.timers[-1]

        trigger.stop()
        self.assertTrue(pending.cancelled)
        self.assertTrue(stub.stopped)

    def test_is_streaming_and_is_started_delegate_to_client(self) -> None:
        clock, rec, stub = _FakeClock(), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec)
        stub.is_streaming = False
        stub.is_started = True
        self.assertFalse(trigger.is_streaming)
        self.assertTrue(trigger.is_started)

    def test_rearm_and_read_surface_delegate_to_client(self) -> None:
        # Rearm design memo §4.6: the tick consumes the whole delivery-backed
        # read surface through the trigger — pure delegations, no local state.
        clock, rec, stub = _FakeClock(), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec, context_id_factory=lambda: "almgr-fresh-1")
        stub.running = False
        stub.frames_delivered = 7
        stub.trips_total = 2
        stub.consecutive_failures = 6

        self.assertFalse(trigger.is_running())
        stub.running = True
        self.assertTrue(trigger.is_running())
        self.assertEqual(trigger.frames_delivered, 7)
        self.assertEqual(trigger.trips_total, 2)
        self.assertEqual(trigger.consecutive_failures, 6)
        self.assertTrue(trigger.rearm())
        self.assertEqual(stub.rearm_ids, ["almgr-fresh-1"])

    def test_reset_liveness_clears_the_epoch(self) -> None:
        # Rearm design memo §4.6/§7.2: without this, an hours-old epoch fires
        # the throttled 'stream-dead' alert every 30 min after a re-arm — the
        # metronome relocated onto a different key.
        clock, rec, stub = _FakeClock(100.0), _TimerRecorder(), _StubClient()
        trigger = _make_trigger(stub, clock, rec)
        trigger.on_heartbeat(100.0)
        clock.advance(50400.0)  # the incident's 14h dark stretch
        self.assertEqual(trigger.seconds_since_last_message(), 50400.0)

        trigger.reset_liveness()
        self.assertIsNone(trigger.seconds_since_last_message())
        # The epoch is documented single-writer = stream thread; this second,
        # main-thread writer is safe ONLY while the reader is dead — the
        # docstring must carry that condition (memo §4.6 "a test pins it").
        doc = StreamTrigger.reset_liveness.__doc__ or ""
        self.assertIn("is_running", doc)

    def test_context_id_factory_reaches_the_client(self) -> None:
        # Context rotation is mandatory (memo §4.3): every rearm() mints a FRESH
        # id via the injected factory and hands it to the client verbatim.
        clock, rec, stub = _FakeClock(), _TimerRecorder(), _StubClient()
        ids = iter(["almgr-a-1", "almgr-b-2"])
        trigger = _make_trigger(stub, clock, rec, context_id_factory=lambda: next(ids))

        trigger.rearm()
        trigger.rearm()
        self.assertEqual(stub.rearm_ids, ["almgr-a-1", "almgr-b-2"])


# ---- SIM-rail positive control (mirrors test_saxo_sim_only_rail.py doctrine) ----


class _AnyTokenProvider:
    """OAuth-like provider (NOT a StaticTokenProvider)."""

    def get_access_token(self) -> str:
        return "tok"

    def invalidate(self) -> None:
        pass


class _FakeSubscriber:
    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": "CK-1"}

    def create_positions_subscription(self, **_: Any) -> tuple[int, dict[str, Any]]:
        return 201, {}

    def create_orders_subscription(self, **_: Any) -> tuple[int, dict[str, Any]]:
        return 201, {}

    def delete_all_subscriptions(self, _context_id: str) -> list[tuple[int, dict[str, Any]]]:
        return [(202, {}), (202, {})]


class TestStreamTriggerSimRail(unittest.TestCase):
    """The default client factory builds a real SaxoStreamingClient, so the SIM
    rail is enforced at the trigger boundary too (positive control)."""

    def test_live_streaming_host_raises_live_blocked(self) -> None:
        with self.assertRaises(SaxoLiveEnvironmentBlockedError):
            StreamTrigger(
                token_provider=_AnyTokenProvider(),
                subscriber=_FakeSubscriber(),
                context_id="almgr-1-2",
                streaming_base_url="wss://live-streaming.saxobank.com/oapi/streaming/ws/connect",
            )

    def test_sim_streaming_base_url_constructs(self) -> None:
        trigger = StreamTrigger(
            token_provider=_AnyTokenProvider(),
            subscriber=_FakeSubscriber(),
            context_id="almgr-1-2",
            streaming_base_url=SIM_STREAMING_BASE_URL,
        )
        self.assertIsInstance(trigger.wake_event, threading.Event)

    def test_static_token_provider_refuses_to_start(self) -> None:
        trigger = StreamTrigger(
            token_provider=StaticTokenProvider("static-24h-token"),
            subscriber=_FakeSubscriber(),
            context_id="almgr-1-2",
        )
        self.assertFalse(trigger.start())

    def test_client_factory_is_never_given_an_alert_sink(self) -> None:
        # PR #900 / rearm memo §7.14: SaxoStreamingClient accepts an optional
        # ``alert`` and production is safe only because this factory call omits
        # it — _trip_breaker stays journald-only on the READER thread; both
        # Telegram sinks are main-thread-only. A future implementer threading
        # the sink through here would undo that.
        seen_kwargs: dict[str, Any] = {}

        def factory(**kwargs: Any) -> _StubClient:
            seen_kwargs.update(kwargs)
            return _StubClient()

        StreamTrigger(
            token_provider=_AnyTokenProvider(),
            subscriber=_FakeSubscriber(),
            context_id="almgr-1-2",
            client_factory=factory,
        )
        self.assertNotIn("alert", seen_kwargs)


class TestStreamTriggerDefaults(unittest.TestCase):
    def test_default_debounce_is_one_second(self) -> None:
        self.assertEqual(DEFAULT_STREAM_DEBOUNCE_S, 1.0)

    def test_default_context_id_factory_keeps_the_saxo_constraint(self) -> None:
        # <=50 chars, [a-zA-Z0-9-] only — the format moved out of
        # _build_stream_handles so the initial context and every rearm rotation
        # share ONE home (rearm design memo §4.3).
        from alphalens_pipeline.brokers.automanager.streaming_trigger import (
            default_context_id_factory,
        )

        minted = default_context_id_factory()
        self.assertLessEqual(len(minted), 50)
        self.assertRegex(minted, r"^[a-zA-Z0-9-]+$")
        self.assertTrue(minted.startswith("almgr-"))

    def test_two_mints_in_the_same_second_are_distinct(self) -> None:
        # A collided rotation would append the LIVE context id to the retired
        # deque, whose next drain best-effort DELETEs its subscriptions — so
        # the factory carries a monotonic serial, not just second resolution.
        from alphalens_pipeline.brokers.automanager.streaming_trigger import (
            default_context_id_factory,
        )

        first = default_context_id_factory()
        second = default_context_id_factory()
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
