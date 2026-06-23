# apps/alphalens-research/tests/feedback/test_execution_cost.py
import unittest

from alphalens_pipeline.feedback import execution_cost as ec


class TestImpactBps(unittest.TestCase):
    def test_buckets_and_default(self):
        self.assertAlmostEqual(ec.impact_bps_for_mcap(500e9), 2.0)  # mega
        self.assertAlmostEqual(ec.impact_bps_for_mcap(20e9), 5.0)  # mid
        self.assertAlmostEqual(ec.impact_bps_for_mcap(3e9), 12.0)  # small
        self.assertAlmostEqual(ec.impact_bps_for_mcap(3e8), 25.0)  # micro
        for bad in (None, float("nan"), 0.0, -1.0):
            self.assertAlmostEqual(
                ec.impact_bps_for_mcap(bad), 12.0
            )  # conservative default, not cheapest


class TestHalfSpreadBps(unittest.TestCase):
    def test_proxy_and_fallback(self):
        # mid=100, (h-l)=2 -> half-spread = 0.5*2/100 = 1% = 100 bps
        self.assertAlmostEqual(ec.half_spread_bps_from_bar({"h": 101.0, "l": 99.0}), 100.0)
        # tight: mid=100, (h-l)=0.2 -> 10 bps
        self.assertAlmostEqual(ec.half_spread_bps_from_bar({"h": 100.1, "l": 99.9}), 10.0)
        for bad in (None, {}, {"h": 1.0}, {"h": 1.0, "l": 2.0}, {"h": 0.0, "l": 0.0}):
            self.assertAlmostEqual(ec.half_spread_bps_from_bar(bad), 25.0)


class TestArmHaircut(unittest.TestCase):
    def test_resting_arms_zero(self):
        for arm in ec.RESTING_LIMIT_ARMS:
            self.assertEqual(
                ec.arm_haircut_bps(arm, market_cap=3e8, first_rth_bar={"h": 101.0, "l": 99.0}), 0.0
            )

    def test_always_fill_sums_spread_and_impact(self):
        bps = ec.arm_haircut_bps(
            "market_at_arrival", market_cap=3e9, first_rth_bar={"h": 101.0, "l": 99.0}
        )
        self.assertAlmostEqual(bps, 100.0 + 12.0)

    def test_unknown_arm_raises(self):
        with self.assertRaises(ValueError):
            ec.arm_haircut_bps("nope", market_cap=1e9, first_rth_bar=None)


class TestApplyHaircut(unittest.TestCase):
    def test_none_passthrough(self):
        self.assertIsNone(
            ec.apply_haircut_to_excess(
                None, arm="market_at_arrival", market_cap=1e9, first_rth_bar=None
            )
        )

    def test_resting_unchanged(self):
        self.assertAlmostEqual(
            ec.apply_haircut_to_excess(
                0.05, arm="baseline", market_cap=3e8, first_rth_bar={"h": 9.0, "l": 1.0}
            ),
            0.05,
        )

    def test_always_fill_strictly_lower(self):
        out = ec.apply_haircut_to_excess(
            0.05, arm="vwap_arrival", market_cap=3e9, first_rth_bar={"h": 101.0, "l": 99.0}
        )
        self.assertLess(out, 0.05)
        self.assertAlmostEqual(out, 0.05 - (100.0 + 12.0) / 10_000)

    def test_negative_excess_still_charged(self):
        out = ec.apply_haircut_to_excess(
            -0.02, arm="market_at_arrival", market_cap=3e9, first_rth_bar={"h": 101.0, "l": 99.0}
        )
        self.assertLess(out, -0.02)


if __name__ == "__main__":
    unittest.main()
