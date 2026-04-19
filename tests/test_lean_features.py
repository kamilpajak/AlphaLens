import unittest

import numpy as np
import pandas as pd


class TestRateOfChange(unittest.TestCase):
    def test_positive_return(self):
        from alphalens.lean_screener.lean_project.features import rate_of_change

        series = pd.Series([100.0, 105.0, 110.0, 115.0, 120.0])
        self.assertAlmostEqual(rate_of_change(series, 4), 0.20, places=6)

    def test_zero_when_unchanged(self):
        from alphalens.lean_screener.lean_project.features import rate_of_change

        self.assertAlmostEqual(rate_of_change([100.0, 100.0, 100.0], 2), 0.0)

    def test_insufficient_history_raises(self):
        from alphalens.lean_screener.lean_project.features import rate_of_change

        with self.assertRaises(ValueError):
            rate_of_change([100.0, 101.0], 5)

    def test_past_zero_returns_nan(self):
        from alphalens.lean_screener.lean_project.features import rate_of_change

        result = rate_of_change([0.0, 1.0, 2.0], 2)
        self.assertTrue(np.isnan(result))


class TestSMA(unittest.TestCase):
    def test_average_of_last_window(self):
        from alphalens.lean_screener.lean_project.features import sma

        self.assertAlmostEqual(sma([10, 20, 30, 40, 50], 3), 40.0)


class TestVolumeSurprise(unittest.TestCase):
    def test_above_average(self):
        from alphalens.lean_screener.lean_project.features import volume_surprise

        vol = [100] * 10 + [300]  # 10-day avg = 100, today = 300
        self.assertAlmostEqual(volume_surprise(vol, 10), 3.0)

    def test_below_average(self):
        from alphalens.lean_screener.lean_project.features import volume_surprise

        vol = [100] * 10 + [50]
        self.assertAlmostEqual(volume_surprise(vol, 10), 0.5)

    def test_zero_avg_returns_nan(self):
        from alphalens.lean_screener.lean_project.features import volume_surprise

        self.assertTrue(np.isnan(volume_surprise([0] * 10 + [100], 10)))


class TestDistanceToHigh(unittest.TestCase):
    def test_at_high_returns_zero(self):
        from alphalens.lean_screener.lean_project.features import distance_to_high

        self.assertAlmostEqual(distance_to_high([90, 100, 95, 100], 4), 0.0)

    def test_half_returns_0_5(self):
        from alphalens.lean_screener.lean_project.features import distance_to_high

        self.assertAlmostEqual(distance_to_high([100, 80, 90, 50], 4), 0.5)

    def test_clipped_to_unit_interval(self):
        from alphalens.lean_screener.lean_project.features import distance_to_high

        # Even if last close is 0, distance = 1 - 0/100 = 1
        self.assertLessEqual(distance_to_high([100, 50, 25, 0], 4), 1.0)


class TestBreakout(unittest.TestCase):
    def test_true_when_above_high_with_volume(self):
        from alphalens.lean_screener.lean_project.features import breakout

        close = [100] * 20 + [101]  # new high (prior max was 100)
        volume = [100] * 20 + [200]  # 2x avg
        self.assertTrue(breakout(close, volume, 20, volume_multiple=1.5))

    def test_false_when_below_high(self):
        from alphalens.lean_screener.lean_project.features import breakout

        close = [100] * 20 + [99]  # not a new high
        volume = [100] * 20 + [500]
        self.assertFalse(breakout(close, volume, 20, volume_multiple=1.5))

    def test_false_without_volume_confirmation(self):
        from alphalens.lean_screener.lean_project.features import breakout

        close = [100] * 20 + [105]  # high, but anaemic volume
        volume = [100] * 20 + [110]
        self.assertFalse(breakout(close, volume, 20, volume_multiple=1.5))


class TestTrendStrength(unittest.TestCase):
    def test_full_uptrend_scores_one(self):
        from alphalens.lean_screener.lean_project.features import trend_strength

        close = list(range(1, 201))  # steadily rising 1..200
        self.assertAlmostEqual(
            trend_strength(close, sma_short=5, sma_medium=20, sma_long=50), 1.0
        )

    def test_full_downtrend_scores_zero(self):
        from alphalens.lean_screener.lean_project.features import trend_strength

        close = list(range(200, 0, -1))
        self.assertAlmostEqual(
            trend_strength(close, sma_short=5, sma_medium=20, sma_long=50), 0.0
        )

    def test_invalid_window_order_raises(self):
        from alphalens.lean_screener.lean_project.features import trend_strength

        with self.assertRaises(ValueError):
            trend_strength([1] * 200, sma_short=20, sma_medium=20, sma_long=50)


class TestDollarVolumeAverage(unittest.TestCase):
    def test_constant_case(self):
        from alphalens.lean_screener.lean_project.features import (
            dollar_volume_average,
        )

        close = [10.0] * 20
        volume = [1000] * 20
        self.assertAlmostEqual(dollar_volume_average(close, volume, 20), 10000.0)


class TestZScore(unittest.TestCase):
    def test_standard_normal_distribution(self):
        from alphalens.lean_screener.lean_project.features import z_score

        out = z_score([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(float(np.mean(out)), 0.0, places=6)
        self.assertAlmostEqual(float(np.std(out)), 1.0, places=6)

    def test_constant_returns_zeros(self):
        from alphalens.lean_screener.lean_project.features import z_score

        out = z_score([5.0, 5.0, 5.0])
        self.assertTrue(np.all(out == 0.0))


if __name__ == "__main__":
    unittest.main()
