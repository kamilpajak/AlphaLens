import unittest
from pathlib import Path


class TestMomentumConfig(unittest.TestCase):
    def test_metric_weights_sum_to_one(self):
        from alphalens.screeners.themed.config import THEMED_DEFAULTS

        total = sum(v for k, v in THEMED_DEFAULTS.items() if k.startswith("weight_"))
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_guardrails_positive(self):
        from alphalens.screeners.themed.config import THEMED_DEFAULTS

        for key in ["min_market_cap", "min_avg_volume", "min_price"]:
            self.assertGreater(THEMED_DEFAULTS[key], 0, f"{key} must be positive")

    def test_top_n_is_selective(self):
        from alphalens.screeners.themed.config import THEMED_DEFAULTS

        self.assertLessEqual(
            THEMED_DEFAULTS["top_n"],
            10,
            "momentum output should be selective (<=10)",
        )

    def test_universe_path_points_to_yaml(self):
        from alphalens.screeners.themed.config import UNIVERSE_PATH

        self.assertTrue(str(UNIVERSE_PATH).endswith(".yaml"))
        self.assertTrue(UNIVERSE_PATH.exists(), "universe.yaml must ship with the package")


class TestUniverseLoader(unittest.TestCase):
    def test_loads_all_themes(self):
        from alphalens.screeners.themed.universe import load_universe

        themes = load_universe()
        self.assertIn("quantum", themes)
        self.assertIn("ai", themes)
        self.assertIn("biotech", themes)

    def test_each_theme_is_list_of_strings(self):
        from alphalens.screeners.themed.universe import load_universe

        themes = load_universe()
        for name, tickers in themes.items():
            self.assertIsInstance(tickers, list, f"{name} must be list")
            self.assertTrue(
                all(isinstance(t, str) for t in tickers),
                f"{name} must contain only strings",
            )
            self.assertGreater(len(tickers), 20, f"{name} should have >20 tickers")

    def test_no_duplicates_within_theme(self):
        from alphalens.screeners.themed.universe import load_universe

        for name, tickers in load_universe().items():
            self.assertEqual(
                len(tickers),
                len(set(tickers)),
                f"{name} has duplicate tickers",
            )

    def test_tickers_are_uppercase(self):
        from alphalens.screeners.themed.universe import load_universe

        for name, tickers in load_universe().items():
            for t in tickers:
                self.assertEqual(t, t.upper(), f"{t} in {name} not uppercase")

    def test_contains_qubt_sanity_anchor(self):
        """QUBT is the canonical sanity-check ticker — if it's not in the universe,
        the screener can't surface QUBT-style setups at all."""
        from alphalens.screeners.themed.universe import load_universe

        all_tickers = {t for tickers in load_universe().values() for t in tickers}
        self.assertIn("QUBT", all_tickers)


class TestFlattenUniverse(unittest.TestCase):
    def test_dedup_across_themes(self):
        from alphalens.screeners.themed.universe import flatten_universe

        themes = {"a": ["AAA", "BBB"], "b": ["BBB", "CCC"]}
        flat = flatten_universe(themes)
        self.assertEqual(sorted(flat.keys()), ["AAA", "BBB", "CCC"])
        self.assertEqual(flat["BBB"], ["a", "b"])
        self.assertEqual(flat["AAA"], ["a"])

    def test_preserves_insertion_order_of_themes(self):
        from alphalens.screeners.themed.universe import flatten_universe

        themes = {"quantum": ["X"], "ai": ["X"]}
        flat = flatten_universe(themes)
        self.assertEqual(flat["X"], ["quantum", "ai"])

    def test_empty_theme_ignored(self):
        from alphalens.screeners.themed.universe import flatten_universe

        flat = flatten_universe({"a": [], "b": ["X"]})
        self.assertEqual(flat, {"X": ["b"]})


class TestLoadUniverseFromCustomPath(unittest.TestCase):
    def test_load_custom_path(self):
        from alphalens.screeners.themed.universe import load_universe

        tmp = Path("/tmp/test_momentum_universe.yaml")
        tmp.write_text("foo:\n  - AAA\n  - BBB\n")
        try:
            themes = load_universe(tmp)
            self.assertEqual(themes, {"foo": ["AAA", "BBB"]})
        finally:
            tmp.unlink()

    def test_missing_file_raises(self):
        from alphalens.screeners.themed.universe import load_universe

        with self.assertRaises(FileNotFoundError):
            load_universe(Path("/tmp/does_not_exist_momentum.yaml"))


if __name__ == "__main__":
    unittest.main()
