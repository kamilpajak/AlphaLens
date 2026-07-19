import unittest

import numpy as np


class TestComputePositionWeights(unittest.TestCase):
    def test_equal_weights_sum_to_one(self):
        from alphalens_research.backtest.weighting import compute_position_weights

        w = compute_position_weights(5, "equal")
        self.assertAlmostEqual(w.sum(), 1.0)
        for v in w:
            self.assertAlmostEqual(v, 0.2)

    def test_linear_descends(self):
        from alphalens_research.backtest.weighting import compute_position_weights

        w = compute_position_weights(10, "linear")
        self.assertAlmostEqual(w.sum(), 1.0)
        # w[0] highest, w[-1] lowest
        for i in range(len(w) - 1):
            self.assertGreater(w[i], w[i + 1])

    def test_conviction_three_tiers(self):
        from alphalens_research.backtest.weighting import compute_position_weights

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
        from alphalens_research.backtest.weighting import compute_position_weights

        w = compute_position_weights(9, "conviction")
        # Ratios 2:1:0.5 preserved after normalization
        top, mid, bottom = w[0], w[3], w[6]
        self.assertAlmostEqual(top / mid, 2.0, places=5)
        self.assertAlmostEqual(mid / bottom, 2.0, places=5)

    def test_conviction_small_n(self):
        """N=3 should collapse gracefully to 1 top, 1 mid, 1 bottom."""
        from alphalens_research.backtest.weighting import compute_position_weights

        w = compute_position_weights(3, "conviction")
        self.assertEqual(len(w), 3)
        self.assertAlmostEqual(w.sum(), 1.0)
        self.assertGreater(w[0], w[-1])

    def test_unknown_scheme_raises(self):
        from alphalens_research.backtest.weighting import compute_position_weights

        with self.assertRaises(ValueError):
            compute_position_weights(5, "wild")  # type: ignore[arg-type]

    def test_empty_n(self):
        from alphalens_research.backtest.weighting import compute_position_weights

        w = compute_position_weights(0, "equal")
        self.assertEqual(len(w), 0)

    def test_n_equals_1(self):
        from alphalens_research.backtest.weighting import compute_position_weights

        for scheme in ("equal", "linear", "conviction"):
            w = compute_position_weights(1, scheme)
            self.assertEqual(len(w), 1)
            self.assertAlmostEqual(w[0], 1.0)


class TestWeightedReturn(unittest.TestCase):
    def test_equal_weight_equals_mean(self):
        from alphalens_research.backtest.weighting import weighted_return

        returns = np.array([0.01, 0.02, -0.01, 0.03, 0.0])
        weights = np.full(5, 0.2)
        self.assertAlmostEqual(weighted_return(returns, weights), np.mean(returns))

    def test_top_heavy_weighting(self):
        from alphalens_research.backtest.weighting import weighted_return

        returns = np.array([0.10, 0.0, 0.0, 0.0, 0.0])
        # All weight on top — portfolio return = 0.10
        weights = np.array([1.0, 0.0, 0.0, 0.0, 0.0])
        self.assertAlmostEqual(weighted_return(returns, weights), 0.10)

    def test_nan_excluded_and_weights_renormalized(self):
        from alphalens_research.backtest.weighting import weighted_return

        returns = np.array([0.02, np.nan, 0.04])
        weights = np.array([0.5, 0.25, 0.25])
        # Valid: 0.02 * 0.5 + 0.04 * 0.25 normalized by 0.75
        # = (0.02 * 0.5 + 0.04 * 0.25) / 0.75 = (0.01 + 0.01) / 0.75 = 0.02667
        expected = (0.02 * 0.5 + 0.04 * 0.25) / 0.75
        self.assertAlmostEqual(weighted_return(returns, weights), expected)

    def test_all_nan_returns_zero(self):
        from alphalens_research.backtest.weighting import weighted_return

        returns = np.array([np.nan, np.nan, np.nan])
        weights = np.array([1 / 3, 1 / 3, 1 / 3])
        self.assertEqual(weighted_return(returns, weights), 0.0)

    def test_empty_inputs(self):
        from alphalens_research.backtest.weighting import weighted_return

        self.assertEqual(weighted_return(np.array([]), np.array([])), 0.0)

    def test_length_mismatch_raises(self):
        from alphalens_research.backtest.weighting import weighted_return

        with self.assertRaises(ValueError):
            weighted_return(np.array([0.01, 0.02]), np.array([0.5]))


class TestComputePositionWeightsMutationHardening(unittest.TestCase):
    """Pin operator/constant mutations in compute_position_weights that the
    behavioural tests above miss (cosmic-ray survivors). Each test names the
    mutation it kills."""

    def test_negative_n_returns_empty(self):
        # `n <= 0` guards negative sizes too; an `== 0` mutant lets n=-1 fall
        # through to np.full(-1, ...) which raises. Kills `<=`->`==` (L32).
        from alphalens_research.backtest.weighting import compute_position_weights

        self.assertEqual(len(compute_position_weights(-1, "equal")), 0)

    def test_equal_scheme_matched_by_value_not_identity(self):
        # A non-interned "equal" string is == but not `is` the source literal;
        # an `==`->`is` mutant would skip the branch and raise. Kills `==`->`is` (L35).
        from alphalens_research.backtest.weighting import compute_position_weights

        scheme = "".join(["eq", "ual"])  # equal value, distinct (non-interned) object
        w = compute_position_weights(4, scheme)  # type: ignore[arg-type]
        self.assertTrue(np.allclose(w, 0.25))

    def test_linear_scheme_matched_by_value_not_identity(self):
        # Kills `==`->`is` on the linear-scheme comparison (L38).
        from alphalens_research.backtest.weighting import compute_position_weights

        scheme = "".join(["lin", "ear"])
        w = compute_position_weights(3, scheme)  # type: ignore[arg-type]
        self.assertAlmostEqual(w[0], 0.6060606060606061)

    def test_conviction_scheme_matched_by_value_not_identity(self):
        # Kills `==`->`is` on the conviction-scheme comparison (L43).
        from alphalens_research.backtest.weighting import compute_position_weights

        scheme = "".join(["convic", "tion"])
        w = compute_position_weights(3, scheme)  # type: ignore[arg-type]
        self.assertAlmostEqual(w[0], 0.5714285714285714)

    def test_scheme_lexically_below_conviction_still_raises(self):
        # `scheme == "conviction"` must reject a scheme that merely sorts before
        # it. An `==`->`<=` mutant would accept "aaa" (< "conviction") and run
        # the conviction branch instead of raising. Kills `==`->`<=` (L43).
        from alphalens_research.backtest.weighting import compute_position_weights

        with self.assertRaises(ValueError):
            compute_position_weights(3, "aaa")  # type: ignore[arg-type]

    def test_linear_weights_are_exact(self):
        # Exact linspace(2.0, 0.2, n)/sum — kills every NumberReplacer on the
        # two linspace endpoints (L40: 2.0->{1,3}, 0.2->{-0.8,1.2}).
        from alphalens_research.backtest.weighting import compute_position_weights

        w = compute_position_weights(3, "linear")
        np.testing.assert_allclose(w, [0.6060606060606061, 0.3333333333333333, 0.06060606060606061])

    def test_conviction_weights_are_exact_across_n(self):
        # Exact tier geometry for n=1..9 pins the top/middle/bottom counts, so
        # any mutation to the tier arithmetic (L46 (n+2)//3, L47 n//3, L52
        # max(0,middle)) changes at least one array. Kills the tier-boundary
        # NumberReplacer / bit-op mutants.
        from alphalens_research.backtest.weighting import compute_position_weights

        expected = {
            1: [1.0],
            2: [0.8, 0.2],
            3: [0.5714285714285714, 0.2857142857142857, 0.14285714285714285],
            4: [0.36363636363636365, 0.36363636363636365, 0.18181818181818182, 0.09090909090909091],
            5: [
                0.3076923076923077,
                0.3076923076923077,
                0.15384615384615385,
                0.15384615384615385,
                0.07692307692307693,
            ],
            6: [
                0.2857142857142857,
                0.2857142857142857,
                0.14285714285714285,
                0.14285714285714285,
                0.07142857142857142,
                0.07142857142857142,
            ],
            7: [
                0.2222222222222222,
                0.2222222222222222,
                0.2222222222222222,
                0.1111111111111111,
                0.1111111111111111,
                0.05555555555555555,
                0.05555555555555555,
            ],
            8: [0.2, 0.2, 0.2, 0.1, 0.1, 0.1, 0.05, 0.05],
            9: [
                0.19047619047619047,
                0.19047619047619047,
                0.19047619047619047,
                0.09523809523809523,
                0.09523809523809523,
                0.09523809523809523,
                0.047619047619047616,
                0.047619047619047616,
                0.047619047619047616,
            ],
        }
        for n, exp in expected.items():
            np.testing.assert_allclose(
                compute_position_weights(n, "conviction"), exp, err_msg=f"n={n}"
            )


class TestWeightedReturnMutationHardening(unittest.TestCase):
    """Pin operator/constant mutations in weighted_return's guards (cosmic-ray
    survivors) that the behavioural tests above miss."""

    def test_empty_returns_with_nonempty_weights_returns_zero(self):
        # The empty-guard is an OR over both lengths; an empty returns array
        # must short-circuit to 0.0 regardless of the weights. Kills the
        # returns-arm `== 0`->`== -1`/`< 0` mutants and `or`->`and` (L72).
        from alphalens_research.backtest.weighting import weighted_return

        self.assertEqual(weighted_return(np.array([]), np.array([0.5])), 0.0)

    def test_nonempty_returns_with_empty_weights_returns_zero(self):
        # Mirror: an empty weights array must short-circuit to 0.0. Kills the
        # weights-arm `== 0`->`== -1`/`< 0` mutants (L72).
        from alphalens_research.backtest.weighting import weighted_return

        self.assertEqual(weighted_return(np.array([0.5]), np.array([])), 0.0)

    def test_single_position_is_computed_not_short_circuited(self):
        # A length-1 portfolio is valid and must be computed, not treated as a
        # degenerate empty case. Kills the `len(returns) == 0`->`== 1` mutant (L72).
        from alphalens_research.backtest.weighting import weighted_return

        self.assertAlmostEqual(weighted_return(np.array([0.5]), np.array([0.3])), 0.5)

    def test_returns_shorter_than_weights_raises_value_error(self):
        # The mismatch guard is `!=`, not `>`: returns SHORTER than weights must
        # raise ValueError. A `>` mutant only fires when returns is longer, so
        # this shorter case would slip through to a different error. Kills
        # `!=`->`>` (L74).
        from alphalens_research.backtest.weighting import weighted_return

        with self.assertRaises(ValueError):
            weighted_return(np.array([0.5]), np.array([0.3, 0.2]))

    def test_equal_large_lengths_do_not_raise(self):
        # 300 is outside CPython's small-int cache, so `len(a) is len(b)` is
        # False even when the lengths are equal; an `!=`->`is not` mutant would
        # spuriously raise on matched arrays. Kills `!=`->`is not` (L74).
        from alphalens_research.backtest.weighting import weighted_return

        result = weighted_return(np.zeros(300), np.full(300, 1.0 / 300))
        self.assertIsInstance(result, float)

    def test_zero_sum_weights_returns_exactly_zero(self):
        # All-zero weights make w.sum() == 0; the guard must return exactly 0.0
        # rather than divide by zero. Kills the `== 0`->`== -1`/`< 0` mutants on
        # the weight-sum guard (L80) and the returned-constant mutants (L81:
        # 0.0->{1.0,-1.0}).
        from alphalens_research.backtest.weighting import weighted_return

        self.assertEqual(weighted_return(np.array([0.5, 0.5]), np.array([0.0, 0.0])), 0.0)

    def test_negative_sum_weights_are_computed(self):
        # Weights summing to a NEGATIVE value are still non-degenerate and must
        # be re-normalised, not zeroed. A `== 0`->`<= 0` mutant would zero them.
        # Kills `== 0`->`<= 0` on the weight-sum guard (L80).
        from alphalens_research.backtest.weighting import weighted_return

        self.assertAlmostEqual(weighted_return(np.array([0.5, 0.5]), np.array([-1.0, -2.0])), 0.5)


if __name__ == "__main__":
    unittest.main()
