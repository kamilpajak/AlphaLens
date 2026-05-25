"""Tests for T0 timing rules (after-hours / pre-market / regular-hours filings).

Eliminates intraday lookahead bias: a filing accepted at 16:30 ET cannot
be traded the same day. Per pre-reg ``t0_after_hours_rule``:
- hour < 16:00 ET (pre-market or regular hours): market reacts same day
- hour >= 16:00 ET or hour unknown: market reacts next trading day

The drift window [d+2, d+60] is computed from the market-reaction day, NOT
from the filing day. So an after-hours filing on Tue effectively shifts
entry to Mon (Tue + 4 trading days, because Tue is announce-day, Wed is
reaction-day, Fri is entry).
"""

from __future__ import annotations

import unittest
from datetime import date


class _StubCalendar:
    """Minimal trading calendar for tests: skip Sat/Sun only (no holidays)."""

    def snap_to_trading_day(self, d: date) -> date:
        # If d is Sat -> Mon; Sun -> Mon; otherwise unchanged.
        wd = d.weekday()
        if wd == 5:
            return date.fromordinal(d.toordinal() + 2)
        if wd == 6:
            return date.fromordinal(d.toordinal() + 1)
        return d

    def next_trading_day(self, d: date) -> date:
        n = self.snap_to_trading_day(d)
        # advance one calendar day, then snap forward
        return self.snap_to_trading_day(date.fromordinal(n.toordinal() + 1))

    def add_trading_days(self, d: date, n: int) -> date:
        cur = self.snap_to_trading_day(d)
        if n == 0:
            return cur
        if n > 0:
            for _ in range(n):
                cur = self.next_trading_day(cur)
            return cur
        # negative n: walk backward
        for _ in range(-n):
            prev = date.fromordinal(cur.toordinal() - 1)
            wd = prev.weekday()
            if wd == 5:
                prev = date.fromordinal(prev.toordinal() - 1)
            elif wd == 6:
                prev = date.fromordinal(prev.toordinal() - 2)
            cur = prev
        return cur


class TestMarketAnnouncementDay(unittest.TestCase):
    """Map (filed_date, hour) to the day the market first reacts."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.t0_timing import (
            market_announcement_day,
        )

        self._fn = market_announcement_day
        self._cal = _StubCalendar()

    def test_after_hours_shifts_to_next_trading_day(self):
        # Tue 2024-08-06 16:30 ET -> Wed 2024-08-07 (next trading day)
        result = self._fn(date(2024, 8, 6), 16, calendar=self._cal)
        self.assertEqual(result, date(2024, 8, 7))

    def test_at_close_4pm_treated_as_after_hours(self):
        # 16:00 boundary -> after-hours (PEAD convention: day reaction is next day)
        result = self._fn(date(2024, 8, 6), 16, calendar=self._cal)
        self.assertEqual(result, date(2024, 8, 7))

    def test_late_afternoon_pre_close_is_intraday(self):
        # 15:55 ET -> still during regular session -> same day
        result = self._fn(date(2024, 8, 6), 15, calendar=self._cal)
        self.assertEqual(result, date(2024, 8, 6))

    def test_regular_hours_is_intraday(self):
        # 11 ET on Wed 2024-08-07 -> same day
        result = self._fn(date(2024, 8, 7), 11, calendar=self._cal)
        self.assertEqual(result, date(2024, 8, 7))

    def test_pre_market_is_same_day(self):
        # 7 ET on Wed 2024-08-07 -> same day (open will reflect)
        result = self._fn(date(2024, 8, 7), 7, calendar=self._cal)
        self.assertEqual(result, date(2024, 8, 7))

    def test_unknown_hour_is_conservative_after_hours(self):
        # hour None -> conservative default: next trading day
        result = self._fn(date(2024, 8, 6), None, calendar=self._cal)
        self.assertEqual(result, date(2024, 8, 7))

    def test_friday_after_hours_skips_weekend(self):
        # Fri 2024-08-09 17:00 ET -> Mon 2024-08-12
        result = self._fn(date(2024, 8, 9), 17, calendar=self._cal)
        self.assertEqual(result, date(2024, 8, 12))

    def test_filed_on_saturday_snaps_to_monday(self):
        # Saturday filing (rare but possible if SEC accepts on weekend) ->
        # snap to Monday, treated as same-day if hour < 16, next-day otherwise.
        result = self._fn(date(2024, 8, 10), 11, calendar=self._cal)  # Sat
        self.assertEqual(result, date(2024, 8, 12))  # Monday


class TestDriftEntryDay(unittest.TestCase):
    """Compose market_announcement_day + Engelberg's [d+2, d+60] holding window."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.t0_timing import drift_entry_day

        self._fn = drift_entry_day
        self._cal = _StubCalendar()

    def test_intraday_announcement_entry_is_market_day_plus_2(self):
        # Wed 2024-08-07 11:00 ET -> announce-day = Wed -> entry = Fri (Wed + 2 trading days)
        result = self._fn(date(2024, 8, 7), 11, calendar=self._cal, skip_days=2)
        self.assertEqual(result, date(2024, 8, 9))

    def test_after_hours_announcement_entry_shifts_one_extra_day(self):
        # Wed 2024-08-07 17:00 ET -> announce-day = Thu -> entry = Mon (Thu + 2 trading days)
        result = self._fn(date(2024, 8, 7), 17, calendar=self._cal, skip_days=2)
        self.assertEqual(result, date(2024, 8, 12))


class TestDriftExitDay(unittest.TestCase):
    """Compose market_announcement_day + d+60 exit window."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.t0_timing import drift_exit_day

        self._fn = drift_exit_day
        self._cal = _StubCalendar()

    def test_exit_is_60_trading_days_after_market_announce_day(self):
        # Wed 2024-08-07 11:00 ET -> announce = Wed -> exit = 60 trading days later
        # 60 business days from Wed 2024-08-07 with no holidays: 2024-10-30
        result = self._fn(date(2024, 8, 7), 11, calendar=self._cal, exit_days=60)
        # Manual check: 60 weekdays from 2024-08-07 (Wed) = 2024-10-30 (Wed)
        self.assertEqual(result, date(2024, 10, 30))


if __name__ == "__main__":
    unittest.main()
