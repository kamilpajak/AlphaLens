"""TDD coverage for signal-vs-vol-regime conditional analysis.

Two bugs surfaced in the 2026-05-10 insider_form4 Layer 4 overlay design
session that this module/test pair pin down:

1. Quintile bucketing must be per-window (per phase), not pooled, when
   secular vol drift would otherwise put one phase entirely in Q1 or Q5.

2. Counter-cyclical classification must handle all sign combinations of
   mean(Q1+Q2) and mean(Q4+Q5) — naive R = mean_high / mean_low ratio
   loses interpretability when the denominator is negative or zero.
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd
from alphalens_research.attribution.signal_vol_regime import (
    CounterCyclicalVerdict,
    CyclicalityExcessVerdict,
    VolRegimeQuintileSummary,
    aggregate_returns_by_regime,
    assign_vol_regime_quintiles,
    classify_cyclicality,
    classify_cyclicality_excess,
)


class TestAssignVolRegimeQuintiles(unittest.TestCase):
    def test_5_equal_buckets(self):
        vol = pd.Series([0.10, 0.15, 0.20, 0.25, 0.30] * 20)  # 100 obs
        quintiles = assign_vol_regime_quintiles(vol, n_quintiles=5)
        counts = quintiles.value_counts().sort_index()
        self.assertGreaterEqual(counts.min(), 19)
        self.assertLessEqual(counts.max(), 21)
        self.assertEqual(sorted(counts.index.tolist()), ["Q1", "Q2", "Q3", "Q4", "Q5"])

    def test_monotone_in_vol(self):
        vol = pd.Series(np.linspace(0.05, 0.50, 100))
        quintiles = assign_vol_regime_quintiles(vol, n_quintiles=5)
        self.assertEqual(quintiles.iloc[0], "Q1")
        self.assertEqual(quintiles.iloc[-1], "Q5")
        df = pd.DataFrame({"vol": vol, "q": quintiles})
        means = df.groupby("q", observed=True)["vol"].mean()
        self.assertLess(means["Q1"], means["Q2"])
        self.assertLess(means["Q2"], means["Q3"])
        self.assertLess(means["Q3"], means["Q4"])
        self.assertLess(means["Q4"], means["Q5"])

    def test_drops_nan(self):
        vol = pd.Series([0.10, np.nan, 0.20, np.nan, 0.30, 0.15, 0.25, 0.05, 0.40, 0.35])
        quintiles = assign_vol_regime_quintiles(vol, n_quintiles=5)
        self.assertEqual(quintiles.isna().sum(), 2)
        non_nan = quintiles.dropna()
        self.assertTrue(set(non_nan.unique()).issubset({"Q1", "Q2", "Q3", "Q4", "Q5"}))

    def test_too_few_obs_raises(self):
        vol = pd.Series([0.10, 0.20, 0.30])  # 3 obs, can't form 5 quintiles
        with self.assertRaisesRegex(ValueError, "at least"):
            assign_vol_regime_quintiles(vol, n_quintiles=5)


class TestAggregateReturnsByRegime(unittest.TestCase):
    def test_basic_pooled(self):
        rng = np.random.default_rng(0)
        n_per = 100
        quintile_means = {"Q1": -0.001, "Q2": 0.000, "Q3": 0.001, "Q4": 0.002, "Q5": 0.003}
        returns_chunks = []
        quintile_chunks = []
        for q, mu in quintile_means.items():
            returns_chunks.append(pd.Series(rng.normal(mu, 0.01, n_per)))
            quintile_chunks.append(pd.Series([q] * n_per))
        returns = pd.concat(returns_chunks, ignore_index=True)
        quintiles = pd.concat(quintile_chunks, ignore_index=True)

        summary = aggregate_returns_by_regime(returns, quintiles, periods_per_year=252)

        for q, expected_mu in quintile_means.items():
            self.assertLess(abs(summary.quintile_means[q] - expected_mu), 0.002)
        self.assertTrue((summary.quintile_counts == n_per).all())
        expected_sharpe_q5 = quintile_means["Q5"] / 0.01 * math.sqrt(252)
        self.assertLess(abs(summary.quintile_sharpes["Q5"] - expected_sharpe_q5), 1.0)

    def test_handles_nan_returns(self):
        quintiles = pd.Series(
            ["Q1", "Q1", "Q1", "Q5", "Q5", "Q5", "Q2", "Q2", "Q3", "Q3", "Q4", "Q4"]
        )
        returns = pd.Series(
            [0.001, np.nan, 0.002, 0.005, 0.006, np.nan, 0.001, 0.001, 0.0015, 0.0015, 0.002, 0.002]
        )
        summary = aggregate_returns_by_regime(returns, quintiles, periods_per_year=252)
        self.assertLess(abs(summary.quintile_means["Q1"] - 0.0015), 1e-9)
        self.assertEqual(summary.quintile_counts["Q1"], 2)
        self.assertEqual(summary.quintile_counts["Q5"], 2)

    def test_misaligned_lengths_raises(self):
        quintiles = pd.Series(["Q1", "Q2", "Q3"], index=[0, 1, 2])
        returns = pd.Series([0.01, 0.02], index=[0, 1])  # length mismatch
        with self.assertRaisesRegex(ValueError, "length"):
            aggregate_returns_by_regime(returns, quintiles, periods_per_year=252)

    def test_empty_quintile_raises(self):
        quintiles = pd.Series(["Q1", "Q1", "Q5", "Q5"])  # missing Q2/Q3/Q4
        returns = pd.Series([0.001, 0.002, 0.003, 0.004])
        with self.assertRaisesRegex(ValueError, "missing|empty"):
            aggregate_returns_by_regime(returns, quintiles, periods_per_year=252)


def _make_summary(q1_mu, q2_mu, q3_mu, q4_mu, q5_mu, *, std=0.01):
    means = pd.Series({"Q1": q1_mu, "Q2": q2_mu, "Q3": q3_mu, "Q4": q4_mu, "Q5": q5_mu})
    stds = pd.Series({"Q1": std, "Q2": std, "Q3": std, "Q4": std, "Q5": std})
    counts = pd.Series({"Q1": 100, "Q2": 100, "Q3": 100, "Q4": 100, "Q5": 100})
    sharpes = means / stds * math.sqrt(252)
    return VolRegimeQuintileSummary(
        quintile_means=means,
        quintile_stds=stds,
        quintile_counts=counts,
        quintile_sharpes=sharpes,
    )


class TestClassifyCyclicality(unittest.TestCase):
    """Bug-fix epicenter: must handle all sign combinations of mean(Q1+Q2) vs mean(Q4+Q5)."""

    def test_both_positive_strong_counter_cyclical_rejects(self):
        s = _make_summary(0.0005, 0.0010, 0.0020, 0.0030, 0.0040)
        v = classify_cyclicality(s)
        self.assertTrue(v.classification.startswith("STRONG counter-cyclical"))
        self.assertFalse(v.proceed)
        expected_R = (0.003 + 0.004) / (0.0005 + 0.0010)
        self.assertLess(abs(v.r_mean - expected_R), 0.01)

    def test_both_positive_orthogonal_proceeds(self):
        s = _make_summary(0.001, 0.001, 0.001, 0.0011, 0.0009)
        v = classify_cyclicality(s)
        self.assertTrue(v.classification.startswith("orthogonal"))
        self.assertTrue(v.proceed)

    def test_both_positive_calm_concentrated_proceeds(self):
        s = _make_summary(0.003, 0.004, 0.002, 0.001, 0.0005)
        v = classify_cyclicality(s)
        self.assertTrue(v.classification.startswith("calm-period"))
        self.assertTrue(v.proceed)

    def test_sign_flip_q1q2_neg_q4q5_pos_extreme_counter_cyclical_rejects(self):
        # CRITICAL bug case: actual insider_form4 pattern.
        s = _make_summary(-0.001, 0.0, 0.0014, 0.0022, 0.0027)
        v = classify_cyclicality(s)
        self.assertTrue(v.classification.startswith("EXTREME counter-cyclical"))
        self.assertFalse(v.proceed)
        # R_mean should be negative or +inf when denominator is non-positive
        self.assertTrue(v.r_mean < 0 or math.isinf(v.r_mean))

    def test_sign_flip_q1q2_pos_q4q5_neg_extreme_calm_concentrated_proceeds(self):
        s = _make_summary(0.003, 0.002, 0.0, -0.001, -0.0015)
        v = classify_cyclicality(s)
        self.assertTrue(v.classification.startswith("EXTREME calm-period"))
        self.assertTrue(v.proceed)

    def test_both_negative_inconclusive(self):
        s = _make_summary(-0.002, -0.001, -0.0015, -0.001, -0.002)
        v = classify_cyclicality(s)
        self.assertTrue(v.classification.startswith("INCONCLUSIVE"))
        self.assertIsNone(v.proceed)

    def test_q1q2_zero_handled_without_division_by_zero(self):
        s = _make_summary(0.0, 0.0, 0.001, 0.002, 0.003)
        v = classify_cyclicality(s)
        cls = v.classification
        self.assertTrue(
            cls.startswith("EXTREME counter-cyclical") or cls.startswith("STRONG counter-cyclical")
        )
        self.assertFalse(v.proceed)
        # No division-by-zero crash; R_mean either +inf or specially flagged
        self.assertTrue(math.isinf(v.r_mean) or v.r_mean > 1e6)

    def test_sharpe_cross_check_consistent_with_mean_no_override(self):
        s = _make_summary(-0.001, 0.0, 0.0014, 0.0022, 0.0027, std=0.01)
        v = classify_cyclicality(s)
        # Sign-flip in mean → also in Sharpe
        self.assertLess(v.r_sharpe, 0)
        self.assertFalse(v.proceed)

    def test_sharpe_flat_overrides_strong_counter_cyclical_to_proceed(self):
        # Mean strongly counter-cyclical BUT Sharpe constant across quintiles
        # (mean and std scale together) → vol-target overlay would be neutral on Sharpe basis
        means = pd.Series({"Q1": 0.0005, "Q2": 0.001, "Q3": 0.0015, "Q4": 0.002, "Q5": 0.003})
        stds = means / 0.001 * 0.01  # constant Sharpe
        counts = pd.Series({"Q1": 100, "Q2": 100, "Q3": 100, "Q4": 100, "Q5": 100})
        sharpes = means / stds * math.sqrt(252)
        summary = VolRegimeQuintileSummary(
            quintile_means=means,
            quintile_stds=stds,
            quintile_counts=counts,
            quintile_sharpes=sharpes,
        )
        v = classify_cyclicality(summary)
        self.assertGreaterEqual(v.r_mean, 1.5)
        self.assertGreater(v.r_sharpe, 0.95)
        self.assertLess(v.r_sharpe, 1.05)
        # Decision flipped to PROCEED with annotation
        self.assertTrue(v.proceed)
        self.assertIn("sharpe", v.rationale.lower())

    def test_returns_dataclass_with_required_fields(self):
        s = _make_summary(0.001, 0.001, 0.001, 0.001, 0.001)
        v = classify_cyclicality(s)
        self.assertIsInstance(v, CounterCyclicalVerdict)
        self.assertIsInstance(v.r_mean, float)
        self.assertIsInstance(v.r_sharpe, float)
        self.assertIsInstance(v.sign_pattern, str)
        self.assertIsInstance(v.classification, str)
        self.assertIsInstance(v.rationale, str)
        self.assertIn(v.proceed, (True, False, None))


class TestClassifyCyclicalityExcess(unittest.TestCase):
    """Strategy-specific cyclicality classification (excess over benchmark baseline).

    Per session 2026-05-10 finding: R2000 long-only strategies inherit IWM
    benchmark's EXTREME counter-cyclical baseline (R≈-2.0). Strategy-specific
    cyclicality requires comparison against this baseline. Strategy is
    GENUINELY counter-cyclical (warrants Layer 4 overlay rejection) only if
    its R_mean is meaningfully BELOW benchmark R_mean.
    """

    def _make_summary_from_R(self, mean_low: float, mean_high: float, std: float = 0.01):
        """Construct VolRegimeQuintileSummary with target Q1+Q2 mean and Q4+Q5 mean."""
        # Use linear interpolation across Q1-Q5 to hit target means
        means = pd.Series(
            {
                "Q1": mean_low * 1.1,
                "Q2": mean_low * 0.9,
                "Q3": (mean_low + mean_high) / 2,
                "Q4": mean_high * 1.1,
                "Q5": mean_high * 0.9,
            }
        )
        stds = pd.Series(dict.fromkeys(["Q1", "Q2", "Q3", "Q4", "Q5"], std))
        counts = pd.Series(dict.fromkeys(["Q1", "Q2", "Q3", "Q4", "Q5"], 100))
        sharpes = means / stds * math.sqrt(252)
        return VolRegimeQuintileSummary(means, stds, counts, sharpes)

    def test_strategy_far_more_counter_cyclical_than_baseline_rejects(self):
        # Strategy R≈-4.7 (insider_form4-like), benchmark R≈-2.0 (IWM-like)
        strat = self._make_summary_from_R(mean_low=-0.001, mean_high=0.0027)  # R_mean ≈ -2.7
        bench = self._make_summary_from_R(mean_low=-0.0006, mean_high=0.0009)  # R_mean ≈ -1.5
        v = classify_cyclicality_excess(strat, bench)
        # Excess R_mean strongly negative → strategy-specific counter-cyclical
        self.assertLess(v.excess_r_mean, -1.0)
        self.assertEqual(v.classification, "strategy-specific counter-cyclical")
        self.assertFalse(v.proceed)  # Layer 4 overlay would structurally hurt

    def test_strategy_matches_baseline_does_not_trigger_rejection(self):
        # Strategy R ≈ benchmark R (both ≈ -2.0)
        strat = self._make_summary_from_R(mean_low=-0.0005, mean_high=0.001)
        bench = self._make_summary_from_R(mean_low=-0.0005, mean_high=0.001)
        v = classify_cyclicality_excess(strat, bench)
        # Excess ≈ 0 → matches baseline → not strategy-specific cyclical
        self.assertLess(abs(v.excess_r_mean), 0.5)
        self.assertEqual(v.classification, "matches benchmark baseline")
        self.assertTrue(
            v.proceed
        )  # Layer 4 overlay decision driven by other factors, not cyclicality

    def test_strategy_less_counter_cyclical_than_baseline_proceeds(self):
        # Strategy LESS counter-cyclical than benchmark (e.g., mom+lowvol pattern)
        strat = self._make_summary_from_R(mean_low=0.0001, mean_high=0.0005)  # both positive, mild
        bench = self._make_summary_from_R(
            mean_low=-0.0006, mean_high=0.0009
        )  # benchmark is sign-flip
        v = classify_cyclicality_excess(strat, bench)
        # Strategy R is positive (or near zero), benchmark R is negative → excess > 0
        self.assertGreater(v.excess_r_mean, 0.5)
        self.assertEqual(v.classification, "less counter-cyclical than benchmark")
        self.assertTrue(v.proceed)

    def test_returns_dataclass_with_required_fields(self):
        strat = self._make_summary_from_R(mean_low=0.001, mean_high=0.002)
        bench = self._make_summary_from_R(mean_low=0.001, mean_high=0.002)
        v = classify_cyclicality_excess(strat, bench)
        self.assertIsInstance(v, CyclicalityExcessVerdict)
        self.assertIsInstance(v.strategy_r_mean, float)
        self.assertIsInstance(v.benchmark_r_mean, float)
        self.assertIsInstance(v.excess_r_mean, float)
        self.assertIsInstance(v.classification, str)
        self.assertIsInstance(v.rationale, str)
        self.assertIn(v.proceed, (True, False, None))

    def test_excess_threshold_is_configurable(self):
        # Same strategy/benchmark, different threshold → different classification
        strat = self._make_summary_from_R(mean_low=-0.001, mean_high=0.0017)  # R ≈ -1.7
        bench = self._make_summary_from_R(mean_low=-0.0008, mean_high=0.0012)  # R ≈ -1.5
        # Excess ≈ -0.2; default threshold -1.0 → matches baseline
        v_default = classify_cyclicality_excess(strat, bench)
        self.assertEqual(v_default.classification, "matches benchmark baseline")
        # Stricter threshold -0.1 → would classify as strategy-specific
        v_strict = classify_cyclicality_excess(strat, bench, excess_strong_threshold=-0.1)
        self.assertEqual(v_strict.classification, "strategy-specific counter-cyclical")

    def test_inconclusive_when_benchmark_not_counter_cyclical(self):
        # If benchmark R ≥ 0 (not counter-cyclical), excess concept is mathematically
        # invalid (subtraction reverses semantic meaning across sign boundary).
        # Per zen 2026-05-10 review: short-circuit to INCONCLUSIVE with proceed=None.
        strat = self._make_summary_from_R(mean_low=-0.001, mean_high=0.0017)
        # Benchmark with both quintiles positive AND equal means → R ≈ 1.1
        bench = self._make_summary_from_R(mean_low=0.001, mean_high=0.0011)
        v = classify_cyclicality_excess(strat, bench)
        # Hard-fail to INCONCLUSIVE — no fall-through to standard branches
        self.assertIsNone(v.proceed)
        self.assertTrue(v.classification.startswith("INCONCLUSIVE"))
        self.assertTrue(math.isnan(v.excess_r_mean))


if __name__ == "__main__":
    unittest.main()
