import unittest

from alphalens.rotation.config import Rule


def _rule(name, signal, op, threshold, tilt):
    return Rule(name=name, signal=signal, operator=op, threshold=threshold, tilt=tilt)


class TestMacroRegimeFrozen(unittest.TestCase):
    def test_macro_regime_is_frozen(self):
        from alphalens.macro.scorer import MacroRegime

        regime = MacroRegime(flags={"yield_steep": True}, tilt_sum={"SPY": -0.05})
        with self.assertRaises(Exception):
            regime.flags = {}


class TestRuleBasedScorer(unittest.TestCase):
    def test_rule_fires_when_operator_matches(self):
        from alphalens.macro.scorer import RuleBasedScorer

        rules = (
            _rule(
                "yield_steep",
                "yield_curve_slope",
                "gt",
                1.0,
                {"QQQ": 0.05, "SPY": -0.05},
            ),
        )
        scorer = RuleBasedScorer(rules)

        regime = scorer.score({"yield_curve_slope": 1.5})

        self.assertTrue(regime.flags["yield_steep"])
        self.assertAlmostEqual(regime.tilt_sum["QQQ"], 0.05)
        self.assertAlmostEqual(regime.tilt_sum["SPY"], -0.05)

    def test_rule_does_not_fire_when_condition_false(self):
        from alphalens.macro.scorer import RuleBasedScorer

        rules = (
            _rule(
                "yield_steep",
                "yield_curve_slope",
                "gt",
                1.0,
                {"QQQ": 0.05, "SPY": -0.05},
            ),
        )
        scorer = RuleBasedScorer(rules)

        regime = scorer.score({"yield_curve_slope": 0.5})

        self.assertFalse(regime.flags["yield_steep"])
        self.assertEqual(regime.tilt_sum, {})

    def test_missing_signal_leaves_rule_unfired(self):
        """Safe default when a signal is NaN / unavailable."""
        from alphalens.macro.scorer import RuleBasedScorer

        rules = (
            _rule(
                "vix_elevated",
                "vix_decile",
                "gt",
                0.75,
                {"SPY": 0.05, "QQQ": -0.05},
            ),
        )
        scorer = RuleBasedScorer(rules)

        regime = scorer.score({"vix_decile": float("nan")})

        self.assertFalse(regime.flags["vix_elevated"])
        self.assertEqual(regime.tilt_sum, {})

    def test_multiple_rules_compose_tilts(self):
        from alphalens.macro.scorer import RuleBasedScorer

        rules = (
            _rule(
                "yield_steep",
                "yield_curve_slope",
                "gt",
                1.0,
                {"QQQ": 0.05, "SPY": -0.05},
            ),
            _rule(
                "vix_elevated",
                "vix_decile",
                "gt",
                0.75,
                {"SPY": 0.05, "QQQ": -0.05},
            ),
        )
        scorer = RuleBasedScorer(rules)

        regime = scorer.score({"yield_curve_slope": 1.5, "vix_decile": 0.9})

        self.assertTrue(regime.flags["yield_steep"])
        self.assertTrue(regime.flags["vix_elevated"])
        # QQQ: +0.05 -0.05 = 0;  SPY: -0.05 +0.05 = 0
        self.assertAlmostEqual(regime.tilt_sum.get("QQQ", 0.0), 0.0)
        self.assertAlmostEqual(regime.tilt_sum.get("SPY", 0.0), 0.0)

    def test_operators_lt_ge_le(self):
        from alphalens.macro.scorer import RuleBasedScorer

        rules = (
            _rule("lt_rule", "x", "lt", 1.0, {"SPY": 0.01}),
            _rule("ge_rule", "x", "ge", 1.0, {"QQQ": 0.02}),
            _rule("le_rule", "y", "le", 1.0, {"IWM": 0.03}),
        )
        scorer = RuleBasedScorer(rules)

        regime = scorer.score({"x": 0.5, "y": 1.0})

        self.assertTrue(regime.flags["lt_rule"])
        self.assertFalse(regime.flags["ge_rule"])
        self.assertTrue(regime.flags["le_rule"])

    def test_explain_returns_per_rule_detail(self):
        from alphalens.macro.scorer import RuleBasedScorer

        rules = (
            _rule(
                "yield_steep",
                "yield_curve_slope",
                "gt",
                1.0,
                {"QQQ": 0.05, "SPY": -0.05},
            ),
        )
        scorer = RuleBasedScorer(rules)

        detail = scorer.explain({"yield_curve_slope": 1.5})

        self.assertEqual(detail["yield_steep"]["fired"], True)
        self.assertAlmostEqual(detail["yield_steep"]["signal_value"], 1.5)
        self.assertAlmostEqual(detail["yield_steep"]["threshold"], 1.0)
        self.assertEqual(detail["yield_steep"]["operator"], "gt")

    def test_rule_order_preserved_in_flags(self):
        from alphalens.macro.scorer import RuleBasedScorer

        rules = (
            _rule("a", "x", "gt", 0.0, {"SPY": 0.01}),
            _rule("b", "x", "gt", 0.0, {"SPY": 0.01}),
        )
        scorer = RuleBasedScorer(rules)

        regime = scorer.score({"x": 1.0})

        self.assertListEqual(list(regime.flags.keys()), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
