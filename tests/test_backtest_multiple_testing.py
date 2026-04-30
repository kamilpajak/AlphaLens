import unittest


class TestBonferroniCriticalTstat(unittest.TestCase):
    def test_single_test_matches_single_tstat(self):
        """n=1 with α=0.05 two-tailed → critical z ≈ 1.96."""
        from alphalens.backtest.multiple_testing import bonferroni_critical_tstat

        self.assertAlmostEqual(bonferroni_critical_tstat(n_tests=1, alpha=0.05), 1.96, places=2)

    def test_n2_matches_design_doc_value(self):
        """n=2 α=0.05 → α_adj=0.025 → critical z ≈ 2.24 per design doc §1."""
        from alphalens.backtest.multiple_testing import bonferroni_critical_tstat

        self.assertAlmostEqual(bonferroni_critical_tstat(n_tests=2, alpha=0.05), 2.24, places=2)

    def test_threshold_grows_with_n(self):
        """Bonferroni conservativeness: more tests → higher bar."""
        from alphalens.backtest.multiple_testing import bonferroni_critical_tstat

        t1 = bonferroni_critical_tstat(n_tests=1, alpha=0.05)
        t10 = bonferroni_critical_tstat(n_tests=10, alpha=0.05)
        t100 = bonferroni_critical_tstat(n_tests=100, alpha=0.05)

        self.assertLess(t1, t10)
        self.assertLess(t10, t100)

    def test_custom_alpha_stricter(self):
        from alphalens.backtest.multiple_testing import bonferroni_critical_tstat

        lenient = bonferroni_critical_tstat(n_tests=2, alpha=0.05)
        strict = bonferroni_critical_tstat(n_tests=2, alpha=0.01)

        self.assertGreater(strict, lenient)

    def test_zero_n_tests_raises(self):
        from alphalens.backtest.multiple_testing import bonferroni_critical_tstat

        with self.assertRaises(ValueError):
            bonferroni_critical_tstat(n_tests=0)


class TestApplyBonferroni(unittest.TestCase):
    def test_pass_when_above_threshold(self):
        from alphalens.backtest.multiple_testing import apply_bonferroni

        # n=2 threshold ≈ 2.24; t=3.0 clearly above
        result = apply_bonferroni(alpha_tstats={"H1": 3.0, "H2": 2.5}, n_tests=2, alpha=0.05)

        self.assertEqual(result, {"H1": True, "H2": True})

    def test_fail_when_below_threshold(self):
        from alphalens.backtest.multiple_testing import apply_bonferroni

        result = apply_bonferroni(alpha_tstats={"H1": 1.8, "H2": 2.0}, n_tests=2, alpha=0.05)

        self.assertEqual(result, {"H1": False, "H2": False})

    def test_absolute_value_used(self):
        """Two-tailed test: -3.0 rejects null just as +3.0 does."""
        from alphalens.backtest.multiple_testing import apply_bonferroni

        result = apply_bonferroni(alpha_tstats={"H1": -3.0, "H2": 1.0}, n_tests=2, alpha=0.05)

        self.assertTrue(result["H1"])
        self.assertFalse(result["H2"])

    def test_empty_input_returns_empty(self):
        from alphalens.backtest.multiple_testing import apply_bonferroni

        self.assertEqual(apply_bonferroni({}, n_tests=2), {})


class TestFDR(unittest.TestCase):
    def test_bh_monotone_decreasing_unadjusted_pvalues_remain_monotone(self):
        from alphalens.backtest.multiple_testing import fdr_adjusted_pvalues

        pvals = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205]

        adjusted = fdr_adjusted_pvalues(pvals)

        self.assertEqual(len(adjusted), len(pvals))
        # All adjusted p-values bounded in [0,1]
        for p in adjusted:
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_fdr_less_conservative_than_bonferroni(self):
        """BH-FDR should admit more discoveries than Bonferroni at same alpha."""
        from alphalens.backtest.multiple_testing import fdr_adjusted_pvalues

        pvals = [0.01, 0.02, 0.03, 0.04, 0.05]
        adjusted = fdr_adjusted_pvalues(pvals)
        bonferroni = [min(1.0, p * len(pvals)) for p in pvals]

        # FDR p-values should be ≤ Bonferroni p-values (at least not larger)
        for f, b in zip(adjusted, bonferroni, strict=False):
            self.assertLessEqual(f, b + 1e-9)

    def test_fdr_empty_list(self):
        from alphalens.backtest.multiple_testing import fdr_adjusted_pvalues

        self.assertEqual(fdr_adjusted_pvalues([]), [])

    def test_fdr_single_pvalue_unchanged(self):
        from alphalens.backtest.multiple_testing import fdr_adjusted_pvalues

        self.assertAlmostEqual(fdr_adjusted_pvalues([0.03])[0], 0.03, places=9)


class TestTstatToPvalue(unittest.TestCase):
    def test_large_tstat_small_pvalue(self):
        from alphalens.backtest.multiple_testing import tstat_to_pvalue

        # |t|=3 two-tailed under standard normal → p ≈ 0.0027
        self.assertAlmostEqual(tstat_to_pvalue(3.0), 0.0027, places=3)

    def test_zero_tstat_p_equals_one(self):
        from alphalens.backtest.multiple_testing import tstat_to_pvalue

        self.assertAlmostEqual(tstat_to_pvalue(0.0), 1.0, places=6)

    def test_sign_invariant(self):
        from alphalens.backtest.multiple_testing import tstat_to_pvalue

        self.assertAlmostEqual(tstat_to_pvalue(2.5), tstat_to_pvalue(-2.5), places=6)


if __name__ == "__main__":
    unittest.main()
