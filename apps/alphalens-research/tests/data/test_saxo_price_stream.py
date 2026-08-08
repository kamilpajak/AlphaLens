from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest import mock

import alphalens_pipeline.data.alt_data.saxo_price_stream as sps
from alphalens_pipeline.data.alt_data.saxo_price_stream import QuoteCache, SaxoPriceStream
from alphalens_pipeline.data.alt_data.session_reclaim import ReclaimLimiter

_T0 = dt.datetime(2026, 8, 7, 13, 48, 0, tzinfo=dt.UTC)


def _row(**over) -> dict:
    row = {
        "Uic": 211,
        "LastUpdated": "2026-08-07T13:47:59Z",
        "Quote": {"Bid": 314.01, "Ask": 314.04, "DelayedByMinutes": 0},
    }
    row.update(over)
    return row


class TestQuoteCache(unittest.TestCase):
    def test_snapshot_row_is_stored(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        q = c.get(211)
        self.assertEqual((q.bid, q.ask), (314.01, 314.04))
        self.assertEqual(q.delayed_by_minutes, 0)
        self.assertEqual(q.event_time, dt.datetime(2026, 8, 7, 13, 47, 59, tzinfo=dt.UTC))

    def test_delta_with_one_side_keeps_the_other(self):
        """THE delta rule. Saxo omits unchanged fields; treating an absent Ask as
        'no ask' would blank half the quote and produce a None mid."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:01Z", "Quote": {"Bid": 314.10}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        q = c.get(211)
        self.assertEqual(q.bid, 314.10)
        self.assertEqual(q.ask, 314.04)  # preserved

    def test_delta_without_a_quote_block_still_advances_event_time(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:05Z"},
            received_at=_T0 + dt.timedelta(seconds=5),
        )
        q = c.get(211)
        self.assertEqual(q.event_time, dt.datetime(2026, 8, 7, 13, 48, 5, tzinfo=dt.UTC))
        self.assertEqual(q.bid, 314.01)

    def test_delayed_flag_is_carried_and_updatable(self):
        """Session demotion arrives as a flag change on an otherwise healthy
        quote - the ONLY signal that prices went 15 minutes stale."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:02Z", "Quote": {"DelayedByMinutes": 15}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        self.assertEqual(c.get(211).delayed_by_minutes, 15)

    def test_out_of_order_event_time_is_dropped(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:47:50Z", "Quote": {"Bid": 1.0}},
            received_at=_T0 + dt.timedelta(seconds=3),
        )
        self.assertEqual(c.get(211).bid, 314.01)  # regression ignored

    def test_out_of_order_row_still_applies_a_carried_delayed_flag(self):
        """A regressive row is dropped for price/event_time (conservative and
        safe), but NOT for DelayedByMinutes: losing a newly-reported delay
        would leave a demoted session looking healthy, serving 15-minute-old
        prices to an order decision. The flag must apply even when the rest
        of the row is rejected."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:47:50Z",  # regressive
                "Quote": {"Bid": 1.0, "DelayedByMinutes": 15},
            },
            received_at=_T0 + dt.timedelta(seconds=3),
        )
        q = c.get(211)
        self.assertEqual(q.delayed_by_minutes, 15)  # flag still applies
        self.assertEqual(q.bid, 314.01)  # price untouched
        self.assertEqual(q.ask, 314.04)
        self.assertEqual(
            q.event_time, dt.datetime(2026, 8, 7, 13, 47, 59, tzinfo=dt.UTC)
        )  # event_time untouched

    def test_out_of_order_row_without_the_delay_key_leaves_flag_untouched(self):
        """The OMITTED-key semantics still hold on the regression path: a
        regressive row that does not carry DelayedByMinutes must not disturb
        the cached flag, and must not resurrect any stale price either."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:47:50Z", "Quote": {"Bid": 1.0}},
            received_at=_T0 + dt.timedelta(seconds=3),
        )
        q = c.get(211)
        self.assertEqual(q.delayed_by_minutes, 0)  # unchanged
        self.assertEqual(q.bid, 314.01)  # not resurrected to the regressive 1.0

    def test_naive_last_updated_yields_none_event_time(self):
        """A ``LastUpdated`` string carrying no offset (e.g. Saxo omitting the
        'Z'/offset suffix) must not silently become a naive datetime: that
        value would flow into ``PricePoint.event_time`` and later blow up
        ``is_fresh``'s ``(now - event_time)`` subtraction against an aware
        ``now`` with a ``TypeError`` that would crash the daemon tick. A
        timestamp whose timezone we cannot determine is a doubt, and a doubt
        is a veto: ``_parse_utc`` must return None instead."""
        c = QuoteCache()
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:47:59",  # no trailing Z / offset
                "Quote": {"Bid": 314.01, "Ask": 314.04, "DelayedByMinutes": 0},
            },
            received_at=_T0,
        )
        q = c.get(211)
        self.assertIsNone(q.event_time)

    def test_unknown_uic_returns_none(self):
        self.assertIsNone(QuoteCache().get(999))

    def test_row_without_uic_is_ignored(self):
        c = QuoteCache()
        c.apply({"LastUpdated": "2026-08-07T13:48:00Z", "Quote": {"Bid": 1.0}}, received_at=_T0)
        self.assertIsNone(c.get(211))

    def test_explicit_null_bid_propagates_as_unknown_not_preserved(self):
        """A PRESENT-but-null Bid (a plausible one-sided-market / halt signal)
        must BLANK the cached bid, unlike an OMITTED Bid key which preserves
        it. The two are distinguished by KEY PRESENCE, not truthiness: an
        explicit null means 'no bid right now' and must propagate so the
        downstream freshness gate vetoes, rather than silently reusing a
        stale price."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:01Z", "Quote": {"Bid": None}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        q = c.get(211)
        self.assertIsNone(q.bid)
        self.assertEqual(q.ask, 314.04)  # untouched (omitted) key still preserved

    def test_identical_event_time_delta_is_still_applied(self):
        """Saxo timestamps carry only second resolution in the observed
        fixtures, so two updates sharing a LastUpdated during active trading
        are common, not a corner case. The regression guard is strict '<':
        an equal-timestamp update must still apply. Changing the guard to
        '<=' would freeze the price for the rest of every second - this test
        pins the current, correct, strict comparison."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:47:59Z", "Quote": {"Bid": 315.00}},
            received_at=_T0 + dt.timedelta(milliseconds=500),
        )
        self.assertEqual(c.get(211).bid, 315.00)

    def test_delta_before_any_snapshot_is_half_blank(self):
        """A delta for a uic never seen before (no prior snapshot - the same
        code path as a delta for an unknown uic) produces a Quote with the
        untouched side still None. That half-blank shape is acceptable ONLY
        because the downstream freshness gate must treat a missing side as
        no-price, never as a stale-but-valid one."""
        c = QuoteCache()
        c.apply(
            {"Uic": 900, "LastUpdated": "2026-08-07T13:48:00Z", "Quote": {"Bid": 100.0}},
            received_at=_T0,
        )
        q = c.get(900)
        self.assertEqual(q.bid, 100.0)
        self.assertIsNone(q.ask)

    def test_delayed_by_minutes_preserved_across_unrelated_bid_only_delta(self):
        """DelayedByMinutes preservation across an unrelated (Bid-only) delta
        is not covered by test_delta_with_one_side_keeps_the_other (which only
        checks bid/ask) - pinned independently so reverting just this default
        does not pass the whole suite."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:01Z", "Quote": {"Bid": 314.10}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        self.assertEqual(c.get(211).delayed_by_minutes, 0)

    def test_any_delayed_is_false_on_an_empty_cache(self):
        self.assertFalse(QuoteCache().any_delayed())

    def test_any_delayed_is_false_while_every_quote_is_fresh(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        self.assertFalse(c.any_delayed())

    def test_any_delayed_is_true_once_any_quote_reports_a_delay(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:02Z", "Quote": {"DelayedByMinutes": 15}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        self.assertTrue(c.any_delayed())


class _FakeMarketDataClient:
    """Stand-in for SaxoMarketDataClient - _apply_frame never touches it."""


class _FakeTokenProvider:
    """Stand-in for LiveTokenProvider - _apply_frame never touches it."""


class _ResolvingClient:
    """Stand-in for SaxoMarketDataClient exposing only resolve_uic, records
    every call so a test can assert on cache-hit vs cache-miss behavior.
    Upper-cases like the real client does (see its docstring) so the fixture
    matches production instead of asserting on a fake-only behavior."""

    def __init__(self, responses: dict[tuple[str, str], int | None]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def resolve_uic(self, ticker: str, *, exchange_mic: str) -> int | None:
        self.calls.append((ticker, exchange_mic))
        return self._responses.get((ticker.upper(), exchange_mic.upper()))


class TestLiveUicFor(unittest.TestCase):
    """Fix round 2 (Task 7 review), finding 3: live_uic_for had zero hermetic
    coverage. Pins the cache-on-success-only contract its own docstring
    claims."""

    def test_a_successful_resolution_is_cached_not_re_resolved(self):
        client = _ResolvingClient({("AAPL", "XNYS"): 211})
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        self.assertEqual(stream.live_uic_for("AAPL", exchange_mic="XNYS"), 211)
        self.assertEqual(stream.live_uic_for("AAPL", exchange_mic="XNYS"), 211)
        self.assertEqual(client.calls, [("AAPL", "XNYS")])  # ONE REST call, not two

    def test_a_failed_resolution_is_retried_not_cached(self):
        """A None (unknown venue, no match, ambiguous match, or a transient
        failure) must NOT be cached -- caching it would veto that ticker for
        the rest of the process instead of retrying on the next tick."""
        client = _ResolvingClient({})  # every lookup misses
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        self.assertIsNone(stream.live_uic_for("QUBT", exchange_mic="ZZZZ"))
        self.assertIsNone(stream.live_uic_for("QUBT", exchange_mic="ZZZZ"))
        self.assertEqual(client.calls, [("QUBT", "ZZZZ"), ("QUBT", "ZZZZ")])  # retried both times

    def test_the_cache_key_is_upper_cased_ticker_and_venue(self):
        client = _ResolvingClient({("AAPL", "XNAS"): 5})
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        self.assertEqual(stream.live_uic_for("aapl", exchange_mic="xnas"), 5)
        self.assertEqual(stream.live_uic_for("AAPL", exchange_mic="XNAS"), 5)
        self.assertEqual(client.calls, [("aapl", "xnas")])  # 2nd call hit the cache


def _build_frame(message_id: int, reference_id: str, payload: bytes, *, fmt: int = 0) -> bytes:
    """Encode one Saxo streaming envelope message (same layout as the SIM
    frame builder in test_saxo_streaming.py) so _apply_frame can be exercised
    without a real WebSocket."""
    ref = reference_id.encode("ascii")
    return (
        message_id.to_bytes(8, "little")
        + b"\x00\x00"  # reserved
        + bytes([len(ref)])
        + ref
        + bytes([fmt])
        + len(payload).to_bytes(4, "little")
        + payload
    )


class TestDefaultContextId(unittest.TestCase):
    """A fixed default context_id means a rebuild after a dead reader thread
    (nothing calls stop()) re-POSTs the SAME ContextId+ReferenceId, and two
    processes on the same LIVE login would collide too. The default must be
    per-process unique, mirroring the SIM auto-manager's
    f"almgr-{os.getpid()}-{int(time.time())}" convention (control_loop.py),
    with a "px" marker distinguishing the two."""

    def test_default_context_id_is_per_process_unique_with_px_marker(self):
        with (
            mock.patch.object(sps.os, "getpid", return_value=4242),
            mock.patch.object(sps.time, "time", return_value=1754570000.0),
        ):
            stream = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        self.assertEqual(stream._context_id, "almgr-px-4242-1754570000")

    def test_two_instances_in_the_same_process_get_different_context_ids(self):
        """Constructed at different times (the getpid stays fixed, the clock
        moves), so a rebuilt stream never reuses the dead stream's ContextId."""
        with mock.patch.object(sps.os, "getpid", return_value=4242):
            with mock.patch.object(sps.time, "time", return_value=1754570000.0):
                first = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
            with mock.patch.object(sps.time, "time", return_value=1754570099.0):
                second = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        self.assertNotEqual(first._context_id, second._context_id)

    def test_context_id_stays_injectable_for_tests(self):
        stream = SaxoPriceStream(
            _FakeMarketDataClient(), _FakeTokenProvider(), context_id="custom-ctx"
        )
        self.assertEqual(stream._context_id, "custom-ctx")


class TestSaxoPriceStreamApplyFrame(unittest.TestCase):
    """_apply_frame is synchronous decode-and-apply glue - testable directly,
    without mocking a socket (only the async recv loop needs the live probe)."""

    def test_malformed_non_dict_row_is_dropped_with_a_debug_log(self):
        stream = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        payload = json.dumps(
            [
                {"Uic": 5, "LastUpdated": "2026-08-07T13:48:00Z", "Quote": {"Bid": 1.0}},
                "garbage-not-a-row",
                None,
            ]
        ).encode("utf-8")
        frame = _build_frame(1, "px", payload)
        with self.assertLogs(
            "alphalens_pipeline.data.alt_data.saxo_price_stream", level="DEBUG"
        ) as cm:
            stream._apply_frame(frame)
        self.assertTrue(
            any("garbage-not-a-row" in line or "non-dict" in line.lower() for line in cm.output),
            cm.output,
        )
        q = stream.get(5)
        self.assertEqual(q.bid, 1.0)


class _ReclaimTrackingClient:
    """Stand-in for SaxoMarketDataClient exposing only ``elevate_session``,
    used to assert the reclaim wiring without a real HTTP call."""

    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = iter(outcomes)
        self.calls = 0

    def elevate_session(self) -> bool:
        self.calls += 1
        return next(self._outcomes)


def _delayed_frame(message_id: int, *, delayed_by_minutes: int) -> bytes:
    payload = json.dumps(
        [
            {
                "Uic": 5,
                "LastUpdated": f"2026-08-07T13:48:{message_id:02d}Z",
                "Quote": {"Bid": 1.0, "Ask": 1.1, "DelayedByMinutes": delayed_by_minutes},
            }
        ]
    ).encode("utf-8")
    return _build_frame(message_id, "px", payload)


class TestSaxoPriceStreamReclaim(unittest.TestCase):
    """The reclaim fires on a TRANSITION into the delayed state, not once per
    message - a 1 Hz stream would otherwise burn the whole hourly budget in
    seconds."""

    def test_delayed_transition_triggers_one_reclaim_attempt(self):
        client = _ReclaimTrackingClient([True])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.assertEqual(client.calls, 1)

    def test_reclaim_does_not_fire_again_while_still_delayed(self):
        client = _ReclaimTrackingClient([True, True, True])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        stream._apply_frame(_delayed_frame(2, delayed_by_minutes=15))
        stream._apply_frame(_delayed_frame(3, delayed_by_minutes=15))
        self.assertEqual(client.calls, 1)

    def test_reclaim_fires_again_after_recovering_then_delaying_once_more(self):
        client = _ReclaimTrackingClient([True, True])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        stream._apply_frame(_delayed_frame(2, delayed_by_minutes=0))
        stream._apply_frame(_delayed_frame(3, delayed_by_minutes=15))
        self.assertEqual(client.calls, 2)

    def test_healthy_stream_never_calls_elevate(self):
        client = _ReclaimTrackingClient([])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=0))
        self.assertEqual(client.calls, 0)

    def test_budget_exhausted_logs_a_warning_and_leaves_the_quote_delayed(self):
        """No bypass: the freshness gate already vetoes delayed quotes, so the
        safe outcome on exhaustion is automatic - do nothing and wait for the
        budget to refill."""
        exhausted_limiter = ReclaimLimiter(
            max_per_hour=0, clock=lambda: dt.datetime(2026, 8, 7, 13, 48, tzinfo=dt.UTC)
        )
        client = _ReclaimTrackingClient([])
        stream = SaxoPriceStream(client, _FakeTokenProvider(), reclaim_limiter=exhausted_limiter)
        with self.assertLogs(
            "alphalens_pipeline.data.alt_data.saxo_price_stream", level="WARNING"
        ) as cm:
            stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.assertTrue(any("budget" in line.lower() for line in cm.output), cm.output)
        self.assertEqual(client.calls, 0)
        self.assertEqual(stream.get(5).delayed_by_minutes, 15)


class _FakeSharedInstance:
    """Stand-in for SaxoPriceStream at the get_shared_price_stream() level -
    the getter only ever calls start() and is_running() on what it holds."""

    def __init__(self, *, running: bool) -> None:
        self._running = running
        self.started = False

    def start(self) -> None:
        self.started = True

    def is_running(self) -> bool:
        return self._running


class TestGetSharedPriceStream(unittest.TestCase):
    """Fix round 2 (Task 7 review), finding 4: after the reconnect circuit
    breaker trips, _supervise returns but self._thread stays non-None, so a
    dead stream would sit in the module singleton silently for the rest of
    the process. These patch the construction chain (LiveAuthConfig,
    LiveTokenProvider, SaxoMarketDataClient, SaxoPriceStream) so no real
    network/auth is touched."""

    def setUp(self) -> None:
        # The singleton is process-global state; reset it around each test.
        patcher = mock.patch.object(sps, "_shared_stream", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patched_construction(self, *stream_instances):
        return (
            mock.patch.object(sps, "LiveAuthConfig"),
            mock.patch.object(sps, "LiveTokenProvider"),
            mock.patch.object(sps, "SaxoMarketDataClient"),
            mock.patch.object(sps, "SaxoPriceStream", side_effect=list(stream_instances)),
        )

    def test_first_call_constructs_and_starts_the_stream(self):
        instance = _FakeSharedInstance(running=True)
        p1, p2, p3, p4 = self._patched_construction(instance)
        with p1, p2, p3, p4:
            stream = sps.get_shared_price_stream()
        self.assertIs(stream, instance)
        self.assertTrue(instance.started)

    def test_a_running_stream_is_reused_not_rebuilt(self):
        instance = _FakeSharedInstance(running=True)
        # side_effect has exactly ONE instance -- a second construction
        # attempt raises StopIteration, failing the test loudly.
        p1, p2, p3, p4 = self._patched_construction(instance)
        with p1, p2, p3, p4:
            first = sps.get_shared_price_stream()
            second = sps.get_shared_price_stream()
        self.assertIs(first, second)

    def test_a_dead_reader_thread_is_rebuilt_not_reused(self):
        """The regression this finding is about: a stream whose reader thread
        died (circuit breaker tripped) must be replaced, not handed back
        forever with only a log warning as the trace."""
        dead = _FakeSharedInstance(running=False)
        alive = _FakeSharedInstance(running=True)
        p1, p2, p3, p4 = self._patched_construction(dead, alive)
        with p1, p2, p3, p4:
            first = sps.get_shared_price_stream()
            self.assertIs(first, dead)
            second = sps.get_shared_price_stream()
        self.assertIs(second, alive)
        self.assertTrue(alive.started)


if __name__ == "__main__":
    unittest.main()
