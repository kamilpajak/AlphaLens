from __future__ import annotations

import datetime as dt
import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
