"""Unit tests for the trading-day-aware no-dispatch gauge state + computation.

The edgar-detect cron emits ``alphalens_edgar_trading_days_since_last_dispatch``
on every run. PromQL cannot consult a holiday calendar, so the calendar
awareness lives here (Python, where ``exchange_calendars`` ships the holiday
table). These tests pin the two halves:

* ``trading_days_between`` — a pure, exchange-aware count of sessions in the
  OPEN interval ``(start, end)`` (both endpoints excluded), so a pure weekend
  never increments and a holiday never increments.
* ``compute_trading_days_since_dispatch`` — the gauge value: 0 on a dispatch
  run / cold start, otherwise the trading-day gap between the last dispatch and
  today (today excluded — a dispatch may still arrive later today).
* ``load_last_dispatch_date`` / ``stamp_last_dispatch_date`` — durable
  ISO-date persistence under ``~/.alphalens/edgar-detect/dispatch_state.json``.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path


class TestTradingDaysBetween(unittest.TestCase):
    """Pure session-count helper over the OPEN interval ``(start, end)``."""

    def _fn(self):
        from alphalens_pipeline.edgar_detector.dispatch_state import trading_days_between

        return trading_days_between

    def test_weekend_only_gap_is_zero(self) -> None:
        # Fri 2026-01-16 dispatched; "today" = Mon 2026-01-19 (which is MLK,
        # itself a holiday). Open interval (Fri, Mon) holds only Sat + Sun, no
        # session -> 0. A pure weekend never increments the gauge.
        fri = dt.date(2026, 1, 16)
        mon = dt.date(2026, 1, 19)
        self.assertEqual(self._fn()(fri, mon), 0)

    def test_us_holiday_in_interval_does_not_count(self) -> None:
        # MLK day is Mon 2026-01-19 (NYSE closed). Dispatch on Fri 2026-01-16,
        # "today" = Tue 2026-01-20. Open interval (Fri, Tue) spans Sat, Sun,
        # Mon(MLK) -> still 0 sessions because none of the three is a session.
        fri = dt.date(2026, 1, 16)
        tue = dt.date(2026, 1, 20)
        self.assertEqual(self._fn()(fri, tue), 0)

    def test_single_trading_day_gap(self) -> None:
        # Dispatch Mon 2026-01-12, "today" = Wed 2026-01-14. Open interval
        # (Mon, Wed) holds only Tue 2026-01-13, a normal session -> 1.
        mon = dt.date(2026, 1, 12)
        wed = dt.date(2026, 1, 14)
        self.assertEqual(self._fn()(mon, wed), 1)

    def test_clean_business_week_counts_inner_sessions(self) -> None:
        # Dispatch Mon 2026-01-12, "today" = Mon 2026-01-19 (MLK). Open
        # interval (Mon, Mon) excludes both endpoints; inner sessions are
        # Tue/Wed/Thu/Fri 13-16 = 4 (Sat/Sun not sessions, both Mondays
        # excluded).
        start = dt.date(2026, 1, 12)
        end = dt.date(2026, 1, 19)
        self.assertEqual(self._fn()(start, end), 4)

    def test_end_not_after_start_clamps_to_zero(self) -> None:
        d = dt.date(2026, 1, 14)
        self.assertEqual(self._fn()(d, d), 0)
        self.assertEqual(self._fn()(d, dt.date(2026, 1, 13)), 0)


class TestComputeTradingDaysSinceDispatch(unittest.TestCase):
    """Gauge value across dispatch / quiet / cold-start cases."""

    def _fn(self):
        from alphalens_pipeline.edgar_detector.dispatch_state import (
            compute_trading_days_since_dispatch,
        )

        return compute_trading_days_since_dispatch

    def test_cold_start_returns_zero(self) -> None:
        # No persisted last_dispatch_date -> never emit a huge/Inf value.
        self.assertEqual(self._fn()(None, dt.date(2026, 1, 20)), 0)

    def test_dispatch_today_returns_zero(self) -> None:
        # last_dispatch_date == today (a dispatch run sets it to today) -> 0.
        today = dt.date(2026, 1, 14)
        self.assertEqual(self._fn()(today, today), 0)

    def test_today_is_excluded(self) -> None:
        # Dispatch Mon 2026-01-12; today = Wed 2026-01-14. Only Tue 13 counts;
        # Wed (today) is excluded because a dispatch may still arrive later
        # today.
        self.assertEqual(self._fn()(dt.date(2026, 1, 12), dt.date(2026, 1, 14)), 1)

    def test_weekend_gap_does_not_increment(self) -> None:
        # Dispatch Fri 2026-01-16; today = Mon 2026-01-19 (MLK). No session
        # strictly between -> 0.
        self.assertEqual(self._fn()(dt.date(2026, 1, 16), dt.date(2026, 1, 19)), 0)

    def test_multi_trading_day_gap(self) -> None:
        # Dispatch Mon 2026-01-12; today = Mon 2026-01-19 (MLK). Inner sessions
        # Tue-Fri 13-16 = 4.
        self.assertEqual(self._fn()(dt.date(2026, 1, 12), dt.date(2026, 1, 19)), 4)


class TestDispatchStatePersistence(unittest.TestCase):
    """Round-trip the ISO-date JSON state file under a temp home."""

    def test_load_missing_file_returns_none(self) -> None:
        from alphalens_pipeline.edgar_detector.dispatch_state import load_last_dispatch_date

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_last_dispatch_date(Path(tmp)))

    def test_stamp_then_load_round_trips(self) -> None:
        from alphalens_pipeline.edgar_detector.dispatch_state import (
            load_last_dispatch_date,
            stamp_last_dispatch_date,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            d = dt.date(2026, 1, 14)
            stamp_last_dispatch_date(home, d)
            self.assertEqual(load_last_dispatch_date(home), d)

    def test_stamp_writes_iso_date_string(self) -> None:
        import json

        from alphalens_pipeline.edgar_detector.dispatch_state import stamp_last_dispatch_date

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stamp_last_dispatch_date(home, dt.date(2026, 1, 14))
            payload = json.loads((home / "dispatch_state.json").read_text())
            self.assertEqual(payload["last_dispatch_date"], "2026-01-14")

    def test_stamp_overwrites_previous(self) -> None:
        from alphalens_pipeline.edgar_detector.dispatch_state import (
            load_last_dispatch_date,
            stamp_last_dispatch_date,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stamp_last_dispatch_date(home, dt.date(2026, 1, 12))
            stamp_last_dispatch_date(home, dt.date(2026, 1, 14))
            self.assertEqual(load_last_dispatch_date(home), dt.date(2026, 1, 14))

    def test_load_tolerates_corrupt_file(self) -> None:
        # A truncated/garbage file must not crash the cron run; treat as cold
        # start (None) so the next dispatch re-stamps cleanly.
        from alphalens_pipeline.edgar_detector.dispatch_state import load_last_dispatch_date

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "dispatch_state.json").write_text("{not json")
            self.assertIsNone(load_last_dispatch_date(home))


if __name__ == "__main__":
    unittest.main()
