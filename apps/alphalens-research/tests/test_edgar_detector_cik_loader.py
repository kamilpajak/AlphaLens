import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

FAKE_TICKERS_JSON = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
}


def _make_sec_client(payload=None):
    """Mock SecEdgarClient that returns ``payload`` from fetch_company_tickers."""
    from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

    client = MagicMock(spec=SecEdgarClient)
    client.fetch_company_tickers.return_value = payload or FAKE_TICKERS_JSON
    return client


class TestCIKLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "company_tickers.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_download_when_cache_missing(self):
        from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader

        sec = _make_sec_client()
        loader = CIKLoader(cache_path=self.cache_path, sec_client=sec)
        loader.load()

        self.assertTrue(self.cache_path.exists())
        self.assertEqual(sec.fetch_company_tickers.call_count, 1)

    def test_use_cache_when_fresh(self):
        from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader

        self.cache_path.write_text(json.dumps(FAKE_TICKERS_JSON))

        sec = _make_sec_client()
        loader = CIKLoader(cache_path=self.cache_path, sec_client=sec)
        loader.load()
        self.assertEqual(sec.fetch_company_tickers.call_count, 0)

    def test_refresh_after_cache_expiry(self):
        from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader

        self.cache_path.write_text(json.dumps(FAKE_TICKERS_JSON))
        eight_days_ago = time.time() - (8 * 86400)
        import os

        os.utime(self.cache_path, (eight_days_ago, eight_days_ago))

        sec = _make_sec_client()
        loader = CIKLoader(
            cache_path=self.cache_path,
            ttl_seconds=7 * 86400,
            sec_client=sec,
        )
        loader.load()
        self.assertEqual(sec.fetch_company_tickers.call_count, 1)

    def test_get_cik_returns_10_digit_padded(self):
        from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader

        sec = _make_sec_client()
        loader = CIKLoader(cache_path=self.cache_path, sec_client=sec)
        loader.load()

        self.assertEqual(loader.get_cik("AAPL"), "0000320193")
        self.assertEqual(loader.get_cik("MSFT"), "0000789019")
        self.assertEqual(loader.get_cik("GOOGL"), "0001652044")

    def test_get_cik_returns_none_for_unknown_ticker(self):
        from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader

        sec = _make_sec_client()
        loader = CIKLoader(cache_path=self.cache_path, sec_client=sec)
        loader.load()

        self.assertIsNone(loader.get_cik("FAKE"))

    def test_default_cache_path_is_in_alphalens_home(self):
        from alphalens_pipeline.edgar_detector.sources.cik_loader import default_cik_cache_path

        expected = Path.home() / ".alphalens" / "edgar-detect" / "company_tickers.json"
        self.assertEqual(default_cik_cache_path(), expected)

    def test_case_insensitive_ticker_lookup(self):
        from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader

        sec = _make_sec_client()
        loader = CIKLoader(cache_path=self.cache_path, sec_client=sec)
        loader.load()

        self.assertEqual(loader.get_cik("aapl"), "0000320193")

    def test_default_sec_client_used_when_none_injected(self):
        """Without an injected sec_client, the loader falls back to the
        process-wide singleton so a single SEC throttle is shared."""
        from alphalens_pipeline.data.alt_data import sec_edgar_client as mod
        from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader

        mod._reset_default_client_for_tests()
        try:
            loader = CIKLoader(cache_path=self.cache_path)
            self.assertIs(loader._sec, mod.get_default_sec_client())
        finally:
            mod._reset_default_client_for_tests()


if __name__ == "__main__":
    unittest.main()
