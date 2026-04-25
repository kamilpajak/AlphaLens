import unittest

import numpy as np
import pandas as pd


def _daily(values: list[float], start: str = "2020-01-02") -> pd.Series:
    idx = pd.date_range(start=start, periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


class TestYieldCurveSlope(unittest.TestCase):
    def test_slope_is_dgs10_minus_dgs2(self):
        from alphalens.macro.signals import yield_curve_slope

        dgs10 = _daily([3.0, 3.1, 3.2])
        dgs2 = _daily([2.0, 2.1, 2.2])

        slope = yield_curve_slope(dgs10, dgs2)

        pd.testing.assert_series_equal(slope, _daily([1.0, 1.0, 1.0]), check_names=False)

    def test_slope_intersects_indexes(self):
        from alphalens.macro.signals import yield_curve_slope

        dgs10 = _daily([3.0, 3.1, 3.2, 3.3])
        # dgs2 missing first date
        dgs2 = pd.Series(
            [2.0, 2.1, 2.2],
            index=dgs10.index[1:],
            dtype=float,
        )

        slope = yield_curve_slope(dgs10, dgs2)

        self.assertEqual(len(slope), 3)
        self.assertListEqual(list(slope.index), list(dgs10.index[1:]))


class TestVixDecile(unittest.TestCase):
    def test_percentile_rank_returns_in_unit_interval(self):
        from alphalens.macro.signals import vix_decile

        # linear ramp so the last value is the highest → rank → 1.0
        vix = _daily(list(range(1, 261)))  # 260 days

        decile = vix_decile(vix, lookback=252)

        # First 251 values → NaN (not enough history)
        self.assertTrue(decile.iloc[:251].isna().all())
        valid = decile.dropna()
        self.assertGreaterEqual(valid.min(), 0.0)
        self.assertLessEqual(valid.max(), 1.0)
        # linear ramp + last-in-window = rank_max → 1.0
        self.assertAlmostEqual(decile.iloc[-1], 1.0, places=6)

    def test_small_lookback(self):
        from alphalens.macro.signals import vix_decile

        vix = _daily([10.0, 20.0, 15.0, 25.0, 12.0])
        decile = vix_decile(vix, lookback=3)

        # First 2 NaN; last three: rank of current in trailing-3 window
        self.assertTrue(np.isnan(decile.iloc[0]))
        self.assertTrue(np.isnan(decile.iloc[1]))
        # window [10,20,15], current=15 → rank 2/3
        self.assertAlmostEqual(decile.iloc[2], 2 / 3, places=6)
        # window [20,15,25], current=25 → rank 3/3
        self.assertAlmostEqual(decile.iloc[3], 1.0, places=6)


class TestTrailingReturnSpread(unittest.TestCase):
    def test_basic_spread(self):
        from alphalens.macro.signals import trailing_return_spread

        # leader doubles, laggard flat → spread = 1.0 (100 pp)
        leader = _daily([100.0, 100.0, 100.0, 200.0])
        laggard = _daily([100.0, 100.0, 100.0, 100.0])

        spread = trailing_return_spread(leader, laggard, lookback=3)

        self.assertTrue(np.isnan(spread.iloc[0]))
        self.assertTrue(np.isnan(spread.iloc[1]))
        self.assertTrue(np.isnan(spread.iloc[2]))
        self.assertAlmostEqual(spread.iloc[3], 1.0, places=6)

    def test_negative_spread_when_laggard_leads(self):
        from alphalens.macro.signals import trailing_return_spread

        leader = _daily([100.0, 100.0, 100.0, 110.0])
        laggard = _daily([100.0, 100.0, 100.0, 120.0])

        spread = trailing_return_spread(leader, laggard, lookback=3)

        self.assertAlmostEqual(spread.iloc[-1], 0.10 - 0.20, places=6)

    def test_zero_spread_when_identical(self):
        from alphalens.macro.signals import trailing_return_spread

        a = _daily([100.0, 101.0, 102.0, 103.0])
        spread = trailing_return_spread(a, a, lookback=3)
        self.assertAlmostEqual(spread.iloc[-1], 0.0, places=9)


class TestBuildSignalSet(unittest.TestCase):
    def test_bundles_all_signals_indexed_by_date(self):
        from alphalens.macro.signals import SignalSet, build_signal_set

        n = 260
        dgs10 = _daily([3.0] * n)
        dgs2 = _daily([2.0] * n)
        vix = _daily(list(np.linspace(10, 30, n)))
        qqq = _daily(list(np.linspace(200, 400, n)))
        iwm = _daily(list(np.linspace(150, 200, n)))

        signals = build_signal_set(dgs10=dgs10, dgs2=dgs2, vix=vix, qqq_close=qqq, iwm_close=iwm)

        self.assertIsInstance(signals, SignalSet)
        self.assertEqual(len(signals.yield_curve_slope), n)
        self.assertTrue(signals.vix_decile.dropna().notna().all())
        self.assertTrue(signals.qqq_iwm_spread.dropna().notna().all())

    def test_get_on_date_returns_scalar_dict(self):
        from alphalens.macro.signals import build_signal_set

        n = 260
        dgs10 = _daily([3.5] * n)
        dgs2 = _daily([1.5] * n)
        vix = _daily([15.0] * n)
        qqq = _daily(list(np.linspace(200, 400, n)))
        iwm = _daily(list(np.linspace(150, 200, n)))

        signals = build_signal_set(dgs10=dgs10, dgs2=dgs2, vix=vix, qqq_close=qqq, iwm_close=iwm)
        snap = signals.as_of(signals.yield_curve_slope.index[-1])

        self.assertAlmostEqual(snap["yield_curve_slope"], 2.0, places=6)
        self.assertIn("vix_decile", snap)
        self.assertIn("qqq_iwm_spread", snap)


if __name__ == "__main__":
    unittest.main()
