import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


class TestFetchMcapErrorPaths(unittest.TestCase):
    def test_returns_none_when_yfinance_returns_none_mcap(self):
        fake_fast_info = MagicMock(spec=["market_cap"])
        fake_fast_info.market_cap = None
        fake_ticker = SimpleNamespace(fast_info=fake_fast_info)
        with patch("yfinance.Ticker", return_value=fake_ticker):
            self.assertIsNone(mcap_filter.fetch_mcap("UNKNOWN"))

    def test_returns_none_on_yfinance_exception(self):
        # Network errors, delisted tickers, parse failures — all collapsed
        # to None so the caller can drop the candidate cleanly.
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            self.assertIsNone(mcap_filter.fetch_mcap("DEAD"))


class TestFetchMcapYfinanceContract(unittest.TestCase):
    """Pin the yfinance fast_info attribute-access pattern.

    yfinance's FastInfo exposes `market_cap` as an attribute, NOT as a
    `.get("market_cap")` dict key (which silently returns None). If anyone
    clones fetch_mcap to add fetch_volume / fetch_pe / similar helpers and
    regresses to `.get(...)`, this test fails.
    """

    def test_uses_attribute_access_not_dict_get(self):
        # FastInfo stand-in: attribute access returns the real value;
        # .get("market_cap") returns None (regression target). If the
        # implementation uses .get(), it gets None -> fetch_mcap returns
        # None -> assertion fails.
        fake_fast_info = MagicMock(spec=["market_cap"])
        fake_fast_info.market_cap = 1_780_000_000.0
        fake_fast_info.get = MagicMock(return_value=None)
        fake_ticker = SimpleNamespace(fast_info=fake_fast_info)

        with patch("yfinance.Ticker", return_value=fake_ticker):
            mc = mcap_filter.fetch_mcap("QUBT")
        self.assertEqual(mc, 1_780_000_000.0)
        # Sanity: the regression path (dict-get) was NOT used.
        fake_fast_info.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
