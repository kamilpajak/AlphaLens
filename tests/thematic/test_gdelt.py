import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphalens.thematic.sources import gdelt
from alphalens.thematic.sources.schema import NEWS_COLUMNS

SAMPLE_GDELT_RESPONSE = {
    "articles": [
        {
            "url": "https://wired.com/cuda-quantum",
            "url_mobile": "",
            "title": "CUDA Proves Nvidia Is a Software Company",
            "seendate": "20260515T011500Z",
            "socialimage": "https://...",
            "domain": "wired.com",
            "language": "English",
            "sourcecountry": "United States",
        },
        {
            "url": "https://fool.com/quantum-ipo",
            "title": "This Quantum Computing Stock Could Be a Multibagger",
            "seendate": "20260514T184500Z",
            "domain": "fool.com",
            "language": "English",
            "sourcecountry": "United States",
        },
    ]
}

THEME_BUCKETS_FIXTURE = {
    "ai_quantum": '("CUDA" OR "quantum computing") AND (NVIDIA OR IBM)',
    "biotech": "(mRNA OR FDA approval) AND (Moderna OR Regeneron)",
}


class TestGdeltTransform(unittest.TestCase):
    def test_transforms_to_unified_schema(self):
        df = gdelt.transform(SAMPLE_GDELT_RESPONSE["articles"], theme="ai_quantum")
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns), NEWS_COLUMNS)
        self.assertTrue((df["source"] == "gdelt").all())
        self.assertTrue(df["timestamp"].dt.tz is not None)

    def test_seendate_parses_to_utc_timestamp(self):
        df = gdelt.transform(SAMPLE_GDELT_RESPONSE["articles"], theme="ai_quantum")
        ts = df.iloc[0]["timestamp"]
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.month, 5)
        self.assertEqual(ts.tzinfo, dt.UTC)

    def test_theme_stored_in_extra(self):
        df = gdelt.transform(SAMPLE_GDELT_RESPONSE["articles"], theme="ai_quantum")
        extra = json.loads(df.iloc[0]["extra"])
        self.assertEqual(extra["theme_bucket"], "ai_quantum")
        self.assertEqual(extra["domain"], "wired.com")

    def test_id_is_url_hash_stable(self):
        df1 = gdelt.transform(SAMPLE_GDELT_RESPONSE["articles"], theme="ai_quantum")
        df2 = gdelt.transform(SAMPLE_GDELT_RESPONSE["articles"], theme="ai_quantum")
        self.assertEqual(df1["id"].tolist(), df2["id"].tolist())

    def test_empty_returns_empty_frame(self):
        df = gdelt.transform([], theme="ai_quantum")
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), NEWS_COLUMNS)


class TestGdeltQueryBuilder(unittest.TestCase):
    def test_build_url_includes_required_params(self):
        url = gdelt.build_query_url(
            query="NVIDIA AND quantum",
            timespan="1d",
            maxrecords=50,
        )
        self.assertIn("api.gdeltproject.org/api/v2/doc/doc", url)
        self.assertIn("query=NVIDIA+AND+quantum", url)
        self.assertIn("timespan=1d", url)
        self.assertIn("maxrecords=50", url)
        self.assertIn("mode=artlist", url)
        self.assertIn("format=json", url)


class TestGdeltFetch(unittest.TestCase):
    def test_fetch_theme_calls_endpoint_and_returns_frame(self):
        with patch.object(gdelt, "_http_get_json", return_value=SAMPLE_GDELT_RESPONSE):
            df = gdelt.fetch_theme(
                theme="ai_quantum",
                query='("CUDA" OR "quantum computing") AND (NVIDIA OR IBM)',
                timespan="1d",
            )
        self.assertEqual(len(df), 2)
        self.assertTrue((df["source"] == "gdelt").all())

    def test_fetch_daily_news_iterates_all_themes(self):
        calls = []

        def fake_call(url, **kwargs):
            calls.append(url)
            return SAMPLE_GDELT_RESPONSE

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(gdelt, "_http_get_json", side_effect=fake_call):
                df = gdelt.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    theme_buckets=THEME_BUCKETS_FIXTURE,
                    cache_dir=Path(tmpdir),
                    inter_query_sleep_sec=0,
                )

            self.assertEqual(len(calls), len(THEME_BUCKETS_FIXTURE))
            # Same fixture articles in both buckets -> dedup leaves 2 unique rows
            self.assertEqual(len(df), 2)
            cached = Path(tmpdir) / "2026-05-15.parquet"
            self.assertTrue(cached.exists())

    def test_fetch_daily_news_dedupes_articles_seen_in_multiple_themes(self):
        # Same article appears in both query buckets
        def fake_call(url, **kwargs):
            return SAMPLE_GDELT_RESPONSE

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(gdelt, "_http_get_json", side_effect=fake_call):
                df = gdelt.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    theme_buckets=THEME_BUCKETS_FIXTURE,
                    cache_dir=Path(tmpdir),
                    inter_query_sleep_sec=0,
                )
            # After dedup by id, only 2 unique articles
            self.assertEqual(df["id"].nunique(), 2)

    def test_fetch_daily_news_reads_cache_on_second_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(gdelt, "_http_get_json", return_value=SAMPLE_GDELT_RESPONSE):
                gdelt.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    theme_buckets=THEME_BUCKETS_FIXTURE,
                    cache_dir=Path(tmpdir),
                    inter_query_sleep_sec=0,
                )
            with patch.object(gdelt, "_http_get_json", side_effect=AssertionError("no call")):
                df2 = gdelt.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    theme_buckets=THEME_BUCKETS_FIXTURE,
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df2), 2)

    def test_theme_buckets_loaded_from_yaml(self):
        # Just verify the config loader returns a dict[str, str]
        buckets = gdelt.load_theme_buckets()
        self.assertIsInstance(buckets, dict)
        self.assertGreater(len(buckets), 0)
        for k, v in buckets.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, str)


if __name__ == "__main__":
    unittest.main()
