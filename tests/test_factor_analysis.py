"""Tests for alphalens.attribution.factor_analysis — Carhart 4F + HAC + rolling + industry."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd


def _synthetic_carhart(n: int = 500, seed: int = 0) -> pd.DataFrame:
    """Simulated daily Carhart factor frame (Mkt-RF, SMB, HML, Mom, RF)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.0004, 0.01, n),
            "SMB": rng.normal(0.0001, 0.006, n),
            "HML": rng.normal(0.0001, 0.005, n),
            "Mom": rng.normal(0.0002, 0.008, n),
            "RF": np.full(n, 0.00002),
        },
        index=idx,
    )


class TestRunRegressionPeriodsPerYear(unittest.TestCase):
    """Tier 2.A: AlphaResult exposes alpha_per_period + periods_per_year_assumption.

    Bug 1 + Tier 2.A coverage: makes the annualization math transparent.
    Pre-2026-05-05 schema reported only `alpha_annualized = alpha_per_period * periods_per_year`
    with default periods_per_year=252 — strided callers got 50× inflated αpct.
    """

    def test_periods_per_year_is_required(self):
        """Issue #67: removed the default to prevent 27 strided callers from
        silently getting 5× wrong annualization. Omission must raise TypeError."""
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(500, seed=11)
        port = ff["Mkt-RF"] + ff["RF"]
        with self.assertRaises(TypeError) as cm:
            run_regression(port, ff, factor_columns=["Mkt-RF"], spec_name="CAPM")
        self.assertIn("periods_per_year", str(cm.exception))

    def test_explicit_periods_per_year_252_for_daily(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(500, seed=11)
        port = ff["Mkt-RF"] + ff["RF"]
        res = run_regression(
            port, ff, factor_columns=["Mkt-RF"], spec_name="CAPM", periods_per_year=252
        )
        self.assertEqual(res.periods_per_year_assumption, 252)
        self.assertAlmostEqual(res.alpha_per_period, res.alpha_daily, places=12)
        self.assertAlmostEqual(res.alpha_annualized, res.alpha_daily * 252, places=10)

    def test_explicit_periods_per_year_50_for_weekly_stride(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(500, seed=12)
        port = ff["Mkt-RF"] + ff["RF"]
        res = run_regression(
            port,
            ff,
            factor_columns=["Mkt-RF"],
            spec_name="CAPM",
            periods_per_year=50,  # 252/5 ≈ 50 for weekly stride
        )
        self.assertEqual(res.periods_per_year_assumption, 50)
        self.assertAlmostEqual(res.alpha_annualized, res.alpha_per_period * 50, places=10)
        # alpha_per_period should equal the regression-day alpha regardless of annualization
        self.assertAlmostEqual(res.alpha_per_period, res.alpha_daily, places=12)


class TestRunRegression(unittest.TestCase):
    """Core: run_regression(port_returns, factors, factor_columns, cov_type, spec_name, periods_per_year=252)."""

    def test_capm_portfolio_near_zero_alpha_beta_one(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(500, seed=1)
        rng = np.random.default_rng(2)
        port = ff["Mkt-RF"] + ff["RF"] + rng.normal(0, 0.001, len(ff))

        res = run_regression(
            port, ff, factor_columns=["Mkt-RF"], spec_name="CAPM", periods_per_year=252
        )

        self.assertLess(abs(res.alpha_daily), 0.0005)
        self.assertAlmostEqual(res.betas["Mkt-RF"], 1.0, delta=0.15)
        self.assertEqual(res.spec_name, "CAPM")
        self.assertEqual(res.cov_type, "HAC")

    def test_carhart_detects_umd_beta_for_momentum_mimicking_portfolio(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(500, seed=3)
        rng = np.random.default_rng(4)
        # Portfolio = pure Mom factor + RF + small noise → beta_mom ≈ 1, others ≈ 0
        port = ff["Mom"] + ff["RF"] + rng.normal(0, 0.0005, len(ff))

        res = run_regression(
            port,
            ff,
            factor_columns=["Mkt-RF", "SMB", "HML", "Mom"],
            spec_name="Carhart-4F",
            periods_per_year=252,
        )

        self.assertAlmostEqual(res.betas["Mom"], 1.0, delta=0.15)
        self.assertLess(abs(res.betas["Mkt-RF"]), 0.2)
        self.assertLess(abs(res.alpha_daily), 0.0005)

    def test_detects_injected_alpha_with_significant_tstat(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(800, seed=5)
        rng = np.random.default_rng(6)
        # +10 bps/day pure alpha on top of market beta
        port = ff["Mkt-RF"] + ff["RF"] + 0.0010 + rng.normal(0, 0.002, len(ff))

        res = run_regression(
            port, ff, factor_columns=["Mkt-RF"], spec_name="CAPM", periods_per_year=252
        )

        self.assertGreater(res.alpha_daily, 0.0005)
        self.assertGreater(res.alpha_tstat, 2.0)

    def test_hac_tstat_lower_than_ols_for_autocorrelated_noise(self):
        """Autocorrelation trap: AR(1) errors inflate OLS t-stat; HAC must dampen it."""
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(1000, seed=7)
        rng = np.random.default_rng(8)
        # Generate AR(1) noise with rho=0.7 and zero true alpha
        rho = 0.7
        eps = rng.normal(0, 0.001, len(ff))
        noise = np.zeros(len(ff))
        noise[0] = eps[0]
        for i in range(1, len(ff)):
            noise[i] = rho * noise[i - 1] + eps[i]
        port = ff["Mkt-RF"] + ff["RF"] + pd.Series(noise, index=ff.index)

        res_ols = run_regression(
            port,
            ff,
            factor_columns=["Mkt-RF"],
            cov_type="nonrobust",
            spec_name="CAPM-OLS",
            periods_per_year=252,
        )
        res_hac = run_regression(
            port,
            ff,
            factor_columns=["Mkt-RF"],
            cov_type="HAC",
            spec_name="CAPM-HAC",
            periods_per_year=252,
        )

        self.assertAlmostEqual(res_ols.alpha_daily, res_hac.alpha_daily, places=10)
        self.assertLess(abs(res_hac.alpha_tstat), abs(res_ols.alpha_tstat))

    def test_missing_factor_column_raises_with_name(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(200, seed=9)
        port = ff["Mkt-RF"] + ff["RF"]

        with self.assertRaises(ValueError) as cm:
            run_regression(port, ff, factor_columns=["Mkt-RF", "QMJ"], periods_per_year=252)
        self.assertIn("QMJ", str(cm.exception))

    def test_missing_rf_column_raises(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(200, seed=10).drop(columns=["RF"])
        port = pd.Series(np.random.default_rng(0).normal(0, 0.01, len(ff)), index=ff.index)

        with self.assertRaises(ValueError) as cm:
            run_regression(port, ff, factor_columns=["Mkt-RF"], periods_per_year=252)
        self.assertIn("RF", str(cm.exception))

    def test_hac_maxlags_explicit_override(self):
        """Explicit hac_maxlags overrides the daily-tuned formula. For overlapping
        returns (e.g. stride=5 + holding=20 → MA(3-4) by construction), the caller
        must pass an explicit maxlags or HAC SEs are biased.
        """
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(500, seed=20)
        rng = np.random.default_rng(21)
        # Construct strongly autocorrelated noise — formula maxlags is too small.
        eps = rng.normal(0, 0.001, len(ff))
        noise = np.zeros(len(ff))
        for i in range(len(ff)):
            window = noise[max(0, i - 6) : i]
            noise[i] = 0.85 * window.mean() + eps[i] if len(window) > 0 else eps[i]
        port = ff["Mkt-RF"] + ff["RF"] + pd.Series(noise, index=ff.index)

        res_default = run_regression(
            port, ff, factor_columns=["Mkt-RF"], cov_type="HAC", periods_per_year=252
        )
        res_lag10 = run_regression(
            port,
            ff,
            factor_columns=["Mkt-RF"],
            cov_type="HAC",
            hac_maxlags=10,
            periods_per_year=252,
        )

        # Same point estimate, different t-stat (HAC SE differs with maxlags).
        self.assertAlmostEqual(res_default.alpha_daily, res_lag10.alpha_daily, places=10)
        self.assertNotAlmostEqual(res_default.alpha_tstat, res_lag10.alpha_tstat, places=4)

    def test_hac_maxlags_ignored_for_nonrobust(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(200, seed=22)
        port = ff["Mkt-RF"] + ff["RF"]
        # Should not raise, even though hac_maxlags is set with cov_type=nonrobust.
        res = run_regression(
            port,
            ff,
            factor_columns=["Mkt-RF"],
            cov_type="nonrobust",
            hac_maxlags=5,
            periods_per_year=252,
        )
        self.assertEqual(res.cov_type, "nonrobust")

    def test_subtract_rf_false_allows_missing_rf_column(self):
        """Long-short factor returns are already excess. Caller passes subtract_rf=False."""
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(500, seed=30).drop(columns=["RF"])
        rng = np.random.default_rng(31)
        # Already-excess synthetic L/S return with +5 bps/day pure alpha
        ls_factor = pd.Series(0.0005 + rng.normal(0, 0.002, len(ff)), index=ff.index)

        res = run_regression(
            ls_factor,
            ff,
            factor_columns=["Mkt-RF", "SMB", "HML"],
            spec_name="L/S factor",
            subtract_rf=False,
            periods_per_year=252,
        )
        self.assertGreater(res.alpha_daily, 0.0002)
        # Sanity: requesting subtract_rf=True without RF still raises
        with self.assertRaises(ValueError):
            run_regression(
                ls_factor, ff, factor_columns=["Mkt-RF"], subtract_rf=True, periods_per_year=252
            )

    def test_insufficient_overlap_raises(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(10, seed=11)
        port = pd.Series(np.random.default_rng(0).normal(0, 0.01, 10), index=ff.index)

        with self.assertRaises(ValueError):
            run_regression(port, ff, factor_columns=["Mkt-RF"], periods_per_year=252)

    def test_betas_dict_has_one_entry_per_factor(self):
        from alphalens.attribution.factor_analysis import run_regression

        ff = _synthetic_carhart(400, seed=12)
        port = ff["Mkt-RF"] + ff["RF"]

        res = run_regression(
            port, ff, factor_columns=["Mkt-RF", "SMB", "HML", "Mom"], periods_per_year=252
        )
        self.assertEqual(set(res.betas.keys()), {"Mkt-RF", "SMB", "HML", "Mom"})


class TestCarhartAttribution(unittest.TestCase):
    """run_carhart_attribution returns [CAPM, FF3, Carhart-4F] so you can see
    where alpha survives or dies as factors are added."""

    def test_returns_three_specs_in_order(self):
        from alphalens.attribution.factor_analysis import run_carhart_attribution

        ff = _synthetic_carhart(500, seed=13)
        port = ff["Mkt-RF"] + ff["RF"] + 0.0005

        results = run_carhart_attribution(port, ff)

        self.assertEqual([r.spec_name for r in results], ["CAPM", "FF3", "Carhart-4F"])

    def test_carhart_spec_includes_momentum_beta(self):
        from alphalens.attribution.factor_analysis import run_carhart_attribution

        ff = _synthetic_carhart(500, seed=14)
        port = ff["Mom"] + ff["RF"]

        results = run_carhart_attribution(port, ff)
        carhart = results[-1]
        self.assertIn("Mom", carhart.betas)
        self.assertAlmostEqual(carhart.betas["Mom"], 1.0, delta=0.2)

    def test_momentum_repackaging_kills_alpha_in_carhart_but_not_ff3(self):
        """Synthetic repackaged-momentum portfolio: alpha looks real under FF3
        (no UMD to absorb it) but vanishes under Carhart."""
        from alphalens.attribution.factor_analysis import run_carhart_attribution

        ff = _synthetic_carhart(1000, seed=15)
        rng = np.random.default_rng(16)
        # Portfolio = 1.0 * Mom + RF + tiny noise → pure momentum factor exposure, zero true alpha
        port = ff["Mom"] + ff["RF"] + rng.normal(0, 0.0005, len(ff))

        results = run_carhart_attribution(port, ff)
        by_spec = {r.spec_name: r for r in results}

        # FF3 attributes the Mom-driven return to "alpha" (no Mom factor in regression)
        self.assertGreater(by_spec["FF3"].alpha_daily, 0.0)
        # Carhart correctly attributes it to Mom beta → alpha ≈ 0, beta_mom ≈ 1
        self.assertAlmostEqual(by_spec["Carhart-4F"].alpha_daily, 0.0, delta=0.0002)
        self.assertAlmostEqual(by_spec["Carhart-4F"].betas["Mom"], 1.0, delta=0.2)


class TestRollingRegression(unittest.TestCase):
    def test_returns_timeseries_of_betas_plus_alpha(self):
        from alphalens.attribution.factor_analysis import run_rolling_regression

        ff = _synthetic_carhart(300, seed=17)
        port = ff["Mkt-RF"] + ff["RF"]

        window = 60
        df = run_rolling_regression(port, ff, factor_columns=["Mkt-RF", "SMB"], window=window)

        self.assertIn("alpha", df.columns)
        self.assertIn("beta_Mkt-RF", df.columns)
        self.assertIn("beta_SMB", df.columns)
        # Each window produces one row; first window-1 rows are NaN.
        self.assertEqual(len(df), len(ff))
        # Non-NaN rows = total - window + 1
        valid = df["alpha"].notna().sum()
        self.assertEqual(valid, len(ff) - window + 1)

    def test_rolling_mkt_beta_close_to_one_for_mkt_mimicking_portfolio(self):
        from alphalens.attribution.factor_analysis import run_rolling_regression

        ff = _synthetic_carhart(300, seed=18)
        rng = np.random.default_rng(19)
        port = ff["Mkt-RF"] + ff["RF"] + rng.normal(0, 0.0005, len(ff))

        df = run_rolling_regression(port, ff, factor_columns=["Mkt-RF"], window=60)
        betas = df["beta_Mkt-RF"].dropna()
        self.assertTrue((betas.mean() > 0.8) and (betas.mean() < 1.2))


class TestIndustryControls(unittest.TestCase):
    def test_sector_tilt_alpha_shrinks_when_industry_added_as_regressor(self):
        """Portfolio = 1.0 * BusEq industry return + RF + noise.
        Carhart alone leaves residual 'alpha'; adding BusEq as regressor absorbs it."""
        from alphalens.attribution.factor_analysis import run_regression

        rng = np.random.default_rng(20)
        n = 800
        idx = pd.date_range("2020-01-01", periods=n, freq="B")
        mkt = rng.normal(0.0004, 0.01, n)
        buseq = mkt + rng.normal(0.0003, 0.012, n)  # tech: mkt correlated, higher vol

        factors = pd.DataFrame(
            {
                "Mkt-RF": mkt,
                "SMB": rng.normal(0.0001, 0.006, n),
                "HML": rng.normal(0.0001, 0.005, n),
                "Mom": rng.normal(0.0002, 0.008, n),
                "BusEq": buseq,
                "RF": np.full(n, 0.00002),
            },
            index=idx,
        )

        port = factors["BusEq"] + factors["RF"] + rng.normal(0, 0.0005, n)

        carhart_only = run_regression(
            port,
            factors,
            factor_columns=["Mkt-RF", "SMB", "HML", "Mom"],
            spec_name="Carhart-4F",
            periods_per_year=252,
        )
        with_industry = run_regression(
            port,
            factors,
            factor_columns=["Mkt-RF", "SMB", "HML", "Mom", "BusEq"],
            spec_name="Carhart-4F + BusEq",
            periods_per_year=252,
        )

        self.assertLess(abs(with_industry.alpha_daily), abs(carhart_only.alpha_daily))
        self.assertAlmostEqual(with_industry.betas["BusEq"], 1.0, delta=0.2)


class TestCarhartPlusIndustryRobustness(unittest.TestCase):
    """Zen raised near-collinearity risk: Mkt-RF is ~ cap-weighted mean of
    industry returns, so Carhart (has Mkt-RF) + 12 Industries is rank-deficient-ish.
    statsmodels OLS uses pinv → distributes loading along null space, shouldn't NaN."""

    def test_no_nan_betas_with_near_collinear_mkt_and_industries(self):
        from alphalens.attribution.factor_analysis import run_regression

        rng = np.random.default_rng(42)
        n = 1000
        idx = pd.date_range("2020-01-01", periods=n, freq="B")

        industry_names = [
            "NoDur",
            "Durbl",
            "Manuf",
            "Enrgy",
            "Chems",
            "BusEq",
            "Telcm",
            "Utils",
            "Shops",
            "Hlth",
            "Money",
            "Other",
        ]
        industries = pd.DataFrame(
            rng.normal(0.0003, 0.012, (n, 12)),
            index=idx,
            columns=industry_names,
        )
        # Mkt-RF mostly driven by industry mean (near-collinear) + small idiosyncratic noise.
        mkt_rf = 0.85 * industries.mean(axis=1) + 0.15 * rng.normal(0.0004, 0.01, n)

        factors = pd.DataFrame(
            {
                "Mkt-RF": mkt_rf.values,
                "SMB": rng.normal(0.0001, 0.006, n),
                "HML": rng.normal(0.0001, 0.005, n),
                "Mom": rng.normal(0.0002, 0.008, n),
                "RF": np.full(n, 0.00002),
            },
            index=idx,
        )
        factors = pd.concat([factors, industries], axis=1)

        port = factors["Mkt-RF"] + factors["RF"] + rng.normal(0, 0.001, n)

        carhart_cols = ["Mkt-RF", "SMB", "HML", "Mom"]
        res = run_regression(
            port,
            factors,
            factor_columns=carhart_cols + industry_names,
            spec_name="Carhart-4F + 12 Industries",
            periods_per_year=252,
        )

        self.assertTrue(np.isfinite(res.alpha_daily))
        self.assertTrue(np.isfinite(res.alpha_tstat))
        self.assertEqual(len(res.betas), 16)
        for name, beta in res.betas.items():
            self.assertTrue(np.isfinite(beta), f"NaN / inf beta for {name}")


class TestFormatSummary(unittest.TestCase):
    def test_format_contains_spec_alpha_and_all_betas(self):
        from alphalens.attribution.factor_analysis import AlphaResult, format_alpha_summary

        res = AlphaResult(
            spec_name="Carhart-4F",
            alpha_daily=0.0002,
            alpha_annualized=0.05,
            alpha_tstat=2.31,
            betas={"Mkt-RF": 1.05, "SMB": 0.22, "HML": -0.15, "Mom": 0.40},
            r_squared=0.51,
            n_observations=1200,
            cov_type="HAC",
        )
        text = format_alpha_summary(res)

        self.assertIn("Carhart-4F", text)
        self.assertIn("alpha", text)
        self.assertIn("Mkt-RF", text)
        self.assertIn("Mom", text)
        self.assertIn("HAC", text)

    def test_attribution_table_lists_each_spec_on_one_line(self):
        from alphalens.attribution.factor_analysis import (
            AlphaResult,
            format_attribution_table,
        )

        results = [
            AlphaResult("CAPM", 0.0003, 0.076, 2.5, {"Mkt-RF": 1.0}, 0.4, 1000, "HAC"),
            AlphaResult(
                "FF3",
                0.0002,
                0.050,
                1.9,
                {"Mkt-RF": 1.0, "SMB": 0.2, "HML": 0.1},
                0.45,
                1000,
                "HAC",
            ),
            AlphaResult(
                "Carhart-4F",
                0.00005,
                0.013,
                0.5,
                {"Mkt-RF": 1.0, "SMB": 0.2, "HML": 0.1, "Mom": 0.7},
                0.55,
                1000,
                "HAC",
            ),
        ]

        text = format_attribution_table(results)
        for spec in ["CAPM", "FF3", "Carhart-4F"]:
            self.assertIn(spec, text)


class TestBootstrapCarhartAlphaCi(unittest.TestCase):
    """Moving-block bootstrap on Carhart-4F α intercept."""

    def test_zero_alpha_strategy_ci_brackets_zero(self):
        from alphalens.attribution.factor_analysis import bootstrap_carhart_alpha_ci

        carhart = _synthetic_carhart(n=500, seed=1)
        rng = np.random.default_rng(2)
        # Returns = 1.0 × Mkt-RF + RF + zero-mean noise → no residual α
        returns = (carhart["Mkt-RF"] + carhart["RF"] + rng.normal(0, 0.005, len(carhart))).rename(
            "port"
        )

        ci_low, ci_high = bootstrap_carhart_alpha_ci(returns, carhart, iterations=500, seed=42)

        self.assertLess(ci_low, 0)
        self.assertGreater(ci_high, 0)

    def test_strong_positive_alpha_ci_excludes_zero(self):
        from alphalens.attribution.factor_analysis import bootstrap_carhart_alpha_ci

        n = 1000
        carhart = _synthetic_carhart(n=n, seed=3)
        # Inject ~50 bps daily α (~125% annualized) — far from any noise band
        returns = (
            carhart["Mkt-RF"] + carhart["RF"] + 0.005 + np.random.default_rng(4).normal(0, 0.003, n)
        ).rename("port")

        ci_low, ci_high = bootstrap_carhart_alpha_ci(returns, carhart, iterations=500, seed=42)

        self.assertGreater(ci_low, 0)
        self.assertLess(ci_low, ci_high)

    def test_ci_returned_in_annualized_units(self):
        from alphalens.attribution.factor_analysis import bootstrap_carhart_alpha_ci

        carhart = _synthetic_carhart(n=300, seed=5)
        returns = (carhart["Mkt-RF"] + carhart["RF"]).rename("port")

        ci_low, ci_high = bootstrap_carhart_alpha_ci(returns, carhart, iterations=200, seed=42)

        # Annualized α magnitudes should easily land in [-2, 2] for benign noise;
        # daily α would be ~250× smaller. Sanity-check the scale only.
        self.assertLess(abs(ci_low), 2.0)
        self.assertLess(abs(ci_high), 2.0)

    def test_too_few_observations_raises(self):
        from alphalens.attribution.factor_analysis import bootstrap_carhart_alpha_ci

        carhart = _synthetic_carhart(n=40, seed=6)
        returns = (carhart["Mkt-RF"] + carhart["RF"]).rename("port")

        with self.assertRaises(ValueError):
            bootstrap_carhart_alpha_ci(returns, carhart, iterations=100, seed=42)

    def test_confidence_level_widens_ci(self):
        from alphalens.attribution.factor_analysis import bootstrap_carhart_alpha_ci

        carhart = _synthetic_carhart(n=400, seed=7)
        returns = (carhart["Mkt-RF"] + carhart["RF"]).rename("port")

        low_95, high_95 = bootstrap_carhart_alpha_ci(
            returns, carhart, iterations=300, seed=42, confidence=0.95
        )
        low_99, high_99 = bootstrap_carhart_alpha_ci(
            returns, carhart, iterations=300, seed=42, confidence=0.99
        )

        # 99% CI must be at least as wide as 95% CI on both sides
        self.assertLessEqual(low_99, low_95)
        self.assertGreaterEqual(high_99, high_95)


class TestFitCarhart4FInvestedOnly(unittest.TestCase):
    """Paradigm-14 PEAD v2 Phase C: invested-days-only Carhart-4F with NW HAC.

    Contract per plan §1.C1: caller marks uninvested days as NaN in
    ``daily_returns``; helper drops them, reindexes factors on the surviving
    invested index, and fits Carhart-4F with HAC SE at the caller-chosen lag
    count (default maxlags=20, half the 20-day PEAD hold).
    """

    def test_nan_days_excluded(self) -> None:
        """NaN days in daily_returns must NOT enter the regression — n_observations
        equals count of non-NaN days, and a zero-return uninvested day does not
        get treated as a real observation that drags the alpha toward zero."""
        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        carhart = _synthetic_carhart(500, seed=7)
        # Build a port return that captures a clean +5bps/day alpha when
        # invested. Mask 60% of days as NaN (uninvested).
        invested_alpha_daily = 0.0005
        port = (carhart["Mkt-RF"] + carhart["RF"]).copy() + invested_alpha_daily
        port.iloc[: int(0.6 * len(port))] = np.nan
        expected_n = int(port.notna().sum())

        result = fit_carhart_4f_invested_only(port, carhart)
        self.assertEqual(result.n_observations, expected_n)
        # Sanity: invested-days regression must recover the injected α (≈5bps).
        self.assertAlmostEqual(result.alpha_daily, invested_alpha_daily, places=4)

    def test_hac_maxlags_passed_to_statsmodels(self) -> None:
        """Default maxlags=20 must take a different HAC path than maxlags=1
        on positively-autocorrelated errors → different t-stats. Locks the
        plumbing of the ``maxlags`` kwarg through ``run_regression`` to the
        statsmodels HAC ``cov_kwds``."""
        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        rng = np.random.default_rng(123)
        n = 800
        idx = pd.date_range("2020-01-01", periods=n, freq="B")
        # Build factors first.
        carhart = pd.DataFrame(
            {
                "Mkt-RF": rng.normal(0.0004, 0.01, n),
                "SMB": rng.normal(0.0001, 0.006, n),
                "HML": rng.normal(0.0001, 0.005, n),
                "Mom": rng.normal(0.0002, 0.008, n),
                "RF": np.full(n, 0.00002),
            },
            index=idx,
        )
        # AR(1) shock with ρ=0.6 added to portfolio — positively autocorrelated
        # residual that HAC SHOULD inflate SE for.
        shock = np.zeros(n)
        rho = 0.6
        eps = rng.normal(0.0, 0.005, n)
        for i in range(1, n):
            shock[i] = rho * shock[i - 1] + eps[i]
        port = pd.Series(carhart["Mkt-RF"].to_numpy() + carhart["RF"].to_numpy() + shock, index=idx)

        res_lag20 = fit_carhart_4f_invested_only(port, carhart, maxlags=20)
        res_lag1 = fit_carhart_4f_invested_only(port, carhart, maxlags=1)
        # alpha point estimate is identical (same OLS); only SE/t-stat differ.
        self.assertAlmostEqual(res_lag20.alpha_daily, res_lag1.alpha_daily, places=12)
        self.assertNotAlmostEqual(res_lag20.alpha_tstat, res_lag1.alpha_tstat, places=3)
        # Positively-autocorr errors → longer lag widens HAC SE → |t| shrinks.
        self.assertLess(abs(res_lag20.alpha_tstat), abs(res_lag1.alpha_tstat))
        # Both report HAC cov_type.
        self.assertEqual(res_lag20.cov_type, "HAC")
        self.assertEqual(res_lag1.cov_type, "HAC")

    def test_alpha_tstat_matches_manual_computation_on_known_AR1_series(self) -> None:
        """Sanity-check NW HAC on a known positively-autocorrelated series.

        Two assertions: (1) HAC t-stat from the helper exactly matches a
        direct ``sm.OLS(...).fit(cov_type='HAC', cov_kwds={'maxlags': 20})``
        call — locks the maxlags plumbing through ``run_regression`` to
        statsmodels. (2) HAC SE is materially larger than the plain-OLS
        SE on the same fit — verifies the Bartlett kernel is actually
        widening the SE in response to the AR(1) autocorrelation in errors
        (not silently disabled).
        """
        import warnings

        import statsmodels.api as sm

        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        rng = np.random.default_rng(2026)
        n = 600
        idx = pd.date_range("2020-01-01", periods=n, freq="B")
        # Independent zero-mean factors — uncorrelated with port (which is
        # pure AR(1) + drift). Realistic daily-factor scale keeps the design
        # well-conditioned; the OLS fit absorbs no port variance because the
        # factors are random-walk-independent of port by construction.
        carhart = pd.DataFrame(
            {
                "Mkt-RF": rng.normal(0.0, 0.01, n),
                "SMB": rng.normal(0.0, 0.006, n),
                "HML": rng.normal(0.0, 0.005, n),
                "Mom": rng.normal(0.0, 0.008, n),
                "RF": np.zeros(n),
            },
            index=idx,
        )
        alpha_true = 0.0005
        shock = np.zeros(n)
        rho = 0.5
        eps = rng.normal(0.0, 0.01, n)
        for i in range(1, n):
            shock[i] = rho * shock[i - 1] + eps[i]
        y = alpha_true + shock
        port = pd.Series(y, index=idx)

        # Suppress statsmodels HAC sandwich numerical-stability warnings
        # (known v0.15 divide-by-zero/overflow in the lag-product sums on
        # AR(1) synthetic input — same warnings appear in existing
        # test_factor_analysis tests). Functionally harmless.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            res = fit_carhart_4f_invested_only(port, carhart, maxlags=20)

            # (1) Direct sm.OLS reference call with the same maxlags must
            # produce the identical intercept t-stat — exact equality (no
            # FP drift) since the helper just wraps the same statsmodels
            # code path.
            X_ref = sm.add_constant(carhart[["Mkt-RF", "SMB", "HML", "Mom"]])
            ref_hac = sm.OLS(port, X_ref).fit(cov_type="HAC", cov_kwds={"maxlags": 20})
            ref_ols = sm.OLS(port, X_ref).fit()

        self.assertAlmostEqual(res.alpha_tstat, float(ref_hac.tvalues["const"]), places=10)

        # (2) HAC SE > OLS SE on positively-autocorrelated errors — the
        # whole point of using HAC. AR(1) with ρ=0.5 has theoretical
        # long-run variance (1+ρ)/(1-ρ) = 3× the contemporaneous variance,
        # so HAC SE should be ~√3 ≈ 1.73× OLS SE at infinite n. Finite-sample
        # Bartlett kernel attenuates; 1.4× is the empirically-observed
        # floor on n=600 with ρ=0.5 (typical run gives ~1.45-1.65×).
        hac_se = float(ref_hac.bse["const"])
        ols_se = float(ref_ols.bse["const"])
        self.assertGreater(hac_se, 1.4 * ols_se)

    def test_factors_reindexed_on_invested_subset(self) -> None:
        """Factors must be aligned on the post-mask invested index — passing
        a factor frame with extra dates does NOT bleed those dates into the
        regression (regression sees only invested-day factor rows)."""
        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        carhart = _synthetic_carhart(500, seed=9)
        port = (carhart["Mkt-RF"] + carhart["RF"]).copy() + 0.0005
        port.iloc[100:200] = np.nan  # mid-series gap of 100 days
        invested_n = int(port.notna().sum())

        result = fit_carhart_4f_invested_only(port, carhart)
        self.assertEqual(result.n_observations, invested_n)

    def test_too_few_invested_days_raises(self) -> None:
        """<20 invested days → ValueError (matches run_regression's contract)."""
        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        carhart = _synthetic_carhart(500, seed=11)
        port = (carhart["Mkt-RF"] + carhart["RF"]).copy() + 0.0005
        port.iloc[15:] = np.nan  # only 15 invested days

        with self.assertRaises(ValueError):
            fit_carhart_4f_invested_only(port, carhart)

    def test_invested_mask_marks_uninvested_days(self) -> None:
        """invested_mask=False selects uninvested days even when daily_returns
        carries 0.0 on those days (the B2 adapter contract). Safer than
        relying on NaN-marking which is silently broken when the caller
        passes a zero-filled series."""
        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        carhart = _synthetic_carhart(500, seed=13)
        # Build a port that has 0.0 on the first 60% of days (matching how
        # B2's portfolio_returns_from_weights returns zero on uninvested
        # days) — no NaN markers anywhere.
        invested_alpha_daily = 0.0005
        port = (carhart["Mkt-RF"] + carhart["RF"]).copy() + invested_alpha_daily
        n_uninvested = int(0.6 * len(port))
        port.iloc[:n_uninvested] = 0.0
        mask = pd.Series(False, index=port.index)
        mask.iloc[n_uninvested:] = True
        expected_n = int(mask.sum())

        result = fit_carhart_4f_invested_only(port, carhart, invested_mask=mask)
        self.assertEqual(result.n_observations, expected_n)
        # α should be recoverable from the invested-only subset (≈5bps),
        # NOT compressed by the zero-filled uninvested rows that would
        # otherwise drag it toward zero.
        self.assertAlmostEqual(result.alpha_daily, invested_alpha_daily, places=4)

    def test_invested_mask_differs_from_zero_filled_default(self) -> None:
        """Direct comparison: same series passed (a) with mask, (b) without
        mask. The masked α recovers ~5bps; the un-masked α is compressed
        toward zero by the included zero-return days. Locks the contract
        that the mask is the correct API for B2-style return series.

        Uses a pure-alpha port (zero factor exposure) so the intercept
        must average over all observations — the OLS factor betas cannot
        absorb the zero-day structural difference.
        """
        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        carhart = _synthetic_carhart(500, seed=17)
        invested_alpha_daily = 0.0005
        # Pure-alpha port: invested days = α + RF; uninvested = 0. After
        # subtract_rf inside the regression, invested becomes α and uninvested
        # becomes -RF ≈ 0. With no factor exposure, β≈0 and intercept must
        # take the literal sample mean.
        port = pd.Series(carhart["RF"].to_numpy() + invested_alpha_daily, index=carhart.index)
        n_uninvested = int(0.6 * len(port))
        port.iloc[:n_uninvested] = 0.0
        mask = pd.Series(False, index=port.index)
        mask.iloc[n_uninvested:] = True

        masked = fit_carhart_4f_invested_only(port, carhart, invested_mask=mask)
        unmasked = fit_carhart_4f_invested_only(port, carhart)  # 0.0 days included
        # Masked α ≈ true α; unmasked α is compressed toward 0 by the
        # zero-filled rows that the helper cannot distinguish from
        # real-but-zero invested-day returns.
        self.assertAlmostEqual(masked.alpha_daily, invested_alpha_daily, places=4)
        self.assertLess(unmasked.alpha_daily, masked.alpha_daily * 0.6)

    def test_zero_invested_days_raises_specific_error(self) -> None:
        """All-False mask → specific 'zero invested days' message
        distinguishes operator errors from the generic <20-obs error of
        run_regression."""
        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        carhart = _synthetic_carhart(500, seed=19)
        port = (carhart["Mkt-RF"] + carhart["RF"]).copy()
        mask = pd.Series(False, index=port.index)  # nothing invested

        with self.assertRaises(ValueError) as cm:
            fit_carhart_4f_invested_only(port, carhart, invested_mask=mask)
        self.assertIn("zero invested days", str(cm.exception))

    def test_periods_per_year_override(self) -> None:
        """periods_per_year is exposed as a kwarg for strategies with
        non-daily rebalance cadence — e.g. weekly rotation."""
        from alphalens.attribution.factor_analysis import fit_carhart_4f_invested_only

        carhart = _synthetic_carhart(500, seed=23)
        port = (carhart["Mkt-RF"] + carhart["RF"]).copy() + 0.0005

        daily = fit_carhart_4f_invested_only(port, carhart)
        weekly = fit_carhart_4f_invested_only(port, carhart, periods_per_year=52)
        self.assertEqual(daily.periods_per_year_assumption, 252)
        self.assertEqual(weekly.periods_per_year_assumption, 52)
        self.assertAlmostEqual(weekly.alpha_annualized, weekly.alpha_per_period * 52, places=12)


if __name__ == "__main__":
    unittest.main()
