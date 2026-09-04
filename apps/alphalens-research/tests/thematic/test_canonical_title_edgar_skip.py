"""``canonical_title_for`` never fetches sec.gov (#1296).

Every SEC EDGAR request goes through the canonical ``SecEdgarClient``; the
display-only og:title fetcher must not become a shadow EDGAR client when an
event-lane row carries an EDGAR filing-index URL as its catalyst.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.sources import canonical_title as ct

FALLBACK = "Insider purchase cluster: 2 officers/directors bought $120k of stock (SEC Form 4)"


class TestEdgarHostSkip(unittest.TestCase):
    def _run(self, url: str) -> tuple[str, list[str]]:
        calls: list[str] = []

        def fetcher(u: str) -> str:
            calls.append(u)
            return "<html><head><meta property='og:title' content='Insider purchase cluster filing page'></head></html>"

        with tempfile.TemporaryDirectory() as tmp:
            got = ct.canonical_title_for(
                url, fallback=FALLBACK, cache_dir=Path(tmp), fetcher=fetcher
            )
        return got, calls

    def test_sec_gov_returns_fallback_without_fetching(self):
        for url in (
            "https://www.sec.gov/Archives/edgar/data/99/000000000226000001/x-index.htm",
            "https://sec.gov/cgi-bin/browse-edgar?action=getcurrent",
            "https://efts.sec.gov/LATEST/search-index?q=x",
        ):
            with self.subTest(url=url):
                got, calls = self._run(url)
                self.assertEqual(got, FALLBACK)
                self.assertEqual(calls, [])

    def test_publisher_host_still_fetches(self):
        got, calls = self._run("https://pub.test/insider-purchase-cluster")
        self.assertEqual(calls, ["https://pub.test/insider-purchase-cluster"])
        self.assertNotEqual(got, "")

    def test_lookalike_host_is_not_skipped(self):
        _, calls = self._run("https://notsec.gov.example.com/a")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
