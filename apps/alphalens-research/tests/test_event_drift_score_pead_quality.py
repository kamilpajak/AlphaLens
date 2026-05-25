"""Tests for the PEAD x quality scorer (the integrating engine).

Composes event-window construction, single-active-window invariant,
trailing-90d cohort quantiles, sector filter and Day-1 sign confirmation
to produce a daily DataFrame[ticker, score] for the long-only portfolio.
"""

from __future__ import annotations

import unittest
from datetime import date

from alphalens_research.screeners.event_drift.announcement_dates import EarningsAnnouncement
from alphalens_research.screeners.event_drift.sector_filter import SectorFilter


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


def _ann(ticker, period_end, filed_date, hour=11):
    return EarningsAnnouncement(
        ticker=ticker,
        period_end=period_end,
        filed_date=filed_date,
        accepted_hour_et=hour,
        source="10-Q",
    )


def _run_scorer(
    *,
    asof,
    universe,
    announcements_by_ticker,
    sue_map,
    accruals_map,
    day1_returns,
    sic_map=None,
    **kwargs,
):
    """Helper: invoke ``score_pead_quality`` with stubbed lookups."""
    from alphalens_research.screeners.event_drift.score_pead_quality import score_pead_quality

    def sue_lookup(ticker, period_end):
        return sue_map.get((ticker, period_end))

    def accruals_lookup(ticker, period_end):
        return accruals_map.get((ticker, period_end))

    def announcement_lookup(ticker):
        return announcements_by_ticker.get(ticker, [])

    def day1_return_lookup(ticker, market_day):
        return day1_returns.get((ticker, market_day))

    sector_filter = SectorFilter(sic_map=sic_map or {}, unknown_policy="include")

    return score_pead_quality(
        asof=asof,
        universe=universe,
        sue_lookup=sue_lookup,
        accruals_lookup=accruals_lookup,
        announcement_lookup=announcement_lookup,
        day1_return_lookup=day1_return_lookup,
        sector_filter=sector_filter,
        calendar=_StubCalendar(),
        **kwargs,
    )


class TestScorerEmptyAndDegenerate(unittest.TestCase):
    def test_empty_universe_returns_empty_dataframe(self):
        df = _run_scorer(
            asof=date(2024, 9, 1),
            universe=[],
            announcements_by_ticker={},
            sue_map={},
            accruals_map={},
            day1_returns={},
        )
        self.assertEqual(len(df), 0)
        self.assertEqual(set(df.columns), {"ticker", "score"})

    def test_no_announcements_returns_empty_dataframe(self):
        df = _run_scorer(
            asof=date(2024, 9, 1),
            universe=["AAPL", "MSFT"],
            announcements_by_ticker={"AAPL": [], "MSFT": []},
            sue_map={},
            accruals_map={},
            day1_returns={},
        )
        self.assertEqual(len(df), 0)

    def test_announcement_outside_window_returns_empty(self):
        # Announcement is from > 60 days ago -> window expired before asof.
        ann = _ann("AAPL", date(2024, 3, 31), date(2024, 5, 1))
        df = _run_scorer(
            asof=date(2024, 12, 1),  # Way past d+60
            universe=["AAPL"],
            announcements_by_ticker={"AAPL": [ann]},
            sue_map={("AAPL", date(2024, 3, 31)): 2.5},
            accruals_map={("AAPL", date(2024, 3, 31)): -0.02},
            day1_returns={("AAPL", date(2024, 5, 1)): 0.03},
        )
        self.assertEqual(len(df), 0)


class TestScorerSingleTicker(unittest.TestCase):
    def test_single_ticker_passing_all_gates_appears(self):
        ann = _ann("AAPL", date(2024, 6, 30), date(2024, 8, 1))
        df = _run_scorer(
            asof=date(2024, 9, 1),  # inside [d+2, d+60]
            universe=["AAPL"],
            announcements_by_ticker={"AAPL": [ann]},
            sue_map={("AAPL", date(2024, 6, 30)): 2.5},
            accruals_map={("AAPL", date(2024, 6, 30)): -0.02},
            day1_returns={("AAPL", date(2024, 8, 1)): 0.03},
        )
        # Single name, no cohort to compare against -> degenerate quintile -> kept.
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["ticker"], "AAPL")
        self.assertAlmostEqual(df.iloc[0]["score"], 2.5)

    def test_sector_excluded_ticker_dropped(self):
        ann = _ann("WFC", date(2024, 6, 30), date(2024, 8, 1))
        df = _run_scorer(
            asof=date(2024, 9, 1),
            universe=["WFC"],
            announcements_by_ticker={"WFC": [ann]},
            sue_map={("WFC", date(2024, 6, 30)): 2.5},
            accruals_map={("WFC", date(2024, 6, 30)): -0.02},
            day1_returns={("WFC", date(2024, 8, 1)): 0.03},
            sic_map={"WFC": 6020},  # commercial bank -> Financials -> excluded
        )
        self.assertEqual(len(df), 0)

    def test_day1_sign_mismatch_drops_name(self):
        ann = _ann("AAPL", date(2024, 6, 30), date(2024, 8, 1))
        df = _run_scorer(
            asof=date(2024, 9, 1),
            universe=["AAPL"],
            announcements_by_ticker={"AAPL": [ann]},
            sue_map={("AAPL", date(2024, 6, 30)): 2.5},  # positive SUE
            accruals_map={("AAPL", date(2024, 6, 30)): -0.02},
            day1_returns={("AAPL", date(2024, 8, 1)): -0.03},  # negative day-1 (bull trap)
        )
        self.assertEqual(len(df), 0)

    def test_missing_sue_drops_name(self):
        ann = _ann("AAPL", date(2024, 6, 30), date(2024, 8, 1))
        df = _run_scorer(
            asof=date(2024, 9, 1),
            universe=["AAPL"],
            announcements_by_ticker={"AAPL": [ann]},
            sue_map={},  # SUE not computable
            accruals_map={("AAPL", date(2024, 6, 30)): -0.02},
            day1_returns={("AAPL", date(2024, 8, 1)): 0.03},
        )
        self.assertEqual(len(df), 0)

    def test_missing_day1_return_drops_name(self):
        ann = _ann("AAPL", date(2024, 6, 30), date(2024, 8, 1))
        df = _run_scorer(
            asof=date(2024, 9, 1),
            universe=["AAPL"],
            announcements_by_ticker={"AAPL": [ann]},
            sue_map={("AAPL", date(2024, 6, 30)): 2.5},
            accruals_map={("AAPL", date(2024, 6, 30)): -0.02},
            day1_returns={},  # missing -> filter drops
        )
        self.assertEqual(len(df), 0)


class TestScorerCohortQuantiles(unittest.TestCase):
    """Trailing-90d cohort quantile gates."""

    def _build_cohort(self, n=10, *, sue_high_idx=None, accrual_low_idx=None):
        """Return dicts for a cohort of n tickers with announcements 30 days before asof."""
        announcements_by_ticker = {}
        sue_map = {}
        accruals_map = {}
        day1_returns = {}
        sic_map = {}
        universe = []
        # All announcements at filed_date 2024-08-01 (30 days before asof 2024-09-01)
        # SUE values 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0 (low->high)
        # Accruals 0.05, 0.04, 0.03, 0.02, 0.01, 0.0, -0.01, -0.02, -0.03, -0.04 (high->low)
        for i in range(n):
            t = f"T{i:02d}"
            universe.append(t)
            ann = _ann(t, date(2024, 6, 30), date(2024, 8, 1))
            announcements_by_ticker[t] = [ann]
            sue_map[(t, date(2024, 6, 30))] = 0.5 * (i + 1)  # 0.5..5.0
            accruals_map[(t, date(2024, 6, 30))] = 0.05 - 0.01 * i  # 0.05..-0.04
            day1_returns[(t, date(2024, 8, 1))] = 0.03  # all positive (sign-confirm)
        return announcements_by_ticker, sue_map, accruals_map, day1_returns, universe

    def test_top_quintile_sue_keeps_only_top_20pct(self):
        ann_dict, sue_map, accruals_map, day1_returns, universe = self._build_cohort(n=10)
        # Top 20% of n=10 -> top 2: sue values 4.5, 5.0 (T08, T09)
        # Below median accruals -> bottom 5: T05 (0.0), T06 (-0.01), T07 (-0.02),
        #   T08 (-0.03), T09 (-0.04). Intersection with top-quintile SUE: T08, T09.
        df = _run_scorer(
            asof=date(2024, 9, 1),
            universe=universe,
            announcements_by_ticker=ann_dict,
            sue_map=sue_map,
            accruals_map=accruals_map,
            day1_returns=day1_returns,
            sue_quantile_top_pct=20.0,
            accrual_quantile_bottom_pct=50.0,
        )
        self.assertEqual(set(df["ticker"]), {"T08", "T09"})
        # Score is the SUE value
        self.assertAlmostEqual(df.set_index("ticker").loc["T09", "score"], 5.0)
        self.assertAlmostEqual(df.set_index("ticker").loc["T08", "score"], 4.5)

    def test_below_median_accruals_excludes_high_accrual_names(self):
        ann_dict, sue_map, accruals_map, day1_returns, universe = self._build_cohort(n=10)
        # Force top-quintile to be names with HIGH accruals to test median filter.
        # T00 (acc=0.05) gets highest SUE, T01 (acc=0.04) gets 2nd. T02..T09 get
        # very low SUE so they cannot enter top-quintile.
        sue_map[("T00", date(2024, 6, 30))] = 5.0
        sue_map[("T01", date(2024, 6, 30))] = 4.5
        for i in range(2, 10):
            sue_map[(f"T{i:02d}", date(2024, 6, 30))] = 0.1 * (i - 1)  # 0.1..0.8 — all below T01

        # T00 has accruals 0.05 (TOP, high accruals = LOW quality), T01 has 0.04.
        # Bottom-half accruals threshold ~= median = 0.005 (between T04 and T05).
        # Names T00/T01 have accruals 0.05/0.04, both ABOVE median -> EXCLUDED.
        df = _run_scorer(
            asof=date(2024, 9, 1),
            universe=universe,
            announcements_by_ticker=ann_dict,
            sue_map=sue_map,
            accruals_map=accruals_map,
            day1_returns=day1_returns,
        )
        self.assertEqual(len(df), 0)

    def test_no_active_windows_returns_empty(self):
        # asof BEFORE any announcement entry day -> no active windows
        ann = _ann("AAPL", date(2024, 6, 30), date(2024, 8, 1))
        df = _run_scorer(
            asof=date(2024, 7, 1),  # before announcement
            universe=["AAPL"],
            announcements_by_ticker={"AAPL": [ann]},
            sue_map={("AAPL", date(2024, 6, 30)): 2.5},
            accruals_map={("AAPL", date(2024, 6, 30)): -0.02},
            day1_returns={("AAPL", date(2024, 8, 1)): 0.03},
        )
        self.assertEqual(len(df), 0)


class TestScorerOverlappingAnnouncements(unittest.TestCase):
    """Single-active-window invariant integration."""

    def test_overlapping_announcement_dropped_keeps_first(self):
        ann_q1 = _ann("AAPL", date(2024, 6, 30), date(2024, 8, 1))
        ann_q2_overlap = _ann("AAPL", date(2024, 9, 30), date(2024, 9, 15))
        df = _run_scorer(
            asof=date(2024, 10, 1),  # both windows would be active without invariant
            universe=["AAPL"],
            announcements_by_ticker={"AAPL": [ann_q1, ann_q2_overlap]},
            sue_map={
                ("AAPL", date(2024, 6, 30)): 2.5,
                ("AAPL", date(2024, 9, 30)): 4.0,  # higher but should be dropped
            },
            accruals_map={
                ("AAPL", date(2024, 6, 30)): -0.02,
                ("AAPL", date(2024, 9, 30)): -0.03,
            },
            day1_returns={
                ("AAPL", date(2024, 8, 1)): 0.03,
                ("AAPL", date(2024, 9, 15)): 0.05,
            },
        )
        # Q1 window kept; Q2 dropped despite higher SUE.
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(df.iloc[0]["score"], 2.5)


if __name__ == "__main__":
    unittest.main()
