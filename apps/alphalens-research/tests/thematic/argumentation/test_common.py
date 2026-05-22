import unittest

from alphalens_research.thematic.argumentation import _common


class TestPositionPctFromConf(unittest.TestCase):
    def test_maps_confidence_to_locked_position_size(self):
        # Per memo §2: 1.5% (conf 3), 2.0% (conf 4), 2.5% (conf 5); 1.0% for low.
        self.assertEqual(_common.position_pct_from_conf(5), 2.5)
        self.assertEqual(_common.position_pct_from_conf(4), 2.0)
        self.assertEqual(_common.position_pct_from_conf(3), 1.5)
        self.assertEqual(_common.position_pct_from_conf(2), 1.0)
        self.assertEqual(_common.position_pct_from_conf(1), 1.0)

    def test_handles_none_and_nan(self):
        import math

        self.assertEqual(_common.position_pct_from_conf(None), 1.0)
        self.assertEqual(_common.position_pct_from_conf(math.nan), 1.0)

    def test_handles_non_numeric_garbage(self):
        self.assertEqual(_common.position_pct_from_conf("bogus"), 1.0)


class TestCatalystFailureExitWeeks(unittest.TestCase):
    def test_catalyst_failure_exit_is_4_weeks(self):
        # Per memo §2: "8 weeks from entry (4 if catalyst-failure-triggered)"
        self.assertEqual(_common.TIME_EXIT_DEFAULT_WEEKS, 8)
        self.assertEqual(_common.TIME_EXIT_ON_CATALYST_FAILURE_WEEKS, 4)


if __name__ == "__main__":
    unittest.main()
