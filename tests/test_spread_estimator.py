"""TDD tests for EDGE / Abdi-Ranaldo / Corwin-Schultz spread estimators.

EDGE (Ardia, Guidotti, Kroencke, JFE 2024) is the primary estimator.
AR (2017) and CS (2012) ship as sanity-check fallbacks.
"""

import unittest

import numpy as np
import pandas as pd


def _simulate_ohlc_with_spread(
    n_days: int,
    spread: float,
    seed: int = 42,
    start_price: float = 100.0,
    drift: float = 0.0,
    daily_vol: float = 0.01,
    trades_per_day: int = 100,
) -> pd.DataFrame:
    """Generate synthetic OHLCV bars from a random-walk mid-price with bid-ask bounce.

    Each day simulates `trades_per_day` trades. Mid-price walks geometrically.
    Each trade lands at bid (mid * (1 - spread/2)) or ask (mid * (1 + spread/2))
    with equal probability. OHLC are extracted from the trade sequence.

    Returns a DataFrame with DatetimeIndex and columns
    ``open, high, low, close, volume``.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    intraday_vol = daily_vol / np.sqrt(trades_per_day)
    half_spread = spread / 2.0

    bars = []
    p_prev_close = start_price
    for _ in range(n_days):
        mid_returns = rng.normal(drift / trades_per_day, intraday_vol, size=trades_per_day)
        mids = p_prev_close * np.cumprod(1.0 + mid_returns)
        signs = rng.choice([-1.0, 1.0], size=trades_per_day)
        trade_prices = mids * (1.0 + signs * half_spread)
        open_ = float(trade_prices[0])
        close = float(trade_prices[-1])
        high = float(trade_prices.max())
        low = float(trade_prices.min())
        bars.append(
            {"open": open_, "high": high, "low": low, "close": close, "volume": 100_000}
        )
        p_prev_close = close

    return pd.DataFrame(bars, index=dates)


class TestEdgeSpreadSinglePeriod(unittest.TestCase):
    """`edge_spread_single` returns a scalar estimate from a single OHLC window."""

    def test_recovers_known_2pct_spread(self):
        from alphalens.backtest.spread_estimator import edge_spread_single

        df = _simulate_ohlc_with_spread(n_days=250, spread=0.02, seed=1)
        est = edge_spread_single(
            df["open"].to_numpy(),
            df["high"].to_numpy(),
            df["low"].to_numpy(),
            df["close"].to_numpy(),
        )
        # Tolerance ±35% on a 250-bar sample — EDGE paper reports ~20% RMS error
        # at this sample size but worst-case single-seed draws run wider.
        self.assertGreater(est, 0.013)
        self.assertLess(est, 0.027)

    def test_recovers_known_50bps_spread(self):
        from alphalens.backtest.spread_estimator import edge_spread_single

        df = _simulate_ohlc_with_spread(n_days=500, spread=0.005, seed=2)
        est = edge_spread_single(
            df["open"].to_numpy(),
            df["high"].to_numpy(),
            df["low"].to_numpy(),
            df["close"].to_numpy(),
        )
        # Tighter spread, longer sample — still allow ±40% because small estimators
        # have higher relative error at the low end.
        self.assertGreater(est, 0.003)
        self.assertLess(est, 0.007)

    def test_returns_nan_for_short_series(self):
        from alphalens.backtest.spread_estimator import edge_spread_single

        # <3 observations → estimator undefined per paper.
        o = np.array([100.0, 101.0])
        h = np.array([101.0, 102.0])
        l = np.array([99.0, 100.0])
        c = np.array([100.5, 101.5])
        self.assertTrue(np.isnan(edge_spread_single(o, h, l, c)))

    def test_returns_nan_for_constant_prices(self):
        from alphalens.backtest.spread_estimator import edge_spread_single

        flat = np.full(50, 100.0)
        est = edge_spread_single(flat, flat, flat, flat)
        # No variation → no signal to extract; estimator should return NaN.
        self.assertTrue(np.isnan(est))

    def test_mismatched_lengths_raise(self):
        from alphalens.backtest.spread_estimator import edge_spread_single

        with self.assertRaises(ValueError):
            edge_spread_single(
                np.array([1.0, 2.0]),
                np.array([1.0, 2.0, 3.0]),
                np.array([1.0, 2.0]),
                np.array([1.0, 2.0]),
            )


class TestEdgeSpreadRolling(unittest.TestCase):
    """`edge_spread` applies the estimator over a rolling window, returns pd.Series."""

    def test_returns_series_aligned_with_input_index(self):
        from alphalens.backtest.spread_estimator import edge_spread

        df = _simulate_ohlc_with_spread(n_days=100, spread=0.02, seed=3)
        s = edge_spread(df["open"], df["high"], df["low"], df["close"], window=21)
        self.assertIsInstance(s, pd.Series)
        self.assertTrue(s.index.equals(df.index))

    def test_nan_before_window_fills(self):
        from alphalens.backtest.spread_estimator import edge_spread

        df = _simulate_ohlc_with_spread(n_days=30, spread=0.01, seed=4)
        s = edge_spread(df["open"], df["high"], df["low"], df["close"], window=21)
        # First ~20 bars should be NaN — need a full window.
        self.assertTrue(s.iloc[:19].isna().all())
        # Later bars should be finite.
        self.assertTrue(s.iloc[25:].notna().any())

    def test_rolling_estimate_tracks_true_spread(self):
        from alphalens.backtest.spread_estimator import edge_spread

        df = _simulate_ohlc_with_spread(n_days=300, spread=0.02, seed=5)
        s = edge_spread(df["open"], df["high"], df["low"], df["close"], window=21)
        # Mean over settled window should be in ballpark of true spread.
        mean_est = s.dropna().mean()
        self.assertGreater(mean_est, 0.012)
        self.assertLess(mean_est, 0.028)

    def test_negative_estimates_clipped_to_zero(self):
        from alphalens.backtest.spread_estimator import edge_spread

        # Construct a series where the estimator would naturally produce
        # zero/near-zero or negative signed estimates. Flat high-low-close
        # combined with random opens makes EDGE's signed variant go negative.
        n = 50
        idx = pd.bdate_range("2024-01-01", periods=n)
        c = pd.Series(100.0, index=idx)
        h = pd.Series(100.001, index=idx)
        l = pd.Series(99.999, index=idx)
        o = c.copy()
        s = edge_spread(o, h, l, c, window=21)
        # All non-NaN values must be >= 0 (clipping applied).
        valid = s.dropna()
        if not valid.empty:
            self.assertTrue((valid >= 0).all())


class TestAbdiRanaldoFallback(unittest.TestCase):
    """AR (2017) estimator — sanity-check fallback."""

    def test_returns_series(self):
        from alphalens.backtest.spread_estimator import abdi_ranaldo_spread

        df = _simulate_ohlc_with_spread(n_days=100, spread=0.02, seed=6)
        s = abdi_ranaldo_spread(df["high"], df["low"], df["close"], window=21)
        self.assertIsInstance(s, pd.Series)
        self.assertTrue(s.index.equals(df.index))

    def test_positive_estimates_on_spread_sample(self):
        from alphalens.backtest.spread_estimator import abdi_ranaldo_spread

        df = _simulate_ohlc_with_spread(n_days=300, spread=0.02, seed=7)
        s = abdi_ranaldo_spread(df["high"], df["low"], df["close"], window=21)
        valid = s.dropna()
        # Noisy estimator; expect at least half the values strictly positive.
        self.assertGreater((valid > 0).mean(), 0.5)

    def test_negative_estimates_clipped(self):
        from alphalens.backtest.spread_estimator import abdi_ranaldo_spread

        # Flat bars — AR can produce negatives that we clip.
        n = 50
        idx = pd.bdate_range("2024-01-01", periods=n)
        c = pd.Series(100.0, index=idx)
        h = pd.Series(100.001, index=idx)
        l = pd.Series(99.999, index=idx)
        s = abdi_ranaldo_spread(h, l, c, window=21)
        valid = s.dropna()
        if not valid.empty:
            self.assertTrue((valid >= 0).all())


class TestCorwinSchultzFallback(unittest.TestCase):
    """CS (2012) estimator — sanity-check fallback."""

    def test_returns_series(self):
        from alphalens.backtest.spread_estimator import corwin_schultz_spread

        df = _simulate_ohlc_with_spread(n_days=100, spread=0.02, seed=8)
        s = corwin_schultz_spread(df["high"], df["low"], window=21)
        self.assertIsInstance(s, pd.Series)
        self.assertTrue(s.index.equals(df.index))

    def test_positive_estimates_on_spread_sample(self):
        from alphalens.backtest.spread_estimator import corwin_schultz_spread

        df = _simulate_ohlc_with_spread(n_days=300, spread=0.02, seed=9)
        s = corwin_schultz_spread(df["high"], df["low"], window=21)
        valid = s.dropna()
        self.assertGreater((valid > 0).mean(), 0.5)

    def test_negative_estimates_clipped(self):
        from alphalens.backtest.spread_estimator import corwin_schultz_spread

        n = 50
        idx = pd.bdate_range("2024-01-01", periods=n)
        h = pd.Series(100.001, index=idx)
        l = pd.Series(99.999, index=idx)
        s = corwin_schultz_spread(h, l, window=21)
        valid = s.dropna()
        if not valid.empty:
            self.assertTrue((valid >= 0).all())


if __name__ == "__main__":
    unittest.main()
