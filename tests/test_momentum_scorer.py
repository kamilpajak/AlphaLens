import unittest

import numpy as np
import pandas as pd


def _trending_up_df(start: float = 10.0, days: int = 260, slope: float = 0.05) -> pd.DataFrame:
    """Build an uptrending OHLCV frame: steady daily gain, expanding volume at end."""
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    close = np.array([start * (1 + slope) ** (i / days * 20) for i in range(days)])
    vol = np.full(days, 1_500_000.0)
    vol[-5:] = 5_000_000.0  # recent surge
    return pd.DataFrame(
        {
            "Open": close * 0.995,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": vol,
        },
        index=idx,
    )


def _flat_df(price: float = 10.0, days: int = 260) -> pd.DataFrame:
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    return pd.DataFrame(
        {
            "Open": [price] * days,
            "High": [price] * days,
            "Low": [price] * days,
            "Close": [price] * days,
            "Volume": [1_500_000.0] * days,
        },
        index=idx,
    )


class TestNearHighScore(unittest.TestCase):
    def test_at_52w_high_full_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        df = _trending_up_df()
        score = MomentumScorer._near_high_score(df, pct=0.15)
        self.assertGreaterEqual(score, 0.95)

    def test_far_below_high_zero_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        df = _trending_up_df()
        # Move last close way below high
        df = df.copy()
        df.loc[df.index[-1], "Close"] = df["High"].max() * 0.5
        score = MomentumScorer._near_high_score(df, pct=0.15)
        self.assertLess(score, 0.3)


class TestPct20dScore(unittest.TestCase):
    def test_positive_move_high_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        df = _trending_up_df()
        s = MomentumScorer._pct_20d_score(df)
        self.assertGreater(s, 0.5)

    def test_negative_move_low_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        df = _flat_df()
        df = df.copy()
        df.loc[df.index[-20:], "Close"] = np.linspace(10.0, 8.0, 20)
        s = MomentumScorer._pct_20d_score(df)
        self.assertLess(s, 0.5)

    def test_flat_midpoint(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._pct_20d_score(_flat_df())
        self.assertAlmostEqual(s, 0.5, places=2)


class TestVolumeSurgeScore(unittest.TestCase):
    def test_2x_surge_full_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        df = _flat_df()
        df = df.copy()
        df.loc[df.index[-1], "Volume"] = 3_000_000  # 2x the 1.5M baseline
        s = MomentumScorer._volume_surge_score(df, threshold=2.0)
        self.assertGreaterEqual(s, 0.9)

    def test_no_surge_low_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._volume_surge_score(_flat_df(), threshold=2.0)
        self.assertLess(s, 0.6)


class TestRelStrengthScore(unittest.TestCase):
    def test_outperforms_benchmark_high_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        stock = _trending_up_df(slope=0.10)
        bench = _trending_up_df(slope=0.02)
        s = MomentumScorer._rel_strength_score(stock, bench)
        self.assertGreater(s, 0.6)

    def test_underperforms_benchmark_low_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        stock = _trending_up_df(slope=0.02)
        bench = _trending_up_df(slope=0.10)
        s = MomentumScorer._rel_strength_score(stock, bench)
        self.assertLess(s, 0.4)

    def test_no_benchmark_neutral(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._rel_strength_score(_flat_df(), None)
        self.assertEqual(s, 0.5)


class TestRsiScore(unittest.TestCase):
    def test_rsi_in_window_full_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._rsi_score(60, low=50, high=75)
        self.assertEqual(s, 1.0)

    def test_rsi_below_window(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._rsi_score(40, low=50, high=75)
        self.assertLess(s, 1.0)

    def test_rsi_overbought_tapers(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._rsi_score(85, low=50, high=75)
        self.assertLess(s, 1.0)

    def test_rsi_none_neutral(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        self.assertEqual(MomentumScorer._rsi_score(None), 0.5)


class TestAdxScore(unittest.TestCase):
    def test_strong_trend_full_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        self.assertEqual(MomentumScorer._adx_score(30, threshold=25), 1.0)

    def test_weak_trend_proportional(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._adx_score(10, threshold=25)
        self.assertAlmostEqual(s, 10 / 25, places=2)


class TestMacdScore(unittest.TestCase):
    def test_positive_hist_high_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._macd_score(0.5)
        self.assertGreater(s, 0.6)

    def test_negative_hist_low_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        s = MomentumScorer._macd_score(-0.5)
        self.assertLess(s, 0.4)

    def test_none_neutral(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        self.assertEqual(MomentumScorer._macd_score(None), 0.5)


class TestScoreAll(unittest.TestCase):
    def test_returns_expected_columns(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        scorer = MomentumScorer()
        prices = {"ABC": _trending_up_df(), "SPY": _flat_df()}
        result = scorer.score_all(["ABC"], prices, benchmark_ticker="SPY")
        expected = {
            "ticker",
            "near_high_score",
            "pct_20d_score",
            "volume_surge_score",
            "rel_strength_score",
            "rsi_score",
            "adx_score",
            "macd_score",
            "momentum_score",
        }
        self.assertEqual(expected, set(result.columns))

    def test_uptrending_scores_higher_than_flat(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        scorer = MomentumScorer()
        prices = {"UP": _trending_up_df(), "FLAT": _flat_df(), "SPY": _flat_df()}
        result = scorer.score_all(["UP", "FLAT"], prices, benchmark_ticker="SPY")
        up_score = float(result.loc[result.ticker == "UP", "momentum_score"].iloc[0])
        flat_score = float(result.loc[result.ticker == "FLAT", "momentum_score"].iloc[0])
        self.assertGreater(up_score, flat_score)

    def test_missing_price_data_gets_zero_score(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        scorer = MomentumScorer()
        result = scorer.score_all(["MISSING"], {"SPY": _flat_df()}, benchmark_ticker="SPY")
        score = float(result.loc[result.ticker == "MISSING", "momentum_score"].iloc[0])
        self.assertEqual(score, 0.0)

    def test_composite_score_in_valid_range(self):
        from alphalens.momentum_screener.momentum_scorer import MomentumScorer

        scorer = MomentumScorer()
        prices = {"ABC": _trending_up_df(), "SPY": _flat_df()}
        result = scorer.score_all(["ABC"], prices, benchmark_ticker="SPY")
        score = float(result["momentum_score"].iloc[0])
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
