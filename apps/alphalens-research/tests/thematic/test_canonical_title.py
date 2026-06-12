"""Tests for canonical publisher-title enrichment (og:title fetch + guard).

All tests inject a fetcher stub — no live HTTP. Covers extraction order,
entity-decode/whitespace/truncation, the tri-state cache (hit / miss-writes /
failure-not-cached / junk-not-cached / TTL re-fetch) and the replacement guard
(junk denylist + length bounds + >=2 shared content tokens).
"""

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.sources import canonical_title as ct

# The reported real-world case: GDELT mangled title vs the publisher headline.
_GDELT_TITLE = (
    "Scientists are fast-tracking 3 Ebola vaccines in hopes of "
    "shortening the outbreak when could they be ready?"
)
_PUBLISHER_TITLE = (
    "3 new Ebola vaccines are being fast-tracked amid the current "
    "outbreak — when could they be ready?"
)


def _html(*, og=None, twitter=None, title=None) -> str:
    parts = ["<html><head>"]
    if og is not None:
        parts.append(f'<meta property="og:title" content="{og}">')
    if twitter is not None:
        parts.append(f'<meta name="twitter:title" content="{twitter}">')
    if title is not None:
        parts.append(f"<title>{title}</title>")
    parts.append("</head><body>x</body></html>")
    return "".join(parts)


def _fetcher_returning(html_text):
    calls = {"n": 0}

    def _fetch(url):
        calls["n"] += 1
        return html_text

    _fetch.calls = calls
    return _fetch


class TestExtractTitle(unittest.TestCase):
    def test_prefers_og_title(self):
        h = _html(og="OG Headline", twitter="TW Headline", title="Tag Headline")
        self.assertEqual(ct._extract_title(h), "OG Headline")

    def test_falls_back_to_twitter_title(self):
        h = _html(twitter="TW Headline", title="Tag Headline")
        self.assertEqual(ct._extract_title(h), "TW Headline")

    def test_falls_back_to_title_tag(self):
        h = _html(title="Tag Headline")
        self.assertEqual(ct._extract_title(h), "Tag Headline")

    def test_none_when_no_title_anywhere(self):
        self.assertIsNone(ct._extract_title("<html><head></head><body>x</body></html>"))

    def test_decodes_entities_and_collapses_whitespace(self):
        h = _html(og="Nvidia&#8217;s   Computex    keynote")
        self.assertEqual(ct._extract_title(h), "Nvidia’s Computex keynote")

    def test_truncates_to_max_len(self):
        long = "word " * 100
        h = _html(og=long)
        out = ct._extract_title(h)
        self.assertLessEqual(len(out), ct._TITLE_MAX_LEN)


class TestSafeUrl(unittest.TestCase):
    def test_accepts_public_https(self):
        self.assertTrue(ct._is_safe_url("https://www.livescience.com/article"))

    def test_rejects_non_http_scheme(self):
        self.assertFalse(ct._is_safe_url("file:///etc/passwd"))
        self.assertFalse(ct._is_safe_url("ftp://host/x"))

    def test_rejects_localhost_and_private_and_metadata(self):
        for url in (
            "http://localhost/x",
            "http://127.0.0.1/x",
            "http://10.0.0.5/x",
            "http://192.168.1.1/x",
            "http://169.254.169.254/latest/meta-data/",
        ):
            self.assertFalse(ct._is_safe_url(url), url)


class TestFetchOgTitleCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_miss_fetches_extracts_and_caches(self):
        f = _fetcher_returning(_html(og=_PUBLISHER_TITLE))
        out = ct.fetch_og_title("https://x.test/a", cache_dir=self.cache_dir, fetcher=f)
        self.assertEqual(
            out,
            "3 new Ebola vaccines are being fast-tracked amid the current outbreak — when could they be ready?",
        )
        self.assertEqual(f.calls["n"], 1)
        # A cache file now exists for this URL.
        self.assertEqual(len(list(self.cache_dir.glob("*.txt"))), 1)

    def test_hit_returns_cached_without_refetch(self):
        f = _fetcher_returning(_html(og=_PUBLISHER_TITLE))
        ct.fetch_og_title("https://x.test/a", cache_dir=self.cache_dir, fetcher=f)
        out = ct.fetch_og_title("https://x.test/a", cache_dir=self.cache_dir, fetcher=f)
        self.assertEqual(f.calls["n"], 1)  # second call served from cache
        self.assertIn("Ebola", out)

    def test_fetcher_exception_returns_none_and_does_not_cache(self):
        def boom(url):
            raise RuntimeError("network down")

        out = ct.fetch_og_title("https://x.test/a", cache_dir=self.cache_dir, fetcher=boom)
        self.assertIsNone(out)
        self.assertEqual(list(self.cache_dir.glob("*.txt")), [])  # no poisoning

    def test_unsafe_url_returns_none_without_fetch_or_cache(self):
        f = _fetcher_returning(_html(og=_PUBLISHER_TITLE))
        out = ct.fetch_og_title("http://169.254.169.254/x", cache_dir=self.cache_dir, fetcher=f)
        self.assertIsNone(out)
        self.assertEqual(f.calls["n"], 0)  # never fetched
        self.assertEqual(list(self.cache_dir.glob("*.txt")), [])

    def test_junk_title_returns_none_and_does_not_cache(self):
        f = _fetcher_returning(_html(og="Just a moment..."))
        out = ct.fetch_og_title("https://x.test/a", cache_dir=self.cache_dir, fetcher=f)
        self.assertIsNone(out)
        self.assertEqual(list(self.cache_dir.glob("*.txt")), [])

    def test_empty_extracted_title_returns_none_and_does_not_cache(self):
        f = _fetcher_returning("<html><head></head><body>x</body></html>")
        out = ct.fetch_og_title("https://x.test/a", cache_dir=self.cache_dir, fetcher=f)
        self.assertIsNone(out)
        self.assertEqual(list(self.cache_dir.glob("*.txt")), [])

    def test_ttl_expiry_triggers_refetch(self):
        f = _fetcher_returning(_html(og=_PUBLISHER_TITLE))
        ct.fetch_og_title("https://x.test/a", cache_dir=self.cache_dir, fetcher=f)
        # Age the cache file past the TTL.
        cache_file = next(self.cache_dir.glob("*.txt"))
        old = dt.datetime.now() - dt.timedelta(days=ct._CACHE_TTL_DAYS + 5)
        os.utime(cache_file, (old.timestamp(), old.timestamp()))
        ct.fetch_og_title("https://x.test/a", cache_dir=self.cache_dir, fetcher=f)
        self.assertEqual(f.calls["n"], 2)  # stale → refetched


class TestCanonicalTitleFor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_replaces_with_publisher_title_on_reword(self):
        # Publisher reworded the headline but it is the same article — shares
        # >=2 content tokens (ebola, vaccines, outbreak, ...). Jaccard>=0.6 would
        # wrongly reject; the >=2-shared-token guard accepts.
        f = _fetcher_returning(_html(og=_PUBLISHER_TITLE))
        out = ct.canonical_title_for(
            "https://x.test/a", fallback=_GDELT_TITLE, cache_dir=self.cache_dir, fetcher=f
        )
        self.assertEqual(
            out,
            "3 new Ebola vaccines are being fast-tracked amid the current outbreak — when could they be ready?",
        )
        self.assertIn("—", out)

    def test_keeps_fallback_when_unrelated_title(self):
        f = _fetcher_returning(_html(og="Quarterly municipal bond yields summary today"))
        out = ct.canonical_title_for(
            "https://x.test/a", fallback=_GDELT_TITLE, cache_dir=self.cache_dir, fetcher=f
        )
        self.assertEqual(out, _GDELT_TITLE)  # <2 shared tokens → fallback

    def test_keeps_fallback_on_junk_page(self):
        f = _fetcher_returning(_html(og="Are you a robot?"))
        out = ct.canonical_title_for(
            "https://x.test/a", fallback=_GDELT_TITLE, cache_dir=self.cache_dir, fetcher=f
        )
        self.assertEqual(out, _GDELT_TITLE)

    def test_empty_url_returns_fallback_without_fetch(self):
        f = _fetcher_returning(_html(og=_PUBLISHER_TITLE))
        out = ct.canonical_title_for("", fallback=_GDELT_TITLE, cache_dir=self.cache_dir, fetcher=f)
        self.assertEqual(out, _GDELT_TITLE)
        self.assertEqual(f.calls["n"], 0)

    def test_blank_fallback_takes_og_title(self):
        # No source title to cross-check; og:title is the only candidate for the
        # URL and passed the junk filter → take it.
        f = _fetcher_returning(_html(og=_PUBLISHER_TITLE))
        out = ct.canonical_title_for(
            "https://x.test/a", fallback="", cache_dir=self.cache_dir, fetcher=f
        )
        self.assertIn("Ebola", out)

    def test_never_raises_when_fetcher_raises(self):
        def boom(url):
            raise RuntimeError("network down")

        out = ct.canonical_title_for(
            "https://x.test/a", fallback=_GDELT_TITLE, cache_dir=self.cache_dir, fetcher=boom
        )
        self.assertEqual(out, _GDELT_TITLE)


if __name__ == "__main__":
    unittest.main()
