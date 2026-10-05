"""One validity window per pick, counted from its trade date (#1734).

The window is what an armed pick has to be placed in, and what an entry placed
late is allowed to rest for. It is anchored on ``meta.trade_date`` so waiting
never starts a new one.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager import pick_window as pw
from broker_contract.constants import MAX_ORDER_TTL_DAYS


class PickWindowTest(unittest.TestCase):
    def test_a_stated_zero_means_the_default_seven_sessions(self) -> None:
        window = pw.pick_window(dt.date(2026, 9, 30), 0, "XNYS")
        self.assertEqual(window.last_session, dt.date(2026, 10, 9))
        self.assertEqual(window.window_end, dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.UTC))

    def test_seven_and_zero_are_the_same_window(self) -> None:
        self.assertEqual(
            pw.pick_window(dt.date(2026, 9, 30), 7, "XNYS"),
            pw.pick_window(dt.date(2026, 9, 30), 0, "XNYS"),
        )

    def test_a_stated_ttl_is_honoured(self) -> None:
        window = pw.pick_window(dt.date(2026, 9, 30), 3, "XNYS")
        self.assertEqual(window.last_session, dt.date(2026, 10, 5))

    def test_the_close_is_the_venue_s_own(self) -> None:
        # XWAR closes at 17:00 local, 15:00 UTC in October: a venue whose hours
        # differ from New York is the only fixture that tells the MICs apart.
        window = pw.pick_window(dt.date(2026, 9, 30), 7, "XWAR")
        self.assertEqual(window.window_end, dt.datetime(2026, 10, 9, 15, 0, tzinfo=dt.UTC))

    def test_a_trade_date_on_a_weekend_rolls_forward_first(self) -> None:
        # Saturday 2026-10-03: the first session is Monday, then seven more.
        window = pw.pick_window(dt.date(2026, 10, 3), 7, "XNYS")
        self.assertEqual(window.last_session, dt.date(2026, 10, 14))

    def test_an_iso_string_trade_date_is_accepted(self) -> None:
        self.assertEqual(
            pw.pick_window("2026-09-30", 7, "XNYS"),
            pw.pick_window(dt.date(2026, 9, 30), 7, "XNYS"),
        )

    def test_a_negative_ttl_is_refused_not_raised_from_the_calendar(self) -> None:
        with self.assertRaises(pw.PickWindowError):
            pw.pick_window(dt.date(2026, 9, 30), -3, "XNYS")

    def test_a_ttl_beyond_the_bound_is_refused(self) -> None:
        with self.assertRaises(pw.PickWindowError):
            pw.pick_window(dt.date(2026, 9, 30), MAX_ORDER_TTL_DAYS + 1, "XNYS")

    def test_the_bound_itself_is_a_window(self) -> None:
        window = pw.pick_window(dt.date(2026, 9, 30), MAX_ORDER_TTL_DAYS, "XNYS")
        self.assertGreater(window.last_session, dt.date(2026, 12, 1))

    def test_a_venue_with_no_calendar_is_refused(self) -> None:
        with self.assertRaises(pw.PickWindowError):
            pw.pick_window(dt.date(2026, 9, 30), 7, "XNOPE")

    def test_a_date_past_the_calendar_is_refused(self) -> None:
        with self.assertRaises(pw.PickWindowError):
            pw.pick_window(dt.date(2031, 1, 6), 7, "XNYS")

    def test_window_of_reads_the_document(self) -> None:
        intent = _intent(trade_date="2026-09-30", ttl=3, mic="XNAS")
        self.assertEqual(pw.window_of(intent), pw.pick_window(dt.date(2026, 9, 30), 3, "XNAS"))


class RemainingSessionsTest(unittest.TestCase):
    """The GTD session count a late placement gets: what is LEFT of the window."""

    def setUp(self) -> None:
        self.window = pw.pick_window(dt.date(2026, 9, 30), 7, "XNYS")

    def test_on_the_trade_date_the_whole_window_remains(self) -> None:
        self.assertEqual(pw.remaining_sessions(self.window, dt.date(2026, 9, 30), "XNYS"), 7)

    def test_five_sessions_in_two_remain(self) -> None:
        # 2026-10-07 is the fifth session after 09-30; the window ends 10-09.
        self.assertEqual(pw.remaining_sessions(self.window, dt.date(2026, 10, 7), "XNYS"), 2)

    def test_on_the_last_session_zero_remain(self) -> None:
        self.assertEqual(pw.remaining_sessions(self.window, dt.date(2026, 10, 9), "XNYS"), 0)

    def test_past_the_last_session_nothing_remains(self) -> None:
        self.assertIsNone(pw.remaining_sessions(self.window, dt.date(2026, 10, 12), "XNYS"))

    def test_a_weekend_day_counts_from_the_next_session(self) -> None:
        # Saturday 10-03 -> Monday 10-05; 10-06..10-09 are four more.
        self.assertEqual(pw.remaining_sessions(self.window, dt.date(2026, 10, 3), "XNYS"), 4)

    def test_the_weekend_after_the_last_session_has_nothing_left(self) -> None:
        self.assertIsNone(pw.remaining_sessions(self.window, dt.date(2026, 10, 10), "XNYS"))

    def test_the_count_lands_on_the_last_session_as_the_adapter_walks_it(self) -> None:
        # The Saxo adapter computes the GTD date as advance_trading_sessions(today, n):
        # the count is correct exactly when that walk lands on the window's last session.
        from alphalens_pipeline.market.calendar import advance_trading_sessions

        for day in range(30):
            today = dt.date(2026, 9, 28) + dt.timedelta(days=day)
            with self.subTest(today=today):
                n = pw.remaining_sessions(self.window, today, "XNYS")
                if n is None:
                    self.assertGreater(today, self.window.last_session)
                    continue
                self.assertEqual(
                    advance_trading_sessions(today, n, exchange="XNYS"), self.window.last_session
                )

    def test_a_calendar_failure_is_none_not_an_exception(self) -> None:
        self.assertIsNone(pw.remaining_sessions(self.window, dt.date(2026, 10, 7), "XNOPE"))


def _intent(*, trade_date: str, ttl: int, mic: str) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        meta=SimpleNamespace(trade_date=trade_date),
        spec=SimpleNamespace(order_ttl_days=ttl),
        instrument=SimpleNamespace(mic=mic),
    )


if __name__ == "__main__":
    unittest.main()
