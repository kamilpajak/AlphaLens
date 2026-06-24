import datetime as dt
import json as _json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.literature_scanner.perplexity_client import AskResult
from alphalens_pipeline.thematic.sources import perplexity
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS


class TestPerplexityHelpers(unittest.TestCase):
    def test_prompt_has_date_and_no_source_steering(self):
        p = perplexity.build_prompt("2026-06-12")
        self.assertIn("2026-06-12", p)
        self.assertIn("JSON", p)
        low = p.lower()
        for banned in (
            "reuters",
            "bloomberg",
            "avoid blog",
            "reddit",
            "reputable",
            "price target",
            "market cap",
        ):
            self.assertNotIn(banned, low)

    def test_parse_well_formed(self):
        content = (
            '{"stories": [{"headline": "SpaceX IPO", "summary": "Debut.", "url": "https://a.com"},'
            '{"headline": "Iran deal", "summary": "Oil falls.", "url": "https://b.com"}]}'
        )
        out = perplexity.parse_stories(content)
        self.assertEqual([s["headline"] for s in out], ["SpaceX IPO", "Iran deal"])
        self.assertEqual(out[0]["url"], "https://a.com")

    def test_parse_tolerates_trailing_prose_and_fence(self):
        content = '```json\n{"stories": [{"headline": "H", "summary": "S", "url": "u"}]}\n```\nHope this helps!'
        out = perplexity.parse_stories(content)
        self.assertEqual([s["headline"] for s in out], ["H"])

    def test_parse_skips_malformed_and_nonjson(self):
        self.assertEqual(perplexity.parse_stories("sorry, no json"), [])
        content = '{"stories": ["notadict", {"headline": "", "summary": "s", "url": "u"}, {"headline": "OK", "summary": "s", "url": "u"}]}'
        self.assertEqual([s["headline"] for s in perplexity.parse_stories(content)], ["OK"])

    def test_parse_skips_explicit_null_fields(self):
        content = '{"stories": [{"headline": null, "url": null, "summary": "s"}, {"headline": "OK", "url": "https://ok.com", "summary": null}]}'
        out = perplexity.parse_stories(content)
        self.assertEqual([s["headline"] for s in out], ["OK"])
        self.assertEqual(out[0]["summary"], "")  # null summary coerced to empty string, not "None"

    def test_stable_id_deterministic(self):
        self.assertEqual(
            perplexity._stable_id("https://a.com"), perplexity._stable_id("https://a.com")
        )
        self.assertNotEqual(
            perplexity._stable_id("https://a.com"), perplexity._stable_id("https://b.com")
        )


class TestFetchDailyNews(unittest.TestCase):
    def _client(self):
        c = mock.Mock()
        c.ask_with_citations.return_value = AskResult(
            content='{"stories": [{"headline": "SpaceX IPO", "summary": "Debut.", "url": "https://a.com"}]}',
            citations=["https://a.com", "https://b.com"],
            search_results=[{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        return c

    def test_maps_to_news_columns(self):
        with tempfile.TemporaryDirectory() as d:
            df = perplexity.fetch_daily_news(
                date=dt.date(2026, 6, 12), client=self._client(), cache_dir=Path(d)
            )
        self.assertEqual(list(df.columns), NEWS_COLUMNS)
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["source"], "perplexity")
        self.assertEqual(row["title"], "SpaceX IPO")
        self.assertEqual(row["body"], "Debut.")
        self.assertEqual(row["url"], "https://a.com")
        self.assertEqual(list(row["tickers"]), [])
        self.assertEqual(_json.loads(row["extra"])["citation_count"], 2)
        self.assertTrue(str(row["id"]).startswith("perplexity:"))
        self.assertEqual(row["timestamp"], pd.Timestamp("2026-06-12", tz="UTC"))

    def test_passes_pit_date_filters(self):
        c = self._client()
        with tempfile.TemporaryDirectory() as d:
            perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=c, cache_dir=Path(d))
        kw = c.ask_with_citations.call_args.kwargs
        self.assertEqual(kw["search_after_date_filter"], "06/11/2026")
        self.assertEqual(kw["search_before_date_filter"], "06/13/2026")
        self.assertEqual(kw["search_context_size"], "high")

    def test_caches_raw_and_skips_second_call(self):
        c = self._client()
        with tempfile.TemporaryDirectory() as d:
            perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=c, cache_dir=Path(d))
            self.assertTrue((Path(d) / "2026-06-12.json").exists())
            perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=c, cache_dir=Path(d))
        self.assertEqual(c.ask_with_citations.call_count, 1)  # second run hit the cache

    def test_empty_stories_returns_empty_frame(self):
        c = mock.Mock()
        c.ask_with_citations.return_value = AskResult(
            content="no json here", citations=[], search_results=[]
        )
        with tempfile.TemporaryDirectory() as d:
            df = perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=c, cache_dir=Path(d))
        self.assertEqual(list(df.columns), NEWS_COLUMNS)
        self.assertEqual(len(df), 0)

    def test_adapter_does_not_stamp_ingested_at(self):
        # ingested_at is owned by ingest_daily (one injectable transaction-time
        # per run); the adapter must leave it unset, like the other 4 sources.
        with tempfile.TemporaryDirectory() as d:
            df = perplexity.fetch_daily_news(
                date=dt.date(2026, 6, 12), client=self._client(), cache_dir=Path(d)
            )
        self.assertTrue(df["ingested_at"].isna().all())
