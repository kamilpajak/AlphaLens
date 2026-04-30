import unittest


class TestScreenerRegistry(unittest.TestCase):
    def test_registry_exposes_all_screeners(self):
        from alphalens.archive.screeners.insider.pipeline import InsiderPipeline
        from alphalens.archive.screeners.lean.pipeline import LeanScreenerPipeline
        from alphalens.archive.screeners.themed.pipeline import ThemedPipeline
        from alphalens.registry import SCREENERS
        from alphalens.screeners.prescreener.integration import PrescreenerPipeline

        self.assertIs(SCREENERS["themed"], ThemedPipeline)
        self.assertIs(SCREENERS["prescreener"], PrescreenerPipeline)
        self.assertIs(SCREENERS["lean"], LeanScreenerPipeline)
        self.assertIs(SCREENERS["insider"], InsiderPipeline)

    def test_source_priority_mapping(self):
        from alphalens.registry import SOURCE_PRIORITY

        self.assertEqual(SOURCE_PRIORITY["watchdog_sec"], 0)
        self.assertEqual(SOURCE_PRIORITY["momentum"], 10)
        self.assertEqual(SOURCE_PRIORITY["early-stage"], 10)
        self.assertEqual(SOURCE_PRIORITY["insider"], 12)
        self.assertEqual(SOURCE_PRIORITY["lean"], 15)
        self.assertEqual(SOURCE_PRIORITY["prescreener"], 20)

    def test_themed_pipeline_source_names_are_registered(self):
        """Themed pipeline can emit candidates tagged `momentum` or `early-stage`
        depending on injected scorer. Both source names must exist in
        SOURCE_PRIORITY so the queue can resolve priority on claim."""
        from alphalens.registry import SOURCE_PRIORITY

        self.assertIn("momentum", SOURCE_PRIORITY)
        self.assertIn("early-stage", SOURCE_PRIORITY)

    def test_non_themed_screener_keys_match_source_names(self):
        """For single-scorer screeners (lean, prescreener) the pipeline key
        equals the source_name. Themed is the exception — it's decoupled."""
        from alphalens.registry import SCREENERS, SOURCE_PRIORITY

        for key in SCREENERS:
            if key == "themed":
                continue  # decoupled — tested separately
            self.assertIn(key, SOURCE_PRIORITY, f"{key} missing priority mapping")

    def test_lean_priority_between_momentum_and_prescreener(self):
        """Lean is daily+quant, so it deserves to beat prescreener but not
        momentum (which runs on a tighter universe signal)."""
        from alphalens.registry import SOURCE_PRIORITY

        self.assertLess(SOURCE_PRIORITY["momentum"], SOURCE_PRIORITY["lean"])
        self.assertLess(SOURCE_PRIORITY["lean"], SOURCE_PRIORITY["prescreener"])


if __name__ == "__main__":
    unittest.main()
