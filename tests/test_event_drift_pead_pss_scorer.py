"""Tests for the PEAD daily-rebalance adapter (paradigm-14 B2 + B3).

CONVENTION: ``weights[t]`` represents the position held DURING day ``t``'s
session, capturing ``returns[t] = close(t) / close(t-1) - 1``. A position
OPENED at close(t) has no daytime exposure on day t, so weight[t]=0; first
non-zero weight is day t+1 (which captures the close(t)→close(t+1) return).

B2 timing assertions per plan §1.B3 (zen-mandated):
  - pre-market event at day t → portfolio captures close(t)→close(t+1)
    return as the day-t+1 P&L entry (weight[t]=0, weight[t+1]=1/n_fixed).
  - post-market event at day t → portfolio has ZERO exposure to
    close(t)→close(t+1) (weight[t]=0 AND weight[t+1]=0). First captured
    return is close(t+1)→close(t+2) as the day-t+2 P&L (weight[t+2]=1/n_fixed).

B3 end-to-end test: 3-stock dummy matrix with hard-coded events + returns.
"""

from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from alphalens.screeners.event_drift.av_earnings_ingestion import (
    AVEarningsAnnouncement,
)


def _trading_days(start: date, end: date) -> list[date]:
    """Weekday range, no holidays. Fixture dates are chosen to avoid weekends."""
    return [d.date() for d in pd.bdate_range(start=start, end=end)]


def _event(
    ticker: str,
    reported: date,
    *,
    report_time: str = "post-market",
    rep: float = 1.10,
    est: float = 1.00,
    period_end: date | None = None,
) -> AVEarningsAnnouncement:
    return AVEarningsAnnouncement(
        ticker=ticker,
        period_end=period_end or date(reported.year, max(1, reported.month - 1), 1),
        reported_date=reported,
        reported_eps=rep,
        estimated_eps=est,
        report_time=report_time,  # type: ignore[arg-type]
    )


class TestEntryDay(unittest.TestCase):
    """Entry day = day on whose CLOSE the position is opened. Pre-market
    enters at close of reported_date; post-market at close of NEXT trading
    day. (Both have zero daytime exposure on entry_day per convention.)"""

    def test_premarket_event_enters_at_close_of_report_day(self) -> None:
        from alphalens.screeners.event_drift.pead_pss_scorer import compute_entry_day

        calendar = _trading_days(date(2020, 6, 1), date(2020, 6, 30))
        event = _event("A", date(2020, 6, 10), report_time="pre-market")  # Wed
        self.assertEqual(compute_entry_day(event, calendar), date(2020, 6, 10))

    def test_postmarket_event_enters_at_close_of_next_trading_day(self) -> None:
        from alphalens.screeners.event_drift.pead_pss_scorer import compute_entry_day

        calendar = _trading_days(date(2020, 6, 1), date(2020, 6, 30))
        event = _event("A", date(2020, 6, 10), report_time="post-market")  # Wed
        self.assertEqual(compute_entry_day(event, calendar), date(2020, 6, 11))  # Thu

    def test_postmarket_friday_event_enters_at_close_of_monday(self) -> None:
        """Trading-day arithmetic must skip weekends."""
        from alphalens.screeners.event_drift.pead_pss_scorer import compute_entry_day

        calendar = _trading_days(date(2020, 6, 1), date(2020, 6, 30))
        friday_event = _event("A", date(2020, 6, 12), report_time="post-market")
        self.assertEqual(compute_entry_day(friday_event, calendar), date(2020, 6, 15))  # Mon

    def test_premarket_weekend_event_rolls_forward_to_next_trading_day(self) -> None:
        from alphalens.screeners.event_drift.pead_pss_scorer import compute_entry_day

        calendar = _trading_days(date(2020, 6, 1), date(2020, 6, 30))
        weekend_event = _event("A", date(2020, 6, 13), report_time="pre-market")  # Sat
        self.assertEqual(compute_entry_day(weekend_event, calendar), date(2020, 6, 15))


class TestExitDay(unittest.TestCase):
    def test_exit_is_entry_plus_hold_days_trading_days(self) -> None:
        """Exit close = entry close + hold_days trading days. Position
        captures hold_days returns (close(entry+1)..close(entry+hold_days))."""
        from alphalens.screeners.event_drift.pead_pss_scorer import compute_exit_day

        calendar = _trading_days(date(2020, 6, 1), date(2020, 8, 31))
        # Entry Wed 2020-06-10. +20 trading days: 06-11..06-12, 06-15..06-19,
        # 06-22..06-26, 06-29..06-30, 07-01..07-03, 07-06..07-08 → 2020-07-08.
        self.assertEqual(
            compute_exit_day(entry_day=date(2020, 6, 10), calendar=calendar, hold_days=20),
            date(2020, 7, 8),
        )

    def test_exit_handles_hold_days_extending_past_calendar(self) -> None:
        """Silent truncation hides PIT bugs; raise instead."""
        from alphalens.screeners.event_drift.pead_pss_scorer import compute_exit_day

        calendar = _trading_days(date(2020, 6, 1), date(2020, 6, 15))
        with self.assertRaises(ValueError):
            compute_exit_day(entry_day=date(2020, 6, 10), calendar=calendar, hold_days=20)


class TestDailyWeightsTiming(unittest.TestCase):
    """Plan §1.B3 timing assertions: explicit, verified."""

    def test_premarket_event_at_t_has_zero_weight_on_t_first_active_is_tplus1(
        self,
    ) -> None:
        """Pre-market entry at close(t): position has no daytime exposure on
        t. First captured return = close(t)→close(t+1), which lands as
        weight[t+1] · returns[t+1]."""
        from alphalens.screeners.event_drift.pead_pss_scorer import build_daily_weights

        calendar = _trading_days(date(2020, 6, 1), date(2020, 7, 31))
        event = _event("A", date(2020, 6, 10), report_time="pre-market")  # Wed
        weights = build_daily_weights(events=[event], calendar=calendar, n_fixed=150, hold_days=20)

        # Day t itself: ZERO (no daytime exposure).
        self.assertEqual(
            weights.loc[date(2020, 6, 10), "A"],
            0.0,
            "pre-market entry day must have zero weight — position is opened "
            "at close, no daytime returns are captured on day t.",
        )
        # Day t+1: 1/150 (first captured return is close(t)→close(t+1)).
        self.assertAlmostEqual(weights.loc[date(2020, 6, 11), "A"], 1 / 150)

    def test_postmarket_event_at_t_does_not_capture_t_to_tplus1_return(self) -> None:
        """Post-market: entry at close(t+1). Weight[t]=0 AND weight[t+1]=0
        (entry-day-itself has no daytime exposure under our convention).
        First captured return is close(t+1)→close(t+2) → weight[t+2]=1/150."""
        from alphalens.screeners.event_drift.pead_pss_scorer import build_daily_weights

        calendar = _trading_days(date(2020, 6, 1), date(2020, 7, 31))
        event = _event("A", date(2020, 6, 10), report_time="post-market")  # Wed
        weights = build_daily_weights(events=[event], calendar=calendar, n_fixed=150, hold_days=20)

        # Day t: ZERO (report not yet known at the close).
        self.assertEqual(weights.loc[date(2020, 6, 10), "A"], 0.0)
        # Day t+1: still ZERO — entry happened at close(t+1), no daytime
        # exposure on day t+1. Crucially: the t→t+1 return is NOT captured.
        self.assertEqual(
            weights.loc[date(2020, 6, 11), "A"],
            0.0,
            "post-market entry: day t+1 weight must be ZERO; otherwise the "
            "close(t)→close(t+1) intraday move the report caused leaks into "
            "the portfolio — classic post-announcement lookahead.",
        )
        # Day t+2: 1/150 (first full-day exposure, captures close(t+1)→close(t+2)).
        self.assertAlmostEqual(weights.loc[date(2020, 6, 12), "A"], 1 / 150)

    def test_index_alignment_no_off_by_one(self) -> None:
        """DataFrame index must equal calendar exactly. ±1 shift would
        silently corrupt every return calculation."""
        from alphalens.screeners.event_drift.pead_pss_scorer import build_daily_weights

        calendar = _trading_days(date(2020, 6, 1), date(2020, 6, 30))
        event = _event("A", date(2020, 6, 10), report_time="pre-market")
        weights = build_daily_weights(events=[event], calendar=calendar, n_fixed=150, hold_days=5)

        self.assertEqual(list(weights.index), calendar)
        # hold_days=5 → entry t=06-10, weight active on (06-10, 06-17] =
        # 06-11..06-17 (5 trading days: Thu, Fri, Mon, Tue, Wed).
        self.assertEqual(weights.loc[date(2020, 6, 10), "A"], 0.0)
        self.assertAlmostEqual(weights.loc[date(2020, 6, 11), "A"], 1 / 150)
        self.assertAlmostEqual(weights.loc[date(2020, 6, 17), "A"], 1 / 150)
        # Day after exit: ZERO (position closed at close(06-17)).
        self.assertEqual(weights.loc[date(2020, 6, 18), "A"], 0.0)


class TestDailyWeightsSparse(unittest.TestCase):
    def test_no_events_returns_zero_weights_all_days(self) -> None:
        from alphalens.screeners.event_drift.pead_pss_scorer import build_daily_weights

        calendar = _trading_days(date(2020, 6, 1), date(2020, 6, 30))
        weights = build_daily_weights(events=[], calendar=calendar, n_fixed=150, hold_days=20)

        self.assertEqual(list(weights.index), calendar)
        self.assertEqual(weights.sum(axis=1).sum(), 0.0)

    def test_multiple_concurrent_positions_each_get_one_over_n_fixed(self) -> None:
        """α2 sub-leverage: 5 active positions concurrent → gross 5/150, NOT 1.
        Critical: new entrant does NOT force rebalance of existing."""
        from alphalens.screeners.event_drift.pead_pss_scorer import build_daily_weights

        calendar = _trading_days(date(2020, 6, 1), date(2020, 7, 31))
        events = [
            _event(t, date(2020, 6, 10), report_time="pre-market")
            for t in ("A", "B", "C", "D", "E")
        ]
        weights = build_daily_weights(events=events, calendar=calendar, n_fixed=150, hold_days=20)

        # First active day is 06-11 (entry day 06-10 has zero weight).
        row = weights.loc[date(2020, 6, 11)]
        for ticker in ("A", "B", "C", "D", "E"):
            self.assertAlmostEqual(row[ticker], 1 / 150)
        self.assertAlmostEqual(row.sum(), 5 / 150)

    def test_exit_at_end_of_hold_window_then_clean_drop(self) -> None:
        """Day after exit_day must drop the position cleanly (weight 0)."""
        from alphalens.screeners.event_drift.pead_pss_scorer import build_daily_weights

        calendar = _trading_days(date(2020, 6, 1), date(2020, 7, 31))
        event = _event("A", date(2020, 6, 10), report_time="pre-market")  # entry t=06-10
        weights = build_daily_weights(events=[event], calendar=calendar, n_fixed=150, hold_days=5)

        # hold_days=5 → active (06-10, 06-17] → last weighted day 06-17.
        self.assertAlmostEqual(weights.loc[date(2020, 6, 17), "A"], 1 / 150)
        self.assertEqual(weights.loc[date(2020, 6, 18), "A"], 0.0)


class TestDummyMatrixEndToEnd(unittest.TestCase):
    """B3: 3-stock × 60 trading days dummy matrix end-to-end."""

    def test_three_stock_matrix_produces_expected_portfolio_returns(self) -> None:
        from alphalens.screeners.event_drift.pead_pss_scorer import (
            build_daily_weights,
            portfolio_returns_from_weights,
        )

        # 60 weekdays starting 2020-06-01 (Mon).
        calendar = _trading_days(date(2020, 6, 1), date(2020, 8, 23))[:60]
        self.assertEqual(len(calendar), 60)

        # Events at chosen indices into calendar.
        events = [
            # A: pre-market on day 5 → entry day 5, active (5, 15] = days 6..15
            _event("A", calendar[5], report_time="pre-market"),
            # B: post-market on day 10 → entry day 11, active (11, 21] = days 12..21
            _event("B", calendar[10], report_time="post-market"),
            # C: pre-market on day 20 → entry day 20, active (20, 30] = days 21..30
            _event("C", calendar[20], report_time="pre-market"),
        ]
        weights = build_daily_weights(events=events, calendar=calendar, n_fixed=10, hold_days=10)

        # Constant +1% daily return for every ticker, every day.
        returns = pd.DataFrame(0.01, index=calendar, columns=["A", "B", "C"])
        port = portfolio_returns_from_weights(weights, returns)

        # Day 0..4: no events → zero portfolio return.
        self.assertEqual(port.loc[calendar[0]], 0.0)
        self.assertEqual(port.loc[calendar[4]], 0.0)
        # Day 5: A's entry day, weight 0 → still zero.
        self.assertEqual(port.loc[calendar[5]], 0.0)
        # Day 6: A active (weight 1/10), gross 1/10, return 1% → 0.001.
        self.assertAlmostEqual(port.loc[calendar[6]], 0.001)
        # Day 12: A still active (window 6..15) + B newly active (12..21) →
        # gross 2/10, return 1% → 0.002.
        self.assertAlmostEqual(port.loc[calendar[12]], 0.002)
        # Day 16: A exited (last active 15), B still active → gross 1/10.
        self.assertAlmostEqual(port.loc[calendar[16]], 0.001)
        # Day 22: A + B both exited (A last active 15, B last active 21),
        # C entered at close of day 20 → C first active day is 21 (window 21..30).
        # So day 22: only C active → gross 1/10, return 0.001.
        self.assertAlmostEqual(port.loc[calendar[22]], 0.001)
        # Day 25: C still active → gross 1/10, return 0.001.
        self.assertAlmostEqual(port.loc[calendar[25]], 0.001)
        # Day 31: C exited (last active 30) → zero portfolio return.
        self.assertEqual(port.loc[calendar[31]], 0.0)


if __name__ == "__main__":
    unittest.main()
