import unittest
from itertools import pairwise

from alphalens_pipeline.thematic.trade_setup import levels


class TestDetectSwingPoints(unittest.TestCase):
    def test_detects_alternating_pivots_above_threshold(self):
        # Triangle wave: up 100->120, down ->100, up ->120. threshold=10 (< the
        # 20-point legs) must yield alternating H/L pivots at the turning points.
        up = list(range(100, 121))
        down = list(range(119, 99, -1))
        up2 = list(range(100, 121))
        closes = up + down + up2
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]

        pivots = levels.detect_swing_points(highs, lows, threshold=10.0)

        kinds = [k for _, _, k in pivots]
        self.assertGreaterEqual(len(pivots), 2)
        # Pivots must alternate H/L (no two same-kind in a row).
        for a, b in pairwise(kinds):
            self.assertNotEqual(a, b)

    def test_ignores_noise_below_threshold(self):
        # Small +-1 oscillation with a huge threshold yields no confirmed pivot.
        closes = [100 + (i % 2) for i in range(50)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        self.assertEqual(levels.detect_swing_points(highs, lows, threshold=20.0), [])

    def test_empty_and_degenerate_threshold(self):
        self.assertEqual(levels.detect_swing_points([], [], threshold=5.0), [])
        self.assertEqual(levels.detect_swing_points([1.0], [1.0], threshold=5.0), [])
        self.assertEqual(levels.detect_swing_points([1.0, 2.0], [1.0, 2.0], threshold=0.0), [])


class TestClusterPrices(unittest.TestCase):
    def test_merges_within_radius(self):
        # 100, 100.4 collapse (radius 0.5); 110 stands alone.
        zones = levels.cluster_prices([100.0, 100.4, 110.0], radius=0.5)
        self.assertEqual(len(zones), 2)
        self.assertAlmostEqual(zones[0], 100.2, places=3)
        self.assertAlmostEqual(zones[1], 110.0, places=3)

    def test_empty(self):
        self.assertEqual(levels.cluster_prices([], radius=1.0), [])


class TestSupportResistance(unittest.TestCase):
    def test_splits_and_orders_relative_to_close(self):
        pivots = [
            (1, 90.0, "L"),
            (2, 110.0, "H"),
            (3, 95.0, "L"),
            (4, 115.0, "H"),
        ]
        supports, resistances = levels.support_resistance(100.0, pivots, atr=2.0)
        # supports < close, nearest-first (descending)
        self.assertTrue(all(s < 100.0 for s in supports))
        self.assertEqual(supports, sorted(supports, reverse=True))
        # resistances > close, nearest-first (ascending)
        self.assertTrue(all(r > 100.0 for r in resistances))
        self.assertEqual(resistances, sorted(resistances))


if __name__ == "__main__":
    unittest.main()
