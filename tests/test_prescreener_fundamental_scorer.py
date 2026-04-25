import unittest


class TestFundamentalScorerStatic(unittest.TestCase):
    """Test individual scoring functions with known inputs."""

    def test_pe_score_below_threshold(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        score = FundamentalScorer._pe_score(15.0)
        self.assertGreater(score, 0.5)

    def test_pe_score_at_threshold(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        score = FundamentalScorer._pe_score(25.0)
        self.assertAlmostEqual(score, 0.5)

    def test_pe_score_above_threshold(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        score = FundamentalScorer._pe_score(40.0)
        self.assertLess(score, 0.5)

    def test_pe_score_none_returns_neutral(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        self.assertAlmostEqual(FundamentalScorer._pe_score(None), 0.5)

    def test_pe_score_negative_returns_zero(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        score = FundamentalScorer._pe_score(-5.0)
        self.assertAlmostEqual(score, 0.0)

    def test_peg_score_below_threshold(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        self.assertGreater(FundamentalScorer._peg_score(1.0), 0.5)

    def test_peg_score_none_returns_neutral(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        self.assertAlmostEqual(FundamentalScorer._peg_score(None), 0.5)

    def test_roe_score_above_threshold(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        self.assertGreater(FundamentalScorer._roe_score(0.20), 0.5)

    def test_roe_score_below_threshold(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        self.assertLess(FundamentalScorer._roe_score(0.05), 0.5)

    def test_roe_score_none_returns_neutral(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        self.assertAlmostEqual(FundamentalScorer._roe_score(None), 0.5)

    def test_growth_score_high_growth(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        self.assertGreater(FundamentalScorer._growth_score(0.25), 0.5)

    def test_growth_score_negative(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        score = FundamentalScorer._growth_score(-0.10)
        self.assertAlmostEqual(score, 0.0)

    def test_debt_ebitda_score_low_debt(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        score = FundamentalScorer._debt_ebitda_score(1.5)
        self.assertGreater(score, 0.5)

    def test_debt_ebitda_score_high_debt(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        score = FundamentalScorer._debt_ebitda_score(5.0)
        self.assertLess(score, 0.5)

    def test_all_scores_bounded_zero_one(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        for fn in [
            FundamentalScorer._pe_score,
            FundamentalScorer._peg_score,
            FundamentalScorer._roe_score,
            FundamentalScorer._growth_score,
            FundamentalScorer._debt_ebitda_score,
        ]:
            for val in [None, -100, 0, 0.5, 1, 5, 50, 1000]:
                score = fn(val)
                self.assertGreaterEqual(score, 0.0, f"{fn.__name__}({val}) < 0")
                self.assertLessEqual(score, 1.0, f"{fn.__name__}({val}) > 1")


class TestFundamentalScorerAll(unittest.TestCase):
    """Test score_all with synthetic data dicts."""

    def test_good_company_beats_bad_company(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        scorer = FundamentalScorer()
        fundamentals = {
            "GOOD": {
                "trailingPE": 15,
                "pegRatio": 1.0,
                "returnOnEquity": 0.20,
                "debtToEquity": 50,
                "totalDebt": 5e9,
                "ebitda": 1e10,
                "earningsGrowth": 0.20,
                "marketCap": 3e12,
            },
            "BAD": {
                "trailingPE": 50,
                "pegRatio": 3.0,
                "returnOnEquity": 0.03,
                "debtToEquity": 400,
                "totalDebt": 4e10,
                "ebitda": 1e9,
                "earningsGrowth": -0.10,
                "marketCap": 5e9,
            },
        }
        result = scorer.score_all(fundamentals)
        good_score = result.loc[result["ticker"] == "GOOD", "fundamental_score"].values[0]
        bad_score = result.loc[result["ticker"] == "BAD", "fundamental_score"].values[0]
        self.assertGreater(good_score, bad_score)

    def test_empty_info_returns_neutral(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        scorer = FundamentalScorer()
        result = scorer.score_all({"MYSTERY": {}})
        score = result.loc[result["ticker"] == "MYSTERY", "fundamental_score"].values[0]
        self.assertAlmostEqual(score, 0.5, places=1)

    def test_output_has_expected_columns(self):
        from alphalens.screeners.prescreener.fundamental_scorer import FundamentalScorer

        scorer = FundamentalScorer()
        result = scorer.score_all({"AAPL": {"trailingPE": 20}})
        for col in [
            "ticker",
            "pe_score",
            "peg_score",
            "roe_score",
            "debt_score",
            "growth_score",
            "fundamental_score",
        ]:
            self.assertIn(col, result.columns, f"Missing column: {col}")


if __name__ == "__main__":
    unittest.main()
