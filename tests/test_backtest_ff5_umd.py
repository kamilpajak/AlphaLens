import unittest
from datetime import date

import numpy as np
import pandas as pd


def _synthetic_ff5_umd(n: int = 500) -> pd.DataFrame:
    """Deterministic synthetic factor data spanning n business days."""
    rng = np.random.default_rng(42)
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.0004, 0.010, n),
            "SMB": rng.normal(0.0001, 0.005, n),
            "HML": rng.normal(0.0000, 0.005, n),
            "RMW": rng.normal(0.0001, 0.004, n),
            "CMA": rng.normal(0.0000, 0.004, n),
            "Mom": rng.normal(0.0002, 0.006, n),
            "RF": 0.00008,  # ~2% annual
        },
        index=idx,
    )


class TestLoadFf5Umd(unittest.TestCase):
    def test_joined_frame_has_all_six_factors_plus_rf(self):
        from alphalens.backtest.factors import load_ff5_umd_daily

        # Integration-style: exercise the actual Dartmouth CSVs.
        df = load_ff5_umd_daily(start=date(2023, 1, 1), end=date(2023, 12, 31))

        for col in ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom", "RF"):
            self.assertIn(col, df.columns)

    def test_returns_decimals_not_percent(self):
        from alphalens.backtest.factors import load_ff5_umd_daily

        df = load_ff5_umd_daily(start=date(2023, 1, 1), end=date(2023, 6, 30))

        # Daily factor returns in decimal form rarely exceed ±0.2 (20%).
        # Anything above 1.0 would indicate percent-not-decimal parsing bug.
        self.assertLess(df[["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]].abs().max().max(), 1.0)


class TestFf5UmdAttribution(unittest.TestCase):
    def test_returns_alpha_result_with_six_factor_betas(self):
        from alphalens.backtest.factor_analysis import run_ff5_umd_attribution

        factors = _synthetic_ff5_umd()
        # Synthesize a portfolio return = 0.5 × Mkt-RF + RF + 2 bps/day alpha + noise
        rng = np.random.default_rng(7)
        portfolio = (
            0.0002 + 0.5 * factors["Mkt-RF"] + factors["RF"] + rng.normal(0, 0.008, len(factors))
        )

        result = run_ff5_umd_attribution(portfolio, factors)

        self.assertEqual(result.spec_name, "FF5+UMD")
        self.assertEqual(
            set(result.betas.keys()),
            {"Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"},
        )

    def test_alpha_recovered_within_noise_margin(self):
        from alphalens.backtest.factor_analysis import run_ff5_umd_attribution

        true_alpha_daily = 0.0003  # 3 bps/day ≈ 7.5% ann
        factors = _synthetic_ff5_umd(n=2000)
        rng = np.random.default_rng(11)
        portfolio = (
            true_alpha_daily
            + 0.8 * factors["Mkt-RF"]
            + factors["RF"]
            + rng.normal(0, 0.004, len(factors))
        )

        result = run_ff5_umd_attribution(portfolio, factors)

        # Recovered alpha should be within ±50% of true alpha at n=2000
        self.assertAlmostEqual(result.alpha_daily, true_alpha_daily, delta=1.5e-4)

    def test_hac_cov_type_used_by_default(self):
        from alphalens.backtest.factor_analysis import run_ff5_umd_attribution

        factors = _synthetic_ff5_umd()
        portfolio = factors["Mkt-RF"] + factors["RF"]

        result = run_ff5_umd_attribution(portfolio, factors)

        self.assertEqual(result.cov_type, "HAC")


if __name__ == "__main__":
    unittest.main()
