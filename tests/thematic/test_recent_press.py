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
        # Polygon returned successfully with zero items — real "no press in
        # window" signal, distinct from a fetch error. Stays False per tri-state.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(recent_press, "_http_get_json", return_value={"results": []}):
                result = recent_press.has_theme_in_recent_press(
                    ticker="UNKN",
                    asof=dt.date(2026, 5, 15),
                    keywords=["anything"],
                    api_key="testkey",
                    cache_dir=cache_dir,
                )
                self.assertIs(result, False)

    def test_has_theme_returns_none_on_api_error(self):
        # Polygon rate limit / network error = unknown, not False. Operator
        # can distinguish "we couldn't check" from "we checked and no hit".
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(
                recent_press, "_http_get_json", side_effect=RuntimeError("rate limit")
            ):
                self.assertIsNone(
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
    """Tri-state semantics for the in-memory frame matcher.

    - ``True``  — ticker has rows in the frame AND a keyword hit.
    - ``False`` — ticker has rows in the frame, NO keyword hit (real "no").
    - ``None``  — ticker has NO rows in the frame (we don't know; the
      orchestrator should fall back to a per-ticker fetch). Also returned
      when the frame is empty.

    The None case prevents silent false-negatives when Polygon's batch
    firehose fails to tag a ticker on articles that do mention it.
    """

    @staticmethod
    def _two_ticker_frame():
        import pandas as pd

        return pd.DataFrame(
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

    def test_returns_true_when_ticker_present_and_keyword_hits(self):
        df = self._two_ticker_frame()
        self.assertIs(
            recent_press.has_theme_in_press_frame(ticker="BEEM", keywords=["quantum"], press_df=df),
            True,
        )

    def test_returns_false_when_ticker_present_but_keyword_misses(self):
        # NVDA HAS rows in the frame but none mention "quantum" — this is a
        # real "no" (we checked, didn't find), distinct from "we couldn't
        # check because no rows for this ticker". Stays False.
        df = self._two_ticker_frame()
        self.assertIs(
            recent_press.has_theme_in_press_frame(ticker="NVDA", keywords=["quantum"], press_df=df),
            False,
        )

    def test_returns_none_when_ticker_absent_from_frame(self):
        # Polygon's batch firehose did not tag this ticker on any article in
        # the window — we have no evidence either way. Return None so the
        # orchestrator falls back to a per-ticker fetch instead of treating
        # the silence as a real "no". (Issue #149 root cause.)
        df = self._two_ticker_frame()
        self.assertIsNone(
            recent_press.has_theme_in_press_frame(ticker="VRT", keywords=["quantum"], press_df=df)
        )

    def test_empty_frame_returns_none(self):
        # An empty frame is equivalent to "no rows for this ticker" — no
        # evidence, fall through to per-ticker.
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
        self.assertIsNone(
            recent_press.has_theme_in_press_frame(ticker="BEEM", keywords=["quantum"], press_df=df)
        )

    def test_empty_keywords_returns_none(self):
        # Defensive: caller passed an empty keyword iterable. We can't say
        # "no" (we never matched anything) — return None, not False.
        df = self._two_ticker_frame()
        self.assertIsNone(
            recent_press.has_theme_in_press_frame(ticker="BEEM", keywords=[], press_df=df)
        )


class TestFetchRecentNewsPagination(unittest.TestCase):
    def test_paginates_past_old_max_pages_cap_of_ten(self):
        # Regression test for issue #149 bug 2: the previous cap of 10 pages
        # × 100 limit = 1000 rows covered ~3 days on Polygon's US firehose,
        # NOT the intended 30-day lookback. Bump to 200 pages should let the
        # fetcher follow next_url until the window naturally exhausts.
        pages_to_serve = 15  # ten was the old ceiling

        def fake_call(url, **kwargs):
            calls.append(url)
            page_n = len(calls)
            # Each page returns one article so we can count rows precisely.
            result = {
                "results": [
                    {
                        "id": f"p{page_n}",
                        "published_utc": "2026-05-01T00:00:00Z",
                        "title": f"page {page_n}",
                        "description": "",
                        "tickers": ["BEEM"],
                        "keywords": [],
                        "article_url": f"https://example.com/{page_n}",
                        "publisher": {"name": "x"},
                    }
                ]
            }
            if page_n < pages_to_serve:
                result["next_url"] = f"https://api.polygon.io/v2/reference/news?cursor={page_n}"
            return result

        calls: list[str] = []
        with patch.object(recent_press, "_http_get_json", side_effect=fake_call):
            items = recent_press.fetch_recent_news(
                ticker=None,
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                api_key="testkey",
            )
        self.assertEqual(
            len(items),
            pages_to_serve,
            f"expected all {pages_to_serve} pages fetched; got {len(items)}",
        )
        self.assertEqual(len(calls), pages_to_serve)


if __name__ == "__main__":
    unittest.main()
