import unittest

import pandas as pd


class TestVolumeScorerStatic(unittest.TestCase):
    def test_volume_score_high(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        score = VolumeScorer._avg_volume_score(2_000_000)
        self.assertGreater(score, 0.5)

    def test_volume_score_low(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        score = VolumeScorer._avg_volume_score(100_000)
        self.assertLess(score, 0.5)

    def test_volume_score_none(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        self.assertAlmostEqual(VolumeScorer._avg_volume_score(None), 0.5)

    def test_market_cap_score_large(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        score = VolumeScorer._market_cap_score(100e9)
        self.assertGreater(score, 0.5)

    def test_market_cap_score_below_floor(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        score = VolumeScorer._market_cap_score(500e6)
        self.assertLess(score, 0.5)

    def test_volume_trend_rising(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        score = VolumeScorer._volume_trend_score(avg_20d=1_500_000, avg_50d=1_000_000)
        self.assertGreater(score, 0.5)

    def test_volume_trend_falling(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        score = VolumeScorer._volume_trend_score(avg_20d=500_000, avg_50d=1_000_000)
        self.assertLess(score, 0.5)

    def test_all_scores_bounded(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        for val in [None, 0, 100_000, 500_000, 1_000_000, 10_000_000]:
            self.assertGreaterEqual(VolumeScorer._avg_volume_score(val), 0.0)
            self.assertLessEqual(VolumeScorer._avg_volume_score(val), 1.0)


class TestVolumeScorerAll(unittest.TestCase):
    def _make_price_df(self, volumes, close=100.0):
        n = len(volumes)
        dates = pd.bdate_range(end="2024-01-15", periods=n)
        return pd.DataFrame(
            {
                "Open": [close] * n,
                "High": [close * 1.01] * n,
                "Low": [close * 0.99] * n,
                "Close": [close] * n,
                "Volume": volumes,
            },
            index=dates,
        )

    def test_score_all_returns_expected_columns(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        scorer = VolumeScorer()
        prices = self._make_price_df([1_000_000] * 60)
        result = scorer.score_all(
            {"AAPL": prices},
            {"AAPL": {"marketCap": 3e12, "averageVolume": 50_000_000}},
            ["AAPL"],
        )
        for col in ["ticker", "volume_score"]:
            self.assertIn(col, result.columns)

    def test_liquid_stock_beats_illiquid(self):
        from alphalens.screeners.prescreener.volume_scorer import VolumeScorer

        scorer = VolumeScorer()
        liquid = self._make_price_df([5_000_000] * 60)
        illiquid = self._make_price_df([50_000] * 60)
        result = scorer.score_all(
            {"LIQ": liquid, "ILLIQ": illiquid},
            {
                "LIQ": {"marketCap": 50e9, "averageVolume": 5_000_000},
                "ILLIQ": {"marketCap": 200e6, "averageVolume": 50_000},
            },
            ["LIQ", "ILLIQ"],
        )
        liq_score = result.loc[result["ticker"] == "LIQ", "volume_score"].values[0]
        illiq_score = result.loc[result["ticker"] == "ILLIQ", "volume_score"].values[0]
        self.assertGreater(liq_score, illiq_score)


if __name__ == "__main__":
    unittest.main()
