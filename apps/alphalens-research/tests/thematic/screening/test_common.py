import unittest

from alphalens_pipeline.thematic.screening import _common


class TestPercentileRank(unittest.TestCase):
    def test_empty_peers_returns_neutral_50(self):
        # Empty cohort → "no information" → midpoint.
        self.assertEqual(_common.percentile_rank(100.0, []), 50.0)

    def test_top_of_cohort_is_100(self):
        self.assertAlmostEqual(_common.percentile_rank(100.0, [10.0, 20.0]), 100.0)

    def test_bottom_of_cohort_includes_self(self):
        # Candidate value not in peers -> auto-included; candidate at bottom -> low.
        result = _common.percentile_rank(0.0, [10.0, 20.0])
        # Cohort = [10, 20, 0]; le_count for 0 = 1; 1/3 ≈ 33.3.
        self.assertAlmostEqual(result, 100.0 / 3, places=2)

    def test_when_value_in_peers_no_duplicate_inclusion(self):
        # Avoid double-counting if candidate already in peer list.
        result = _common.percentile_rank(10.0, [10.0, 20.0])
        # Cohort = [10, 20]; le_count for 10 = 1; 1/2 = 50.
        self.assertEqual(result, 50.0)


class TestClampTax(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_common.clamp_tax(None))

    def test_in_range_returns_value(self):
        self.assertEqual(_common.clamp_tax(0.21), 0.21)

    def test_below_floor_clamps_to_zero(self):
        self.assertEqual(_common.clamp_tax(-0.1), 0.0)

    def test_above_ceiling_clamps_to_035(self):
        self.assertEqual(_common.clamp_tax(0.45), 0.35)


if __name__ == "__main__":
    unittest.main()
