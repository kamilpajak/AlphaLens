"""Merton distance-to-default (naive KMV) — pure-function tests.

Naive KMV per Bharath-Shumway 2008: V_E = equity_mcap, V = V_E + D,
sigma_V approximated as sigma_E. Adequate for cross-sectional ranking
(rho ~ 0.95 vs full iterative). All tests are deterministic — no I/O.
"""

from __future__ import annotations

import math
import unittest

import numpy as np


class MertonD2Tests(unittest.TestCase):
    def test_d2_known_value(self):
        """Hand-calc fixture: V=200, D=100, sigma=0.30, r=0.04, T=1
        d2 = (ln(2.0) + (0.04 - 0.5*0.09)) / 0.30
            = (0.6931 + (-0.005)) / 0.30
            = 2.293
        """
        from alphalens_research.screeners.distress_credit.merton import merton_d2

        d2 = merton_d2(
            equity_mcap=100.0,
            total_liabilities=100.0,
            sigma_equity=0.30,
            rf=0.04,
            horizon_years=1.0,
        )
        self.assertIsNotNone(d2)
        self.assertAlmostEqual(d2, 2.2937, places=3)

    def test_d2_returns_none_on_zero_debt(self):
        from alphalens_research.screeners.distress_credit.merton import merton_d2

        d2 = merton_d2(equity_mcap=100.0, total_liabilities=0.0, sigma_equity=0.30, rf=0.04)
        self.assertIsNone(d2)

    def test_d2_returns_none_on_negative_debt(self):
        from alphalens_research.screeners.distress_credit.merton import merton_d2

        d2 = merton_d2(equity_mcap=100.0, total_liabilities=-50.0, sigma_equity=0.30, rf=0.04)
        self.assertIsNone(d2)

    def test_d2_returns_none_on_zero_mcap(self):
        from alphalens_research.screeners.distress_credit.merton import merton_d2

        d2 = merton_d2(equity_mcap=0.0, total_liabilities=100.0, sigma_equity=0.30, rf=0.04)
        self.assertIsNone(d2)

    def test_d2_returns_none_on_zero_sigma(self):
        from alphalens_research.screeners.distress_credit.merton import merton_d2

        d2 = merton_d2(equity_mcap=100.0, total_liabilities=100.0, sigma_equity=0.0, rf=0.04)
        self.assertIsNone(d2)

    def test_d2_returns_none_on_non_finite_input(self):
        from alphalens_research.screeners.distress_credit.merton import merton_d2

        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(
                merton_d2(equity_mcap=bad, total_liabilities=100.0, sigma_equity=0.30, rf=0.04)
            )
            self.assertIsNone(
                merton_d2(equity_mcap=100.0, total_liabilities=bad, sigma_equity=0.30, rf=0.04)
            )
            self.assertIsNone(
                merton_d2(equity_mcap=100.0, total_liabilities=100.0, sigma_equity=bad, rf=0.04)
            )

    def test_d2_increases_with_lower_leverage(self):
        """Higher equity / lower debt → higher d2 → lower PD (safer)."""
        from alphalens_research.screeners.distress_credit.merton import merton_d2

        safe = merton_d2(equity_mcap=300.0, total_liabilities=100.0, sigma_equity=0.30, rf=0.04)
        risky = merton_d2(equity_mcap=100.0, total_liabilities=300.0, sigma_equity=0.30, rf=0.04)
        self.assertGreater(safe, risky)

    def test_d2_decreases_with_higher_volatility(self):
        """Higher sigma → lower d2 (riskier) when V/D > 1."""
        from alphalens_research.screeners.distress_credit.merton import merton_d2

        low_vol = merton_d2(equity_mcap=200.0, total_liabilities=100.0, sigma_equity=0.20, rf=0.04)
        high_vol = merton_d2(equity_mcap=200.0, total_liabilities=100.0, sigma_equity=0.60, rf=0.04)
        self.assertGreater(low_vol, high_vol)


class MertonPDTests(unittest.TestCase):
    def test_pd_at_zero_d2_is_half(self):
        """PD = N(-0) = 0.5 — coin-flip at d2 = 0."""
        from alphalens_research.screeners.distress_credit.merton import merton_pd

        self.assertAlmostEqual(merton_pd(0.0), 0.5, places=6)

    def test_pd_monotone_decreasing_in_d2(self):
        from alphalens_research.screeners.distress_credit.merton import merton_pd

        pds = [merton_pd(d2) for d2 in [-3.0, -1.0, 0.0, 1.0, 3.0]]
        for i in range(len(pds) - 1):
            self.assertGreater(pds[i], pds[i + 1])

    def test_pd_in_unit_interval(self):
        from alphalens_research.screeners.distress_credit.merton import merton_pd

        for d2 in [-5.0, -2.0, 0.0, 2.0, 5.0]:
            pd_val = merton_pd(d2)
            self.assertGreaterEqual(pd_val, 0.0)
            self.assertLessEqual(pd_val, 1.0)


class RealizedVolTests(unittest.TestCase):
    def test_realised_vol_60d_matches_numpy_std(self):
        """Annualized stdev of log returns on 60d window."""
        from alphalens_research.screeners.distress_credit.merton import realised_vol_60d

        rng = np.random.default_rng(42)
        # Construct 65 closes from log-normal returns with sigma=0.02 daily.
        log_rets = rng.normal(0.0, 0.02, size=64)
        closes = np.exp(np.cumsum(np.concatenate(([0.0], log_rets))))
        vol = realised_vol_60d(closes)
        self.assertIsNotNone(vol)
        # Annualised: 0.02 * sqrt(252) ≈ 0.317. Allow loose tolerance for sample noise.
        self.assertAlmostEqual(vol, 0.02 * math.sqrt(252), delta=0.10)

    def test_realised_vol_60d_returns_none_when_insufficient_bars(self):
        from alphalens_research.screeners.distress_credit.merton import realised_vol_60d

        closes = np.array([100.0, 101.0, 102.0])  # only 3 bars, need 60+1
        self.assertIsNone(realised_vol_60d(closes))

    def test_realised_vol_60d_returns_none_on_constant_price(self):
        """Zero variance → degenerate, return None."""
        from alphalens_research.screeners.distress_credit.merton import realised_vol_60d

        closes = np.full(65, 100.0)
        self.assertIsNone(realised_vol_60d(closes))

    def test_realised_vol_60d_returns_none_on_non_finite(self):
        from alphalens_research.screeners.distress_credit.merton import realised_vol_60d

        closes = np.array([100.0, 101.0, np.nan] + [100.0] * 62)
        self.assertIsNone(realised_vol_60d(closes))


if __name__ == "__main__":
    unittest.main()
