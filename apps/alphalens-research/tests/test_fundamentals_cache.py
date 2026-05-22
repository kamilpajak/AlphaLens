"""Disk-backed TTL cache for fundamentals. Reads/writes JSON per ticker in
`~/.alphalens/fundamentals/{ticker}.json`. TTL default 90d (quarterly cycle).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


class TestFundamentalsCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_get_missing_returns_none(self):
        from alphalens_research.data.fundamentals.cache import FundamentalsCache

        cache = FundamentalsCache(root=self.root)
        self.assertIsNone(cache.get("AAPL"))

    def test_put_then_get_roundtrips(self):
        from alphalens_research.data.fundamentals.cache import FundamentalsCache

        cache = FundamentalsCache(root=self.root)
        features = {"cash_runway_months": 24.0, "ps_ratio": 10.0}
        cache.put("AAPL", features)

        fetched = cache.get("AAPL")
        self.assertEqual(fetched["cash_runway_months"], 24.0)
        self.assertEqual(fetched["ps_ratio"], 10.0)

    def test_ticker_is_uppercased_in_cache_file(self):
        from alphalens_research.data.fundamentals.cache import FundamentalsCache

        cache = FundamentalsCache(root=self.root)
        cache.put("aapl", {"ps_ratio": 1.0})
        self.assertTrue((self.root / "AAPL.json").exists())
        self.assertEqual(cache.get("aapl")["ps_ratio"], 1.0)

    def test_stale_entry_beyond_ttl_returns_none(self):
        from alphalens_research.data.fundamentals.cache import FundamentalsCache

        cache = FundamentalsCache(root=self.root, ttl_days=90)
        # Simulate an old entry by writing with a timestamp past TTL.
        stale_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        (self.root / "AAPL.json").write_text(
            json.dumps({"features": {"ps_ratio": 1.0}, "fetched_at": stale_ts})
        )
        self.assertIsNone(cache.get("AAPL"))

    def test_fresh_entry_within_ttl_returned(self):
        from alphalens_research.data.fundamentals.cache import FundamentalsCache

        cache = FundamentalsCache(root=self.root, ttl_days=90)
        fresh_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        (self.root / "AAPL.json").write_text(
            json.dumps({"features": {"ps_ratio": 5.0}, "fetched_at": fresh_ts})
        )
        self.assertEqual(cache.get("AAPL")["ps_ratio"], 5.0)

    def test_corrupted_json_returns_none(self):
        """If the cache file is malformed (manual edit / partial write), treat
        it as missing rather than crashing."""
        from alphalens_research.data.fundamentals.cache import FundamentalsCache

        cache = FundamentalsCache(root=self.root)
        (self.root / "X.json").write_text("{not valid json")
        self.assertIsNone(cache.get("X"))

    def test_get_or_fetch_uses_cache_when_fresh(self):
        from alphalens_research.data.fundamentals.cache import FundamentalsCache

        cache = FundamentalsCache(root=self.root)
        cache.put("AAPL", {"ps_ratio": 7.5})

        call_count = [0]

        def fetcher(_ticker):
            call_count[0] += 1
            return {"ps_ratio": 999.0}

        features = cache.get_or_fetch("AAPL", fetcher)
        self.assertEqual(features["ps_ratio"], 7.5)
        self.assertEqual(call_count[0], 0)

    def test_get_or_fetch_invokes_fetcher_on_miss(self):
        from alphalens_research.data.fundamentals.cache import FundamentalsCache

        cache = FundamentalsCache(root=self.root)

        def fetcher(ticker):
            return {"ticker_seen": ticker, "ps_ratio": 42.0}

        features = cache.get_or_fetch("NVDA", fetcher)
        self.assertEqual(features["ps_ratio"], 42.0)
        # And the result is now persisted.
        self.assertEqual(cache.get("NVDA")["ps_ratio"], 42.0)


if __name__ == "__main__":
    unittest.main()
