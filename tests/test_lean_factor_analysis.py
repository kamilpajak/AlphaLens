import unittest

import numpy as np
import pandas as pd


def _synthetic_ff3(n=252, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n)
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.0004, 0.01, n),
            "SMB": rng.normal(0.0001, 0.006, n),
            "HML": rng.normal(0.0001, 0.005, n),
            "RF": np.full(n, 0.00002),  # ~0.5% annual RFR
        },
        index=idx,
    )


class TestFamaFrenchAlpha(unittest.TestCase):
    def test_market_mimicking_portfolio_has_near_zero_alpha(self):
        from alphalens.lean_screener.backtest.factor_analysis import fama_french_alpha

        ff3 = _synthetic_ff3(252, seed=1)
        # Portfolio = Mkt-RF + RF + small noise → alpha should be near zero, beta_mkt near 1.
        rng = np.random.default_rng(2)
        port = ff3["Mkt-RF"] + ff3["RF"] + rng.normal(0, 0.001, len(ff3))

        res = fama_french_alpha(port, ff3)

        self.assertLess(abs(res.alpha_daily), 0.001)  # < 10 bps/day
        self.assertAlmostEqual(res.beta_mkt, 1.0, delta=0.2)

    def test_synthetic_alpha_is_detected(self):
        from alphalens.lean_screener.backtest.factor_analysis import fama_french_alpha

        ff3 = _synthetic_ff3(500, seed=3)
        rng = np.random.default_rng(4)
        # Portfolio adds +10 bps/day of pure alpha on top of market beta.
        port = ff3["Mkt-RF"] + ff3["RF"] + 0.0010 + rng.normal(0, 0.002, len(ff3))

        res = fama_french_alpha(port, ff3)

        self.assertGreater(res.alpha_daily, 0.0005)
        self.assertGreater(res.alpha_tstat, 2.0)  # should be statistically significant

    def test_missing_columns_raises(self):
        from alphalens.lean_screener.backtest.factor_analysis import fama_french_alpha

        bad = pd.DataFrame({"Mkt-RF": [0.001], "SMB": [0.001]})  # missing HML, RF
        port = pd.Series([0.001])
        with self.assertRaises(ValueError):
            fama_french_alpha(port, bad)

    def test_insufficient_history_raises(self):
        from alphalens.lean_screener.backtest.factor_analysis import fama_french_alpha

        ff3 = _synthetic_ff3(10, seed=5)
        port = pd.Series(np.random.default_rng(0).normal(0, 0.01, 10), index=ff3.index)
        with self.assertRaises(ValueError):
            fama_french_alpha(port, ff3)


class TestFormatAlphaSummary(unittest.TestCase):
    def test_output_contains_key_lines(self):
        from alphalens.lean_screener.backtest.factor_analysis import (
            AlphaResult,
            format_alpha_summary,
        )

        res = AlphaResult(
            alpha_daily=0.0002,
            alpha_annualized=0.05,
            alpha_tstat=1.85,
            beta_mkt=1.1,
            beta_smb=0.3,
            beta_hml=-0.2,
            r_squared=0.45,
            n_observations=250,
        )
        text = format_alpha_summary(res)

        self.assertIn("alpha (daily)", text)
        self.assertIn("alpha (annualized)", text)
        self.assertIn("alpha t-stat", text)
        self.assertIn("beta[Mkt-RF]", text)


if __name__ == "__main__":
    unittest.main()
