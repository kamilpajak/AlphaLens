import unittest

from alphalens.thematic.config.universe import is_in_universe, load_input_universe


class TestInputUniverse(unittest.TestCase):
    def test_loads_at_least_120_tickers(self):
        self.assertGreaterEqual(len(load_input_universe()), 120)

    def test_dedupes_overlap_between_sp100_and_sector_leaders(self):
        u = load_input_universe()
        self.assertIn("GD", u)
        self.assertEqual(sum(1 for t in u if t == "GD"), 1)

    def test_known_mega_caps_present(self):
        for t in ["NVDA", "AAPL", "MSFT", "GOOGL", "META", "TSLA"]:
            self.assertIn(t, load_input_universe(), f"missing mega cap {t}")

    def test_known_sector_leaders_present(self):
        for t in ["ARM", "TSM", "IONQ", "REGN", "ENPH", "FCX", "NOC"]:
            self.assertIn(t, load_input_universe(), f"missing sector leader {t}")

    def test_is_in_universe_case_insensitive(self):
        self.assertTrue(is_in_universe("nvda"))
        self.assertTrue(is_in_universe("NVDA"))
        self.assertFalse(is_in_universe("BOGUS"))
