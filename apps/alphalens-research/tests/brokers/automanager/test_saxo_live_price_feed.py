from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager.saxo_live_price_feed import SaxoLivePriceFeed
from alphalens_pipeline.data.alt_data.saxo_price_stream import Quote
from broker_contract import price_feed

_NOW = dt.datetime(2026, 8, 7, 13, 48, 0, tzinfo=dt.UTC)


class _Stream:
    def __init__(self, quote: Quote | None, *, running_low: dict[int, float] | None = None):
        self._quote = quote
        self.subscribed: list[int] = []
        self._running_low = dict(running_low or {})
        self.reseed_calls: list[tuple[int, float]] = []
        self.receiving = True
        self.receiving_calls = 0

    def is_receiving(self) -> bool:
        self.receiving_calls += 1
        return self.receiving

    def ensure_subscribed(self, uics):
        self.subscribed = list(uics)

    def get(self, uic):
        return self._quote if self._quote and self._quote.uic == uic else None

    def drain_running_low(self, uic):
        return self._running_low.pop(uic, None)

    def reseed_running_low(self, uic, low):
        # Mirrors QuoteCache.reseed_running_low's min-merge.
        self.reseed_calls.append((uic, low))
        prev = self._running_low.get(uic)
        self._running_low[uic] = low if prev is None else min(prev, low)


def _quote(**over) -> Quote:
    base = {
        "uic": 211,
        "bid": 314.01,
        "ask": 314.04,
        "event_time": _NOW - dt.timedelta(seconds=1),
        "delayed_by_minutes": 0,
        "received_at": _NOW,
    }
    base.update(over)
    return Quote(**base)


def _feed(quote, *, sim_to_live=None, stream=None):
    mapping = sim_to_live if sim_to_live is not None else {211: 211}
    return SaxoLivePriceFeed(
        stream=stream if stream is not None else _Stream(quote),
        resolve_live_uic=mapping.get,
        clock=lambda: _NOW,
    )


class TestSaxoLivePriceFeed(unittest.TestCase):
    def test_fresh_quote_becomes_a_pricepoint(self):
        p = _feed(_quote()).latest(211)
        self.assertEqual((p.bid, p.ask), (314.01, 314.04))
        self.assertEqual(p.source, "saxo-live-l1")
        self.assertEqual(p.event_time, _NOW - dt.timedelta(seconds=1))

    def test_delayed_quote_is_vetoed_even_though_it_looks_healthy(self):
        """Session demotion: prices keep arriving and keep moving, 15 minutes
        old. Age alone would not catch it because LastUpdated also lags."""
        self.assertIsNone(_feed(_quote(delayed_by_minutes=15)).latest(211))

    def test_stale_quote_is_vetoed(self):
        """Past the bound the veto still fires. The age used to be 10 s, which
        the retired 3 s bound rejected and the measured 45 s bound accepts —
        uic 641 spent 54% of the lull window above 3 s while the stream was
        demonstrably alive, which is the whole reason the bound moved (#1397)."""
        stale = _quote(event_time=_NOW - dt.timedelta(seconds=price_feed.DEFAULT_MAX_AGE_S + 15.0))
        self.assertIsNone(_feed(stale).latest(211))

    def test_a_ten_second_quote_now_passes_because_that_was_the_defect(self):
        """The counterexample to the test above, and the behaviour change
        itself: a name that simply had nothing new to say is not a stale price."""
        quiet = _quote(event_time=_NOW - dt.timedelta(seconds=10))
        self.assertIsNotNone(_feed(quiet).latest(211))

    def test_missing_side_is_vetoed(self):
        self.assertIsNone(_feed(_quote(bid=None)).latest(211))

    def test_missing_ask_is_vetoed(self):
        self.assertIsNone(_feed(_quote(ask=None)).latest(211))

    def test_non_numeric_bid_is_vetoed_not_raised(self):
        """QuoteCache.apply stores whatever JSON value the socket delivered
        without coercion (see its docstring). A Bid that arrives as a
        non-numeric value must veto like any other doubt, not raise
        ValueError/TypeError out of the read path and skip the tick that also
        runs the never-naked protection pass right after it."""
        self.assertIsNone(_feed(_quote(bid="not-a-number")).latest(211))

    def test_non_numeric_ask_is_vetoed_not_raised(self):
        self.assertIsNone(_feed(_quote(ask="not-a-number")).latest(211))

    def test_unknown_delayed_flag_is_vetoed(self):
        """First quote ever seen for a uic under delta-merge semantics leaves
        DelayedByMinutes unset (None), not 0. Unknown must veto, same as a
        confirmed delay - never read as "assume not delayed"."""
        self.assertIsNone(_feed(_quote(delayed_by_minutes=None)).latest(211))

    def test_unknown_quote_is_vetoed(self):
        self.assertIsNone(_feed(None).latest(211))

    def test_unmapped_uic_is_vetoed(self):
        self.assertIsNone(_feed(_quote(), sim_to_live={}).latest(211))

    def test_returned_point_keeps_the_caller_uic_not_the_live_uic(self):
        """The engine keys everything by the uic it asked for; handing back a
        LIVE uic would silently mismatch the managed position."""
        stream = _Stream(_quote(uic=9999))
        feed = SaxoLivePriceFeed(
            stream=stream, resolve_live_uic={211: 9999}.get, clock=lambda: _NOW
        )
        self.assertEqual(feed.latest(211).uic, 211)


class TestSaxoLivePriceFeedSessionLow(unittest.TestCase):
    """The SupportsSessionLow touch-latch capability: session_low maps the
    caller's uic to the LIVE uic (like latest) and DRAINS the stream's 1 Hz
    running low."""

    def _feed(self, *, running_low, sim_to_live=None):
        mapping = sim_to_live if sim_to_live is not None else {211: 9999}
        return SaxoLivePriceFeed(
            stream=_Stream(_quote(), running_low=running_low),
            resolve_live_uic=mapping.get,
            clock=lambda: _NOW,
        )

    def test_is_a_supports_session_low(self):
        from broker_contract.price_feed import SupportsSessionLow

        self.assertIsInstance(self._feed(running_low={}), SupportsSessionLow)

    def test_session_low_drains_the_live_uic(self):
        # The caller asks by SIM uic 211; the stream is keyed by LIVE uic 9999.
        feed = self._feed(running_low={9999: 313.70})
        self.assertEqual(feed.session_low(211), 313.70)
        self.assertIsNone(feed.session_low(211))  # pop: the second read is empty

    def test_session_low_none_for_unmapped_uic(self):
        feed = self._feed(running_low={9999: 313.70}, sim_to_live={})
        self.assertIsNone(feed.session_low(211))

    def test_reseed_session_low_restores_via_the_live_uic(self):
        # The caller hands the drained-but-unusable low back by SIM uic 211; it
        # must land under LIVE uic 9999 (like session_low's drain), so the next
        # drain by SIM uic returns it.
        stream = _Stream(_quote(), running_low={})
        feed = SaxoLivePriceFeed(
            stream=stream, resolve_live_uic={211: 9999}.get, clock=lambda: _NOW
        )
        feed.reseed_session_low(211, 313.70)
        self.assertEqual(stream.reseed_calls, [(9999, 313.70)])
        self.assertEqual(feed.session_low(211), 313.70)

    def test_reseed_session_low_is_a_silent_noop_for_an_unmapped_uic(self):
        stream = _Stream(_quote(), running_low={})
        feed = SaxoLivePriceFeed(stream=stream, resolve_live_uic={}.get, clock=lambda: _NOW)
        feed.reseed_session_low(211, 313.70)  # must not raise
        self.assertEqual(stream.reseed_calls, [])


class TestStreamHealthIsItsOwnCondition(unittest.TestCase):
    """#1397. The adapter asks TWO questions that used to be one: is the source
    hearing from the venue, and is this quote young enough to trade on.

    They were conflated in a single 3 s age bound, whose docstring justified
    itself as a dead-stream detector. Measured in session on 2026-09-10, uic
    641 was vetoed on 70% of samples with the stream demonstrably alive (its
    own restatement interval reaching 41.7 s), because the bound was really
    measuring how often Saxo speaks about THAT instrument.
    """

    def _parts(self, **quote_over):
        stream = _Stream(_quote(**quote_over))
        return stream, _feed(None, stream=stream)

    def test_a_live_stream_and_a_young_quote_produce_a_point(self):
        _, feed = self._parts()
        self.assertIsNotNone(feed.latest(211))

    def test_a_dark_stream_vetoes_even_a_perfectly_young_quote(self):
        """The condition the age bound used to stand in for, now asked
        directly — and answered immediately on a failed connection rather than
        after a timeout."""
        stream, feed = self._parts()
        stream.receiving = False
        self.assertIsNone(feed.latest(211))

    def test_a_live_stream_does_not_rescue_a_quote_past_the_age_bound(self):
        """The other half: liveness is not a licence to trade on any price.
        The bound still owns "how old may a price be"."""
        _, feed = self._parts(
            event_time=_NOW - dt.timedelta(seconds=price_feed.DEFAULT_MAX_AGE_S + 1.0)
        )
        self.assertIsNone(feed.latest(211))

    def test_a_quote_older_than_the_retired_three_second_bound_now_passes(self):
        """The behaviour change this ticket exists for, pinned against the
        MEASURED distribution rather than a round number: uic 641's median age
        in session was 4.7 s, which the old bound rejected."""
        _, feed = self._parts(event_time=_NOW - dt.timedelta(seconds=4.7))
        self.assertIsNotNone(feed.latest(211))

    def test_health_is_asked_per_call_rather_than_trusted_from_the_caller(self):
        """A precondition a caller must remember is a precondition that gets
        forgotten; the round trip is a local UNIX call."""
        stream, feed = self._parts()
        feed.latest(211)
        feed.latest(211)
        self.assertEqual(stream.receiving_calls, 2)

    def test_a_dark_stream_is_reported_once_rather_than_per_uic_per_tick(self):
        """Silence was the #1392 lesson; a line per uic per tick is the
        opposite failure. One throttled line names the veto."""
        stream, feed = self._parts()
        stream.receiving = False
        with self.assertLogs(
            "alphalens_pipeline.brokers.automanager.saxo_live_price_feed", level="WARNING"
        ) as caught:
            for _ in range(5):
                feed.latest(211)
        self.assertEqual(len(caught.records), 1, caught.output)


if __name__ == "__main__":
    unittest.main()
