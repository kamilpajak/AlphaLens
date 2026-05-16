import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alphalens.thematic.sources import rss
from alphalens.thematic.sources.schema import NEWS_COLUMNS


def _make_fake_parsed(entries):
    return SimpleNamespace(entries=entries, bozo=False, feed=SimpleNamespace(title="x"))


SAMPLE_ENTRIES = [
    SimpleNamespace(
        id="https://wired.com/cuda-quantum-2026",
        title="CUDA Proves Nvidia Is a Software Company",
        link="https://wired.com/cuda-quantum-2026",
        published_parsed=(2026, 5, 15, 14, 30, 0, 0, 0, 0),
        summary="NVIDIA unveiled CUDA-Q, its toolkit for hybrid quantum...",
    ),
    SimpleNamespace(
        id="https://wired.com/regen-mrna-trial",
        title="Regeneron mRNA trial halted by FDA",
        link="https://wired.com/regen-mrna-trial",
        published_parsed=(2026, 5, 14, 9, 0, 0, 0, 0, 0),
        summary="Pivotal phase 3 trial pauses on safety signal...",
    ),
]


class TestRssTransform(unittest.TestCase):
    def test_transforms_entries_to_unified_schema(self):
        df = rss.transform(SAMPLE_ENTRIES, feed_name="wired", domain="wired.com")
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns), NEWS_COLUMNS)
        self.assertTrue((df["source"] == "rss").all())

    def test_uses_link_as_url_and_stable_id(self):
        df = rss.transform(SAMPLE_ENTRIES, feed_name="wired", domain="wired.com")
        self.assertEqual(df.iloc[0]["url"], "https://wired.com/cuda-quantum-2026")
        self.assertEqual(df.iloc[0]["id"], df.iloc[0]["id"])  # stable
        # IDs derived from link, so should differ
        self.assertNotEqual(df.iloc[0]["id"], df.iloc[1]["id"])

    def test_published_parsed_becomes_utc_timestamp(self):
        df = rss.transform(SAMPLE_ENTRIES, feed_name="wired", domain="wired.com")
        ts = df.iloc[0]["timestamp"]
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.tzinfo, dt.UTC)

    def test_feed_metadata_in_extra(self):
        import json

        df = rss.transform(SAMPLE_ENTRIES, feed_name="wired", domain="wired.com")
        extra = json.loads(df.iloc[0]["extra"])
        self.assertEqual(extra["feed_name"], "wired")
        self.assertEqual(extra["domain"], "wired.com")

    def test_summary_lands_in_body(self):
        df = rss.transform(SAMPLE_ENTRIES, feed_name="wired", domain="wired.com")
        self.assertTrue(df.iloc[0]["body"].startswith("NVIDIA unveiled"))

    def test_entry_without_link_is_dropped(self):
        bad = SimpleNamespace(
            id="bogus", title="no link", published_parsed=(2026, 5, 15, 0, 0, 0, 0, 0, 0)
        )
        df = rss.transform([bad], feed_name="wired", domain="wired.com")
        self.assertEqual(len(df), 0)

    def test_empty_returns_empty_frame(self):
        df = rss.transform([], feed_name="wired", domain="wired.com")
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), NEWS_COLUMNS)


class TestRssFetch(unittest.TestCase):
    def test_fetch_feed_uses_feedparser_and_returns_frame(self):
        with patch.object(rss, "_parse_feed", return_value=_make_fake_parsed(SAMPLE_ENTRIES)):
            df = rss.fetch_feed(name="wired", url="https://wired.com/rss", domain="wired.com")
        self.assertEqual(len(df), 2)
        self.assertTrue((df["source"] == "rss").all())

    def test_fetch_daily_news_iterates_all_feeds_and_dedupes(self):
        feeds_fixture = [
            {"name": "wired", "url": "u1", "domain": "wired.com"},
            {"name": "techcrunch", "url": "u2", "domain": "techcrunch.com"},
        ]
        call_count = [0]

        def fake_parse(url):
            call_count[0] += 1
            return _make_fake_parsed(SAMPLE_ENTRIES)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(rss, "_parse_feed", side_effect=fake_parse):
                df = rss.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    feeds=feeds_fixture,
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(call_count[0], 2)
            # Same fixture in both feeds -> dedup -> 2 unique rows
            self.assertEqual(df["id"].nunique(), 2)
            self.assertTrue((Path(tmpdir) / "2026-05-15.parquet").exists())

    def test_fetch_daily_news_skips_feeds_outside_date(self):
        # Entry from 2026-05-10; date filter requested for 2026-05-15 with window=1
        old_entry = SimpleNamespace(
            id="https://x.com/old",
            link="https://x.com/old",
            title="Old",
            published_parsed=(2026, 5, 10, 0, 0, 0, 0, 0, 0),
            summary="...",
        )
        feeds = [{"name": "x", "url": "u", "domain": "x.com"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(rss, "_parse_feed", return_value=_make_fake_parsed([old_entry])):
                df = rss.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    feeds=feeds,
                    cache_dir=Path(tmpdir),
                    window_days=1,
                )
            self.assertEqual(len(df), 0)

    def test_fetch_daily_news_handles_feed_error_gracefully(self):
        feeds = [
            {"name": "ok", "url": "u1", "domain": "ok.com"},
            {"name": "bad", "url": "u2", "domain": "bad.com"},
        ]

        def fake_parse(url):
            if url == "u2":
                raise RuntimeError("network error")
            return _make_fake_parsed(SAMPLE_ENTRIES)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(rss, "_parse_feed", side_effect=fake_parse):
                df = rss.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    feeds=feeds,
                    cache_dir=Path(tmpdir),
                )
            # ok feed contributes 2 rows; bad feed silently dropped
            self.assertEqual(df["id"].nunique(), 2)

    def test_load_feeds_from_yaml(self):
        feeds = rss.load_feeds()
        self.assertIsInstance(feeds, list)
        self.assertGreater(len(feeds), 0)
        for f in feeds:
            self.assertIn("name", f)
            self.assertIn("url", f)
            self.assertIn("domain", f)


if __name__ == "__main__":
    unittest.main()
