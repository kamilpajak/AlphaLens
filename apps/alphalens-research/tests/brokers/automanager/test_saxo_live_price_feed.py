from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager.saxo_live_price_feed import SaxoLivePriceFeed
from alphalens_pipeline.data.alt_data.saxo_price_stream import Quote

_NOW = dt.datetime(2026, 8, 7, 13, 48, 0, tzinfo=dt.UTC)


class _Stream:
    def __init__(self, quote: Quote | None):
        self._quote = quote
        self.subscribed: list[int] = []

    def ensure_subscribed(self, uics):
        self.subscribed = list(uics)

    def get(self, uic):
        return self._quote if self._quote and self._quote.uic == uic else None


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


def _feed(quote, *, sim_to_live=None):
    mapping = sim_to_live if sim_to_live is not None else {211: 211}
    return SaxoLivePriceFeed(
        stream=_Stream(quote),
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
        stale = _quote(event_time=_NOW - dt.timedelta(seconds=10))
        self.assertIsNone(_feed(stale).latest(211))

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


if __name__ == "__main__":
    unittest.main()
