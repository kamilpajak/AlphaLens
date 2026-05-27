import unittest

from alphalens_pipeline.thematic.trade_setup import sizing


class TestEqualRiskAllocations(unittest.TestCase):
    def test_allocations_sum_to_100(self):
        allocs = sizing.equal_risk_allocations([414.0, 407.0, 389.0], stop=374.0)
        self.assertAlmostEqual(sum(allocs), 100.0, places=6)

    def test_loads_nearest_stop_tier_most(self):
        # Deepest tier (closest to stop = smallest risk/share) gets the most.
        allocs = sizing.equal_risk_allocations([414.0, 407.0, 389.0], stop=374.0)
        self.assertEqual(allocs.index(max(allocs)), 2)  # the 389 tier

    def test_respects_custom_weights(self):
        equal = sizing.equal_risk_allocations([414.0, 407.0, 389.0], stop=374.0)
        biased = sizing.equal_risk_allocations(
            [414.0, 407.0, 389.0], stop=374.0, weights=[0.6, 0.3, 0.1]
        )
        # Biasing weight toward the shallow tier raises its allocation.
        self.assertGreater(biased[0], equal[0])

    def test_raises_when_entry_at_or_below_stop(self):
        with self.assertRaises(ValueError):
            sizing.equal_risk_allocations([400.0, 374.0], stop=374.0)


class TestSuggestedSizePct(unittest.TestCase):
    def test_scales_with_risk_budget(self):
        # Lift the exposure cap so the linear scaling is observable.
        size_1 = sizing.suggested_size_pct(
            [414.0, 407.0, 389.0], 374.0, 1.0, max_exposure_pct=100.0
        )
        size_2 = sizing.suggested_size_pct(
            [414.0, 407.0, 389.0], 374.0, 2.0, max_exposure_pct=100.0
        )
        self.assertAlmostEqual(size_2, 2 * size_1, places=6)

    def test_caps_at_max_exposure(self):
        # Tiny risk-per-share -> huge multiplier -> must clamp at the cap.
        size = sizing.suggested_size_pct([100.0], 99.9, 1.0, max_exposure_pct=25.0)
        self.assertLessEqual(size, 25.0)


class TestBlendedEntry(unittest.TestCase):
    def test_allocation_weighted_average(self):
        blended = sizing.blended_entry([410.0, 400.0], [25.0, 75.0])
        self.assertAlmostEqual(blended, 0.25 * 410.0 + 0.75 * 400.0, places=6)


if __name__ == "__main__":
    unittest.main()
