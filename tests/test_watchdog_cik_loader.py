import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

FAKE_TICKERS_JSON = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
}


def _make_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    resp.raise_for_status = MagicMock()
    return resp


class TestCIKLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "company_tickers.json"
        self.user_agent = "AlphaLens Test test@example.com"

    def tearDown(self):
        self.tmp.cleanup()

    @patch("alphalens.watchdog.sources.cik_loader.requests.get")
    def test_download_when_cache_missing(self, mock_get):
        from alphalens.watchdog.sources.cik_loader import CIKLoader

        mock_get.return_value = _make_response(FAKE_TICKERS_JSON)
        loader = CIKLoader(user_agent=self.user_agent, cache_path=self.cache_path)
        loader.load()

        self.assertTrue(self.cache_path.exists())
        self.assertEqual(mock_get.call_count, 1)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["headers"]["User-Agent"], self.user_agent)

    @patch("alphalens.watchdog.sources.cik_loader.requests.get")
    def test_use_cache_when_fresh(self, mock_get):
        from alphalens.watchdog.sources.cik_loader import CIKLoader

        self.cache_path.write_text(json.dumps(FAKE_TICKERS_JSON))

        loader = CIKLoader(user_agent=self.user_agent, cache_path=self.cache_path)
        loader.load()
        self.assertEqual(mock_get.call_count, 0)

    @patch("alphalens.watchdog.sources.cik_loader.requests.get")
    def test_refresh_after_cache_expiry(self, mock_get):
        from alphalens.watchdog.sources.cik_loader import CIKLoader

        mock_get.return_value = _make_response(FAKE_TICKERS_JSON)
        self.cache_path.write_text(json.dumps(FAKE_TICKERS_JSON))
        eight_days_ago = time.time() - (8 * 86400)
        import os

        os.utime(self.cache_path, (eight_days_ago, eight_days_ago))

        loader = CIKLoader(
            user_agent=self.user_agent,
            cache_path=self.cache_path,
            ttl_seconds=7 * 86400,
        )
        loader.load()
        self.assertEqual(mock_get.call_count, 1)

    @patch("alphalens.watchdog.sources.cik_loader.requests.get")
    def test_get_cik_returns_10_digit_padded(self, mock_get):
        from alphalens.watchdog.sources.cik_loader import CIKLoader

        mock_get.return_value = _make_response(FAKE_TICKERS_JSON)
        loader = CIKLoader(user_agent=self.user_agent, cache_path=self.cache_path)
        loader.load()

        self.assertEqual(loader.get_cik("AAPL"), "0000320193")
        self.assertEqual(loader.get_cik("MSFT"), "0000789019")
        self.assertEqual(loader.get_cik("GOOGL"), "0001652044")

    @patch("alphalens.watchdog.sources.cik_loader.requests.get")
    def test_get_cik_returns_none_for_unknown_ticker(self, mock_get):
        from alphalens.watchdog.sources.cik_loader import CIKLoader

        mock_get.return_value = _make_response(FAKE_TICKERS_JSON)
        loader = CIKLoader(user_agent=self.user_agent, cache_path=self.cache_path)
        loader.load()

        self.assertIsNone(loader.get_cik("FAKE"))

    def test_default_cache_path_is_in_alphalens_home(self):
        from alphalens.watchdog.sources.cik_loader import default_cik_cache_path

        expected = Path.home() / ".alphalens" / "watchdog" / "company_tickers.json"
        self.assertEqual(default_cik_cache_path(), expected)

    @patch("alphalens.watchdog.sources.cik_loader.requests.get")
    def test_case_insensitive_ticker_lookup(self, mock_get):
        from alphalens.watchdog.sources.cik_loader import CIKLoader

        mock_get.return_value = _make_response(FAKE_TICKERS_JSON)
        loader = CIKLoader(user_agent=self.user_agent, cache_path=self.cache_path)
        loader.load()

        self.assertEqual(loader.get_cik("aapl"), "0000320193")


if __name__ == "__main__":
    unittest.main()
