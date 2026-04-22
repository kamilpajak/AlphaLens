import unittest


def _alpha_result(spec_name: str, t: float, daily: float = 0.0003):
    from alphalens.backtest.factor_analysis import AlphaResult

    return AlphaResult(
        spec_name=spec_name,
        alpha_daily=daily,
        alpha_annualized=daily * 252,
        alpha_tstat=t,
        betas={"Mkt-RF": 0.5},
        r_squared=0.4,
        n_observations=1200,
        cov_type="HAC",
    )


class TestDecisionMatrix(unittest.TestCase):
    def _base_inputs(self):
        """Minimal set of inputs that should yield GO verdict."""
        return {
            "carhart": _alpha_result("Carhart-4F", t=3.0),
            "ff5_umd": _alpha_result("FF5+UMD", t=2.5),
            "q4": _alpha_result("Q4", t=2.5),
            "net_alpha_primary": 0.05,     # +5% annualized net
            "net_alpha_stress_k15": 0.02,  # +2% net under k=0.15
            "bootstrap_95ci_excludes_zero": True,
            "sharpe_net": 1.2,
            "regime_alpha_tstats": {"bull": 2.0, "bear": 1.8, "flat": 1.9},
            "n_tests": 2,
        }

    def test_all_gates_pass_yields_go(self):
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        report = evaluate_exit_criteria(**self._base_inputs())

        self.assertEqual(report.verdict, "GO")
        self.assertEqual(report.failing_gates, [])

    def test_carhart_far_below_bonferroni_yields_kill(self):
        """α_t < 1.5 is KILL zone per design doc §8."""
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["carhart"] = _alpha_result("Carhart-4F", t=1.0)  # below ambiguous floor

        report = evaluate_exit_criteria(**inputs)

        self.assertEqual(report.verdict, "KILL")
        self.assertIn("carhart_alpha_bonferroni", report.failing_gates)

    def test_ff5_umd_heavy_attenuation_flags_gate(self):
        """R5 locked: >30% attenuation vs Carhart signals profitability loading."""
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["ff5_umd"] = _alpha_result("FF5+UMD", t=0.5)  # huge drop from 3.0

        report = evaluate_exit_criteria(**inputs)

        self.assertIn("ff5_umd_attenuation", report.failing_gates)

    def test_negative_net_alpha_kill(self):
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["net_alpha_primary"] = -0.01

        report = evaluate_exit_criteria(**inputs)

        self.assertEqual(report.verdict, "KILL")
        self.assertIn("net_alpha_primary", report.failing_gates)

    def test_stress_k15_fails_kill(self):
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["net_alpha_stress_k15"] = -0.005  # breaks under stress

        report = evaluate_exit_criteria(**inputs)

        self.assertIn("net_alpha_stress_k15", report.failing_gates)

    def test_sharpe_below_one_fails(self):
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["sharpe_net"] = 0.8

        report = evaluate_exit_criteria(**inputs)

        self.assertIn("sharpe_net", report.failing_gates)

    def test_regime_collapse_flags(self):
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["regime_alpha_tstats"]["bear"] = 1.0  # below 1.5 threshold

        report = evaluate_exit_criteria(**inputs)

        self.assertIn("regime_collapse_bear", report.failing_gates)

    def test_bootstrap_ci_includes_zero_flags(self):
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["bootstrap_95ci_excludes_zero"] = False

        report = evaluate_exit_criteria(**inputs)

        self.assertIn("bootstrap_ci", report.failing_gates)

    def test_q4_missing_is_not_a_blocker(self):
        """Q4 is best-effort per plan; coverage gap 2025-2026 may leave it None."""
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["q4"] = None

        report = evaluate_exit_criteria(**inputs)

        self.assertEqual(report.verdict, "GO")
        self.assertEqual(report.failing_gates, [])

    def test_ambiguous_zone_alpha_t_1p5_to_2p24(self):
        """Per design doc: OOS α_t ∈ [1.5, 2.24] → 6-12mo paper-track."""
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = self._base_inputs()
        inputs["carhart"] = _alpha_result("Carhart-4F", t=1.8)

        report = evaluate_exit_criteria(**inputs)

        self.assertEqual(report.verdict, "PAPER_TRACK")


class TestReportContents(unittest.TestCase):
    def test_all_gates_listed_with_pass_fail(self):
        from alphalens.backtest.decision_matrix import evaluate_exit_criteria

        inputs = {
            "carhart": _alpha_result("Carhart-4F", t=3.0),
            "ff5_umd": _alpha_result("FF5+UMD", t=2.5),
            "q4": _alpha_result("Q4", t=2.5),
            "net_alpha_primary": 0.05,
            "net_alpha_stress_k15": 0.02,
            "bootstrap_95ci_excludes_zero": True,
            "sharpe_net": 1.2,
            "regime_alpha_tstats": {"bull": 2.0, "bear": 1.8, "flat": 1.9},
            "n_tests": 2,
        }

        report = evaluate_exit_criteria(**inputs)

        # Every gate we check must appear in the report's gate dict
        self.assertIn("carhart_alpha_bonferroni", report.gates)
        self.assertIn("ff5_umd_alpha", report.gates)
        self.assertIn("ff5_umd_attenuation", report.gates)
        self.assertIn("net_alpha_primary", report.gates)
        self.assertIn("net_alpha_stress_k15", report.gates)
        self.assertIn("bootstrap_ci", report.gates)
        self.assertIn("sharpe_net", report.gates)
        self.assertIn("regime_collapse_bull", report.gates)
        self.assertIn("regime_collapse_bear", report.gates)
        self.assertIn("regime_collapse_flat", report.gates)


if __name__ == "__main__":
    unittest.main()
