import unittest
from unittest.mock import patch

from alphalens.thematic.verification import mcap_filter


class TestFilterByMcap(unittest.TestCase):
    def test_keeps_tickers_within_bracket(self):
        with patch.object(
            mcap_filter,
            "fetch_mcap",
            side_effect=lambda t: {
                "QUBT": 1_780_000_000,
                "IONQ": 1_800_000_000,
            }.get(t),
        ):
            kept = mcap_filter.filter_by_mcap(
                ["QUBT", "IONQ"], min_cap=500_000_000, max_cap=10_000_000_000
            )
        self.assertEqual(set(kept), {"QUBT", "IONQ"})
        self.assertEqual(kept["QUBT"], 1_780_000_000)

    def test_drops_below_floor(self):
        with patch.object(mcap_filter, "fetch_mcap", return_value=100_000_000):
            kept = mcap_filter.filter_by_mcap(
                ["MICRO"], min_cap=500_000_000, max_cap=10_000_000_000
            )
        self.assertEqual(kept, {})

    def test_drops_above_ceiling(self):
        with patch.object(mcap_filter, "fetch_mcap", return_value=50_000_000_000):
            kept = mcap_filter.filter_by_mcap(["MEGA"], min_cap=500_000_000, max_cap=10_000_000_000)
        self.assertEqual(kept, {})

    def test_drops_when_mcap_unknown(self):
        # yfinance can return None for delisted/odd tickers — drop, don't crash.
        with patch.object(mcap_filter, "fetch_mcap", return_value=None):
            kept = mcap_filter.filter_by_mcap(["DEAD"], min_cap=500_000_000, max_cap=10_000_000_000)
        self.assertEqual(kept, {})

    def test_handles_empty_input(self):
        kept = mcap_filter.filter_by_mcap([], min_cap=500_000_000, max_cap=10_000_000_000)
        self.assertEqual(kept, {})


if __name__ == "__main__":
    unittest.main()
