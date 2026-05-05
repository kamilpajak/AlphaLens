"""Tests for P/C abnormal volume scorer (Pan-Poteshman 2006-inspired proxy).

Pre-registered as `pc_abnormal_volume_retrospective_pre_2018_2026_05_05` in signal class
`options_volume_search_2026_05_05`. Pre-reg sha256:
03ddf4b7906ed07049bbb74dcdd599afa29abda1e8c4f6551a1876c78e45e689.
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from alphalens.screeners.options_volume.pc_abnormal_volume import (
    EQUITY_CONTROLS_FOR_RESIDUAL,
    MIN_ASOF_TICKERS,
    MIN_ROLLING_OBS,
    ROLLING_WINDOW_DAYS,
    compute_abnormal_pcr_series,
    compute_pcr,
    score_pc_abnormal_residual,
)


class TestComputePcr(unittest.TestCase):
    def test_basic_log_ratio(self):
        self.assertAlmostEqual(compute_pcr(100.0, 50.0), math.log(2.0), places=12)

    def test_equal_volumes_zero(self):
        self.assertAlmostEqual(compute_pcr(50.0, 50.0), 0.0, places=12)

    def test_zero_put_volume_returns_nan(self):
        self.assertTrue(math.isnan(compute_pcr(0.0, 50.0)))

    def test_zero_call_volume_returns_nan(self):
        self.assertTrue(math.isnan(compute_pcr(50.0, 0.0)))

    def test_negative_volume_returns_nan(self):
        self.assertTrue(math.isnan(compute_pcr(-1.0, 50.0)))

    def test_none_inputs_return_nan(self):
        self.assertTrue(math.isnan(compute_pcr(None, 50.0)))
        self.assertTrue(math.isnan(compute_pcr(50.0, None)))

    def test_pandas_nan_returns_nan(self):
        self.assertTrue(math.isnan(compute_pcr(float("nan"), 50.0)))


class TestComputeAbnormalPcrSeries(unittest.TestCase):
    """Pre-reg spec: abnormal_pcr_t = pcr_t - mean(pcr over t-60..t-1).
    Min 30 of 60 prior obs non-NaN required."""

    def test_uses_only_past_data_no_lookahead(self):
        # pcr series of all 1.0 except day 100 = 5.0
        n = 200
        opt_put = pd.Series(np.full(n, 100.0))
        opt_call = pd.Series(np.full(n, 100.0))
        opt_put.iloc[100] = 500.0  # spike
        out = compute_abnormal_pcr_series(opt_put, opt_call)
        # Day 100 abnormal = log(5) - mean of prior 60 days (all log(1)=0) = log(5)
        self.assertAlmostEqual(out.iloc[100], math.log(5.0), places=10)
        # Day 99 abnormal must NOT see day 100 spike
        # prior 60 days (39..98) all log(1) = 0; pcr_99 = 0; abnormal = 0
        self.assertAlmostEqual(out.iloc[99], 0.0, places=10)
        # Day 101 abnormal = log(1) - mean(prior 60 incl day 100 spike)
        # mean = (59 * 0 + log(5)) / 60
        expected = 0.0 - math.log(5.0) / 60.0
        self.assertAlmostEqual(out.iloc[101], expected, places=10)

    def test_min_obs_30_required(self):
        # only 25 prior obs available → NaN
        n = 26
        opt_put = pd.Series(np.full(n, 100.0))
        opt_call = pd.Series(np.full(n, 50.0))
        out = compute_abnormal_pcr_series(opt_put, opt_call)
        # First 30 obs cannot have valid rolling baseline
        self.assertTrue(out.iloc[:30].isna().all() or len(out) < 30)
        # All NaN since n < 30
        self.assertTrue(out.isna().all())

    def test_zero_volumes_propagate_nan(self):
        n = 100
        opt_put = pd.Series(np.full(n, 100.0))
        opt_call = pd.Series(np.full(n, 50.0))
        opt_put.iloc[50] = 0.0  # zero-volume row
        out = compute_abnormal_pcr_series(opt_put, opt_call)
        # day 50 pcr is NaN → abnormal at day 50 is NaN
        self.assertTrue(math.isnan(out.iloc[50]))


class TestScorePcAbnormalResidual(unittest.TestCase):
    """Cross-sectional residualization per asof. Score = -residual.
    Asofs with < MIN_ASOF_TICKERS valid rows produce all-NaN scores."""

    def _make_features(self, n_tickers: int, asof: str = "2010-06-30") -> pd.DataFrame:
        rng = np.random.default_rng(42)
        return pd.DataFrame(
            {
                "asof": [asof] * n_tickers,
                "ticker": [f"T{i:03d}" for i in range(n_tickers)],
                "abnormal_pcr": rng.standard_normal(n_tickers) * 0.5,
                "reversal_1m": rng.standard_normal(n_tickers) * 0.05,
                "momentum_6m": rng.standard_normal(n_tickers) * 0.2,
                "rv_30d": rng.uniform(0.1, 0.6, n_tickers),
            }
        )

    def test_score_dimensions(self):
        df = self._make_features(100)
        scores = score_pc_abnormal_residual(df)
        self.assertEqual(len(scores), 100)
        self.assertEqual(scores.name, "score")

    def test_score_uses_negation_of_residual(self):
        # Build deterministic linear regression: abnormal_pcr = 2 * rv_30d (no controls confound)
        n = 80
        rng = np.random.default_rng(7)
        rv = rng.uniform(0.1, 0.6, n)
        # Inject one outlier where abnormal_pcr is 2*rv + 1.0 (large positive residual)
        abnormal_pcr = 2.0 * rv
        abnormal_pcr[0] = 2.0 * rv[0] + 1.0  # +1 residual
        df = pd.DataFrame(
            {
                "asof": ["2010-06-30"] * n,
                "ticker": [f"T{i:03d}" for i in range(n)],
                "abnormal_pcr": abnormal_pcr,
                "reversal_1m": np.zeros(n),
                "momentum_6m": np.zeros(n),
                "rv_30d": rv,
            }
        )
        scores = score_pc_abnormal_residual(df)
        # Outlier at idx 0: residual ~+1, score ~-1 (should be MOST NEGATIVE in cross-section)
        self.assertLess(scores.iloc[0], scores.drop(index=0).min())

    def test_score_skips_small_asofs(self):
        # Asof with fewer than MIN_ASOF_TICKERS rows should produce all-NaN scores
        df = self._make_features(MIN_ASOF_TICKERS - 1)
        scores = score_pc_abnormal_residual(df)
        self.assertTrue(scores.isna().all())

    def test_score_handles_nan_in_features(self):
        df = self._make_features(100)
        df.loc[5, "abnormal_pcr"] = np.nan
        df.loc[10, "rv_30d"] = np.nan
        scores = score_pc_abnormal_residual(df)
        self.assertTrue(math.isnan(scores.iloc[5]))
        self.assertTrue(math.isnan(scores.iloc[10]))
        # Rest should be valid
        self.assertFalse(scores.drop(index=[5, 10]).isna().any())

    def test_score_separates_asofs(self):
        # Two asofs, residualization within-asof only
        df1 = self._make_features(60, asof="2010-06-30")
        df2 = self._make_features(60, asof="2010-07-31")
        df = pd.concat([df1, df2], ignore_index=True)
        scores = score_pc_abnormal_residual(df)
        # Mean of residuals within each asof should be ~0 (OLS property)
        for asof in ("2010-06-30", "2010-07-31"):
            asof_scores = scores[df["asof"] == asof].dropna()
            self.assertAlmostEqual(asof_scores.mean(), 0.0, places=8)


class TestModuleConstants(unittest.TestCase):
    def test_equity_controls_match_v9d_exactly(self):
        # Pre-reg amendment 2026-05-05: log_marketCap dropped (vendor cache marketCap
        # populated 2025-12-24+ only; pre-2018 retrospective infeasible with size).
        # Resulting controls match v9D exactly — minimizes HARK surface.
        self.assertEqual(
            EQUITY_CONTROLS_FOR_RESIDUAL,
            ("reversal_1m", "momentum_6m", "rv_30d"),
        )

    def test_rolling_window_60_min_obs_30(self):
        self.assertEqual(ROLLING_WINDOW_DAYS, 60)
        self.assertEqual(MIN_ROLLING_OBS, 30)

    def test_min_asof_tickers_50(self):
        self.assertEqual(MIN_ASOF_TICKERS, 50)


class TestComputePcrEdgeCases(unittest.TestCase):
    """Cover try/except + length-mismatch branches for SonarCloud coverage."""

    def test_non_numeric_strings_return_nan(self):
        # TypeError/ValueError path in compute_pcr (line 51-52)
        self.assertTrue(math.isnan(compute_pcr("foo", 50.0)))
        self.assertTrue(math.isnan(compute_pcr(50.0, "bar")))

    def test_compute_abnormal_pcr_series_length_mismatch_raises(self):
        # ValueError path in compute_abnormal_pcr_series (line 68)
        a = pd.Series([1.0, 2.0, 3.0])
        b = pd.Series([1.0, 2.0])
        with self.assertRaises(ValueError):
            compute_abnormal_pcr_series(a, b)


if __name__ == "__main__":
    unittest.main()
