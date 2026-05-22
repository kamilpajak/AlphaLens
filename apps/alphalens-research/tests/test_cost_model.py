import unittest

import numpy as np


class TestCostModelFromProfile(unittest.TestCase):
    def test_known_profiles(self):
        from alphalens_research.attribution.cost_model import CostModel

        self.assertEqual(CostModel.from_profile("gross").annual_drag_bps, 0.0)
        self.assertEqual(CostModel.from_profile("aggressive").annual_drag_bps, 75.0)
        self.assertEqual(CostModel.from_profile("moderate").annual_drag_bps, 100.0)
        self.assertEqual(CostModel.from_profile("conservative").annual_drag_bps, 150.0)

    def test_unknown_profile_raises(self):
        from alphalens_research.attribution.cost_model import CostModel

        with self.assertRaises(ValueError):
            CostModel.from_profile("wild")  # type: ignore[arg-type]


class TestPerPeriodDrag(unittest.TestCase):
    def test_100bps_annual_split_over_252(self):
        from alphalens_research.attribution.cost_model import CostModel

        drag = CostModel(annual_drag_bps=100.0).per_period_drag()
        # 100 bps = 0.01; /252 ≈ 3.97e-5
        self.assertAlmostEqual(drag, 0.01 / 252, places=10)


class TestApply(unittest.TestCase):
    def test_gross_returns_unchanged_for_zero_drag(self):
        from alphalens_research.attribution.cost_model import CostModel

        returns = [0.01, -0.005, 0.02]
        net = CostModel(annual_drag_bps=0.0).apply(returns)
        self.assertEqual(list(net), returns)

    def test_moderate_subtracts_small_drag(self):
        from alphalens_research.attribution.cost_model import CostModel

        returns = [0.01] * 252  # one year of 1% days
        net = CostModel(annual_drag_bps=100.0).apply(returns)
        # Total drag over 252 days with 100% turnover = 100 bps = 0.01
        total_drag = (returns[0] - net.iloc[0]) * 252
        self.assertAlmostEqual(total_drag, 0.01, places=6)

    def test_turnover_scales_cost(self):
        from alphalens_research.attribution.cost_model import CostModel

        returns = [0.01] * 10
        full_net = CostModel(annual_drag_bps=100.0).apply(returns)
        half_net = CostModel(annual_drag_bps=100.0).apply(returns, daily_turnover=[0.5] * 10)
        # Half the turnover → half the drag → net is higher.
        self.assertTrue((half_net > full_net).all())

    def test_mismatched_lengths_raise(self):
        from alphalens_research.attribution.cost_model import CostModel

        with self.assertRaises(ValueError):
            CostModel().apply([0.01, 0.02], daily_turnover=[0.5])


class TestSensitivityTable(unittest.TestCase):
    def test_returns_all_profiles_sorted_by_drag(self):
        from alphalens_research.attribution.cost_model import cost_sensitivity_table

        rng = np.random.default_rng(0)
        returns = rng.normal(0.001, 0.01, 252)
        df = cost_sensitivity_table(returns)

        # 5 profiles after adding "long_only_30bps" (v7 retail single-leg).
        # Sorted ascending by drag_bps: gross(0), long_only_30bps(30),
        # aggressive(75), moderate(100), conservative(150).
        self.assertEqual(len(df), 5)
        self.assertListEqual(
            list(df["profile"]),
            ["gross", "long_only_30bps", "aggressive", "moderate", "conservative"],
        )
        # Sharpe should decrease as drag increases.
        self.assertTrue((df["sharpe"].diff().dropna() <= 0).all())


class TestApplyScalarToSharpe(unittest.TestCase):
    def test_gross_sharpe_unchanged_for_zero_drag(self):
        from alphalens_research.attribution.cost_model import CostModel

        self.assertEqual(CostModel(annual_drag_bps=0.0).apply_scalar_to_sharpe(1.0, 0.01), 1.0)

    def test_nonzero_drag_reduces_sharpe(self):
        from alphalens_research.attribution.cost_model import CostModel

        reduced = CostModel(annual_drag_bps=100.0).apply_scalar_to_sharpe(1.0, 0.01)
        self.assertLess(reduced, 1.0)


if __name__ == "__main__":
    unittest.main()
