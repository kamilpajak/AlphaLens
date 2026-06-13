"""The O'Neil panel assembly — earnings YoY, split screen, coverage, sparsity.

Earnings is the one term computed from a store here (technicals are passed in by
the caller); the split screen reads a raw-close window. Both degrade to ``None``
honestly. ``data_coverage`` counts only the two OPTIONAL terms (trend, earnings).
"""

from __future__ import annotations

import datetime as dt
import unittest
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd
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


def _ohlcv(closes: list[float]) -> Callable[[str, dt.date], pd.DataFrame]:
    def fn(ticker: str, asof: dt.date) -> pd.DataFrame:
        return pd.DataFrame({"close": closes})

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
    def test_clean_window_not_suspected(self):
        self.assertIs(comparison._detect_split(pd.Series([100.0, 101.0, 99.0, 102.0])), False)

    def test_split_jump_suspected(self):
        # 100 -> 49 is a ~2:1 split (|ratio-1| = 0.51 > 0.35).
        self.assertIs(comparison._detect_split(pd.Series([100.0, 49.0, 50.0])), True)

    def test_short_window_is_none(self):
        self.assertIsNone(comparison._detect_split(pd.Series([100.0])))


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
            ohlcv_fn=_ohlcv([100.0, 101.0, 102.0]),
        )
        self.assertEqual(panel.ticker, "AAA")
        self.assertEqual(panel.theme, "theme-x")
        self.assertAlmostEqual(panel.earnings_growth_yoy_pct, 20.0)
        self.assertIs(panel.new_high_split_suspected, False)
        self.assertAlmostEqual(panel.data_coverage, 1.0)  # trend + earnings both present

    def test_coverage_half_when_earnings_absent(self):
        panel = compute_oneil_panel(
            "AAA",
            "t",
            ASOF,
            pct_off_52w_high=-3.0,
            ma200_slope_pct_per_day=0.05,
            ma200_distance_pct=8.0,
            store=_FakeStore([120.0]),  # <2 FY => earnings None
            ohlcv_fn=_ohlcv([100.0, 101.0]),
        )
        self.assertIsNone(panel.earnings_growth_yoy_pct)
        self.assertAlmostEqual(panel.data_coverage, 0.5)  # trend only

    def test_coverage_zero_when_both_optional_absent(self):
        panel = compute_oneil_panel(
            "AAA",
            "t",
            ASOF,
            pct_off_52w_high=-3.0,
            ma200_slope_pct_per_day=None,
            ma200_distance_pct=None,
            store=_FakeStore(None),
            ohlcv_fn=_ohlcv([100.0, 101.0]),
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
            ohlcv_fn=None,  # no reader => split unknown
        )
        self.assertIsNone(panel.new_high_split_suspected)


if __name__ == "__main__":
    unittest.main()
