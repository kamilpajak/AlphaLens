import datetime as dt
import json
import unittest
from unittest.mock import patch

import pandas as pd

from alphalens.thematic.sources import polygon_news

SAMPLE_API_RESPONSE = {
    "status": "OK",
    "count": 3,
    "next_url": None,
    "results": [
        {
            "id": "abc123",
            "publisher": {"name": "Motley Fool"},
            "title": "Nvidia announces CUDA-Q for quantum computing",
            "author": "Jane Smith",
            "published_utc": "2026-05-15T14:30:00Z",
            "article_url": "https://fool.com/nvda-cudaq",
            "tickers": ["NVDA", "QUBT"],
            "description": "NVIDIA unveiled CUDA-Q...",
            "keywords": ["quantum", "AI"],
            "insights": [
                {"ticker": "NVDA", "sentiment": "positive", "sentiment_reasoning": "Bullish"}
            ],
        },
        {
            "id": "def456",
            "publisher": {"name": "Reuters"},
            "title": "Coca-Cola raises dividend",
            "author": "John Doe",
            "published_utc": "2026-05-15T16:00:00Z",
            "article_url": "https://reuters.com/ko",
            "tickers": ["KO"],
            "description": "Quarterly dividend...",
            "keywords": ["dividend"],
            "insights": [],
        },
        {
            "id": "ghi789",
            "publisher": {"name": "Random Blog"},
            "title": "Off-universe penny stock pump",
            "author": "Anon",
            "published_utc": "2026-05-15T18:00:00Z",
            "article_url": "https://example.com/penny",
            "tickers": ["XYZQ"],
            "description": "...",
            "keywords": [],
            "insights": [],
        },
    ],
}


class TestPolygonNewsTransform(unittest.TestCase):
    def test_transforms_response_to_unified_schema(self):
        df = polygon_news.transform(SAMPLE_API_RESPONSE["results"], universe={"NVDA", "KO"})
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns)[:3], ["id", "source", "timestamp"])
        self.assertTrue((df["source"] == "polygon").all())
        self.assertTrue(df["timestamp"].dt.tz is not None)

    def test_filters_off_universe_tickers(self):
        df = polygon_news.transform(SAMPLE_API_RESPONSE["results"], universe={"NVDA", "KO"})
        # Row 3 had ticker XYZQ — should be dropped (no universe overlap)
        self.assertNotIn("ghi789", df["id"].tolist())

    def test_intersects_tickers_with_universe(self):
        df = polygon_news.transform(SAMPLE_API_RESPONSE["results"], universe={"NVDA", "KO"})
        # NVDA+QUBT article: tickers should be filtered to just [NVDA] (QUBT not in universe)
        nvda_row = df[df["id"] == "abc123"].iloc[0]
        self.assertEqual(nvda_row["tickers"], ["NVDA"])

    def test_preserves_insights_in_extra_json(self):
        df = polygon_news.transform(SAMPLE_API_RESPONSE["results"], universe={"NVDA"})
        nvda_row = df[df["id"] == "abc123"].iloc[0]
        extra = json.loads(nvda_row["extra"])
        self.assertEqual(extra["publisher"], "Motley Fool")
        self.assertEqual(extra["insights"][0]["sentiment"], "positive")

    def test_empty_results_returns_empty_frame(self):
        df = polygon_news.transform([], universe={"NVDA"})
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), polygon_news.NEWS_COLUMNS)


class TestPolygonNewsFetch(unittest.TestCase):
    def test_fetch_news_range_calls_correct_endpoint(self):
        captured = {}

        def fake_call(url, **kwargs):
            captured["url"] = url
            return SAMPLE_API_RESPONSE

        with patch.object(polygon_news, "_http_get_json", side_effect=fake_call):
            items = polygon_news.fetch_news_range(
                api_key="testkey",
                start=dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
                end=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
            )

        self.assertIn("api.polygon.io/v2/reference/news", captured["url"])
        self.assertIn("published_utc.gte=2026-05-15", captured["url"])
        self.assertIn("apiKey=testkey", captured["url"])
        self.assertEqual(len(items), 3)

    def test_fetch_news_range_handles_pagination(self):
        page1 = {
            **SAMPLE_API_RESPONSE,
            "next_url": "https://api.polygon.io/v2/reference/news?cursor=abc",
        }
        page2 = {**SAMPLE_API_RESPONSE, "next_url": None}
        calls = []

        def fake_call(url, **kwargs):
            calls.append(url)
            return page1 if len(calls) == 1 else page2

        with patch.object(polygon_news, "_http_get_json", side_effect=fake_call):
            items = polygon_news.fetch_news_range(
                api_key="testkey",
                start=dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
                end=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("cursor=abc", calls[1])
        self.assertEqual(len(items), 6)  # 2 pages × 3 items

    def test_fetch_news_range_respects_max_items(self):
        page1 = {
            **SAMPLE_API_RESPONSE,
            "next_url": "https://api.polygon.io/v2/reference/news?cursor=x",
        }
        page2 = {**SAMPLE_API_RESPONSE, "next_url": None}

        def fake_call(url, **kwargs):
            return page1 if "cursor" not in url else page2

        with patch.object(polygon_news, "_http_get_json", side_effect=fake_call):
            items = polygon_news.fetch_news_range(
                api_key="testkey",
                start=dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
                end=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
                max_items=4,
            )

        self.assertLessEqual(len(items), 4)


class TestPolygonNewsCache(unittest.TestCase):
    def test_fetch_daily_news_writes_parquet_and_returns_frame(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(polygon_news, "_http_get_json", return_value=SAMPLE_API_RESPONSE),
                patch.object(
                    polygon_news, "load_input_universe", return_value=frozenset({"NVDA", "KO"})
                ),
            ):
                df = polygon_news.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    api_key="testkey",
                    cache_dir=cache_dir,
                )

            cached = cache_dir / "2026-05-15.parquet"
            self.assertTrue(cached.exists())
            self.assertEqual(len(df), 2)
            self.assertEqual(set(df.columns), set(polygon_news.NEWS_COLUMNS))

            # Second call should hit cache (no new HTTP)
            with patch.object(
                polygon_news, "_http_get_json", side_effect=AssertionError("should not call")
            ):
                df2 = polygon_news.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    api_key="testkey",
                    cache_dir=cache_dir,
                )
            pd.testing.assert_frame_equal(
                df.reset_index(drop=True), df2.reset_index(drop=True), check_dtype=False
            )


if __name__ == "__main__":
    unittest.main()
