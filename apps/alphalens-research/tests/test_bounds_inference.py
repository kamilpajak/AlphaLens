"""TDD tests for :mod:`alphalens_research.backtest.bounds_inference`.

Andrews-Manski (2010) partial-identification bounds for unbiased α-tstat
when the bias source is known and bounded — but the unbiased point estimate
is not directly observable. Maps a contiguous bias range
[bias_lower_pct, bias_upper_pct] in annualized-return space to a
corresponding interval in t-stat space using the regression's standard
error of the alpha estimate.
"""

from __future__ import annotations

import math
import unittest

from alphalens_research.backtest.bounds_inference import (
    BoundsResult,
    andrews_manski_bounds,
)


class AndrewsManskiBoundsTests(unittest.TestCase):
    def test_zero_bias_collapses_to_point_estimate(self) -> None:
        result = andrews_manski_bounds(
            alpha_t=2.29,
            alpha_pct=4.5,
            alpha_pct_se=2.0,
            bias_lower_pct=0.0,
            bias_upper_pct=0.0,
        )

        self.assertAlmostEqual(result.alpha_t_lower, 2.29, places=6)
        self.assertAlmostEqual(result.alpha_t_upper, 2.29, places=6)
        self.assertAlmostEqual(result.alpha_pct_lower, 4.5, places=6)
        self.assertAlmostEqual(result.alpha_pct_upper, 4.5, places=6)

    def test_symmetric_bias_centers_on_point(self) -> None:
        result = andrews_manski_bounds(
            alpha_t=2.0,
            alpha_pct=5.0,
            alpha_pct_se=2.0,
            bias_lower_pct=-1.0,
            bias_upper_pct=1.0,
        )

        # Bias subtraction: αpct_unbiased = αpct_observed - B
        # B ∈ [-1, 1] → αpct_unbiased ∈ [5 - 1, 5 - (-1)] = [4, 6]
        self.assertAlmostEqual(result.alpha_pct_lower, 4.0, places=6)
        self.assertAlmostEqual(result.alpha_pct_upper, 6.0, places=6)
        # T-stat shift: αt_unbiased = αt_observed - B/SE; B/SE ∈ [-0.5, 0.5]
        # → αt_unbiased ∈ [2.0 - 0.5, 2.0 + 0.5] = [1.5, 2.5]
        self.assertAlmostEqual(result.alpha_t_lower, 1.5, places=6)
        self.assertAlmostEqual(result.alpha_t_upper, 2.5, places=6)

    def test_positive_bias_range_shifts_unbiased_downward(self) -> None:
        # Survivor bias is positive — observed αt overstates true αt.
        # bias ∈ [1.0, 2.0] /y → unbiased αpct ∈ [observed - 2.0, observed - 1.0]
        result = andrews_manski_bounds(
            alpha_t=3.5,
            alpha_pct=8.0,
            alpha_pct_se=2.0,
            bias_lower_pct=1.0,
            bias_upper_pct=2.0,
        )

        self.assertAlmostEqual(result.alpha_pct_lower, 6.0, places=6)
        self.assertAlmostEqual(result.alpha_pct_upper, 7.0, places=6)
        self.assertAlmostEqual(result.alpha_t_lower, 2.5, places=6)
        self.assertAlmostEqual(result.alpha_t_upper, 3.0, places=6)

    def test_lower_bound_excludes_zero_when_observed_strong(self) -> None:
        # PASS_ROBUST scenario: αt > +3.5, bias upper 2%, SE typical
        result = andrews_manski_bounds(
            alpha_t=4.0,
            alpha_pct=10.0,
            alpha_pct_se=2.5,
            bias_lower_pct=1.0,
            bias_upper_pct=2.0,
        )

        # lower-bound αt = 4.0 - 2.0/2.5 = 3.2 > 0
        self.assertGreater(result.alpha_t_lower, 0.0)
        self.assertTrue(result.lower_bound_excludes_zero)

    def test_lower_bound_includes_zero_when_observed_marginal(self) -> None:
        # INCONCLUSIVE scenario: αt low, bias upper close to swamping
        result = andrews_manski_bounds(
            alpha_t=1.0,
            alpha_pct=2.5,
            alpha_pct_se=2.5,
            bias_lower_pct=1.0,
            bias_upper_pct=3.0,
        )

        # lower-bound αt = 1.0 - 3.0/2.5 = -0.2 → straddles zero
        self.assertLess(result.alpha_t_lower, 0.0)
        self.assertFalse(result.lower_bound_excludes_zero)

    def test_returns_bounds_result_instance(self) -> None:
        result = andrews_manski_bounds(
            alpha_t=2.0,
            alpha_pct=5.0,
            alpha_pct_se=2.0,
            bias_lower_pct=1.0,
            bias_upper_pct=2.0,
        )

        self.assertIsInstance(result, BoundsResult)

    def test_rejects_invalid_se(self) -> None:
        with self.assertRaises(ValueError):
            andrews_manski_bounds(
                alpha_t=2.0,
                alpha_pct=5.0,
                alpha_pct_se=0.0,
                bias_lower_pct=1.0,
                bias_upper_pct=2.0,
            )
        with self.assertRaises(ValueError):
            andrews_manski_bounds(
                alpha_t=2.0,
                alpha_pct=5.0,
                alpha_pct_se=-1.0,
                bias_lower_pct=1.0,
                bias_upper_pct=2.0,
            )

    def test_rejects_swapped_bias_bounds(self) -> None:
        with self.assertRaises(ValueError):
            andrews_manski_bounds(
                alpha_t=2.0,
                alpha_pct=5.0,
                alpha_pct_se=2.0,
                bias_lower_pct=2.0,
                bias_upper_pct=1.0,
            )

    def test_handles_non_finite_inputs(self) -> None:
        with self.assertRaises(ValueError):
            andrews_manski_bounds(
                alpha_t=float("nan"),
                alpha_pct=5.0,
                alpha_pct_se=2.0,
                bias_lower_pct=1.0,
                bias_upper_pct=2.0,
            )
        with self.assertRaises(ValueError):
            andrews_manski_bounds(
                alpha_t=2.0,
                alpha_pct=float("inf"),
                alpha_pct_se=2.0,
                bias_lower_pct=1.0,
                bias_upper_pct=2.0,
            )

    def test_implied_se_consistency_check(self) -> None:
        # If SE-implied αt = αpct/SE doesn't match the provided αt
        # within tolerance, the inputs are inconsistent — caller may have
        # mismatched the regression outputs. Allow a small tolerance because
        # SE is computed from the same regression but may use different
        # numerical paths (HAC vs OLS). 5% tolerance is loose-but-real.
        # SE from our values: 5.0/2.0 = 2.5, but αt=2.0 → implied SE=2.5
        # consistency: 2.5 / 2.5 = 1.0 ratio ✓
        # If we set alpha_t=2.0 alpha_pct=5.0 SE=2.0, implied=5.0/2.0=2.5
        # vs observed αt=2.0 → ratio 2.5/2.0 = 1.25 = 25% mismatch → SHOULD raise
        with self.assertRaises(ValueError):
            andrews_manski_bounds(
                alpha_t=2.0,
                alpha_pct=5.0,
                alpha_pct_se=2.0,  # implies αt=2.5, mismatches given 2.0
                bias_lower_pct=0.0,
                bias_upper_pct=1.0,
                consistency_tol=0.05,
            )

    def test_consistency_check_disabled_by_default(self) -> None:
        # No consistency check unless explicitly requested via consistency_tol
        result = andrews_manski_bounds(
            alpha_t=2.0,
            alpha_pct=5.0,
            alpha_pct_se=2.0,
            bias_lower_pct=0.0,
            bias_upper_pct=1.0,
        )
        self.assertTrue(math.isfinite(result.alpha_t_lower))


if __name__ == "__main__":
    unittest.main()
