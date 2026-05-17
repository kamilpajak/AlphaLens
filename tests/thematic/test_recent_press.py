import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphalens.thematic.verification import recent_press

SAMPLE_POLYGON_RESPONSE = {
    "results": [
        {
            "id": "p1",
            "published_utc": "2026-05-10T14:30:00Z",
            "title": "Beam Global expands quantum compute partnerships",
            "description": "Press release announces quantum computing pilot.",
            "tickers": ["BEEM"],
            "keywords": ["quantum", "energy storage"],
            "insights": [],
            "article_url": "https://example.com/beem-quantum",
            "publisher": {"name": "PRNewswire"},
        },
        {
            "id": "p2",
            "published_utc": "2026-05-12T08:00:00Z",
            "title": "Beam Global Q1 earnings beat",
            "description": "Revenue up 50% YoY",
            "tickers": ["BEEM"],
            "keywords": ["earnings"],
            "insights": [],
            "article_url": "https://example.com/beem-q1",
            "publisher": {"name": "Reuters"},
        },
    ]
}


class TestFetchRecentNews(unittest.TestCase):
    def test_fetch_recent_news_calls_polygon_with_ticker_filter(self):
        captured = {}

        def fake_call(url, **kwargs):
            captured["url"] = url
            return SAMPLE_POLYGON_RESPONSE

        with patch.object(recent_press, "_http_get_json", side_effect=fake_call):
            items = recent_press.fetch_recent_news(
                ticker="BEEM",
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                api_key="testkey",
            )

        self.assertIn("ticker=BEEM", captured["url"])
        self.assertIn("published_utc.gte=2026-04-15", captured["url"])
        self.assertIn("apiKey=testkey", captured["url"])
        self.assertEqual(len(items), 2)

    def test_fetch_caches_to_parquet_and_reuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(recent_press, "_http_get_json", return_value=SAMPLE_POLYGON_RESPONSE):
                df = recent_press.fetch_recent_news_cached(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    api_key="testkey",
                    cache_dir=cache_dir,
                )
            self.assertEqual(len(df), 2)
            cached = cache_dir / "BEEM_2026-05-15.parquet"
            self.assertTrue(cached.exists())

            with patch.object(
                recent_press, "_http_get_json", side_effect=AssertionError("no call")
            ):
                df2 = recent_press.fetch_recent_news_cached(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    api_key="testkey",
                    cache_dir=cache_dir,
                )
            self.assertEqual(len(df2), 2)


class TestVerificationGate(unittest.TestCase):
    def test_has_theme_in_recent_press_true_on_keyword_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(recent_press, "_http_get_json", return_value=SAMPLE_POLYGON_RESPONSE):
                self.assertTrue(
                    recent_press.has_theme_in_recent_press(
                        ticker="BEEM",
                        asof=dt.date(2026, 5, 15),
                        keywords=["quantum"],
                        api_key="testkey",
                        cache_dir=cache_dir,
                    )
                )

    def test_has_theme_in_recent_press_matches_title_or_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(recent_press, "_http_get_json", return_value=SAMPLE_POLYGON_RESPONSE):
                # 'energy storage' is in the keywords field of p1
                self.assertTrue(
                    recent_press.has_theme_in_recent_press(
                        ticker="BEEM",
                        asof=dt.date(2026, 5, 15),
                        keywords=["energy storage"],
                        api_key="testkey",
                        cache_dir=cache_dir,
                    )
                )

    def test_has_theme_in_recent_press_false_on_miss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(recent_press, "_http_get_json", return_value=SAMPLE_POLYGON_RESPONSE):
                self.assertFalse(
                    recent_press.has_theme_in_recent_press(
                        ticker="BEEM",
                        asof=dt.date(2026, 5, 15),
                        keywords=["alien_invasion", "cybersecurity"],
                        api_key="testkey",
                        cache_dir=cache_dir,
                    )
                )

    def test_has_theme_returns_false_when_no_press_releases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(recent_press, "_http_get_json", return_value={"results": []}):
                self.assertFalse(
                    recent_press.has_theme_in_recent_press(
                        ticker="UNKN",
                        asof=dt.date(2026, 5, 15),
                        keywords=["anything"],
                        api_key="testkey",
                        cache_dir=cache_dir,
                    )
                )

    def test_has_theme_fails_closed_on_api_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(
                recent_press, "_http_get_json", side_effect=RuntimeError("rate limit")
            ):
                self.assertFalse(
                    recent_press.has_theme_in_recent_press(
                        ticker="BEEM",
                        asof=dt.date(2026, 5, 15),
                        keywords=["quantum"],
                        api_key="testkey",
                        cache_dir=cache_dir,
                    )
                )


class TestWindowUniverseFetch(unittest.TestCase):
    def test_fetch_window_universe_caches_one_unfiltered_pull(self):

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            calls = []

            def fake_call(url, **kwargs):
                calls.append(url)
                return SAMPLE_POLYGON_RESPONSE

            with patch.object(recent_press, "_http_get_json", side_effect=fake_call):
                df = recent_press.fetch_window_universe(
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    api_key="testkey",
                    cache_dir=cache_dir,
                )
            self.assertEqual(len(calls), 1)
            self.assertNotIn("ticker=", calls[0])
            self.assertEqual(len(df), 2)
            cache_file = cache_dir / "_universe_2026-05-15.parquet"
            self.assertTrue(cache_file.exists())

    def test_fetch_recent_news_handles_pagination(self):
        page1 = {**SAMPLE_POLYGON_RESPONSE, "next_url": "https://x.com?cursor=a"}
        page2 = {**SAMPLE_POLYGON_RESPONSE, "next_url": None}
        seen = []

        def fake_call(url, **kwargs):
            seen.append(url)
            return page1 if len(seen) == 1 else page2

        with patch.object(recent_press, "_http_get_json", side_effect=fake_call):
            items = recent_press.fetch_recent_news(
                ticker=None,
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                api_key="testkey",
            )
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(items), 4)


class TestHasThemeInPressFrame(unittest.TestCase):
    def test_matches_per_ticker_in_pre_fetched_frame(self):
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "id": "1",
                    "published_utc": "2026-05-10T14:30:00Z",
                    "title": "Beam quantum partnership",
                    "description": "",
                    "url": "u1",
                    "tickers": ["BEEM"],
                    "keywords": ["quantum"],
                    "publisher": "x",
                },
                {
                    "id": "2",
                    "published_utc": "2026-05-11T00:00:00Z",
                    "title": "NVDA earnings",
                    "description": "",
                    "url": "u2",
                    "tickers": ["NVDA"],
                    "keywords": ["earnings"],
                    "publisher": "y",
                },
            ]
        )
        self.assertTrue(
            recent_press.has_theme_in_press_frame(ticker="BEEM", keywords=["quantum"], press_df=df)
        )
        self.assertFalse(
            recent_press.has_theme_in_press_frame(ticker="NVDA", keywords=["quantum"], press_df=df)
        )

    def test_empty_frame_returns_false(self):
        import pandas as pd

        df = pd.DataFrame(
            columns=[
                "id",
                "published_utc",
                "title",
                "description",
                "url",
                "tickers",
                "keywords",
                "publisher",
            ]
        )
        self.assertFalse(
            recent_press.has_theme_in_press_frame(ticker="BEEM", keywords=["quantum"], press_df=df)
        )


if __name__ == "__main__":
    unittest.main()
