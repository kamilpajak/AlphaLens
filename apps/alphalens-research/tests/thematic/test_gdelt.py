import datetime as dt
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from alphalens_pipeline.thematic.sources import gdelt
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS

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

# In-window variant for the fetch_daily_news tests: both articles dated on the
# target day (2026-05-15) so the P1a post-fetch window filter keeps them. The
# module-level SAMPLE_GDELT_RESPONSE intentionally spans two days to exercise
# the transform / parse path; the daily-fetch dedup/iteration tests need both
# rows inside the strict [date, date+1) window.
SAMPLE_GDELT_RESPONSE_IN_WINDOW = {
    "articles": [
        {**SAMPLE_GDELT_RESPONSE["articles"][0], "seendate": "20260515T011500Z"},
        {**SAMPLE_GDELT_RESPONSE["articles"][1], "seendate": "20260515T184500Z"},
    ]
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

    def test_transform_drops_missing_seendate(self):
        """Articles without seendate are dropped (cannot verify window membership).

        P1a: the old ``pd.Timestamp.now(tz="UTC")`` fallback would inject an
        out-of-window timestamp under explicit bounds, so the row is dropped
        instead.
        """
        articles = [
            {
                "url": "https://with-seendate.test",
                "title": "Has date",
                "seendate": "20260515T120000Z",
            },
            {
                "url": "https://no-seendate.test",
                "title": "No date",
                # missing seendate
            },
        ]
        df = gdelt.transform(articles, theme="test")
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["url"], "https://with-seendate.test")


class TestGdeltQueryBuilder(unittest.TestCase):
    def test_build_url_includes_required_params(self):
        url = gdelt.build_query_url(
            query="NVIDIA AND quantum",
            maxrecords=50,
        )
        self.assertIn("api.gdeltproject.org/api/v2/doc/doc", url)
        self.assertIn("query=NVIDIA+AND+quantum", url)
        self.assertIn("maxrecords=50", url)
        self.assertIn("mode=artlist", url)
        self.assertIn("format=json", url)
        # P1a: timespan removed entirely in favour of explicit datetime bounds.
        self.assertNotIn("timespan", url)

    def test_build_query_url_with_explicit_datetimes(self):
        """Verify startdatetime and enddatetime are URL-encoded when present."""
        url = gdelt.build_query_url(
            query="NVIDIA",
            startdatetime="20260515000000",
            enddatetime="20260516000000",
        )
        self.assertIn("startdatetime=20260515000000", url)
        self.assertIn("enddatetime=20260516000000", url)
        self.assertNotIn("timespan", url)

    def test_build_query_url_without_datetimes_omits_both(self):
        """If either datetime is None, neither startdatetime nor enddatetime appears."""
        url = gdelt.build_query_url(
            query="NVIDIA",
            startdatetime=None,
            enddatetime="20260516000000",
        )
        self.assertNotIn("startdatetime", url)
        self.assertNotIn("enddatetime", url)


class TestGdeltDatetimeFormatting(unittest.TestCase):
    def test_format_datetime_for_gdelt_utc(self):
        """Format UTC datetime to YYYYMMDDHHMMSS."""
        dt_ = dt.datetime(2026, 5, 15, 13, 45, 30, tzinfo=dt.UTC)
        result = gdelt._format_datetime_for_gdelt(dt_)
        self.assertEqual(result, "20260515134530")

    def test_format_datetime_for_gdelt_rejects_naive(self):
        """Reject naive (no timezone) datetime."""
        dt_ = dt.datetime(2026, 5, 15, 13, 45, 30)
        with self.assertRaises(ValueError):
            gdelt._format_datetime_for_gdelt(dt_)

    def test_format_datetime_for_gdelt_converts_non_utc_to_utc(self):
        """A tz-aware non-UTC datetime is converted to UTC before formatting."""
        eastern = dt.timezone(dt.timedelta(hours=-5))
        dt_ = dt.datetime(2026, 5, 15, 8, 45, 30, tzinfo=eastern)  # 13:45:30 UTC
        result = gdelt._format_datetime_for_gdelt(dt_)
        self.assertEqual(result, "20260515134530")


class TestGdeltFetch(unittest.TestCase):
    def test_fetch_theme_calls_endpoint_and_returns_frame(self):
        # Window spans both sample seendates (2026-05-14 18:45 and 2026-05-15 01:15).
        start = dt.datetime(2026, 5, 14, 0, 0, 0, tzinfo=dt.UTC)
        end = dt.datetime(2026, 5, 16, 0, 0, 0, tzinfo=dt.UTC)
        with patch.object(gdelt, "_http_get_json", return_value=SAMPLE_GDELT_RESPONSE):
            df = gdelt.fetch_theme(
                theme="ai_quantum",
                query='("CUDA" OR "quantum computing") AND (NVIDIA OR IBM)',
                start=start,
                end=end,
            )
        self.assertEqual(len(df), 2)
        self.assertTrue((df["source"] == "gdelt").all())

    def test_fetch_theme_post_filters_seendate_outside_window(self):
        """Articles with seendate outside [start, end) are dropped post-fetch."""
        articles = [
            {
                "url": "https://in-window.test",
                "title": "In window",
                "seendate": "20260515T120000Z",
            },
            {
                "url": "https://before-window.test",
                "title": "Before window",
                "seendate": "20260514T235959Z",  # just before window
            },
            {
                "url": "https://after-window.test",
                "title": "After window",
                "seendate": "20260516T000001Z",  # just after window
            },
        ]
        start = dt.datetime(2026, 5, 15, 0, 0, 0, tzinfo=dt.UTC)
        end = dt.datetime(2026, 5, 16, 0, 0, 0, tzinfo=dt.UTC)

        with patch.object(gdelt, "_http_get_json", return_value={"articles": articles}):
            df = gdelt.fetch_theme(
                theme="test",
                query="test query",
                start=start,
                end=end,
            )

        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["url"], "https://in-window.test")

    def test_fetch_daily_news_window_is_midnight_to_midnight_utc(self):
        """fetch_daily_news(date) uses [date 00:00 UTC, date+1 00:00 UTC)."""
        target_date = dt.date(2026, 5, 15)
        expected_start = dt.datetime(2026, 5, 15, 0, 0, 0, tzinfo=dt.UTC)
        expected_end = dt.datetime(2026, 5, 16, 0, 0, 0, tzinfo=dt.UTC)

        calls = []

        def fake_fetch_theme(**kwargs):
            calls.append(kwargs)
            return gdelt.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(gdelt, "fetch_theme", side_effect=fake_fetch_theme):
                gdelt.fetch_daily_news(
                    date=target_date,
                    theme_buckets={"test": "query"},
                    cache_dir=Path(tmpdir),
                    inter_query_sleep_sec=0,
                )

        self.assertEqual(calls[0]["start"], expected_start)
        self.assertEqual(calls[0]["end"], expected_end)

    def test_fetch_daily_news_iterates_all_themes(self):
        calls = []

        def fake_call(url, **kwargs):
            calls.append(url)
            return SAMPLE_GDELT_RESPONSE_IN_WINDOW

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
            return SAMPLE_GDELT_RESPONSE_IN_WINDOW

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
            with patch.object(
                gdelt, "_http_get_json", return_value=SAMPLE_GDELT_RESPONSE_IN_WINDOW
            ):
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


class _FakeResponse:
    """Minimal context-manager stand-in for the object returned by urlopen."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


class TestGdeltHttpGetJson(unittest.TestCase):
    def test_phrase_too_short_raises_immediately_without_retry(self):
        body = b"The specified phrase is too short.\n"
        mock_urlopen = MagicMock(return_value=_FakeResponse(body))
        with patch("urllib.request.urlopen", mock_urlopen):
            with self.assertRaises(gdelt.GdeltQueryError) as ctx:
                gdelt._http_get_json("https://example.invalid/q", backoff_sec=0.01)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertIn("phrase is too short", str(ctx.exception))

    def test_html_error_page_raises_immediately_without_retry(self):
        body = b"<html><body>service unavailable</body></html>"
        mock_urlopen = MagicMock(return_value=_FakeResponse(body))
        with patch("urllib.request.urlopen", mock_urlopen):
            with self.assertRaises(gdelt.GdeltQueryError):
                gdelt._http_get_json("https://example.invalid/q", backoff_sec=0.01)
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_empty_body_still_retries(self):
        mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
        with patch("urllib.request.urlopen", mock_urlopen):
            with self.assertRaises(gdelt.GdeltMaxRetriesError):
                gdelt._http_get_json(
                    "https://example.invalid/q",
                    backoff_sec=0.001,
                    max_attempts=3,
                )
        self.assertEqual(mock_urlopen.call_count, 3)

    def test_leading_whitespace_does_not_trigger_permanent_error(self):
        body = b'\n  {"articles": []}\n'
        mock_urlopen = MagicMock(return_value=_FakeResponse(body))
        with patch("urllib.request.urlopen", mock_urlopen):
            data = gdelt._http_get_json("https://example.invalid/q", backoff_sec=0.01)
        self.assertEqual(data, {"articles": []})

    def test_valid_json_returns_payload(self):
        body = b'{"articles": [{"url": "https://x.test"}]}'
        mock_urlopen = MagicMock(return_value=_FakeResponse(body))
        with patch("urllib.request.urlopen", mock_urlopen):
            data = gdelt._http_get_json("https://example.invalid/q", backoff_sec=0.01)
        self.assertEqual(data["articles"][0]["url"], "https://x.test")


class TestGdeltFetchIsolation(unittest.TestCase):
    def test_fetch_daily_news_isolates_failing_bucket(self):
        # Articles dated on the 2026-05-18 target day so the P1a window keeps them.
        good = {
            "articles": [
                {**SAMPLE_GDELT_RESPONSE["articles"][0], "seendate": "20260518T011500Z"},
                {**SAMPLE_GDELT_RESPONSE["articles"][1], "seendate": "20260518T184500Z"},
            ]
        }
        side_effects = [
            gdelt.GdeltQueryError("phrase too short"),
            good,
            urllib.error.URLError("ssl handshake timeout"),
            good,
        ]
        calls = {"n": 0}

        def fake_call(url, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            result = side_effects[i]
            if isinstance(result, Exception):
                raise result
            return result

        fixture = {
            "broken_a": "bad query a",
            "ok_a": "good query a",
            "broken_b": "bad query b",
            "ok_b": "good query b",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(gdelt, "_http_get_json", side_effect=fake_call):
                df = gdelt.fetch_daily_news(
                    date=dt.date(2026, 5, 18),
                    theme_buckets=fixture,
                    cache_dir=Path(tmpdir),
                    inter_query_sleep_sec=0,
                )
            self.assertEqual(calls["n"], 4)
            # Two ok buckets returned the same SAMPLE — dedup leaves 2 unique rows.
            self.assertEqual(df["id"].nunique(), 2)
            cached = Path(tmpdir) / "2026-05-18.parquet"
            self.assertTrue(cached.exists())

    def test_fetch_daily_news_all_buckets_fail_writes_empty_cache(self):
        def boom(url, **kwargs):
            raise gdelt.GdeltQueryError("phrase too short")

        fixture = {"a": "x", "b": "y"}
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(gdelt, "_http_get_json", side_effect=boom):
                df = gdelt.fetch_daily_news(
                    date=dt.date(2026, 5, 18),
                    theme_buckets=fixture,
                    cache_dir=Path(tmpdir),
                    inter_query_sleep_sec=0,
                )
            self.assertEqual(len(df), 0)
            self.assertEqual(list(df.columns), NEWS_COLUMNS)
            self.assertTrue((Path(tmpdir) / "2026-05-18.parquet").exists())


class TestGdeltThemesYamlWellFormed(unittest.TestCase):
    """Static lint of the shipped YAML: no single-word quoted phrases.

    GDELT DOC API rejects quoted phrases shorter than a small threshold
    (empirically: a single short token like ``"CUDA"`` or ``"LLM"`` triggers
    ``HTTP 200 + "The specified phrase is too short."``). A live smoke is in
    ``test_gdelt_live.py`` (opt-in); this test catches the most common bug
    class offline so YAML edits don't ship broken queries.
    """

    def test_no_single_word_quoted_phrases(self):
        import re

        buckets = gdelt.load_theme_buckets()
        offenders: list[tuple[str, str]] = []
        for theme, query in buckets.items():
            for phrase in re.findall(r'"([^"]+)"', query):
                if len(phrase.split()) < 2:
                    offenders.append((theme, phrase))
        self.assertEqual(
            offenders,
            [],
            f"Single-word quoted phrases will trip GDELT 'phrase too short': {offenders}",
        )


class TestGdeltTitleCleaning(unittest.TestCase):
    """GDELT space-pads punctuation in titles (~25% of rows); clean at ingest."""

    def test_drops_space_before_closing_punctuation(self):
        self.assertEqual(
            gdelt.clean_title("California urges drivers to avoid Chevron over gas prices . "),
            "California urges drivers to avoid Chevron over gas prices.",
        )

    def test_drops_spaces_inside_parentheses(self):
        self.assertEqual(
            gdelt.clean_title("Druckenmiller Dumped Alphabet ( Google ) and Bought AI"),
            "Druckenmiller Dumped Alphabet (Google) and Bought AI",
        )

    def test_collapses_internal_runs_and_strips(self):
        self.assertEqual(gdelt.clean_title("  Foo   bar ,  baz  "), "Foo bar, baz")

    def test_clean_title_is_idempotent_noop(self):
        clean = "Apple unveils M5 chip (finally) — analysts cheer."
        self.assertEqual(gdelt.clean_title(clean), clean)

    def test_empty_stays_empty(self):
        self.assertEqual(gdelt.clean_title(""), "")

    def test_handles_multi_space_runs_before_punctuation(self):
        # Pins greedy \s+ behaviour: runs of >1 space adjacent to punctuation
        # are consumed whole, so step order vs the run-collapse pass is moot.
        self.assertEqual(
            gdelt.clean_title("A title ( with parens )  . Final"),
            "A title (with parens). Final",
        )

    def test_drops_space_before_percent(self):
        # GDELT splits "33%" into "33 %"; no legitimate headline has a space
        # before percent (different from currency/units which keep the space).
        self.assertEqual(
            gdelt.clean_title("Stock rockets 33 % on $100M backing"),
            "Stock rockets 33% on $100M backing",
        )

    def test_collapses_hyphen_inside_compound_word(self):
        # GDELT tokenizes compound names like "D-Wave" into "D - Wave". Real
        # text never has a space-padded hyphen between alphanumeric tokens
        # (sentence em-dashes are typed as "—" or written with spaces around
        # a real em-dash, NOT a hyphen).
        self.assertEqual(
            gdelt.clean_title("D - Wave (QBTS) Rockets 33 % as US Govt Places $100M Backing"),
            "D-Wave (QBTS) Rockets 33% as US Govt Places $100M Backing",
        )

    def test_collapses_hyphen_in_brand_name(self):
        self.assertEqual(
            gdelt.clean_title("Coca - Cola tops earnings estimates"),
            "Coca-Cola tops earnings estimates",
        )

    def test_collapses_hyphen_in_iso_date(self):
        self.assertEqual(
            gdelt.clean_title("Filing dated 2026 - 05 - 27 hits EDGAR"),
            "Filing dated 2026-05-27 hits EDGAR",
        )

    def test_collapses_single_char_hyphen_chain(self):
        # Pins the two-pass mechanism: a single-character chain "A - B - C"
        # needs both substitution passes. Pass 1 consumes B in the A-B
        # match, leaving " - C" stranded; pass 2 catches B-C. ISO date
        # already converges in one pass (different code path), so this is
        # the canonical regression test for dropping the 2nd .sub() call.
        self.assertEqual(
            gdelt.clean_title("A - B - C ratings cut"),
            "A-B-C ratings cut",
        )

    def test_preserves_em_dash_separator(self):
        # An em-dash separator (U+2014) between sentence clauses is NOT a
        # GDELT artefact and must survive untouched.
        self.assertEqual(
            gdelt.clean_title("Apple unveils M5 — analysts cheer"),
            "Apple unveils M5 — analysts cheer",
        )


if __name__ == "__main__":
    unittest.main()
