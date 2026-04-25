import unittest

import pandas as pd


class TestTechnicalScorerStatic(unittest.TestCase):
    """Test individual scoring functions."""

    def test_rsi_score_in_range(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        self.assertAlmostEqual(TechnicalScorer._rsi_score(50), 1.0)
        self.assertAlmostEqual(TechnicalScorer._rsi_score(30), 1.0)
        self.assertAlmostEqual(TechnicalScorer._rsi_score(70), 1.0)

    def test_rsi_score_extreme_low(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        score = TechnicalScorer._rsi_score(10)
        self.assertLess(score, 0.5)
        self.assertGreater(score, 0.0)

    def test_rsi_score_extreme_high(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        score = TechnicalScorer._rsi_score(90)
        self.assertLess(score, 0.5)

    def test_rsi_score_at_zero(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        self.assertAlmostEqual(TechnicalScorer._rsi_score(0), 0.0)

    def test_rsi_score_at_hundred(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        self.assertAlmostEqual(TechnicalScorer._rsi_score(100), 0.0)

    def test_rsi_score_none_returns_neutral(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        self.assertAlmostEqual(TechnicalScorer._rsi_score(None), 0.5)

    def test_trend_score_above_ma(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        score = TechnicalScorer._trend_score(150.0, 140.0)
        self.assertGreater(score, 0.5)

    def test_trend_score_below_ma(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        score = TechnicalScorer._trend_score(130.0, 140.0)
        self.assertLess(score, 0.5)

    def test_trend_score_at_ma(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        score = TechnicalScorer._trend_score(100.0, 100.0)
        self.assertAlmostEqual(score, 0.5)

    def test_trend_score_none_ma_returns_neutral(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        self.assertAlmostEqual(TechnicalScorer._trend_score(100.0, None), 0.5)

    def test_adx_score_strong_trend(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        self.assertAlmostEqual(TechnicalScorer._adx_score(35), 1.0)

    def test_adx_score_weak_trend(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        score = TechnicalScorer._adx_score(15)
        self.assertLess(score, 1.0)
        self.assertGreater(score, 0.0)

    def test_adx_score_none_returns_neutral(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        self.assertAlmostEqual(TechnicalScorer._adx_score(None), 0.5)

    def test_all_scores_bounded_zero_one(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        for val in [None, 0, 10, 30, 50, 70, 90, 100]:
            score = TechnicalScorer._rsi_score(val)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)

        for val in [None, 0, 10, 25, 50, 100]:
            score = TechnicalScorer._adx_score(val)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)


class TestTechnicalScorerAll(unittest.TestCase):
    """Test score_all with synthetic price DataFrames."""

    def _make_price_df(self, close_values, volume=1_000_000):
        """Create a synthetic OHLCV DataFrame."""
        n = len(close_values)
        dates = pd.bdate_range(end="2024-01-15", periods=n)
        return pd.DataFrame(
            {
                "Open": close_values,
                "High": [c * 1.02 for c in close_values],
                "Low": [c * 0.98 for c in close_values],
                "Close": close_values,
                "Volume": [volume] * n,
            },
            index=dates,
        )

    def test_score_all_returns_expected_columns(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        scorer = TechnicalScorer()
        # 250 days of steadily rising prices
        prices = self._make_price_df([100 + i * 0.5 for i in range(250)])
        price_data = {"AAPL": prices}
        result = scorer.score_all(price_data, ["AAPL"])
        for col in [
            "ticker",
            "rsi_score",
            "trend_score",
            "adx_score",
            "technical_score",
        ]:
            self.assertIn(col, result.columns)

    def test_uptrend_scores_higher_than_downtrend(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        scorer = TechnicalScorer()
        up_prices = self._make_price_df([50 + i * 0.5 for i in range(250)])
        down_prices = self._make_price_df([175 - i * 0.5 for i in range(250)])
        price_data = {"UP": up_prices, "DOWN": down_prices}
        result = scorer.score_all(price_data, ["UP", "DOWN"])

        up_score = result.loc[result["ticker"] == "UP", "trend_score"].values[0]
        down_score = result.loc[result["ticker"] == "DOWN", "trend_score"].values[0]
        self.assertGreater(up_score, down_score)

    def test_insufficient_data_returns_neutral(self):
        from alphalens.screeners.prescreener.technical_scorer import TechnicalScorer

        scorer = TechnicalScorer()
        # Only 10 data points — not enough for SMA(200)
        short_prices = self._make_price_df([100 + i for i in range(10)])
        price_data = {"SHORT": short_prices}
        result = scorer.score_all(price_data, ["SHORT"])
        score = result.loc[result["ticker"] == "SHORT", "technical_score"].values[0]
        # Should not crash, and score should be reasonable
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
