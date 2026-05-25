import unittest

import pandas as pd


class TestCompositeRanker(unittest.TestCase):
    def setUp(self):
        from alphalens_research.screeners.prescreener.composite_ranker import CompositeRanker

        self.ranker = CompositeRanker()

    def test_weights_sum_to_one(self):
        from alphalens_research.screeners.prescreener.config import PRESCREENER_DEFAULTS

        total = (
            PRESCREENER_DEFAULTS["weight_fundamental"]
            + PRESCREENER_DEFAULTS["weight_technical"]
            + PRESCREENER_DEFAULTS["weight_volume"]
        )
        self.assertAlmostEqual(total, 1.0)

    def test_rank_produces_sorted_output(self):
        tech = pd.DataFrame({"ticker": ["A", "B"], "technical_score": [0.8, 0.4]})
        fund = pd.DataFrame({"ticker": ["A", "B"], "fundamental_score": [0.7, 0.9]})
        vol = pd.DataFrame({"ticker": ["A", "B"], "volume_score": [0.6, 0.5]})
        result = self.ranker.rank(tech, fund, vol)
        # A: 0.45*0.7 + 0.35*0.8 + 0.20*0.6 = 0.315+0.280+0.120 = 0.715
        # B: 0.45*0.9 + 0.35*0.4 + 0.20*0.5 = 0.405+0.140+0.100 = 0.645
        self.assertEqual(result.iloc[0]["ticker"], "A")
        self.assertAlmostEqual(result.iloc[0]["composite_score"], 0.715, places=3)
        self.assertAlmostEqual(result.iloc[1]["composite_score"], 0.645, places=3)

    def test_rank_column_exists(self):
        tech = pd.DataFrame({"ticker": ["X"], "technical_score": [0.5]})
        fund = pd.DataFrame({"ticker": ["X"], "fundamental_score": [0.5]})
        vol = pd.DataFrame({"ticker": ["X"], "volume_score": [0.5]})
        result = self.ranker.rank(tech, fund, vol)
        self.assertIn("rank", result.columns)
        self.assertEqual(result.iloc[0]["rank"], 1)

    def test_top_n_returns_correct_count(self):
        tickers = [f"T{i}" for i in range(20)]
        tech = pd.DataFrame({"ticker": tickers, "technical_score": [i / 20 for i in range(20)]})
        fund = pd.DataFrame({"ticker": tickers, "fundamental_score": [0.5] * 20})
        vol = pd.DataFrame({"ticker": tickers, "volume_score": [0.5] * 20})
        ranked = self.ranker.rank(tech, fund, vol)
        result = self.ranker.top_n(ranked, n=5)
        self.assertEqual(len(result), 5)

    def test_top_n_defaults_to_config(self):
        from alphalens_research.screeners.prescreener.config import PRESCREENER_DEFAULTS

        tickers = [f"T{i}" for i in range(30)]
        tech = pd.DataFrame({"ticker": tickers, "technical_score": [0.5] * 30})
        fund = pd.DataFrame({"ticker": tickers, "fundamental_score": [0.5] * 30})
        vol = pd.DataFrame({"ticker": tickers, "volume_score": [0.5] * 30})
        ranked = self.ranker.rank(tech, fund, vol)
        result = self.ranker.top_n(ranked)
        self.assertEqual(len(result), PRESCREENER_DEFAULTS["top_n"])

    def test_handles_missing_ticker_in_one_scorer(self):
        tech = pd.DataFrame({"ticker": ["A", "B"], "technical_score": [0.8, 0.6]})
        fund = pd.DataFrame({"ticker": ["A"], "fundamental_score": [0.7]})
        vol = pd.DataFrame({"ticker": ["A", "B"], "volume_score": [0.5, 0.5]})
        result = self.ranker.rank(tech, fund, vol)
        # B should still appear with neutral fundamental_score
        self.assertIn("B", result["ticker"].values)


if __name__ == "__main__":
    unittest.main()
