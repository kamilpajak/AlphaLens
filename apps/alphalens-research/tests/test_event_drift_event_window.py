"""Tests for the rolling-60d event window with single-active-window invariant.

Pre-reg locks ``overlapping_window_policy: single_active_window_per_ticker_
hard_reset_on_expiry``. When an earnings announcement arrives while an
earlier window for the same ticker is still active, the new announcement
is DROPPED (not used to extend or replace the window). After natural
expiry at exit_day the next announcement opens a fresh window.
"""

from __future__ import annotations

import unittest
from datetime import date


class _StubCalendar:
    def snap_to_trading_day(self, d: date) -> date:
        wd = d.weekday()
        if wd == 5:
            return date.fromordinal(d.toordinal() + 2)
        if wd == 6:
            return date.fromordinal(d.toordinal() + 1)
        return d

    def next_trading_day(self, d: date) -> date:
        n = self.snap_to_trading_day(d)
        return self.snap_to_trading_day(date.fromordinal(n.toordinal() + 1))

    def add_trading_days(self, d: date, n: int) -> date:
        cur = self.snap_to_trading_day(d)
        if n == 0:
            return cur
        if n > 0:
            for _ in range(n):
                cur = self.next_trading_day(cur)
            return cur
        for _ in range(-n):
            prev = date.fromordinal(cur.toordinal() - 1)
            wd = prev.weekday()
            if wd == 5:
                prev = date.fromordinal(prev.toordinal() - 1)
            elif wd == 6:
                prev = date.fromordinal(prev.toordinal() - 2)
            cur = prev
        return cur


def _make_announcement(*, ticker, period_end, filed_date, hour=None, source="10-Q"):
    from alphalens_research.screeners.event_drift.announcement_dates import (
        EarningsAnnouncement,
    )

    return EarningsAnnouncement(
        ticker=ticker,
        period_end=period_end,
        filed_date=filed_date,
        accepted_hour_et=hour,
        source=source,
    )


class TestBuildEventWindows(unittest.TestCase):
    """Convert announcements + SUE/accruals lookups into EventWindow objects."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.event_window import build_event_windows

        self._fn = build_event_windows
        self._cal = _StubCalendar()

    def test_single_announcement_emits_one_window(self):
        ann = _make_announcement(
            ticker="AAPL",
            period_end=date(2024, 6, 30),
            filed_date=date(2024, 8, 1),  # Thu
            hour=11,
        )
        windows = self._fn(
            [ann],
            sue_lookup=lambda t, d: 2.5,
            accruals_lookup=lambda t, d: -0.02,
            calendar=self._cal,
            skip_days=2,
            exit_days=60,
        )
        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.ticker, "AAPL")
        self.assertEqual(w.market_day, date(2024, 8, 1))  # intraday filing
        self.assertEqual(w.entry_day, date(2024, 8, 5))  # Thu + 2 trading days = Mon
        # exit = market_day + 60 trading days from Thu 2024-08-01
        self.assertGreater(w.exit_day, w.entry_day)
        self.assertAlmostEqual(w.sue, 2.5)
        self.assertAlmostEqual(w.accruals_ratio, -0.02)

    def test_announcement_skipped_when_sue_or_accruals_missing(self):
        ann = _make_announcement(
            ticker="X",
            period_end=date(2024, 6, 30),
            filed_date=date(2024, 8, 1),
            hour=11,
        )
        # Missing SUE
        windows1 = self._fn(
            [ann],
            sue_lookup=lambda t, d: None,
            accruals_lookup=lambda t, d: -0.02,
            calendar=self._cal,
        )
        self.assertEqual(windows1, [])

        # Missing accruals
        windows2 = self._fn(
            [ann],
            sue_lookup=lambda t, d: 2.5,
            accruals_lookup=lambda t, d: None,
            calendar=self._cal,
        )
        self.assertEqual(windows2, [])


class TestSingleActiveWindowInvariant(unittest.TestCase):
    """Drop announcements that fall while an earlier window for the same ticker
    is still active."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.event_window import (
            apply_single_active_window,
            build_event_windows,
        )

        self._build = build_event_windows
        self._invariant = apply_single_active_window
        self._cal = _StubCalendar()

    def test_overlapping_announcements_drop_second(self):
        # Q1 announce 2024-08-01, window expires ~60 trading days later (~Oct).
        # Q2 announce 2024-09-15 (during Q1 window) -> DROPPED.
        ann_q1 = _make_announcement(
            ticker="AAPL",
            period_end=date(2024, 6, 30),
            filed_date=date(2024, 8, 1),
            hour=11,
        )
        ann_q2_overlap = _make_announcement(
            ticker="AAPL",
            period_end=date(2024, 9, 30),
            filed_date=date(2024, 9, 15),  # too soon — Q1 window still active
            hour=11,
        )
        windows = self._build(
            [ann_q1, ann_q2_overlap],
            sue_lookup=lambda t, d: 2.5,
            accruals_lookup=lambda t, d: -0.02,
            calendar=self._cal,
        )
        kept = self._invariant(windows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].market_day, date(2024, 8, 1))

    def test_non_overlapping_announcements_kept(self):
        # Q1 announce 2024-05-01, window ends ~Aug (60 trading days later).
        # Q2 announce 2024-08-15 (after Q1 window expired) -> KEPT.
        ann_q1 = _make_announcement(
            ticker="AAPL",
            period_end=date(2024, 3, 31),
            filed_date=date(2024, 5, 1),
            hour=11,
        )
        ann_q2 = _make_announcement(
            ticker="AAPL",
            period_end=date(2024, 6, 30),
            filed_date=date(2024, 8, 15),  # ~75 trading days after Q1 -> OK
            hour=11,
        )
        windows = self._build(
            [ann_q1, ann_q2],
            sue_lookup=lambda t, d: 2.5,
            accruals_lookup=lambda t, d: -0.02,
            calendar=self._cal,
        )
        kept = self._invariant(windows)
        self.assertEqual(len(kept), 2)
        self.assertEqual([w.market_day for w in kept], [date(2024, 5, 1), date(2024, 8, 15)])

    def test_invariant_independent_per_ticker(self):
        # AAPL Q1 window blocks AAPL Q2, but MSFT announcements are independent.
        aapl_q1 = _make_announcement(
            ticker="AAPL",
            period_end=date(2024, 6, 30),
            filed_date=date(2024, 8, 1),
            hour=11,
        )
        aapl_q2_overlap = _make_announcement(
            ticker="AAPL",
            period_end=date(2024, 9, 30),
            filed_date=date(2024, 9, 15),
            hour=11,
        )
        msft_q = _make_announcement(
            ticker="MSFT",
            period_end=date(2024, 6, 30),
            filed_date=date(2024, 8, 5),
            hour=11,
        )
        windows = self._build(
            [aapl_q1, aapl_q2_overlap, msft_q],
            sue_lookup=lambda t, d: 2.5,
            accruals_lookup=lambda t, d: -0.02,
            calendar=self._cal,
        )
        kept = self._invariant(windows)
        # AAPL Q1 kept, AAPL Q2 dropped, MSFT Q kept.
        kept_tickers = sorted({w.ticker for w in kept})
        self.assertEqual(kept_tickers, ["AAPL", "MSFT"])
        self.assertEqual(len(kept), 2)


class TestWindowActiveQuery(unittest.TestCase):
    """Query: which windows are active on a given asof date."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.event_window import (
            EventWindow,
            windows_active_on,
        )

        self._WIN = EventWindow
        self._fn = windows_active_on

    def test_window_is_active_inside_holding_range(self):
        w = self._WIN(
            ticker="AAPL",
            market_day=date(2024, 8, 1),
            entry_day=date(2024, 8, 5),
            exit_day=date(2024, 10, 24),
            sue=2.5,
            accruals_ratio=-0.02,
        )
        # asof inside [entry, exit] -> active
        self.assertEqual(self._fn([w], date(2024, 9, 15)), [w])

    def test_asof_before_entry_not_active(self):
        w = self._WIN(
            ticker="AAPL",
            market_day=date(2024, 8, 1),
            entry_day=date(2024, 8, 5),
            exit_day=date(2024, 10, 24),
            sue=2.5,
            accruals_ratio=-0.02,
        )
        # asof = 2024-08-04 (one day before entry) -> not yet active
        self.assertEqual(self._fn([w], date(2024, 8, 4)), [])

    def test_asof_after_exit_not_active(self):
        w = self._WIN(
            ticker="AAPL",
            market_day=date(2024, 8, 1),
            entry_day=date(2024, 8, 5),
            exit_day=date(2024, 10, 24),
            sue=2.5,
            accruals_ratio=-0.02,
        )
        # asof = 2024-10-25 (day after exit) -> expired
        self.assertEqual(self._fn([w], date(2024, 10, 25)), [])

    def test_asof_on_entry_day_is_active(self):
        w = self._WIN(
            ticker="AAPL",
            market_day=date(2024, 8, 1),
            entry_day=date(2024, 8, 5),
            exit_day=date(2024, 10, 24),
            sue=2.5,
            accruals_ratio=-0.02,
        )
        self.assertEqual(self._fn([w], date(2024, 8, 5)), [w])

    def test_asof_on_exit_day_is_active(self):
        w = self._WIN(
            ticker="AAPL",
            market_day=date(2024, 8, 1),
            entry_day=date(2024, 8, 5),
            exit_day=date(2024, 10, 24),
            sue=2.5,
            accruals_ratio=-0.02,
        )
        self.assertEqual(self._fn([w], date(2024, 10, 24)), [w])

    def test_multiple_active_windows_returned(self):
        w1 = self._WIN(
            ticker="AAPL",
            market_day=date(2024, 8, 1),
            entry_day=date(2024, 8, 5),
            exit_day=date(2024, 10, 24),
            sue=2.5,
            accruals_ratio=-0.02,
        )
        w2 = self._WIN(
            ticker="MSFT",
            market_day=date(2024, 8, 5),
            entry_day=date(2024, 8, 7),
            exit_day=date(2024, 10, 28),
            sue=1.8,
            accruals_ratio=-0.01,
        )
        active = self._fn([w1, w2], date(2024, 9, 1))
        self.assertEqual({w.ticker for w in active}, {"AAPL", "MSFT"})

    def test_empty_window_list_returns_empty(self):
        self.assertEqual(self._fn([], date(2024, 8, 5)), [])


if __name__ == "__main__":
    unittest.main()
