"""Tests for the canonical :class:`GdeltClient`.

GDELT is keyless/free, but the canonical-client doctrine still applies: one
shared HTTP + retry + permanent-vs-transient seam so the live thematic news
ingest can't grow a second uncoordinated ``urlopen`` against the DOC API. These
tests cover the URL builder, the URL-keyed ``_http_get_json`` fetch (the golden
cassette's patch seam), and the ``GdeltClient.fetch_doc`` wrapper.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from alphalens_pipeline.data.alt_data import gdelt_client


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


class TestBuildQueryUrl(unittest.TestCase):
    def test_build_url_includes_required_params(self):
        url = gdelt_client.build_query_url(query="NVIDIA AND quantum", maxrecords=50)
        self.assertIn("api.gdeltproject.org/api/v2/doc/doc", url)
        self.assertIn("query=NVIDIA+AND+quantum", url)
        self.assertIn("maxrecords=50", url)
        self.assertIn("mode=artlist", url)
        self.assertIn("format=json", url)
        # P1a: timespan removed entirely in favour of explicit datetime bounds.
        self.assertNotIn("timespan", url)

    def test_build_query_url_with_explicit_datetimes(self):
        url = gdelt_client.build_query_url(
            query="NVIDIA",
            startdatetime="20260515000000",
            enddatetime="20260516000000",
        )
        self.assertIn("startdatetime=20260515000000", url)
        self.assertIn("enddatetime=20260516000000", url)
        self.assertNotIn("timespan", url)

    def test_build_query_url_without_datetimes_omits_both(self):
        url = gdelt_client.build_query_url(
            query="NVIDIA",
            startdatetime=None,
            enddatetime="20260516000000",
        )
        self.assertNotIn("startdatetime", url)
        self.assertNotIn("enddatetime", url)


class TestHttpGetJson(unittest.TestCase):
    def test_phrase_too_short_raises_immediately_without_retry(self):
        body = b"The specified phrase is too short.\n"
        mock_urlopen = MagicMock(return_value=_FakeResponse(body))
        with patch("urllib.request.urlopen", mock_urlopen):
            with self.assertRaises(gdelt_client.GdeltQueryError) as ctx:
                gdelt_client._http_get_json("https://example.invalid/q", backoff_sec=0.01)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertIn("phrase is too short", str(ctx.exception))

    def test_html_error_page_raises_immediately_without_retry(self):
        body = b"<html><body>service unavailable</body></html>"
        mock_urlopen = MagicMock(return_value=_FakeResponse(body))
        with patch("urllib.request.urlopen", mock_urlopen):
            with self.assertRaises(gdelt_client.GdeltQueryError):
                gdelt_client._http_get_json("https://example.invalid/q", backoff_sec=0.01)
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_empty_body_still_retries(self):
        mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
        with patch("urllib.request.urlopen", mock_urlopen):
            with self.assertRaises(gdelt_client.GdeltMaxRetriesError):
                gdelt_client._http_get_json(
                    "https://example.invalid/q", backoff_sec=0.001, max_attempts=3
                )
        self.assertEqual(mock_urlopen.call_count, 3)

    def test_leading_whitespace_does_not_trigger_permanent_error(self):
        body = b'\n  {"articles": []}\n'
        mock_urlopen = MagicMock(return_value=_FakeResponse(body))
        with patch("urllib.request.urlopen", mock_urlopen):
            data = gdelt_client._http_get_json("https://example.invalid/q", backoff_sec=0.01)
        self.assertEqual(data, {"articles": []})

    def test_valid_json_returns_payload(self):
        body = b'{"articles": [{"url": "https://x.test"}]}'
        mock_urlopen = MagicMock(return_value=_FakeResponse(body))
        with patch("urllib.request.urlopen", mock_urlopen):
            data = gdelt_client._http_get_json("https://example.invalid/q", backoff_sec=0.01)
        self.assertEqual(data["articles"][0]["url"], "https://x.test")


class TestFetchDoc(unittest.TestCase):
    def test_fetch_doc_builds_url_and_returns_payload(self):
        captured = {}

        def fake_http(url, **kwargs):
            captured["url"] = url
            return {"articles": [{"url": "https://x.test"}]}

        with patch.object(gdelt_client, "_http_get_json", side_effect=fake_http):
            data = gdelt_client.GdeltClient().fetch_doc(
                query="NVIDIA",
                startdatetime="20260515000000",
                enddatetime="20260516000000",
                maxrecords=50,
            )
        self.assertEqual(data["articles"][0]["url"], "https://x.test")
        self.assertIn("query=NVIDIA", captured["url"])
        self.assertIn("startdatetime=20260515000000", captured["url"])
        self.assertIn("maxrecords=50", captured["url"])

    def test_fetch_doc_threads_client_retry_config(self):
        seen = {}

        def fake_http(url, **kwargs):
            seen.update(kwargs)
            return {"articles": []}

        with patch.object(gdelt_client, "_http_get_json", side_effect=fake_http):
            gdelt_client.GdeltClient(timeout=5.0, max_attempts=2, backoff_sec=0.5).fetch_doc(
                query="x"
            )
        self.assertEqual(seen["timeout"], 5.0)
        self.assertEqual(seen["max_attempts"], 2)
        self.assertEqual(seen["backoff_sec"], 0.5)


class TestSingleton(unittest.TestCase):
    def setUp(self):
        gdelt_client._reset_default_client_for_tests()

    def tearDown(self):
        gdelt_client._reset_default_client_for_tests()

    def test_default_is_cached(self):
        self.assertIs(
            gdelt_client.get_default_gdelt_client(), gdelt_client.get_default_gdelt_client()
        )

    def test_reset_clears_singleton(self):
        first = gdelt_client.get_default_gdelt_client()
        gdelt_client._reset_default_client_for_tests()
        self.assertIsNot(first, gdelt_client.get_default_gdelt_client())


if __name__ == "__main__":
    unittest.main()
