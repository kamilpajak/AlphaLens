"""Phase B unit tests for multi_source_two_stage target generator.

Locks the contract for `alphalens_research.screeners.multi_source_two_stage.target`:
- forward_excess_return uses HistoryStore.forward_return convention
  (entry close[asof+1], exit close[asof+holding+1])
- RF subtraction is daily-arithmetic over the entry→exit calendar window
- NaN propagation when forward bars are insufficient (delisting mid-hold)
- build_target_frame aligns on (asof, ticker)
- split_train_holdout strict-temporal cut
- aligned_train_targets drops NaN-target rows and preserves features
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd
from alphalens_research.data.store.history import HistoryStore
from alphalens_research.screeners.multi_source_two_stage.target import (
    aligned_train_targets,
    build_target_frame,
    forward_excess_return,
    split_train_holdout,
)


def _bdays(start: str, periods: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=periods)


def _ohlcv_constant_growth(start: str, periods: int, daily_ret: float = 0.001) -> pd.DataFrame:
    idx = _bdays(start, periods)
    closes = 100.0 * np.exp(np.arange(periods) * daily_ret)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": 1_000_000.0,
        },
        index=idx,
    )


class TestForwardExcessReturn(unittest.TestCase):
    def test_known_horizon_arithmetic(self):
        # Constant 0.1% daily compounding price, RF flat at 0.00005/day.
        # Asof = 2020-01-15 (close[15] used for ranking only — entry is close[16]).
        # 5d-forward: entry close[16], exit close[21]. Pre-RF return = 0.001 × 5
        # in log space, which equals exp(0.005) − 1 ≈ 0.005012541.
        # Cumulative RF over 5 daily bars = 5 × 0.00005 = 0.00025.
        # Excess = 0.005012541 − 0.00025 ≈ 0.004762541.
        store = HistoryStore({"AAPL": _ohlcv_constant_growth("2020-01-01", 30, 0.001)})
        rf = pd.Series(0.00005, index=_bdays("2020-01-01", 30))
        out = forward_excess_return(store, "AAPL", date(2020, 1, 15), rf)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out, np.exp(0.005) - 1 - 0.00025, places=6)

    def test_returns_none_when_forward_bars_insufficient(self):
        store = HistoryStore({"AAPL": _ohlcv_constant_growth("2020-01-01", 5)})
        rf = pd.Series(0.0, index=_bdays("2020-01-01", 5))
        # Only 5 bars; with 5d forward + entry+1 = need 6 future bars
        self.assertIsNone(forward_excess_return(store, "AAPL", date(2020, 1, 6), rf))

    def test_unknown_ticker_returns_none(self):
        store = HistoryStore({})
        rf = pd.Series(0.0, index=_bdays("2020-01-01", 30))
        self.assertIsNone(forward_excess_return(store, "GHOST", date(2020, 1, 15), rf))

    def test_rf_missing_returns_none(self):
        store = HistoryStore({"AAPL": _ohlcv_constant_growth("2020-01-01", 30)})
        rf = pd.Series(dtype=float)  # empty series
        self.assertIsNone(forward_excess_return(store, "AAPL", date(2020, 1, 15), rf))

    def test_rf_with_nan_in_window_returns_none(self):
        # asof=2020-01-15 → entry idx 11 (2020-01-16), exit idx 16 (2020-01-23).
        # NaN at idx 13 falls within (entry, exit] → must be rejected.
        store = HistoryStore({"AAPL": _ohlcv_constant_growth("2020-01-01", 30)})
        idx = _bdays("2020-01-01", 30)
        rf_vals = np.zeros(30)
        rf_vals[13] = np.nan
        rf = pd.Series(rf_vals, index=idx)
        self.assertIsNone(forward_excess_return(store, "AAPL", date(2020, 1, 15), rf))

    def test_zero_or_negative_entry_price_rejected(self):
        # Corrupt the entry bar (idx 11 for asof=2020-01-15).
        idx = _bdays("2020-01-01", 30)
        df = _ohlcv_constant_growth("2020-01-01", 30)
        df.loc[idx[11], "close"] = 0.0
        store = HistoryStore({"AAPL": df})
        rf = pd.Series(0.0, index=idx)
        self.assertIsNone(forward_excess_return(store, "AAPL", date(2020, 1, 15), rf))

    def test_custom_holding_period(self):
        # Same setup, holding=10 → 10 daily steps of 0.001 = exp(0.010)−1.
        store = HistoryStore({"AAPL": _ohlcv_constant_growth("2020-01-01", 60, 0.001)})
        rf = pd.Series(0.0, index=_bdays("2020-01-01", 60))
        out = forward_excess_return(store, "AAPL", date(2020, 1, 15), rf, holding_period=10)
        self.assertAlmostEqual(out, np.exp(0.010) - 1, places=6)


class TestBuildTargetFrame(unittest.TestCase):
    def test_aligns_on_asof_ticker(self):
        store = HistoryStore({"AAPL": _ohlcv_constant_growth("2020-01-01", 30, 0.001)})
        rf = pd.Series(0.0, index=_bdays("2020-01-01", 30))
        feat = pd.DataFrame(
            {
                "asof": [date(2020, 1, 10), date(2020, 1, 15)],
                "ticker": ["AAPL", "AAPL"],
                "extra_col": [0.0, 0.0],
            }
        )
        out = build_target_frame(feat, history_store=store, rf_series=rf)
        self.assertEqual(list(out.columns), ["asof", "ticker", "target"])
        self.assertEqual(len(out), 2)
        self.assertTrue(np.isfinite(out["target"]).all())

    def test_propagates_nan_when_forward_unavailable(self):
        store = HistoryStore({"AAPL": _ohlcv_constant_growth("2020-01-01", 8)})
        rf = pd.Series(0.0, index=_bdays("2020-01-01", 8))
        feat = pd.DataFrame(
            {
                "asof": [date(2020, 1, 1), date(2020, 1, 8)],
                "ticker": ["AAPL", "AAPL"],
            }
        )
        out = build_target_frame(feat, history_store=store, rf_series=rf)
        self.assertEqual(len(out), 2)
        self.assertTrue(np.isfinite(out.loc[0, "target"]))
        self.assertTrue(np.isnan(out.loc[1, "target"]))

    def test_empty_input_returns_empty_frame(self):
        out = build_target_frame(
            pd.DataFrame(columns=["asof", "ticker"]),
            history_store=HistoryStore({}),
            rf_series=pd.Series(dtype=float),
        )
        self.assertTrue(out.empty)
        self.assertEqual(list(out.columns), ["asof", "ticker", "target"])


class TestSplitTrainHoldout(unittest.TestCase):
    def test_strict_temporal_split(self):
        df = pd.DataFrame(
            {
                "asof": [date(2024, 1, 15), date(2024, 4, 29), date(2024, 4, 30), date(2024, 5, 1)],
                "ticker": ["A", "B", "C", "D"],
                "value": [1, 2, 3, 4],
            }
        )
        train, holdout = split_train_holdout(df, holdout_start=date(2024, 4, 30))
        self.assertListEqual(list(train["ticker"]), ["A", "B"])
        self.assertListEqual(list(holdout["ticker"]), ["C", "D"])

    def test_empty_input(self):
        train, holdout = split_train_holdout(pd.DataFrame(), holdout_start=date(2024, 4, 30))
        self.assertTrue(train.empty)
        self.assertTrue(holdout.empty)


class TestAlignedTrainTargets(unittest.TestCase):
    def test_drops_nan_targets_only(self):
        feat = pd.DataFrame(
            {
                "asof": [date(2024, 1, 1), date(2024, 1, 1), date(2024, 1, 1)],
                "ticker": ["A", "B", "C"],
                "f1": [10.0, 20.0, 30.0],
            }
        )
        target = pd.DataFrame(
            {
                "asof": [date(2024, 1, 1), date(2024, 1, 1), date(2024, 1, 1)],
                "ticker": ["A", "B", "C"],
                "target": [0.01, np.nan, 0.03],
            }
        )
        feat_aligned, y = aligned_train_targets(feat, target)
        self.assertEqual(len(feat_aligned), 2)
        self.assertListEqual(list(y.values), [0.01, 0.03])
        self.assertListEqual(list(feat_aligned["ticker"]), ["A", "C"])

    def test_inner_join_drops_unmatched_pairs(self):
        feat = pd.DataFrame(
            {
                "asof": [date(2024, 1, 1)],
                "ticker": ["A"],
                "f1": [1.0],
            }
        )
        target = pd.DataFrame(
            {
                "asof": [date(2024, 1, 1)],
                "ticker": ["B"],
                "target": [0.01],
            }
        )
        feat_aligned, y = aligned_train_targets(feat, target)
        self.assertTrue(feat_aligned.empty)
        self.assertTrue(y.empty)


if __name__ == "__main__":
    unittest.main()
