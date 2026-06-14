"""The O'Neil panel assembly — earnings YoY, split screen, coverage, sparsity.

Earnings is the one term computed from a store here (technicals are passed in by
the caller); the split screen reads the authoritative split calendar (a real split in
the trailing 52 weeks, NOT a price jump). Both degrade to ``None`` honestly.
``data_coverage`` counts the three OPTIONAL terms (R, trend, earnings).
"""

from __future__ import annotations

import datetime as dt
import unittest
from collections.abc import Callable
from dataclasses import dataclass

from alphalens_pipeline.experts.oneil import comparison
from alphalens_pipeline.experts.oneil.comparison import compute_oneil_panel

ASOF = dt.date(2026, 5, 1)


@dataclass
class _FakeStatement:
    net_income: float | None


class _FakeStore:
    """Minimal ``annual_series_as_of`` returning a fixed newest-first list."""

    def __init__(self, net_incomes: list[float | None] | None):
        self._net_incomes = net_incomes

    def annual_series_as_of(self, ticker, asof, *, max_years=10):
        if self._net_incomes is None:
            return []
        return [_FakeStatement(ni) for ni in self._net_incomes]


def _splits(dates: list[dt.date]) -> Callable[[str], list[dt.date]]:
    """A SplitsFn stub: returns a fixed all-time split-date list for any ticker."""

    def fn(ticker: str) -> list[dt.date]:
        return dates

    return fn


class TestEarningsGrowthYoy(unittest.TestCase):
    def test_growth_positive(self):
        growth, near_zero = comparison._earnings_growth_yoy(_FakeStore([120.0, 100.0]), "AAA", ASOF)
        self.assertAlmostEqual(growth, 20.0)
        self.assertIs(near_zero, False)

    def test_growth_negative(self):
        growth, near_zero = comparison._earnings_growth_yoy(_FakeStore([80.0, 100.0]), "AAA", ASOF)
        self.assertAlmostEqual(growth, -20.0)
        self.assertIs(near_zero, False)

    def test_fewer_than_two_fy_is_none(self):
        growth, near_zero = comparison._earnings_growth_yoy(_FakeStore([100.0]), "AAA", ASOF)
        self.assertIsNone(growth)
        self.assertIsNone(near_zero)

    def test_no_data_is_none(self):
        growth, near_zero = comparison._earnings_growth_yoy(_FakeStore(None), "AAA", ASOF)
        self.assertIsNone(growth)
        self.assertIsNone(near_zero)

    def test_missing_net_income_is_none(self):
        growth, near_zero = comparison._earnings_growth_yoy(_FakeStore([120.0, None]), "AAA", ASOF)
        self.assertIsNone(growth)
        self.assertIsNone(near_zero)

    def test_non_positive_prior_is_signflip_excluded(self):
        # Loss-making prior year: growth uninformative, excluded, but NOT near-zero.
        growth, near_zero = comparison._earnings_growth_yoy(_FakeStore([50.0, -10.0]), "AAA", ASOF)
        self.assertIsNone(growth)
        self.assertIs(near_zero, False)

    def test_near_zero_base_excluded_and_flagged(self):
        # prior 1.0 < 0.05 * |200| = 10.0 => exploding ratio excluded, flag True.
        growth, near_zero = comparison._earnings_growth_yoy(_FakeStore([200.0, 1.0]), "AAA", ASOF)
        self.assertIsNone(growth)
        self.assertIs(near_zero, True)


class TestSplitScreen(unittest.TestCase):
    """The screen flags N as contaminated only when a REAL split (from the authoritative
    calendar) falls in the trailing 52-week window — a large single-day earnings move is
    NOT a split, so it no longer nulls the score."""

    def test_split_in_window_is_suspected(self):
        # A split 30 days before asof contaminates the raw 52w-high basis.
        self.assertIs(
            comparison._split_suspected("AAA", ASOF, _splits([ASOF - dt.timedelta(days=30)])),
            True,
        )

    def test_clean_calendar_is_not_suspected(self):
        # No split ever recorded -> a confident False (NOT None), so N is scored.
        self.assertIs(comparison._split_suspected("AAA", ASOF, _splits([])), False)

    def test_split_older_than_window_is_clean(self):
        # A split ~2 years ago no longer touches the trailing 52-week high (this is the
        # TTD case: its 2021 10:1 split is far outside, so a -38% earnings day stays scored).
        self.assertIs(
            comparison._split_suspected("AAA", ASOF, _splits([ASOF - dt.timedelta(days=730)])),
            False,
        )

    def test_future_dated_split_excluded(self):
        # No look-ahead: a split AFTER asof must not count.
        self.assertIs(
            comparison._split_suspected("AAA", ASOF, _splits([ASOF + dt.timedelta(days=5)])),
            False,
        )

    def test_reader_absent_is_none(self):
        self.assertIsNone(comparison._split_suspected("AAA", ASOF, None))

    def test_reader_returns_none_is_none(self):
        # Calendar fetch failed -> tri-state None (leaves N ungated, never a fake False).
        self.assertIsNone(comparison._split_suspected("AAA", ASOF, lambda t: None))

    def test_reader_raising_is_swallowed_to_none(self):
        def boom(ticker):
            raise RuntimeError("yfinance down")

        self.assertIsNone(comparison._split_suspected("AAA", ASOF, boom))


class TestComputePanel(unittest.TestCase):
    def test_full_panel_coverage_one(self):
        panel = compute_oneil_panel(
            "AAA",
            "theme-x",
            ASOF,
            pct_off_52w_high=-3.0,
            ma200_slope_pct_per_day=0.05,
            ma200_distance_pct=8.0,
            store=_FakeStore([120.0, 100.0]),
            splits_fn=_splits([]),  # clean calendar -> split_suspected False
            rs_fn=lambda t, a: 60.0,  # R present
        )
        self.assertEqual(panel.ticker, "AAA")
        self.assertEqual(panel.theme, "theme-x")
        self.assertAlmostEqual(panel.earnings_growth_yoy_pct, 20.0)
        self.assertAlmostEqual(panel.oneil_rs_approx_pct, 60.0)
        self.assertIs(panel.new_high_split_suspected, False)
        self.assertAlmostEqual(panel.data_coverage, 1.0)  # R + trend + earnings all present

    def test_coverage_one_third_when_only_trend(self):
        # 3 OPTIONAL terms now (R + trend + earnings); only trend resolves -> 1/3.
        panel = compute_oneil_panel(
            "AAA",
            "t",
            ASOF,
            pct_off_52w_high=-3.0,
            ma200_slope_pct_per_day=0.05,
            ma200_distance_pct=8.0,
            store=_FakeStore([120.0]),  # <2 FY => earnings None
            splits_fn=_splits([]),
        )  # no rs_fn => R None
        self.assertIsNone(panel.earnings_growth_yoy_pct)
        self.assertIsNone(panel.oneil_rs_approx_pct)
        self.assertAlmostEqual(panel.data_coverage, 1 / 3)  # trend only of 3 optional

    def test_coverage_zero_when_both_optional_absent(self):
        panel = compute_oneil_panel(
            "AAA",
            "t",
            ASOF,
            pct_off_52w_high=-3.0,
            ma200_slope_pct_per_day=None,
            ma200_distance_pct=None,
            store=_FakeStore(None),
            splits_fn=_splits([]),
        )
        self.assertAlmostEqual(panel.data_coverage, 0.0)

    def test_split_window_unavailable_is_none(self):
        panel = compute_oneil_panel(
            "AAA",
            "t",
            ASOF,
            pct_off_52w_high=-3.0,
            ma200_slope_pct_per_day=0.05,
            ma200_distance_pct=8.0,
            store=_FakeStore([120.0, 100.0]),
            splits_fn=None,  # no calendar reader => split unknown
        )
        self.assertIsNone(panel.new_high_split_suspected)


class TestRsTerm(unittest.TestCase):
    """R (relative strength) — injected rs_fn (disk-only in prod); tri-state None;
    optional (counts in coverage when present); fail-soft on a raising reader."""

    def _panel(self, **over):
        kw = {
            "pct_off_52w_high": -3.0,
            "ma200_slope_pct_per_day": None,
            "ma200_distance_pct": None,
            "store": _FakeStore(None),
            "splits_fn": _splits([]),
        }
        kw.update(over)
        return compute_oneil_panel("AAA", "t", ASOF, **kw)

    def test_rs_fn_value_stamped_and_counted(self):
        panel = self._panel(rs_fn=lambda t, a: 72.0)
        self.assertAlmostEqual(panel.oneil_rs_approx_pct, 72.0)
        self.assertAlmostEqual(panel.data_coverage, 1 / 3)  # only R of 3 optional

    def test_rs_fn_none_returns_none_not_counted(self):
        panel = self._panel(rs_fn=lambda t, a: None)
        self.assertIsNone(panel.oneil_rs_approx_pct)
        self.assertAlmostEqual(panel.data_coverage, 0.0)

    def test_rs_fn_absent_is_none(self):
        panel = self._panel()  # no rs_fn
        self.assertIsNone(panel.oneil_rs_approx_pct)

    def test_rs_fn_raising_is_swallowed(self):
        def boom(t, a):
            raise RuntimeError("store error")

        panel = self._panel(rs_fn=boom)
        self.assertIsNone(panel.oneil_rs_approx_pct)  # fail-soft, mirror split screen


if __name__ == "__main__":
    unittest.main()
