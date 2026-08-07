from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.data.alt_data.saxo_price_stream import QuoteCache

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


if __name__ == "__main__":
    unittest.main()
