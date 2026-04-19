import unittest


class TestScreenerRegistry(unittest.TestCase):
    def test_registry_exposes_all_screeners(self):
        from alphalens.lean_screener.pipeline import LeanScreenerPipeline
        from alphalens.momentum_screener.pipeline import MomentumPipeline
        from alphalens.prescreener.integration import PrescreenerPipeline
        from alphalens.registry import SCREENERS

        self.assertIs(SCREENERS["momentum"], MomentumPipeline)
        self.assertIs(SCREENERS["prescreener"], PrescreenerPipeline)
        self.assertIs(SCREENERS["lean"], LeanScreenerPipeline)

    def test_source_priority_mapping(self):
        from alphalens.registry import SOURCE_PRIORITY

        self.assertEqual(SOURCE_PRIORITY["watchdog_sec"], 0)
        self.assertEqual(SOURCE_PRIORITY["momentum"], 10)
        self.assertEqual(SOURCE_PRIORITY["lean"], 15)
        self.assertEqual(SOURCE_PRIORITY["prescreener"], 20)

    def test_all_registered_sources_have_priority(self):
        from alphalens.registry import SCREENERS, SOURCE_PRIORITY

        for source in SCREENERS:
            self.assertIn(source, SOURCE_PRIORITY, f"{source} missing priority mapping")

    def test_lean_priority_between_momentum_and_prescreener(self):
        """Lean is daily+quant, so it deserves to beat prescreener but not
        momentum (which runs on a tighter universe signal)."""
        from alphalens.registry import SOURCE_PRIORITY

        self.assertLess(SOURCE_PRIORITY["momentum"], SOURCE_PRIORITY["lean"])
        self.assertLess(SOURCE_PRIORITY["lean"], SOURCE_PRIORITY["prescreener"])


if __name__ == "__main__":
    unittest.main()
