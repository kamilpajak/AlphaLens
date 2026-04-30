"""Tests for `alphalens research historical-acceptance` helper functions.

Covers the numeric correctness of:
  - _compute_forward_features: PIT entry (next-day close), forward return, max DD, vol
  - _at_pick_trailing_return: trailing lookback computed up to and including pick date
  - _mean_alpha (inside historical_acceptance): NaN-aware mean + accurate n

The 60-sample Gemini replay burns ~6M tokens + 3.3h. If these helpers are
wrong, the CSV output is garbage and we either re-run (expensive) or act on
bad numbers (worse). Hence explicit tests before the full run.
"""

from __future__ import annotations

import unittest

import pandas as pd


def _ramp_df(start: str, n: int, start_price: float, step: float) -> pd.DataFrame:
    """Linear-ramp OHLCV so every close is distinguishable."""
    idx = pd.bdate_range(start=start, periods=n)
    closes = [start_price + i * step for i in range(n)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": 1_000,
        },
        index=idx,
    )


class TestComputeForwardFeatures(unittest.TestCase):
    """_compute_forward_features must enter at NEXT trading day's close (PIT-safe)."""

    def _store(self):
        from alphalens.data.store.history import HistoryStore

        # Ticker: 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111
        # Bench:  200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200 (flat)
        return HistoryStore(
            {
                "A": _ramp_df("2024-01-02", 12, 100.0, 1.0),
                "SPY": _ramp_df("2024-01-02", 12, 200.0, 0.0),
            }
        )

    def test_forward_return_uses_next_day_close_as_entry(self):
        """Signal at pick_date EOD → earliest fill = next day's close.

        pick_date = 2024-01-02 (day 0, close=100). With the PIT convention:
          entry   = 2024-01-03 close = 101
          exit_5d = entry + 5 bars = 2024-01-10 close = 106
          fwd_5d  = (106 - 101) / 101
        """
        from alphalens_cli.commands.research import _compute_forward_features

        store = self._store()
        pick = pd.Timestamp("2024-01-02").date()
        features = _compute_forward_features(store, "A", "SPY", pick)

        expected = (106.0 - 101.0) / 101.0
        self.assertAlmostEqual(features["fwd_5d"], expected, places=6)
        self.assertAlmostEqual(features["bench_5d"], 0.0, places=6)
        self.assertAlmostEqual(features["alpha_5d"], expected, places=6)

    def test_forward_return_matches_history_store_forward_return(self):
        """Our helper should produce the same entry/exit pair as the engine's
        canonical `HistoryStore.forward_return`, which is the backtest's PIT
        source of truth."""
        from alphalens_cli.commands.research import _compute_forward_features

        store = self._store()
        pick = pd.Timestamp("2024-01-02").date()
        features = _compute_forward_features(store, "A", "SPY", pick)
        canonical = store.forward_return("A", pick, 5)

        self.assertIsNotNone(canonical)
        self.assertAlmostEqual(features["fwd_5d"], canonical, places=6)

    def test_returns_none_when_not_enough_forward_history(self):
        """If the window exits past the last bar, all horizon features are None."""
        from alphalens_cli.commands.research import _compute_forward_features

        store = self._store()  # 12 bars total
        # Pick near the end — no room for a 20-day forward window.
        pick = pd.Timestamp("2024-01-12").date()  # day 9 of the ramp
        features = _compute_forward_features(store, "A", "SPY", pick)

        self.assertIsNone(features["fwd_20d"])
        self.assertIsNone(features["bench_20d"])
        self.assertIsNone(features["alpha_20d"])
        self.assertIsNone(features["fwd_max_dd_20d"])

    def test_max_drawdown_captures_intraperiod_trough(self):
        """Feed a price series that dips then recovers — max_dd should be negative."""
        from alphalens.data.store.history import HistoryStore
        from alphalens_cli.commands.research import _compute_forward_features

        idx = pd.bdate_range(start="2024-01-02", periods=12)
        # V-shape: entry@101 → drops to 90 → recovers to 105.
        closes = [100, 101, 95, 90, 92, 98, 102, 103, 104, 105, 106, 107]
        df = pd.DataFrame(
            {
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": 1,
            },
            index=idx,
        )
        bench = _ramp_df("2024-01-02", 12, 200.0, 0.0)
        store = HistoryStore({"B": df, "SPY": bench})

        pick = pd.Timestamp("2024-01-02").date()  # entry=101 next day
        features = _compute_forward_features(store, "B", "SPY", pick)

        # path[2] = 90/101 ≈ 0.891 → max_dd ≈ -0.109
        self.assertLess(features["fwd_max_dd_5d"], -0.08)
        # Benchmark is flat → max_dd = 0.
        self.assertAlmostEqual(features["bench_max_dd_5d"], 0.0, places=6)

    def test_vol_is_positive_on_nonflat_path(self):
        from alphalens_cli.commands.research import _compute_forward_features

        store = self._store()
        pick = pd.Timestamp("2024-01-02").date()
        features = _compute_forward_features(store, "A", "SPY", pick)

        # ticker ramp has strictly positive daily return → vol > 0
        self.assertIsNotNone(features["fwd_vol_5d"])
        self.assertGreater(features["fwd_vol_5d"], 0.0)
        # flat benchmark → vol = 0
        self.assertAlmostEqual(features["bench_vol_5d"], 0.0, places=6)


class TestAtPickTrailingReturn(unittest.TestCase):
    def test_returns_lookback_day_return_up_to_pick(self):
        """trailing_60d = (close[pick] - close[pick-60]) / close[pick-60]."""
        from alphalens.data.store.history import HistoryStore
        from alphalens_cli.commands.research import _at_pick_trailing_return

        df = _ramp_df("2023-01-02", 120, 100.0, 1.0)
        store = HistoryStore({"A": df})

        # Bar index 65 → close = 165. 60-day earlier (index 5) → close = 105.
        pick = df.index[65].date()
        r = _at_pick_trailing_return(store, "A", pick, lookback=60)

        self.assertIsNotNone(r)
        self.assertAlmostEqual(r, (165.0 - 105.0) / 105.0, places=6)

    def test_returns_none_when_insufficient_history(self):
        from alphalens.data.store.history import HistoryStore
        from alphalens_cli.commands.research import _at_pick_trailing_return

        df = _ramp_df("2024-01-02", 30, 100.0, 1.0)
        store = HistoryStore({"A": df})

        # Only 30 bars; asking for 60-day trailing at bar 10 is insufficient.
        pick = df.index[10].date()
        r = _at_pick_trailing_return(store, "A", pick, lookback=60)
        self.assertIsNone(r)

    def test_returns_none_on_unknown_ticker(self):
        from alphalens.data.store.history import HistoryStore
        from alphalens_cli.commands.research import _at_pick_trailing_return

        store = HistoryStore({"A": _ramp_df("2024-01-02", 120, 100.0, 1.0)})
        r = _at_pick_trailing_return(store, "UNKNOWN", pd.Timestamp("2024-02-01").date(), 60)
        self.assertIsNone(r)


class TestMeanAlphaReporting(unittest.TestCase):
    """The markdown `n` column must reflect rows with non-null alpha at that
    horizon, not the total sample size. For 120d horizon, recent picks often
    have NaN fwd_120d → they drop out of the mean but the displayed count
    must not lie."""

    def test_mean_alpha_counts_only_valid_values(self):
        from alphalens_cli.commands.research import _mean_alpha

        rows = [
            {"alpha_120d": 0.10},
            {"alpha_120d": 0.20},
            {"alpha_120d": None},  # picks with no 120d forward yet
            {"alpha_120d": None},
        ]
        mean, n = _mean_alpha(rows, 120)
        self.assertAlmostEqual(mean, 0.15, places=6)
        self.assertEqual(n, 2)

    def test_mean_alpha_treats_nan_like_none(self):
        from alphalens_cli.commands.research import _mean_alpha

        rows = [
            {"alpha_60d": 0.05},
            {"alpha_60d": float("nan")},
        ]
        mean, n = _mean_alpha(rows, 60)
        self.assertAlmostEqual(mean, 0.05, places=6)
        self.assertEqual(n, 1)

    def test_mean_alpha_all_missing_returns_none(self):
        from alphalens_cli.commands.research import _mean_alpha

        rows = [{"alpha_120d": None}, {"alpha_120d": float("nan")}]
        mean, n = _mean_alpha(rows, 120)
        self.assertIsNone(mean)
        self.assertEqual(n, 0)


class TestRatingNormalization(unittest.TestCase):
    """Upstream decisions occasionally arrive with surrounding whitespace or
    variant casing. Accepted/rejected classification must be robust."""

    def test_accepted_buy_with_trailing_whitespace(self):
        from alphalens_cli.commands.research import _classify_rating

        self.assertEqual(_classify_rating(" BUY "), "BUY")
        self.assertEqual(_classify_rating("Overweight"), "OVERWEIGHT")

    def test_rejected_sell_normalizes(self):
        from alphalens_cli.commands.research import _classify_rating

        self.assertEqual(_classify_rating("sell"), "SELL")

    def test_missing_rating_becomes_empty_string(self):
        from alphalens_cli.commands.research import _classify_rating

        self.assertEqual(_classify_rating(None), "")
        self.assertEqual(_classify_rating(""), "")


if __name__ == "__main__":
    unittest.main()
