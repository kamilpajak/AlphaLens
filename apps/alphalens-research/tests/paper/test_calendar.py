"""Tests for ``alphalens_pipeline.paper.calendar`` — trading-day helpers
for paper-trade submission gating.

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``.

Anchor dates pinned to historical sessions so the suite is stable
against future calendar revisions to far-out years:

  XNYS:
    * 2025-07-03 — half-day (early close 13:00 ET).
    * 2025-11-28 — half-day (Black Friday).
    * 2025-12-24 — half-day (Christmas Eve).
    * 2025-12-25 — full holiday (Christmas Day).
    * 2026-05-25 — full holiday (Memorial Day, Monday).
    * 2026-05-29 — normal session (Friday).
    * 2026-06-05 / 2026-06-12 — two consecutive holiday-free Fridays.

  XWAR (Warsaw — exercised so the multi-exchange API stays honest):
    * 2025-01-06 — Three Kings Day, Polish public holiday, GPW closed.
    * 2025-01-07 — normal Polish session (Tuesday).
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    is_half_day,
    is_trading_day,
    n_sessions_before,
    next_trading_open,
    previous_trading_day,
    session_on_or_after,
    session_open_utc,
    trading_days_elapsed,
)

# ---------------------------------------------------------------- is_trading_day


class TestIsTradingDayXNYS(unittest.TestCase):
    def test_normal_friday_is_trading_day(self):
        self.assertTrue(is_trading_day(dt.date(2026, 5, 29)))

    def test_normal_monday_is_trading_day(self):
        self.assertTrue(is_trading_day(dt.date(2026, 6, 1)))

    def test_saturday_is_not_trading_day(self):
        self.assertFalse(is_trading_day(dt.date(2026, 5, 30)))

    def test_sunday_is_not_trading_day(self):
        self.assertFalse(is_trading_day(dt.date(2026, 5, 31)))

    def test_memorial_day_2026_is_not_trading_day(self):
        # Monday holiday — closed.
        self.assertFalse(is_trading_day(dt.date(2026, 5, 25)))

    def test_christmas_2025_is_not_trading_day(self):
        self.assertFalse(is_trading_day(dt.date(2025, 12, 25)))

    def test_half_day_still_counts_as_trading_day(self):
        # Half-days are trading days for gating purposes; only the close
        # time differs. Submission/reconcile must still run on them.
        self.assertTrue(is_trading_day(dt.date(2025, 7, 3)))
        self.assertTrue(is_trading_day(dt.date(2025, 12, 24)))

    def test_accepts_datetime_input(self):
        # Caller may pass a naive datetime (e.g. ``dt.datetime.utcnow()``).
        self.assertTrue(is_trading_day(dt.datetime(2026, 5, 29, 12, 0, 0)))

    def test_default_exchange_is_xnys(self):
        self.assertEqual(DEFAULT_EXCHANGE, "XNYS")


class TestIsTradingDayMultiExchange(unittest.TestCase):
    """Sanity-check the multi-exchange API on Warsaw (XWAR). The
    paper harness will not actually route to GPW today, but pinning a
    non-default venue keeps the parametric API honest — a future
    refactor that accidentally hard-codes XNYS would fail these.
    """

    def test_xwar_three_kings_day_is_not_trading_day(self):
        # 2025-01-06 — Epiphany (Three Kings Day), Polish public holiday;
        # GPW closed but NYSE open.
        self.assertFalse(is_trading_day(dt.date(2025, 1, 6), exchange="XWAR"))
        self.assertTrue(is_trading_day(dt.date(2025, 1, 6), exchange="XNYS"))

    def test_xwar_normal_tuesday_is_trading_day(self):
        self.assertTrue(is_trading_day(dt.date(2025, 1, 7), exchange="XWAR"))


# ---------------------------------------------------------------- is_half_day


class TestIsHalfDay(unittest.TestCase):
    def test_christmas_eve_2025_is_half_day(self):
        self.assertTrue(is_half_day(dt.date(2025, 12, 24)))

    def test_black_friday_2025_is_half_day(self):
        self.assertTrue(is_half_day(dt.date(2025, 11, 28)))

    def test_july_3_2025_is_half_day(self):
        self.assertTrue(is_half_day(dt.date(2025, 7, 3)))

    def test_normal_friday_is_not_half_day(self):
        self.assertFalse(is_half_day(dt.date(2026, 5, 29)))

    def test_weekend_is_not_half_day(self):
        self.assertFalse(is_half_day(dt.date(2026, 5, 30)))

    def test_holiday_is_not_half_day(self):
        # Memorial Day is a full closure, not a half-day.
        self.assertFalse(is_half_day(dt.date(2026, 5, 25)))


# ---------------------------------------------------------------- next_trading_open


class TestNextTradingOpen(unittest.TestCase):
    def test_friday_evening_returns_monday_open(self):
        # Friday 22:00 UTC → next XNYS open is Monday 13:30 UTC (09:30 ET).
        ts = dt.datetime(2026, 5, 29, 22, 0, 0, tzinfo=dt.UTC)
        nxt = next_trading_open(ts)
        self.assertEqual(nxt.tzinfo, dt.UTC)
        self.assertEqual(nxt.date(), dt.date(2026, 6, 1))
        self.assertEqual(nxt.hour, 13)
        self.assertEqual(nxt.minute, 30)

    def test_saturday_returns_monday_open(self):
        ts = dt.datetime(2026, 5, 30, 12, 0, 0, tzinfo=dt.UTC)
        nxt = next_trading_open(ts)
        self.assertEqual(nxt.date(), dt.date(2026, 6, 1))

    def test_friday_before_long_weekend_skips_memorial_day(self):
        # Fri 2026-05-22 22:00 UTC → Mon 5-25 is Memorial Day → next open
        # is Tue 2026-05-26 13:30 UTC.
        ts = dt.datetime(2026, 5, 22, 22, 0, 0, tzinfo=dt.UTC)
        nxt = next_trading_open(ts)
        self.assertEqual(nxt.date(), dt.date(2026, 5, 26))

    def test_after_open_before_close_returns_next_session_open(self):
        # During the session itself, "next open" means *next* session, not
        # this one. Caller usually wants ``is_trading_day(today)`` for the
        # "are we live right now" question; this helper exists for the
        # "submission was deferred; when will it next be attempted?" path.
        ts = dt.datetime(2026, 5, 29, 18, 0, 0, tzinfo=dt.UTC)  # 14:00 ET, mid-session
        nxt = next_trading_open(ts)
        self.assertEqual(nxt.date(), dt.date(2026, 6, 1))

    def test_naive_datetime_treated_as_utc(self):
        ts = dt.datetime(2026, 5, 30, 12, 0, 0)  # naive
        nxt = next_trading_open(ts)
        self.assertEqual(nxt.date(), dt.date(2026, 6, 1))
        self.assertEqual(nxt.tzinfo, dt.UTC)


# ---------------------------------------------------------------- previous_trading_day


class TestPreviousTradingDay(unittest.TestCase):
    def test_monday_returns_previous_friday(self):
        self.assertEqual(
            previous_trading_day(dt.date(2026, 6, 1)),
            dt.date(2026, 5, 29),
        )

    def test_saturday_returns_previous_friday(self):
        self.assertEqual(
            previous_trading_day(dt.date(2026, 5, 30)),
            dt.date(2026, 5, 29),
        )

    def test_tuesday_after_memorial_day_returns_previous_friday(self):
        # Tue 5-26 → previous trading day is Fri 5-22 (skipping Sat/Sun
        # 5-23/5-24 + Memorial Day Mon 5-25).
        self.assertEqual(
            previous_trading_day(dt.date(2026, 5, 26)),
            dt.date(2026, 5, 22),
        )

    def test_trading_day_input_returns_previous_session(self):
        # Even if input itself is a session, return the prior one.
        self.assertEqual(
            previous_trading_day(dt.date(2026, 5, 29)),
            dt.date(2026, 5, 28),
        )


# ---------------------------------------------------------------- trading_days_elapsed


class TestTradingDaysElapsed(unittest.TestCase):
    """Half-open ``(start, end]`` count of sessions, matching the
    "trading days elapsed since plan" reading the reconciler uses.
    """

    def test_same_day_is_zero(self):
        d = dt.date(2026, 5, 29)
        self.assertEqual(trading_days_elapsed(d, d), 0)

    def test_friday_to_monday_is_one_trading_day(self):
        # Fri close → Mon close = 1 trading day elapsed (just Monday).
        self.assertEqual(
            trading_days_elapsed(dt.date(2026, 5, 29), dt.date(2026, 6, 1)),
            1,
        )

    def test_clean_friday_to_friday_one_week_is_five_trading_days(self):
        # Pick a span with NO US holidays — first two June 2026 Fridays.
        self.assertEqual(
            trading_days_elapsed(dt.date(2026, 6, 5), dt.date(2026, 6, 12)),
            5,
        )

    def test_window_with_memorial_day_is_four_trading_days(self):
        # Fri 5-22 → Fri 5-29 spans Mon 5-25 Memorial Day; only Tue/Wed/
        # Thu/Fri-5-29 elapsed. The legacy ``.days`` arithmetic would
        # return 7 here; that calendar-day inflation is exactly the bug
        # PR-B will switch over to this helper to fix.
        self.assertEqual(
            trading_days_elapsed(dt.date(2026, 5, 22), dt.date(2026, 5, 29)),
            4,
        )

    def test_returns_zero_when_end_before_start(self):
        # Caller bug guard — non-monotonic input clamps to zero rather
        # than returning a negative count which the TTL sweep would
        # then mis-interpret as "TTL exceeded".
        self.assertEqual(
            trading_days_elapsed(dt.date(2026, 5, 29), dt.date(2026, 5, 22)),
            0,
        )

    def test_weekend_to_weekend_skips_non_sessions(self):
        # Sat → Sat next clean-week: only the 5 weekday sessions count.
        self.assertEqual(
            trading_days_elapsed(dt.date(2026, 6, 6), dt.date(2026, 6, 13)),
            5,
        )


# ---------------------------------------------------------------- session_on_or_after


class TestSessionOnOrAfterXNYS(unittest.TestCase):
    def test_trading_day_returns_itself(self):
        # 2026-05-29 is a normal Friday session — on-or-after is the day itself
        # (unlike next_trading_open, which would skip to the following session).
        self.assertEqual(session_on_or_after(dt.date(2026, 5, 29)), dt.date(2026, 5, 29))

    def test_saturday_rolls_to_monday(self):
        self.assertEqual(session_on_or_after(dt.date(2026, 5, 30)), dt.date(2026, 6, 1))

    def test_holiday_rolls_forward(self):
        # Memorial Day Monday 2026-05-25 closed -> next session Tue 2026-05-26.
        self.assertEqual(session_on_or_after(dt.date(2026, 5, 25)), dt.date(2026, 5, 26))


# ---------------------------------------------------------------- advance_trading_sessions


class TestAdvanceTradingSessionsXNYS(unittest.TestCase):
    def test_zero_returns_session_on_or_after(self):
        self.assertEqual(advance_trading_sessions(dt.date(2026, 5, 30), 0), dt.date(2026, 6, 1))

    def test_one_session_after_friday_skips_weekend(self):
        self.assertEqual(advance_trading_sessions(dt.date(2026, 5, 29), 1), dt.date(2026, 6, 1))

    def test_five_sessions_spanning_memorial_day(self):
        # Base Fri 2026-05-22; +1 Tue 5-26 (Mon 5-25 holiday skipped), +2 Wed,
        # +3 Thu, +4 Fri 5-29, +5 Mon 2026-06-01. Calendar-day math would land
        # on 5-27; trading-session advance lands on 6-01.
        self.assertEqual(advance_trading_sessions(dt.date(2026, 5, 22), 5), dt.date(2026, 6, 1))

    def test_negative_n_rejected(self):
        with self.assertRaises(ValueError):
            advance_trading_sessions(dt.date(2026, 5, 29), -1)


# ---------------------------------------------------------------- n_sessions_before


class TestNSessionsBeforeXNYS(unittest.TestCase):
    """The lookback analogue of advance_trading_sessions — locates the O'Neil RS
    trailing-return reference date n sessions before the session on-or-before asof."""

    def test_zero_on_session_is_identity(self):
        self.assertEqual(n_sessions_before(dt.date(2026, 6, 12), 0), dt.date(2026, 6, 12))

    def test_five_sessions_clean_week_friday_to_friday(self):
        self.assertEqual(n_sessions_before(dt.date(2026, 6, 12), 5), dt.date(2026, 6, 5))

    def test_five_sessions_spanning_memorial_day(self):
        # Base Fri 2026-06-01; -1 Fri 5-29, -2 Thu, -3 Wed, -4 Tue 5-26 (Mon 5-25
        # holiday skipped), -5 Fri 2026-05-22. Calendar-day math would land 5-27;
        # the session lookback lands on 5-22.
        self.assertEqual(n_sessions_before(dt.date(2026, 6, 1), 5), dt.date(2026, 5, 22))

    def test_252_sessions_is_about_one_year(self):
        # 12-month RS lookback: ~252 trading sessions ≈ one calendar year earlier.
        self.assertEqual(n_sessions_before(dt.date(2026, 6, 12), 252), dt.date(2025, 6, 11))

    def test_non_session_asof_rolls_back_to_prior_session(self):
        # ANCHOR ASYMMETRY: a weekend/holiday asof rolls BACK (not forward) so the
        # lookback anchors on the last close on-or-before it. Sun 2026-06-14 -> Fri 6-12.
        self.assertEqual(n_sessions_before(dt.date(2026, 6, 14), 0), dt.date(2026, 6, 12))

    def test_negative_n_rejected(self):
        with self.assertRaises(ValueError):
            n_sessions_before(dt.date(2026, 6, 12), -1)

    def test_xwar_parity(self):
        # Works for a different MIC (exchange-parametrized helper).
        result = n_sessions_before(dt.date(2026, 6, 12), 5, exchange="XWAR")
        self.assertIsInstance(result, dt.date)
        self.assertLess(result, dt.date(2026, 6, 12))


# ---------------------------------------------------------------- session_open_utc


class TestSessionOpenUtcXNYS(unittest.TestCase):
    def test_summer_session_opens_1330_utc(self):
        # 2026-05-29 is EDT: 09:30 ET == 13:30 UTC.
        open_utc = session_open_utc(dt.date(2026, 5, 29))
        self.assertEqual(open_utc.tzinfo, dt.UTC)
        self.assertEqual((open_utc.hour, open_utc.minute), (13, 30))
        self.assertEqual(open_utc.date(), dt.date(2026, 5, 29))

    def test_non_session_date_raises(self):
        # Unlike session_on_or_after, this requires an EXACT session date so a
        # caller can't silently anchor a window to the wrong day.
        with self.assertRaises(ValueError):
            session_open_utc(dt.date(2026, 5, 30))  # Saturday


# ---------------------------------------------------------------- XWAR parity


class TestSessionHelpersXWAR(unittest.TestCase):
    def test_session_on_or_after_xwar_holiday(self):
        # Three Kings Day 2025-01-06 closed on GPW -> next session 2025-01-07.
        self.assertEqual(
            session_on_or_after(dt.date(2025, 1, 6), exchange="XWAR"), dt.date(2025, 1, 7)
        )

    def test_advance_one_session_xwar(self):
        self.assertEqual(
            advance_trading_sessions(dt.date(2025, 1, 6), 1, exchange="XWAR"), dt.date(2025, 1, 8)
        )


if __name__ == "__main__":
    unittest.main()
