"""TDD for `MarketCharacteristicsStore` — per-ticker rolling spread/vol/ADV.

Point-in-time contract: query at date `t` uses only bars <= `t`.
"""

import unittest
from datetime import date

import numpy as np
import pandas as pd


def _history(prices: list[float], volumes: list[int] | None = None, start="2024-01-01"):
    """Build an OHLCV DataFrame with synthetic bars. High = close*1.01, Low = close*0.99."""
    idx = pd.bdate_range(start=start, periods=len(prices))
    if volumes is None:
        volumes = [100_000] * len(prices)
    df = pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": volumes,
        },
        index=idx,
    )
    return df


def _history_with_random_walk(n: int, seed: int = 0, start_price: float = 100.0):
    """Generate random-walk OHLCV bars. High-Low range is real (spread estimator needs it)."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.01, size=n)
    closes = start_price * np.cumprod(1.0 + rets)
    intraday_range = rng.uniform(0.005, 0.02, size=n)  # 0.5%-2% high-low range
    opens = closes * (1.0 + rng.normal(0.0, 0.002, size=n))
    highs = np.maximum(opens, closes) * (1.0 + intraday_range / 2.0)
    lows = np.minimum(opens, closes) * (1.0 - intraday_range / 2.0)
    vols = rng.integers(500_000, 5_000_000, size=n)
    idx = pd.bdate_range(start="2024-01-01", periods=n)
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
        },
        index=idx,
    )


class TestMarketCharsStorePrime(unittest.TestCase):
    def test_prime_populates_all_characteristics(self):
        from alphalens.backtest.history_store import HistoryStore
        from alphalens.backtest.market_chars_store import MarketCharacteristicsStore

        histories = {"AAPL": _history_with_random_walk(60, seed=1)}
        store = MarketCharacteristicsStore(
            HistoryStore(histories), spread_window=21, vol_window=20, adv_window=20
        )
        store.prime(["AAPL"], start=date(2024, 2, 1), end=date(2024, 3, 15))

        # After priming, every in-range weekday should yield non-NaN values
        # for a ticker with enough history preceding the query.
        val = store.spread_at("AAPL", date(2024, 3, 15))
        self.assertIsNotNone(val)
        self.assertGreater(val, 0.0)

    def test_unknown_ticker_returns_none(self):
        from alphalens.backtest.history_store import HistoryStore
        from alphalens.backtest.market_chars_store import MarketCharacteristicsStore

        histories = {"AAPL": _history_with_random_walk(60, seed=1)}
        store = MarketCharacteristicsStore(HistoryStore(histories))
        store.prime(["AAPL"], start=date(2024, 2, 1), end=date(2024, 3, 1))

        self.assertIsNone(store.spread_at("MSFT", date(2024, 3, 1)))
        self.assertIsNone(store.volatility_at("MSFT", date(2024, 3, 1)))
        self.assertIsNone(store.adv_dollar_at("MSFT", date(2024, 3, 1)))


class TestMarketCharsStoreVolatility(unittest.TestCase):
    def test_volatility_is_close_to_close_std(self):
        from alphalens.backtest.history_store import HistoryStore
        from alphalens.backtest.market_chars_store import MarketCharacteristicsStore

        # Constant 1% daily return ⇒ std == 0 (no variance in log-returns).
        closes = [100.0 * (1.01 ** i) for i in range(60)]
        histories = {"AAPL": _history(closes)}
        store = MarketCharacteristicsStore(HistoryStore(histories), vol_window=20)
        store.prime(["AAPL"], start=date(2024, 2, 1), end=date(2024, 3, 15))

        vol = store.volatility_at("AAPL", date(2024, 3, 15))
        self.assertIsNotNone(vol)
        # Deterministic log-returns of ln(1.01) every day ⇒ std ≈ 0
        self.assertLess(vol, 1e-8)

    def test_random_vol_is_positive(self):
        from alphalens.backtest.history_store import HistoryStore
        from alphalens.backtest.market_chars_store import MarketCharacteristicsStore

        histories = {"AAPL": _history_with_random_walk(60, seed=7)}
        store = MarketCharacteristicsStore(HistoryStore(histories))
        store.prime(["AAPL"], start=date(2024, 2, 1), end=date(2024, 3, 15))

        vol = store.volatility_at("AAPL", date(2024, 3, 15))
        self.assertGreater(vol, 0.0)


class TestMarketCharsStoreADV(unittest.TestCase):
    def test_adv_dollar_equals_close_times_volume_mean(self):
        from alphalens.backtest.history_store import HistoryStore
        from alphalens.backtest.market_chars_store import MarketCharacteristicsStore

        closes = [100.0] * 60
        volumes = [1_000_000] * 60
        histories = {"AAPL": _history(closes, volumes=volumes)}
        store = MarketCharacteristicsStore(HistoryStore(histories), adv_window=20)
        store.prime(["AAPL"], start=date(2024, 2, 1), end=date(2024, 3, 15))

        adv = store.adv_dollar_at("AAPL", date(2024, 3, 15))
        self.assertIsNotNone(adv)
        # Expected: 100 * 1_000_000 = 100_000_000
        self.assertAlmostEqual(adv, 100_000_000.0, delta=1.0)


class TestMarketCharsStorePointInTime(unittest.TestCase):
    def test_query_uses_only_data_up_to_asof(self):
        from alphalens.backtest.history_store import HistoryStore
        from alphalens.backtest.market_chars_store import MarketCharacteristicsStore

        # Two regimes: first 30 bars low-vol, next 30 bars high-vol.
        rng = np.random.default_rng(99)
        low_vol_closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.001, size=30))
        high_vol_closes = float(low_vol_closes[-1]) * np.cumprod(
            1.0 + rng.normal(0.0, 0.05, size=30)
        )
        closes = list(low_vol_closes) + list(high_vol_closes)
        volumes = [1_000_000] * 60
        histories = {"AAPL": _history(closes, volumes=volumes)}
        store = MarketCharacteristicsStore(HistoryStore(histories), vol_window=20)
        store.prime(["AAPL"], start=date(2024, 2, 1), end=date(2024, 3, 30))

        # Query inside the low-vol regime (bar 20).
        idx = pd.bdate_range("2024-01-01", periods=60)
        vol_low_regime = store.volatility_at("AAPL", idx[25].date())
        vol_high_regime = store.volatility_at("AAPL", idx[55].date())

        self.assertIsNotNone(vol_low_regime)
        self.assertIsNotNone(vol_high_regime)
        # High-vol regime must have materially higher volatility.
        self.assertGreater(vol_high_regime, vol_low_regime * 3.0)


class TestMarketCharsStoreBeforeWindow(unittest.TestCase):
    def test_query_before_window_returns_none(self):
        from alphalens.backtest.history_store import HistoryStore
        from alphalens.backtest.market_chars_store import MarketCharacteristicsStore

        histories = {"AAPL": _history_with_random_walk(60, seed=3)}
        store = MarketCharacteristicsStore(
            HistoryStore(histories), spread_window=21, vol_window=20, adv_window=20
        )
        store.prime(["AAPL"], start=date(2024, 2, 1), end=date(2024, 3, 15))

        # First bar — window hasn't filled yet.
        idx = pd.bdate_range("2024-01-01", periods=60)
        first_date = idx[0].date()
        self.assertIsNone(store.spread_at("AAPL", first_date))
        self.assertIsNone(store.volatility_at("AAPL", first_date))
        self.assertIsNone(store.adv_dollar_at("AAPL", first_date))


if __name__ == "__main__":
    unittest.main()
