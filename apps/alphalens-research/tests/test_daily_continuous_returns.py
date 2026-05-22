"""Tests for ``daily_continuous_returns`` — daily-cadence portfolio return
reconstruction from rebalance snapshots + per-ticker OHLCV history.

Pre-reg ledger ``insider_form4_opportunistic_2026_05_08_v2`` mandates
daily continuous-holding returns as the input to the Carhart regression so
that ``hac_maxlags=126`` (trading days) sits in the correct unit.

Convention being verified:
- A rebalance at calendar day ``d`` selects a basket; the basket is held
  starting day ``d+1`` (one-day execution lag, matches engine.portfolio_return).
- Between rebalance ``d_k`` and ``d_{k+1}``, daily portfolio return on day
  ``t`` (for ``d_k < t <= d_{k+1}``) is the equal-weight average of
  ``(close[t] / close[t-1] - 1)`` across the d_k basket.
- After the final rebalance, the basket is held until ``end_date`` (or the
  last available trading day if ``end_date`` is None).
"""

from __future__ import annotations

import unittest
from datetime import date

import pandas as pd
from alphalens_research.backtest.daily_continuous_returns import daily_continuous_returns
from alphalens_research.backtest.engine import RebalanceSnapshot
from alphalens_research.data.store.history import HistoryStore


def _ohlc(prices: dict[str, list[float]], dates: list[date]) -> dict[str, pd.DataFrame]:
    """Build minimal OHLCV mapping for HistoryStore — close column only."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    out = {}
    for ticker, closes in prices.items():
        out[ticker] = pd.DataFrame({"close": closes}, index=idx)
    return out


def _snap(d: date, basket: list[str]) -> RebalanceSnapshot:
    """Minimal RebalanceSnapshot for tests — only date + top_n_tickers consulted."""
    return RebalanceSnapshot(
        date=pd.Timestamp(d),
        scored_count=len(basket),
        top_n_tickers=list(basket),
        top_n_scores=[0.0] * len(basket),
        top_n_forward_returns=[0.0] * len(basket),
        portfolio_return=0.0,
        portfolio_return_holding=0.0,
        universe_median_return=0.0,
        ic=0.0,
    )


class TestDailyContinuousReturnsEmpty(unittest.TestCase):
    def test_empty_rebalances_returns_empty_series(self):
        store = HistoryStore({})
        result = daily_continuous_returns([], store)
        self.assertTrue(result.empty)
        self.assertEqual(result.dtype.kind, "f")


class TestDailyContinuousReturnsSingleRebalance(unittest.TestCase):
    def setUp(self):
        # 5 trading days; basket = [A, B]; A doubles each day, B halves each day
        self.dates = [date(2020, 1, d) for d in (2, 3, 6, 7, 8)]  # mon-tue, mon-wed
        self.histories = _ohlc(
            {
                "A": [10.0, 20.0, 40.0, 80.0, 160.0],  # +100% daily
                "B": [100.0, 50.0, 25.0, 12.5, 6.25],  # -50% daily
                "SPY": [100.0, 101.0, 102.0, 103.0, 104.0],  # ~+1% daily, calendar
            },
            self.dates,
        )
        self.store = HistoryStore(self.histories)

    def test_basket_AB_yields_equal_weight_return(self):
        # Rebalance on day 0 (Jan 2). Expected daily returns for days 1..4:
        # A: +1.0, B: -0.5, mean = +0.25 each day
        rebalances = [_snap(self.dates[0], ["A", "B"])]
        result = daily_continuous_returns(rebalances, self.store, calendar_ticker="SPY")
        self.assertEqual(len(result), 4)
        for v in result.values:
            self.assertAlmostEqual(v, 0.25, places=10)

    def test_index_matches_trading_days_after_first_rebalance(self):
        rebalances = [_snap(self.dates[0], ["A", "B"])]
        result = daily_continuous_returns(rebalances, self.store, calendar_ticker="SPY")
        expected_idx = pd.DatetimeIndex([pd.Timestamp(d) for d in self.dates[1:]])
        pd.testing.assert_index_equal(result.index, expected_idx)


class TestDailyContinuousReturnsBasketSwitch(unittest.TestCase):
    def setUp(self):
        self.dates = [date(2020, 1, d) for d in (2, 3, 6, 7, 8)]
        self.histories = _ohlc(
            {
                "A": [10.0, 20.0, 40.0, 80.0, 160.0],  # +100%/d
                "B": [100.0, 50.0, 25.0, 12.5, 6.25],  # -50%/d
                "C": [10.0, 11.0, 12.1, 13.31, 14.641],  # +10%/d
                "SPY": [100.0, 101.0, 102.0, 103.0, 104.0],
            },
            self.dates,
        )
        self.store = HistoryStore(self.histories)

    def test_switch_basket_at_second_rebalance(self):
        # Rebalance 0 on Jan 2: basket [A, B]. Holds Jan 3 + Jan 6.
        # Rebalance 1 on Jan 6: basket [C]. Holds Jan 7 + Jan 8.
        rebalances = [
            _snap(self.dates[0], ["A", "B"]),
            _snap(self.dates[2], ["C"]),
        ]
        result = daily_continuous_returns(rebalances, self.store, calendar_ticker="SPY")
        self.assertEqual(len(result), 4)
        # Jan 3 and Jan 6: AB basket -> +0.25 each
        # Jan 7 and Jan 8: C basket -> +0.10 each
        self.assertAlmostEqual(result.iloc[0], 0.25, places=10)
        self.assertAlmostEqual(result.iloc[1], 0.25, places=10)
        self.assertAlmostEqual(result.iloc[2], 0.10, places=10)
        self.assertAlmostEqual(result.iloc[3], 0.10, places=10)


class TestDailyContinuousReturnsMissingTicker(unittest.TestCase):
    def setUp(self):
        # Basket has [A, MISSING] — MISSING is not in history_store.
        # Expectation: average over present tickers only.
        self.dates = [date(2020, 1, d) for d in (2, 3, 6)]
        self.histories = _ohlc(
            {
                "A": [10.0, 20.0, 30.0],  # +100%, +50%
                "SPY": [100.0, 101.0, 102.0],
            },
            self.dates,
        )
        self.store = HistoryStore(self.histories)

    def test_missing_ticker_skipped_in_average(self):
        rebalances = [_snap(self.dates[0], ["A", "MISSING"])]
        result = daily_continuous_returns(rebalances, self.store, calendar_ticker="SPY")
        self.assertEqual(len(result), 2)
        # Day 1: A +100%, MISSING skipped -> avg = +1.0
        # Day 2: A +50% -> avg = +0.5
        self.assertAlmostEqual(result.iloc[0], 1.0, places=10)
        self.assertAlmostEqual(result.iloc[1], 0.5, places=10)


class TestDailyContinuousReturnsEndDate(unittest.TestCase):
    def setUp(self):
        self.dates = [date(2020, 1, d) for d in (2, 3, 6, 7, 8)]
        self.histories = _ohlc(
            {
                "A": [10.0, 11.0, 12.1, 13.31, 14.641],
                "SPY": [100.0, 101.0, 102.0, 103.0, 104.0],
            },
            self.dates,
        )
        self.store = HistoryStore(self.histories)

    def test_end_date_truncates_holding(self):
        rebalances = [_snap(self.dates[0], ["A"])]
        result = daily_continuous_returns(
            rebalances,
            self.store,
            calendar_ticker="SPY",
            end_date=date(2020, 1, 6),  # only Jan 3 + Jan 6 returns
        )
        self.assertEqual(len(result), 2)
        pd.testing.assert_index_equal(
            result.index,
            pd.DatetimeIndex([pd.Timestamp(d) for d in self.dates[1:3]]),
        )


class TestDailyContinuousReturnsCalendarFallback(unittest.TestCase):
    def test_calendar_defaults_to_first_basket_ticker(self):
        dates = [date(2020, 1, d) for d in (2, 3, 6)]
        histories = _ohlc({"A": [10.0, 11.0, 12.0]}, dates)
        store = HistoryStore(histories)
        rebalances = [_snap(dates[0], ["A"])]
        result = daily_continuous_returns(rebalances, store)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
