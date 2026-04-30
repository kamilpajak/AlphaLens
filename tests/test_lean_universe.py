import unittest
from pathlib import Path


class TestLeanConfig(unittest.TestCase):
    def test_scoring_weights_sum_to_one(self):
        from alphalens.archive.screeners.lean.config import LEAN_DEFAULTS

        total = sum(v for k, v in LEAN_DEFAULTS.items() if k.startswith("weight_"))
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_guardrails_positive(self):
        from alphalens.archive.screeners.lean.config import LEAN_DEFAULTS

        self.assertGreater(LEAN_DEFAULTS["min_price"], 0)
        self.assertGreater(LEAN_DEFAULTS["max_price"], LEAN_DEFAULTS["min_price"])
        self.assertGreater(LEAN_DEFAULTS["min_avg_dollar_volume"], 0)

    def test_top_n_is_batch_sized(self):
        """Lean emits a daily batch — roughly 20-50 names. Too few hurts diversity,
        too many defeats the point of a screener."""
        from alphalens.archive.screeners.lean.config import LEAN_DEFAULTS

        self.assertGreaterEqual(LEAN_DEFAULTS["top_n"], 10)
        self.assertLessEqual(LEAN_DEFAULTS["top_n"], 50)

    def test_paths_are_absolute(self):
        from alphalens.archive.screeners.lean.config import (
            DATA_DIR,
            LEAN_PROJECT_DIR,
            RESULTS_DIR,
            UNIVERSE_PATH,
        )

        for p in (UNIVERSE_PATH, LEAN_PROJECT_DIR, DATA_DIR, RESULTS_DIR):
            self.assertTrue(p.is_absolute(), f"{p} must be absolute")

    def test_universe_yaml_ships_with_package(self):
        from alphalens.archive.screeners.lean.config import UNIVERSE_PATH

        self.assertTrue(UNIVERSE_PATH.exists())
        self.assertTrue(str(UNIVERSE_PATH).endswith(".yaml"))


class TestUniverseLoader(unittest.TestCase):
    def test_loads_multiple_sectors(self):
        from alphalens.archive.screeners.lean.universe import load_universe

        sectors = load_universe()
        self.assertGreaterEqual(len(sectors), 8, "need broad sector coverage")
        for core in ("technology", "healthcare", "financials"):
            self.assertIn(core, sectors)

    def test_each_sector_is_list_of_strings(self):
        from alphalens.archive.screeners.lean.universe import load_universe

        for name, tickers in load_universe().items():
            self.assertIsInstance(tickers, list, f"{name} must be list")
            self.assertTrue(all(isinstance(t, str) for t in tickers))
            self.assertGreaterEqual(len(tickers), 15, f"{name} too thin")

    def test_no_duplicates_within_sector(self):
        from alphalens.archive.screeners.lean.universe import load_universe

        for name, tickers in load_universe().items():
            self.assertEqual(len(tickers), len(set(tickers)), f"{name} has dupes")

    def test_tickers_are_uppercase(self):
        from alphalens.archive.screeners.lean.universe import load_universe

        for name, tickers in load_universe().items():
            for t in tickers:
                self.assertEqual(t, t.upper(), f"{t} in {name} not uppercase")

    def test_total_universe_size_meets_mvp_target(self):
        """MVP1 target is ~500 tickers minimum; expand toward 1000 over time."""
        from alphalens.archive.screeners.lean.universe import all_tickers

        self.assertGreaterEqual(len(all_tickers()), 300)


class TestFlattenUniverse(unittest.TestCase):
    def test_dedup_across_sectors(self):
        from alphalens.archive.screeners.lean.universe import flatten_universe

        flat = flatten_universe({"tech": ["AAA", "BBB"], "health": ["BBB", "CCC"]})
        self.assertEqual(sorted(flat.keys()), ["AAA", "BBB", "CCC"])
        self.assertEqual(flat["BBB"], ["tech", "health"])

    def test_preserves_sector_insertion_order(self):
        from alphalens.archive.screeners.lean.universe import flatten_universe

        flat = flatten_universe({"a": ["X"], "b": ["X"], "c": ["X"]})
        self.assertEqual(flat["X"], ["a", "b", "c"])

    def test_empty_sector_ignored(self):
        from alphalens.archive.screeners.lean.universe import flatten_universe

        self.assertEqual(flatten_universe({"a": [], "b": ["X"]}), {"X": ["b"]})


class TestAllTickers(unittest.TestCase):
    def test_returns_sorted_deduped(self):
        from alphalens.archive.screeners.lean.universe import all_tickers

        tickers = all_tickers()
        self.assertEqual(tickers, sorted(set(tickers)))

    def test_uses_custom_path(self):
        from alphalens.archive.screeners.lean.universe import all_tickers

        tmp = Path("/tmp/test_lean_universe.yaml")
        tmp.write_text("foo:\n  - BBB\n  - AAA\n")
        try:
            self.assertEqual(all_tickers(tmp), ["AAA", "BBB"])
        finally:
            tmp.unlink()


class TestDelistedLoader(unittest.TestCase):
    def test_load_from_custom_path(self):
        from alphalens.archive.screeners.lean.universe import load_delisted

        tmp = Path("/tmp/test_lean_delisted.yaml")
        tmp.write_text(
            "delisted:\n"
            "  - ticker: ABC\n"
            "    delisted: 2024-05-10\n"
            "    name: Alpha Bravo Corp\n"
            "  - ticker: xyz\n"  # lowercase tolerance
            "    delisted: 2025-12-01\n"
            "    name: XYZ Inc\n"
        )
        try:
            records = load_delisted(tmp)
        finally:
            tmp.unlink()

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].ticker, "ABC")
        self.assertEqual(records[1].ticker, "XYZ")  # normalised

    def test_missing_file_returns_empty(self):
        from alphalens.archive.screeners.lean.universe import load_delisted

        self.assertEqual(load_delisted(Path("/tmp/nope_delisted_lean.yaml")), [])

    def test_skip_malformed_entries(self):
        from alphalens.archive.screeners.lean.universe import load_delisted

        tmp = Path("/tmp/test_lean_delisted_bad.yaml")
        tmp.write_text(
            "delisted:\n"
            "  - ticker: GOOD\n"
            "    delisted: 2024-05-10\n"
            "    name: good\n"
            "  - ticker: NOBAD\n"
            "    name: missing delisted date\n"
            "  - delisted: 2024-06-01\n"
            "    name: missing ticker\n"
        )
        try:
            records = load_delisted(tmp)
        finally:
            tmp.unlink()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].ticker, "GOOD")

    def test_production_file_loads(self):
        """Smoke test: the delisted_universe.yaml shipped with the repo parses cleanly."""
        from alphalens.archive.screeners.lean.config import DELISTED_UNIVERSE_PATH
        from alphalens.archive.screeners.lean.universe import load_delisted

        if not DELISTED_UNIVERSE_PATH.exists():
            self.skipTest(f"delisted_universe.yaml not present at {DELISTED_UNIVERSE_PATH}")

        records = load_delisted()
        self.assertGreater(len(records), 100, "expected 1000+ delisted CS in 2-year window")

    def test_filter_on_or_after(self):
        from datetime import date

        from alphalens.archive.screeners.lean.universe import delisted_tickers_on_or_after

        tmp = Path("/tmp/test_lean_delisted_filter.yaml")
        tmp.write_text(
            "delisted:\n"
            "  - ticker: OLD\n"
            "    delisted: 2024-01-01\n"
            "    name: old\n"
            "  - ticker: MID\n"
            "    delisted: 2024-06-01\n"
            "    name: mid\n"
            "  - ticker: NEW\n"
            "    delisted: 2026-01-01\n"
            "    name: new\n"
        )
        try:
            result = list(delisted_tickers_on_or_after(date(2024, 4, 19), tmp))
        finally:
            tmp.unlink()

        self.assertEqual(result, ["MID", "NEW"])


class TestBenchmarksConfig(unittest.TestCase):
    def test_benchmarks_constant_exists(self):
        from alphalens.archive.screeners.lean.config import BENCHMARKS

        self.assertIsInstance(BENCHMARKS, tuple)
        self.assertGreater(len(BENCHMARKS), 0)
        for b in BENCHMARKS:
            self.assertEqual(b, b.upper(), f"benchmark {b} must be uppercase")

    def test_spy_in_benchmarks(self):
        """SPY is the regime-classification reference — cannot be missing."""
        from alphalens.archive.screeners.lean.config import BENCHMARKS

        self.assertIn("SPY", BENCHMARKS)

    def test_benchmarks_do_not_leak_into_screener_universe(self):
        """BENCHMARKS must stay out of universe.yaml — they are not screening targets,
        they exist so the backtest can reference SPY/QQQ/IWM for regime + FF3 work."""
        from alphalens.archive.screeners.lean.config import BENCHMARKS
        from alphalens.archive.screeners.lean.universe import all_tickers

        screener_universe = set(all_tickers())
        for b in BENCHMARKS:
            self.assertNotIn(b, screener_universe, f"{b} leaked into screener universe.yaml")


class TestLoadUniverseCustomPath(unittest.TestCase):
    def test_custom_path_loads(self):
        from alphalens.archive.screeners.lean.universe import load_universe

        tmp = Path("/tmp/test_lean_universe2.yaml")
        tmp.write_text("sector_a:\n  - AAA\n  - BBB\n")
        try:
            self.assertEqual(load_universe(tmp), {"sector_a": ["AAA", "BBB"]})
        finally:
            tmp.unlink()

    def test_missing_file_raises(self):
        from alphalens.archive.screeners.lean.universe import load_universe

        with self.assertRaises(FileNotFoundError):
            load_universe(Path("/tmp/definitely_not_here_lean.yaml"))


if __name__ == "__main__":
    unittest.main()
