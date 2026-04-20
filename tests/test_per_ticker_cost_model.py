"""TDD for PerTickerCostModel — spread + market impact + commission."""

import math
import unittest


class TestPerTickerCostModelSpreadComponent(unittest.TestCase):
    def test_zero_impact_zero_commission_yields_half_spread_cost(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.0, impact_kappa=0.0, min_spread_bps=0.0
        )
        cost = model.cost_for_trade(
            trade_notional=10_000.0,
            spread_bps=50.0,
            volatility=0.02,
            adv_dollar=10_000_000.0,
            share_price=100.0,
        )
        # 50 bps = 0.5%. Half-spread cost = 0.5% / 2 × 10k = $25
        self.assertAlmostEqual(cost, 25.0, places=6)

    def test_min_spread_floor_enforced(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.0, impact_kappa=0.0, min_spread_bps=10.0
        )
        cost = model.cost_for_trade(
            trade_notional=10_000.0,
            spread_bps=1.0,
            volatility=0.02,
            adv_dollar=10_000_000.0,
            share_price=100.0,
        )
        # Floor of 10 bps → 10/10000/2 × 10k = $5
        self.assertAlmostEqual(cost, 5.0, places=6)


class TestPerTickerCostModelImpactComponent(unittest.TestCase):
    def test_impact_scales_with_sqrt_notional_over_adv(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.0, impact_kappa=0.10, min_spread_bps=0.0
        )
        # Identical spread so we isolate the impact component.
        cost_small = model.cost_for_trade(
            trade_notional=10_000.0,
            spread_bps=0.0,
            volatility=0.02,
            adv_dollar=10_000_000.0,
            share_price=100.0,
        )
        cost_big = model.cost_for_trade(
            trade_notional=40_000.0,
            spread_bps=0.0,
            volatility=0.02,
            adv_dollar=10_000_000.0,
            share_price=100.0,
        )
        # notional ×4 ⇒ sqrt(notional)×2 ⇒ impact_cost (as $) ×8 (notional itself also ×4)
        self.assertAlmostEqual(cost_big / cost_small, 8.0, places=6)

    def test_impact_formula_matches_expected(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.0, impact_kappa=0.10, min_spread_bps=0.0
        )
        notional = 10_000.0
        vol = 0.02
        adv = 10_000_000.0
        expected = 0.10 * vol * math.sqrt(notional / adv) * notional
        cost = model.cost_for_trade(
            trade_notional=notional,
            spread_bps=0.0,
            volatility=vol,
            adv_dollar=adv,
            share_price=100.0,
        )
        self.assertAlmostEqual(cost, expected, places=6)

    def test_zero_volatility_yields_zero_impact(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.0, impact_kappa=0.10, min_spread_bps=0.0
        )
        cost = model.cost_for_trade(
            trade_notional=10_000.0,
            spread_bps=0.0,
            volatility=0.0,
            adv_dollar=10_000_000.0,
            share_price=100.0,
        )
        self.assertEqual(cost, 0.0)


class TestPerTickerCostModelCommission(unittest.TestCase):
    def test_commission_equals_per_share_times_shares(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.005, impact_kappa=0.0, min_spread_bps=0.0
        )
        # 10_000 notional / 100 price = 100 shares; commission = 0.005 × 100 = $0.50
        cost = model.cost_for_trade(
            trade_notional=10_000.0,
            spread_bps=0.0,
            volatility=0.02,
            adv_dollar=10_000_000.0,
            share_price=100.0,
        )
        self.assertAlmostEqual(cost, 0.50, places=6)


class TestPerTickerCostModelCombined(unittest.TestCase):
    def test_all_three_components_sum(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.005, impact_kappa=0.10, min_spread_bps=5.0
        )
        notional = 10_000.0
        spread_bps = 50.0
        vol = 0.02
        adv = 10_000_000.0
        price = 100.0

        # Expected components:
        spread_part = (max(spread_bps, 5.0) / 10_000.0) * notional / 2.0
        impact_part = 0.10 * vol * math.sqrt(notional / adv) * notional
        commission_part = 0.005 * (notional / price)
        expected = spread_part + impact_part + commission_part

        got = model.cost_for_trade(
            trade_notional=notional,
            spread_bps=spread_bps,
            volatility=vol,
            adv_dollar=adv,
            share_price=price,
        )
        self.assertAlmostEqual(got, expected, places=6)


class TestPerTickerCostModelEdgeCases(unittest.TestCase):
    def test_zero_adv_does_not_divide_by_zero(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.0, impact_kappa=0.10, min_spread_bps=0.0
        )
        cost = model.cost_for_trade(
            trade_notional=10_000.0,
            spread_bps=50.0,
            volatility=0.02,
            adv_dollar=0.0,  # degenerate
            share_price=100.0,
        )
        # Should not raise; impact falls back to large but finite value.
        self.assertTrue(math.isfinite(cost))

    def test_zero_price_does_not_divide_by_zero(self):
        from alphalens.backtest.cost_model import PerTickerCostModel

        model = PerTickerCostModel(
            commission_per_share=0.005, impact_kappa=0.0, min_spread_bps=0.0
        )
        cost = model.cost_for_trade(
            trade_notional=10_000.0,
            spread_bps=0.0,
            volatility=0.02,
            adv_dollar=10_000_000.0,
            share_price=0.0,  # degenerate
        )
        self.assertTrue(math.isfinite(cost))


if __name__ == "__main__":
    unittest.main()
