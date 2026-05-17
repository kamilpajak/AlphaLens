import datetime as dt
import unittest

import numpy as np
import pandas as pd

from alphalens.thematic.screening import technicals_signal


def _ohlcv(n: int = 90, start_price: float = 100.0, trend: float = 0.001, vol: float = 0.01):
    """Generate a deterministic OHLCV frame with mild upward drift."""
    rng = np.random.default_rng(seed=42)
    dates = pd.date_range(end=pd.Timestamp("2026-04-14"), periods=n, freq="B")
    daily_ret = rng.normal(loc=trend, scale=vol, size=n)
    close = start_price * np.cumprod(1 + daily_ret)
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    opens = (high + low) / 2
    volume = rng.integers(500_000, 2_000_000, size=n).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": high, "low": low, "close": close, "volume": volume}, index=dates
    )


class TestComputeRSI(unittest.TestCase):
    def test_rsi_in_valid_range(self):
        df = _ohlcv()
        rsi = technicals_signal._compute_rsi(df["close"], period=14)
        self.assertTrue(0.0 <= rsi <= 100.0, f"RSI out of range: {rsi}")

    def test_rsi_returns_none_when_insufficient_history(self):
        df = _ohlcv(n=5)
        self.assertIsNone(technicals_signal._compute_rsi(df["close"], period=14))


class TestMaDistance(unittest.TestCase):
    def test_ma_distance_pct(self):
        # Synthetic flat-then-rally: close > MA50 -> positive distance.
        n = 60
        close = pd.Series([100.0] * 50 + [120.0] * 10)
        d = technicals_signal._ma_distance_pct(close, period=50)
        # Last close = 120; MA50 over (last 50 closes ≈ [100..120]).
        # Should be > 0 (price above MA50).
        self.assertGreater(d, 0.0)

    def test_ma_distance_none_when_insufficient_history(self):
        close = pd.Series([100.0] * 20)
        self.assertIsNone(technicals_signal._ma_distance_pct(close, period=50))


class TestComputeATR(unittest.TestCase):
    def test_atr_pct_returns_positive(self):
        df = _ohlcv()
        atr = technicals_signal._compute_atr_pct(df, period=14)
        self.assertGreater(atr, 0.0)

    def test_atr_none_when_insufficient_history(self):
        df = _ohlcv(n=5)
        self.assertIsNone(technicals_signal._compute_atr_pct(df, period=14))


class TestVolumeZScore(unittest.TestCase):
    def test_zscore_zero_for_flat_volume(self):
        vol = pd.Series([1_000_000.0] * 30)
        # Flat volume → std=0 → return 0.0 (defined as "no spike").
        z = technicals_signal._volume_zscore(vol, period=20)
        self.assertEqual(z, 0.0)

    def test_zscore_positive_for_volume_spike(self):
        vol = pd.Series([1_000_000.0] * 20 + [5_000_000.0])
        z = technicals_signal._volume_zscore(vol, period=20)
        self.assertGreater(z, 0.0)

    def test_zscore_none_when_insufficient_history(self):
        vol = pd.Series([1_000_000.0] * 5)
        self.assertIsNone(technicals_signal._volume_zscore(vol, period=20))


class TestScoreTechnicals(unittest.TestCase):
    def test_returns_full_dict_for_sufficient_history(self):
        df = _ohlcv(n=90)
        out = technicals_signal.score_technicals_from_frame(df)
        for key in ("rsi", "ma50_distance_pct", "atr_pct", "volume_zscore", "summary"):
            self.assertIn(key, out)
        self.assertIsNotNone(out["rsi"])
        self.assertIsInstance(out["summary"], str)
        self.assertIn("RSI", out["summary"])

    def test_returns_none_dict_for_empty_frame(self):
        out = technicals_signal.score_technicals_from_frame(pd.DataFrame())
        self.assertIsNone(out["rsi"])
        self.assertIsNone(out["ma50_distance_pct"])
        self.assertIsNone(out["atr_pct"])
        self.assertIsNone(out["volume_zscore"])
        # Summary is "no data" string, not None.
        self.assertIn("no data", out["summary"].lower())

    def test_score_technicals_falls_back_to_none_when_loader_returns_empty(self):
        # Top-level entry point with a stubbed loader.
        out = technicals_signal.score_technicals(
            ticker="UNKN",
            asof=dt.date(2026, 5, 15),
            loader=lambda ticker, asof: pd.DataFrame(),
        )
        self.assertIsNone(out["rsi"])
        self.assertIn("no data", out["summary"].lower())


if __name__ == "__main__":
    unittest.main()
