"""Unit tests for the market-state pure primitives (PR-0).

Every function under test is a stateless pure function of pandas input →
pandas output (no I/O, no store, no network). Fixtures are inline, mirroring
``test_macro_signals.py``. These primitives are the building blocks the PR-1
``market_state.classify`` will compose; here they are tested in isolation.
"""

import unittest

import numpy as np
import pandas as pd


def _bars(values: list[float], start: str = "2020-01-02") -> pd.Series:
    idx = pd.date_range(start=start, periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


class TestSma(unittest.TestCase):
    def test_sma_is_trailing_rolling_mean(self):
        from alphalens_pipeline.market.primitives import sma

        out = sma(_bars([1.0, 2.0, 3.0, 4.0]), window=2)

        # first value NaN (insufficient window), then trailing means
        self.assertTrue(np.isnan(out.iloc[0]))
        pd.testing.assert_series_equal(
            out.dropna(),
            _bars([1.5, 2.5, 3.5], start="2020-01-03"),
            check_names=False,
        )


class TestEma(unittest.TestCase):
    def test_ema_first_value_equals_first_observation(self):
        from alphalens_pipeline.market.primitives import ema

        out = ema(_bars([10.0, 20.0, 30.0]), window=3)

        # adjust=False EMA seeds on the first observation
        self.assertAlmostEqual(out.iloc[0], 10.0, places=9)
        # monotonic-increasing input → monotonic-increasing EMA, bounded by input
        self.assertTrue((out.diff().dropna() > 0).all())
        self.assertLess(out.iloc[-1], 30.0)


class TestNormalizedSlope(unittest.TestCase):
    def test_positive_when_rising_zero_when_flat(self):
        from alphalens_pipeline.market.primitives import normalized_slope

        rising = normalized_slope(_bars([1.0, 2.0, 3.0, 4.0]), window=1)
        flat = normalized_slope(_bars([5.0, 5.0, 5.0]), window=1)

        self.assertTrue((rising.dropna() > 0).all())
        self.assertTrue((flat.dropna() == 0).all())

    def test_normalizes_by_current_value(self):
        from alphalens_pipeline.market.primitives import normalized_slope

        # (12 - 10) / 12
        out = normalized_slope(_bars([10.0, 10.0, 12.0]), window=1)

        self.assertAlmostEqual(out.iloc[-1], 2.0 / 12.0, places=9)


class TestRollingQuantileRank(unittest.TestCase):
    def test_ramp_last_value_ranks_one(self):
        from alphalens_pipeline.market.primitives import rolling_quantile_rank

        out = rolling_quantile_rank(_bars(list(range(1, 11))), lookback=5)

        self.assertTrue(out.iloc[:4].isna().all())  # first lookback-1 NaN
        valid = out.dropna()
        self.assertGreaterEqual(valid.min(), 0.0)
        self.assertLessEqual(valid.max(), 1.0)
        self.assertAlmostEqual(out.iloc[-1], 1.0, places=9)  # last-in-window is max

    def test_lowest_in_window_ranks_one_over_lookback(self):
        from alphalens_pipeline.market.primitives import rolling_quantile_rank

        # descending ramp: the last value is the smallest in its window
        out = rolling_quantile_rank(_bars([5.0, 4.0, 3.0, 2.0, 1.0]), lookback=5)

        self.assertAlmostEqual(out.iloc[-1], 1.0 / 5.0, places=9)

    def test_raises_on_nonpositive_lookback(self):
        from alphalens_pipeline.market.primitives import rolling_quantile_rank

        with self.assertRaises(ValueError):
            rolling_quantile_rank(_bars([1.0, 2.0]), lookback=0)


class TestTrueRange(unittest.TestCase):
    def test_first_bar_is_high_minus_low(self):
        from alphalens_pipeline.market.primitives import true_range

        tr = true_range(_bars([10.0]), _bars([9.0]), _bars([9.5]))

        self.assertAlmostEqual(tr.iloc[0], 1.0, places=9)

    def test_uses_prior_close_gap_when_it_dominates(self):
        from alphalens_pipeline.market.primitives import true_range

        # bar1 opens far above prior close: gap |high - prev_close| = 5 dominates
        high = _bars([101.0, 105.0])
        low = _bars([99.0, 104.0])
        close = _bars([100.0, 104.5])

        tr = true_range(high, low, close)

        self.assertAlmostEqual(tr.iloc[1], 5.0, places=9)  # max(1, |105-100|, |104-100|)


class TestAtr(unittest.TestCase):
    def test_window_one_equals_true_range(self):
        from alphalens_pipeline.market.primitives import atr, true_range

        high = _bars([101.0, 105.0, 106.0])
        low = _bars([99.0, 104.0, 105.0])
        close = _bars([100.0, 104.5, 105.5])

        pd.testing.assert_series_equal(
            atr(high, low, close, window=1),
            true_range(high, low, close),
            check_names=False,
        )

    def test_constant_range_converges_to_that_range(self):
        from alphalens_pipeline.market.primitives import atr

        # every bar spans 2.0, close flat → true range 2.0 every bar → ATR 2.0
        n = 30
        high = _bars([101.0] * n)
        low = _bars([99.0] * n)
        close = _bars([100.0] * n)

        out = atr(high, low, close, window=14)

        self.assertAlmostEqual(out.iloc[-1], 2.0, places=6)


class TestAtrPct(unittest.TestCase):
    def test_atr_pct_is_atr_over_close(self):
        from alphalens_pipeline.market.primitives import atr, atr_pct

        n = 30
        high = _bars([101.0] * n)
        low = _bars([99.0] * n)
        close = _bars([100.0] * n)

        pct = atr_pct(high, low, close, window=14)
        expected = atr(high, low, close, window=14) / close

        pd.testing.assert_series_equal(pct, expected, check_names=False)


class TestBollingerKeltnerSqueeze(unittest.TestCase):
    def _quiet_then_jump(self):
        # 24 quiet bars (flat close, tiny intraday range), then a single close jump
        close = [100.0] * 24 + [130.0]
        high = [c + 0.05 for c in close]
        low = [c - 0.05 for c in close]
        return _bars(high), _bars(low), _bars(close)

    def test_squeeze_on_during_quiet_consolidation(self):
        from alphalens_pipeline.market.primitives import bollinger_keltner_squeeze

        high, low, close = self._quiet_then_jump()

        sq = bollinger_keltner_squeeze(
            close, high, low, bb_window=20, bb_k=2.0, kc_window=20, kc_mult=1.5
        )

        # index 20: window is all-quiet → BB collapses inside the ATR channel → ON
        self.assertTrue(bool(sq.iloc[20]))

    def test_squeeze_off_on_volatility_expansion(self):
        from alphalens_pipeline.market.primitives import bollinger_keltner_squeeze

        high, low, close = self._quiet_then_jump()

        sq = bollinger_keltner_squeeze(
            close, high, low, bb_window=20, bb_k=2.0, kc_window=20, kc_mult=1.5
        )

        # last bar: the close jump blows BB wider than the (smoothed) ATR channel → OFF
        self.assertFalse(bool(sq.iloc[-1]))

    def test_warmup_is_false(self):
        from alphalens_pipeline.market.primitives import bollinger_keltner_squeeze

        high, low, close = self._quiet_then_jump()

        sq = bollinger_keltner_squeeze(
            close, high, low, bb_window=20, bb_k=2.0, kc_window=20, kc_mult=1.5
        )

        # before enough history for both bands, the flag is False (never NaN-truthy)
        self.assertFalse(bool(sq.iloc[0]))
        self.assertEqual(sq.dtype, bool)


if __name__ == "__main__":
    unittest.main()
