import unittest

import numpy as np


class TestComputePositionWeights(unittest.TestCase):
    def test_equal_weights_sum_to_one(self):
        from alphalens.backtest.weighting import compute_position_weights

        w = compute_position_weights(5, "equal")
        self.assertAlmostEqual(w.sum(), 1.0)
        for v in w:
            self.assertAlmostEqual(v, 0.2)

    def test_linear_descends(self):
        from alphalens.backtest.weighting import compute_position_weights

        w = compute_position_weights(10, "linear")
        self.assertAlmostEqual(w.sum(), 1.0)
        # w[0] najwyższa, w[-1] najniższa
        for i in range(len(w) - 1):
            self.assertGreater(w[i], w[i + 1])

    def test_conviction_three_tiers(self):
        from alphalens.backtest.weighting import compute_position_weights

        w = compute_position_weights(9, "conviction")  # 3 top, 3 middle, 3 bottom
        self.assertAlmostEqual(w.sum(), 1.0)
        # Top three equal and highest
        self.assertAlmostEqual(w[0], w[1])
        self.assertAlmostEqual(w[1], w[2])
        # Middle three equal and mid
        self.assertAlmostEqual(w[3], w[4])
        self.assertAlmostEqual(w[4], w[5])
        # Bottom three equal and lowest
        self.assertAlmostEqual(w[6], w[7])
        self.assertAlmostEqual(w[7], w[8])
        # Top > Mid > Bottom
        self.assertGreater(w[0], w[3])
        self.assertGreater(w[3], w[6])

    def test_conviction_ratio_2_1_05(self):
        from alphalens.backtest.weighting import compute_position_weights

        w = compute_position_weights(9, "conviction")
        # Ratios 2:1:0.5 preserved after normalization
        top, mid, bottom = w[0], w[3], w[6]
        self.assertAlmostEqual(top / mid, 2.0, places=5)
        self.assertAlmostEqual(mid / bottom, 2.0, places=5)

    def test_conviction_small_n(self):
        """N=3 powinno rozpaść się gracefully na 1 top, 1 mid, 1 bottom."""
        from alphalens.backtest.weighting import compute_position_weights

        w = compute_position_weights(3, "conviction")
        self.assertEqual(len(w), 3)
        self.assertAlmostEqual(w.sum(), 1.0)
        self.assertGreater(w[0], w[-1])

    def test_unknown_scheme_raises(self):
        from alphalens.backtest.weighting import compute_position_weights

        with self.assertRaises(ValueError):
            compute_position_weights(5, "wild")  # type: ignore[arg-type]

    def test_empty_n(self):
        from alphalens.backtest.weighting import compute_position_weights

        w = compute_position_weights(0, "equal")
        self.assertEqual(len(w), 0)

    def test_n_equals_1(self):
        from alphalens.backtest.weighting import compute_position_weights

        for scheme in ("equal", "linear", "conviction"):
            w = compute_position_weights(1, scheme)
            self.assertEqual(len(w), 1)
            self.assertAlmostEqual(w[0], 1.0)


class TestWeightedReturn(unittest.TestCase):
    def test_equal_weight_equals_mean(self):
        from alphalens.backtest.weighting import weighted_return

        returns = np.array([0.01, 0.02, -0.01, 0.03, 0.0])
        weights = np.full(5, 0.2)
        self.assertAlmostEqual(weighted_return(returns, weights), np.mean(returns))

    def test_top_heavy_weighting(self):
        from alphalens.backtest.weighting import weighted_return

        returns = np.array([0.10, 0.0, 0.0, 0.0, 0.0])
        # All weight on top — portfolio return = 0.10
        weights = np.array([1.0, 0.0, 0.0, 0.0, 0.0])
        self.assertAlmostEqual(weighted_return(returns, weights), 0.10)

    def test_nan_excluded_and_weights_renormalized(self):
        from alphalens.backtest.weighting import weighted_return

        returns = np.array([0.02, np.nan, 0.04])
        weights = np.array([0.5, 0.25, 0.25])
        # Valid: 0.02 * 0.5 + 0.04 * 0.25 normalized by 0.75
        # = (0.02 * 0.5 + 0.04 * 0.25) / 0.75 = (0.01 + 0.01) / 0.75 = 0.02667
        expected = (0.02 * 0.5 + 0.04 * 0.25) / 0.75
        self.assertAlmostEqual(weighted_return(returns, weights), expected)

    def test_all_nan_returns_zero(self):
        from alphalens.backtest.weighting import weighted_return

        returns = np.array([np.nan, np.nan, np.nan])
        weights = np.array([1 / 3, 1 / 3, 1 / 3])
        self.assertEqual(weighted_return(returns, weights), 0.0)

    def test_empty_inputs(self):
        from alphalens.backtest.weighting import weighted_return

        self.assertEqual(weighted_return(np.array([]), np.array([])), 0.0)

    def test_length_mismatch_raises(self):
        from alphalens.backtest.weighting import weighted_return

        with self.assertRaises(ValueError):
            weighted_return(np.array([0.01, 0.02]), np.array([0.5]))


if __name__ == "__main__":
    unittest.main()
