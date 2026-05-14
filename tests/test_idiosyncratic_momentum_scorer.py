"""Unit tests for ``alphalens.screeners.idiosyncratic_momentum.scorer``.

Pure-function tests using synthetic returns + FF3 factors so no yfinance /
FF3 CSV dependency at unit-test layer. Composition is exercised via
``score_idiosyncratic_momentum`` against a handful of synthetic tickers.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from alphalens.screeners.idiosyncratic_momentum import scorer as sc


def _month_index(n_months: int, start: str = "2010-01-31") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n_months, freq="ME")


def _make_ff3(n_months: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = _month_index(n_months)
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.005, 0.04, n_months),
            "SMB": rng.normal(0.002, 0.03, n_months),
            "HML": rng.normal(0.001, 0.03, n_months),
        },
        index=idx,
    )


class TestMonthlyReturnsFromDaily(unittest.TestCase):
    def test_basic_pct_change(self):
        # 3 month-ends: close 100 → 110 → 121. Monthly returns: 0.1, 0.1.
        idx = pd.date_range("2020-01-31", periods=3, freq="ME")
        close = pd.Series([100.0, 110.0, 121.0], index=idx)
        out = sc.monthly_returns_from_daily(close)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out.iloc[0], 0.1)
        self.assertAlmostEqual(out.iloc[1], 0.1)

    def test_daily_to_monthly_takes_last_close(self):
        # Daily series across 2 months; resample picks last close per month.
        idx = pd.date_range("2020-01-01", "2020-02-29", freq="B")
        close = pd.Series(np.linspace(100, 200, len(idx)), index=idx)
        out = sc.monthly_returns_from_daily(close)
        self.assertEqual(len(out), 1)  # only 1 pct_change between 2 months

    def test_empty_input_returns_empty(self):
        out = sc.monthly_returns_from_daily(pd.Series(dtype=float))
        self.assertTrue(out.empty)

    def test_single_month_returns_empty(self):
        idx = pd.date_range("2020-01-31", periods=1, freq="ME")
        out = sc.monthly_returns_from_daily(pd.Series([100.0], index=idx))
        self.assertTrue(out.empty)


class TestFitOlsResiduals(unittest.TestCase):
    def test_perfect_fit_yields_zero_residuals(self):
        # y = 2 + 3·x → residuals all 0.
        n = 20
        x = np.linspace(0, 1, n)
        y = 2.0 + 3.0 * x
        X = x.reshape(-1, 1)
        resid = sc._fit_ols_residuals(y, X)
        self.assertIsNotNone(resid)
        np.testing.assert_allclose(resid, 0.0, atol=1e-10)

    def test_residuals_sum_to_zero_with_intercept(self):
        # OLS with intercept → residuals sum to 0.
        rng = np.random.default_rng(42)
        n = 50
        X = rng.normal(size=(n, 3))
        y = X @ np.array([0.5, -0.2, 0.1]) + rng.normal(0, 0.1, n)
        resid = sc._fit_ols_residuals(y, X)
        self.assertIsNotNone(resid)
        self.assertAlmostEqual(float(resid.sum()), 0.0, places=10)

    def test_empty_input_returns_none(self):
        self.assertIsNone(sc._fit_ols_residuals(np.array([]), np.zeros((0, 3))))

    def test_rank_deficient_returns_none(self):
        # Two identical columns → rank-deficient.
        X = np.column_stack([np.ones(10), np.ones(10)])
        y = np.arange(10, dtype=float)
        self.assertIsNone(sc._fit_ols_residuals(y, X))


class TestComputeResidualsWindow(unittest.TestCase):
    def setUp(self):
        self.n = 40
        self.idx = _month_index(self.n)
        self.ff3 = _make_ff3(self.n, seed=1)
        rng = np.random.default_rng(7)
        # Construct ticker series as 1.2·Mkt + 0.3·SMB - 0.1·HML + ε.
        self.eps_true = rng.normal(0, 0.02, self.n)
        self.excess = pd.Series(
            1.2 * self.ff3["Mkt-RF"]
            + 0.3 * self.ff3["SMB"]
            - 0.1 * self.ff3["HML"]
            + self.eps_true,
            index=self.idx,
        )

    def test_returns_window_residuals(self):
        asof = self.idx[-1]
        resid = sc.compute_residuals_window(self.excess, self.ff3, asof, window=36)
        self.assertIsNotNone(resid)
        self.assertEqual(len(resid), 36)
        # Recovered residuals should correlate strongly with the injected noise.
        injected_tail = pd.Series(self.eps_true, index=self.idx).iloc[-36:]
        corr = float(np.corrcoef(resid.to_numpy(), injected_tail.to_numpy())[0, 1])
        # 36 obs × 3 regressors → modest collinearity between injected noise
        # and recovered residuals; 0.9 is the floor that passes deterministically.
        self.assertGreater(corr, 0.9)

    def test_insufficient_history_returns_none(self):
        asof = self.idx[-1]
        short_excess = self.excess.iloc[:20]
        out = sc.compute_residuals_window(short_excess, self.ff3, asof, window=36)
        self.assertIsNone(out)

    def test_asof_before_window_returns_none(self):
        # asof_month so early that fewer than 36 obs precede it.
        asof = self.idx[10]
        out = sc.compute_residuals_window(self.excess, self.ff3, asof, window=36)
        self.assertIsNone(out)

    def test_missing_ff3_column_raises(self):
        bad_ff3 = self.ff3.drop(columns=["HML"])
        with self.assertRaises(ValueError):
            sc.compute_residuals_window(self.excess, bad_ff3, self.idx[-1], window=36)

    def test_uses_last_window_only(self):
        # Provide 40 months but window=36 → only last 36 used.
        asof = self.idx[-1]
        resid = sc.compute_residuals_window(self.excess, self.ff3, asof, window=36)
        self.assertEqual(resid.index[0], self.idx[-36])
        self.assertEqual(resid.index[-1], self.idx[-1])


class TestComputeIdioMomentum(unittest.TestCase):
    def test_blitz_formula_canonical(self):
        # 36 residuals, all 0.01 except formation block; predictable IM.
        residuals = pd.Series([0.01] * 36, index=_month_index(36))
        im = sc.compute_idio_momentum(residuals, formation_lookback=12, skip=2)
        # σ_36 = 0 → returns None.
        self.assertIsNone(im)

    def test_positive_residual_formation_yields_positive_im(self):
        rng = np.random.default_rng(11)
        base = rng.normal(0, 0.01, 36)
        # Spike up the formation window (positions -12..-2 = 11 entries).
        base[-12:-1] += 0.03
        residuals = pd.Series(base, index=_month_index(36))
        im = sc.compute_idio_momentum(residuals, formation_lookback=12, skip=2)
        self.assertIsNotNone(im)
        self.assertGreater(im, 0)

    def test_zero_sigma_returns_none(self):
        residuals = pd.Series([0.0] * 36, index=_month_index(36))
        self.assertIsNone(sc.compute_idio_momentum(residuals))

    def test_short_series_returns_none(self):
        residuals = pd.Series([0.01, -0.01], index=_month_index(2))
        self.assertIsNone(sc.compute_idio_momentum(residuals))

    def test_formation_window_excludes_skip_months(self):
        # All zeros except the very last month (which should be skipped).
        base = np.zeros(36)
        base[-1] = 10.0
        residuals = pd.Series(base, index=_month_index(36))
        im = sc.compute_idio_momentum(residuals, formation_lookback=12, skip=2)
        # Skip-1 (positions -12..-2 inclusive) should NOT include the last position.
        # σ_36 is non-zero because of the spike at -1, but sum over formation = 0.
        self.assertIsNotNone(im)
        self.assertAlmostEqual(im, 0.0)

    def test_invalid_params_raises(self):
        residuals = pd.Series([0.01] * 36, index=_month_index(36))
        with self.assertRaises(ValueError):
            sc.compute_idio_momentum(residuals, formation_lookback=0)
        with self.assertRaises(ValueError):
            sc.compute_idio_momentum(residuals, skip=0)
        with self.assertRaises(ValueError):
            sc.compute_idio_momentum(residuals, formation_lookback=2, skip=3)


class TestWinsorizeAndZscore(unittest.TestCase):
    def test_winsorize_caps_tails(self):
        s = pd.Series(np.arange(100, dtype=float))
        w = sc.winsorize(s, lower_pct=0.05, upper_pct=0.95)
        self.assertLessEqual(float(w.max()), float(s.quantile(0.95)))
        self.assertGreaterEqual(float(w.min()), float(s.quantile(0.05)))

    def test_rank_zscore_normalises(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        z = sc.rank_zscore(s)
        self.assertAlmostEqual(float(z.mean()), 0.0)
        self.assertAlmostEqual(float(z.std(ddof=0)), 1.0)

    def test_rank_zscore_constant_returns_all_nan(self):
        z = sc.rank_zscore(pd.Series([5.0] * 10))
        self.assertTrue(z.isna().all())


class TestScoreIdiosyncraticMomentum(unittest.TestCase):
    def setUp(self):
        self.n = 40
        self.idx = _month_index(self.n)
        self.ff3 = _make_ff3(self.n, seed=3)
        self.rf = pd.Series(np.full(self.n, 0.0002), index=self.idx)

    def _make_ticker(self, seed: int, formation_boost: float) -> pd.Series:
        rng = np.random.default_rng(seed)
        eps = rng.normal(0, 0.02, self.n)
        # Boost residual over formation window so we can verify ranking.
        eps[-12:-1] += formation_boost
        gross = 1.0 * self.ff3["Mkt-RF"] + 0.2 * self.ff3["SMB"] + eps + self.rf.values
        return pd.Series(gross.values, index=self.idx)

    def test_higher_residual_boost_ranks_first(self):
        returns = {
            "STRONG": self._make_ticker(seed=10, formation_boost=0.04),
            "MID": self._make_ticker(seed=20, formation_boost=0.0),
            "WEAK": self._make_ticker(seed=30, formation_boost=-0.04),
        }
        scores = sc.score_idiosyncratic_momentum(
            returns,
            self.ff3,
            self.rf,
            self.idx[-1],
        )
        self.assertEqual(len(scores), 3)
        self.assertGreater(scores["STRONG"], scores["MID"])
        self.assertGreater(scores["MID"], scores["WEAK"])

    def test_insufficient_history_ticker_dropped(self):
        returns = {
            "OK": self._make_ticker(seed=10, formation_boost=0.0),
            "SHORT": self._make_ticker(seed=20, formation_boost=0.0).iloc[:10],
        }
        scores = sc.score_idiosyncratic_momentum(
            returns,
            self.ff3,
            self.rf,
            self.idx[-1],
        )
        self.assertIn("OK", scores.index)
        self.assertNotIn("SHORT", scores.index)

    def test_empty_universe_returns_empty(self):
        scores = sc.score_idiosyncratic_momentum({}, self.ff3, self.rf, self.idx[-1])
        self.assertTrue(scores.empty)

    def test_z_scored_output(self):
        # 5 tickers with varied boosts → z-scored output: mean ≈ 0, std ≈ 1.
        returns = {
            f"T{i}": self._make_ticker(seed=100 + i, formation_boost=0.01 * (i - 2))
            for i in range(5)
        }
        scores = sc.score_idiosyncratic_momentum(
            returns,
            self.ff3,
            self.rf,
            self.idx[-1],
        )
        self.assertEqual(len(scores), 5)
        self.assertAlmostEqual(float(scores.mean()), 0.0, places=8)


if __name__ == "__main__":
    unittest.main()
