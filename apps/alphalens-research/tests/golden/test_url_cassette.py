"""Unit tests for the GDELT/RSS URL cassettes (Phase 3b)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.golden.url_cassette import (
    FeedCassette,
    RecordingFeed,
    RecordingUrlJson,
    UrlCassetteMissError,
    UrlJsonCassette,
)

_GDELT_RESP = {
    "articles": [{"url": "https://x.com/a", "seendate": "20260520T120000Z", "title": "Q"}]
}
_STRUCT = (2026, 5, 20, 12, 0, 0, 2, 140, 0)


class _FakeFeed:
    def __init__(self):
        self.bozo = 0
        self.bozo_exception = None
        self.entries = [
            SimpleNamespace(
                link="https://x.com/post",
                title="Quantum leap",
                summary="body text",
                published_parsed=time.struct_time(_STRUCT),
                updated_parsed=None,
            )
        ]


class TestUrlJsonCassette(unittest.TestCase):
    def test_record_then_replay_identity(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "gdelt.json"
            rec = RecordingUrlJson(lambda url, **kw: _GDELT_RESP, store)
            self.assertEqual(rec("https://api.gdelt/q?theme=quantum"), _GDELT_RESP)
            cas = UrlJsonCassette(store)
            self.assertEqual(cas("https://api.gdelt/q?theme=quantum"), _GDELT_RESP)

    def test_miss_is_fail_loud(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "gdelt.json"
            RecordingUrlJson(lambda url, **kw: _GDELT_RESP, store)("https://api.gdelt/q?theme=a")
            cas = UrlJsonCassette(store)
            with self.assertRaises(UrlCassetteMissError):
                cas("https://api.gdelt/q?theme=NEVER")

    def test_miss_returns_empty_when_not_fail_loud(self):
        with tempfile.TemporaryDirectory() as td:
            cas = UrlJsonCassette(Path(td) / "absent.json", fail_on_miss=False)
            self.assertEqual(cas("https://api.gdelt/q?theme=x"), {})


class TestFeedCassette(unittest.TestCase):
    def test_record_then_replay_preserves_entries_and_struct_time(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "rss.json"
            rec = RecordingFeed(lambda url: _FakeFeed(), store)
            rec("https://feed.example/rss")
            cas = FeedCassette(store)
            feed = cas("https://feed.example/rss")
            self.assertEqual(len(feed.entries), 1)
            e = feed.entries[0]
            self.assertEqual(e.link, "https://x.com/post")
            self.assertEqual(e.title, "Quantum leap")
            self.assertEqual(e.summary, "body text")
            # struct_time round-trips exactly (first 6 fields drive the timestamp).
            self.assertEqual(tuple(e.published_parsed)[:6], _STRUCT[:6])
            self.assertIsNone(e.updated_parsed)
            self.assertEqual(feed.bozo, 0)

    def test_miss_is_fail_loud(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "rss.json"
            RecordingFeed(lambda url: _FakeFeed(), store)("https://feed.example/rss")
            cas = FeedCassette(store)
            with self.assertRaises(UrlCassetteMissError):
                cas("https://feed.example/OTHER")

    def test_miss_returns_empty_feed_when_not_fail_loud(self):
        with tempfile.TemporaryDirectory() as td:
            cas = FeedCassette(Path(td) / "absent.json", fail_on_miss=False)
            feed = cas("https://feed.example/rss")
            self.assertEqual(feed.entries, [])
            self.assertEqual(feed.bozo, 1)


if __name__ == "__main__":
    unittest.main()
