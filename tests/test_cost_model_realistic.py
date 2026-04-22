import math
import unittest

import pandas as pd


class TestPrimaryFormula(unittest.TestCase):
    def test_one_way_bps_adds_adverse_selection(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(adverse_selection_bps=5.0)

        self.assertEqual(cm.primary_one_way_bps(half_spread_bps=3.0), 8.0)

    def test_round_trip_is_two_one_way(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(adverse_selection_bps=5.0)

        self.assertEqual(cm.primary_round_trip_bps(half_spread_bps=3.0), 16.0)

    def test_period_drag_scales_linearly_with_turnover(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(adverse_selection_bps=5.0)
        full = cm.primary_period_drag_bps(half_spread_bps=3.0, turnover_fraction=1.0)
        half = cm.primary_period_drag_bps(half_spread_bps=3.0, turnover_fraction=0.5)

        self.assertEqual(full, 16.0)
        self.assertEqual(half, 8.0)

    def test_period_drag_zero_turnover_zero_cost(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(adverse_selection_bps=5.0)

        self.assertEqual(
            cm.primary_period_drag_bps(half_spread_bps=3.0, turnover_fraction=0.0),
            0.0,
        )

    def test_custom_adverse_selection(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(adverse_selection_bps=10.0)

        self.assertEqual(cm.primary_one_way_bps(half_spread_bps=0.0), 10.0)


class TestSecondaryMarketImpact(unittest.TestCase):
    def test_impact_formula_basic(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(k=0.05)
        # k=0.05, size/adv=0.01, vol=0.30, horizon=21d
        # impact_bps = 0.05 * sqrt(0.01) * 0.30 * sqrt(21/252) * 10000
        #            = 0.05 * 0.1 * 0.30 * 0.2887 * 10000 ≈ 4.33 bps
        expected = 0.05 * math.sqrt(0.01) * 0.30 * math.sqrt(21 / 252) * 10_000
        impact = cm.secondary_market_impact_bps(
            trade_size=100.0, adv=10_000.0, annual_vol=0.30, horizon_days=21
        )

        self.assertAlmostEqual(impact, expected, places=6)

    def test_impact_zero_adv_returns_zero(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(k=0.05)

        self.assertEqual(
            cm.secondary_market_impact_bps(trade_size=100, adv=0, annual_vol=0.3, horizon_days=21),
            0.0,
        )

    def test_impact_zero_size_returns_zero(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(k=0.05)

        self.assertEqual(
            cm.secondary_market_impact_bps(
                trade_size=0, adv=10_000, annual_vol=0.3, horizon_days=21
            ),
            0.0,
        )

    def test_impact_scales_sqrt_size_over_adv(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(k=0.05)
        small = cm.secondary_market_impact_bps(trade_size=100, adv=10_000, annual_vol=0.3, horizon_days=21)
        large = cm.secondary_market_impact_bps(trade_size=400, adv=10_000, annual_vol=0.3, horizon_days=21)

        # sqrt(400/10000) / sqrt(100/10000) = sqrt(4) = 2
        self.assertAlmostEqual(large / small, 2.0, places=6)

    def test_impact_scales_linearly_with_vol(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(k=0.05)
        low = cm.secondary_market_impact_bps(trade_size=100, adv=10_000, annual_vol=0.20, horizon_days=21)
        high = cm.secondary_market_impact_bps(trade_size=100, adv=10_000, annual_vol=0.40, horizon_days=21)

        self.assertAlmostEqual(high / low, 2.0, places=6)

    def test_impact_scales_with_sqrt_horizon(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(k=0.05)
        one_day = cm.secondary_market_impact_bps(trade_size=100, adv=10_000, annual_vol=0.3, horizon_days=1)
        four_days = cm.secondary_market_impact_bps(trade_size=100, adv=10_000, annual_vol=0.3, horizon_days=4)

        self.assertAlmostEqual(four_days / one_day, 2.0, places=6)


class TestSecondaryOneWay(unittest.TestCase):
    def test_secondary_combines_all_components(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel(adverse_selection_bps=5.0, k=0.05)

        impact = cm.secondary_market_impact_bps(
            trade_size=100, adv=10_000, annual_vol=0.3, horizon_days=21
        )
        one_way = cm.secondary_one_way_bps(
            half_spread_bps=3.0,
            trade_size=100,
            adv=10_000,
            annual_vol=0.3,
            horizon_days=21,
        )

        self.assertAlmostEqual(one_way, 3.0 + 5.0 + impact, places=6)


class TestCalibrateK(unittest.TestCase):
    def test_recovers_known_k(self):
        """Synthesize observations from a known k, verify calibration inverts."""
        from alphalens.backtest.cost_model import RealisticCostModel, calibrate_k

        true_k = 0.08
        horizon = 21
        trade_sizes = [100, 200, 400, 800]
        advs = [10_000, 10_000, 20_000, 30_000]
        vols = [0.25, 0.30, 0.35, 0.40]
        # Noise-free observed impacts with the true k.
        model = RealisticCostModel(k=true_k)
        observed = [
            model.secondary_market_impact_bps(
                trade_size=s, adv=a, annual_vol=v, horizon_days=horizon
            )
            for s, a, v in zip(trade_sizes, advs, vols)
        ]

        fitted = calibrate_k(
            realized_impact_bps=observed,
            trade_sizes=trade_sizes,
            advs=advs,
            annual_vols=vols,
            horizon_days=horizon,
        )

        self.assertAlmostEqual(fitted, true_k, places=6)

    def test_calibrate_empty_raises(self):
        from alphalens.backtest.cost_model import calibrate_k

        with self.assertRaises(ValueError):
            calibrate_k(
                realized_impact_bps=[],
                trade_sizes=[],
                advs=[],
                annual_vols=[],
            )

    def test_calibrate_zero_adv_row_skipped(self):
        """ADV = 0 is meaningless; ignore such rows."""
        from alphalens.backtest.cost_model import RealisticCostModel, calibrate_k

        true_k = 0.10
        model = RealisticCostModel(k=true_k)
        sizes = [100, 200, 400]
        advs = [10_000, 0, 10_000]  # middle one invalid
        vols = [0.3, 0.3, 0.3]
        obs = [
            model.secondary_market_impact_bps(trade_size=s, adv=a, annual_vol=v, horizon_days=21)
            for s, a, v in zip(sizes, advs, vols)
        ]

        fitted = calibrate_k(
            realized_impact_bps=obs,
            trade_sizes=sizes,
            advs=advs,
            annual_vols=vols,
            horizon_days=21,
        )

        self.assertAlmostEqual(fitted, true_k, places=6)


class TestApplyAnnualDrag(unittest.TestCase):
    def test_subtracts_uniform_daily_drag(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel()
        gross = pd.Series([0.01] * 252)
        net = cm.apply_annual_drag_bps(gross, annual_drag_bps=150.0)

        total_drag = 252 * (gross.iloc[0] - net.iloc[0])
        self.assertAlmostEqual(total_drag, 0.0150, places=6)  # 150 bps

    def test_zero_drag_returns_gross(self):
        from alphalens.backtest.cost_model import RealisticCostModel

        cm = RealisticCostModel()
        gross = pd.Series([0.01, -0.005, 0.02])
        net = cm.apply_annual_drag_bps(gross, annual_drag_bps=0.0)

        pd.testing.assert_series_equal(net, gross)


class TestBackwardCompatibility(unittest.TestCase):
    def test_existing_cost_model_unchanged(self):
        """P1 must not break existing CostModel callers (Lean, themed)."""
        from alphalens.backtest.cost_model import CostModel, _PROFILE_BPS

        # Profile bps values unchanged.
        self.assertEqual(_PROFILE_BPS["aggressive"], 75.0)
        self.assertEqual(_PROFILE_BPS["moderate"], 100.0)
        self.assertEqual(_PROFILE_BPS["conservative"], 150.0)
        self.assertEqual(_PROFILE_BPS["gross"], 0.0)

        cm = CostModel.from_profile("moderate")
        self.assertEqual(cm.annual_drag_bps, 100.0)


if __name__ == "__main__":
    unittest.main()
