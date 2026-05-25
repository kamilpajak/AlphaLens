"""Tests for the canonical AlphaVantageClient.

Covers:
- query() builds the right URL and parses JSON
- API key resolution (constructor arg vs env var)
- AVRateLimitError raised on "Information" rate-limit / api-key / premium signals
- AVSchemaError raised on non-JSON, non-dict, or "Error Message" responses
- Throttling between calls when throttle_seconds > 0
- Lazy singleton get_default_av_client returns same instance per process

Network is never hit — every test injects a fake urlopen_fn callable.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch


def _fake_urlopen_with_body(body: str, *, status: int = 200):
    """Build a fake urlopen result with a context-manager interface matching
    ``urllib.request.urlopen``'s return value."""

    class _CM:
        def __enter__(self):
            class _Resp:
                def read(self):
                    return body.encode("utf-8")

            return _Resp()

        def __exit__(self, *exc):
            return False

    return MagicMock(return_value=_CM())


class TestQueryBuildsRequest(unittest.TestCase):
    def test_query_builds_url_with_function_and_params(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import AlphaVantageClient

        urlopen_fn = _fake_urlopen_with_body(json.dumps({"Symbol": "AAPL"}))

        client = AlphaVantageClient(api_key="DEMO", urlopen_fn=urlopen_fn)
        result = client.query("OVERVIEW", symbol="AAPL")

        self.assertEqual(result, {"Symbol": "AAPL"})
        urlopen_fn.assert_called_once()
        # The call positional arg is the URL string.
        url = urlopen_fn.call_args.args[0]
        self.assertIn("function=OVERVIEW", url)
        self.assertIn("symbol=AAPL", url)
        self.assertIn("apikey=DEMO", url)
        self.assertTrue(url.startswith("https://www.alphavantage.co/query?"))

    def test_constructor_api_key_required(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import AlphaVantageClient

        with self.assertRaises(ValueError):
            AlphaVantageClient(api_key="")

    def test_api_key_from_env_when_omitted(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import AlphaVantageClient

        urlopen_fn = _fake_urlopen_with_body(json.dumps({"Symbol": "AAPL"}))

        with patch.dict("os.environ", {"ALPHA_VANTAGE_API_KEY": "envkey"}, clear=False):
            client = AlphaVantageClient.from_env(urlopen_fn=urlopen_fn)
            client.query("OVERVIEW", symbol="AAPL")

        url = urlopen_fn.call_args.args[0]
        self.assertIn("apikey=envkey", url)

    def test_from_env_raises_when_env_missing(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import AlphaVantageClient

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                AlphaVantageClient.from_env()


class TestRateLimitDetection(unittest.TestCase):
    def test_rate_limit_information_raises(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import (
            AlphaVantageClient,
            AVRateLimitError,
        )

        body = json.dumps(
            {
                "Information": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."
            }
        )
        urlopen_fn = _fake_urlopen_with_body(body)

        client = AlphaVantageClient(api_key="DEMO", urlopen_fn=urlopen_fn)
        with self.assertRaises(AVRateLimitError):
            client.query("EARNINGS", symbol="AAPL")

    def test_api_key_information_raises(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import (
            AlphaVantageClient,
            AVRateLimitError,
        )

        body = json.dumps({"Information": "Invalid API key. Visit ..."})
        urlopen_fn = _fake_urlopen_with_body(body)

        client = AlphaVantageClient(api_key="DEMO", urlopen_fn=urlopen_fn)
        with self.assertRaises(AVRateLimitError):
            client.query("EARNINGS", symbol="AAPL")

    def test_premium_information_raises(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import (
            AlphaVantageClient,
            AVRateLimitError,
        )

        body = json.dumps({"Information": "This is a premium endpoint."})
        urlopen_fn = _fake_urlopen_with_body(body)

        client = AlphaVantageClient(api_key="DEMO", urlopen_fn=urlopen_fn)
        with self.assertRaises(AVRateLimitError):
            client.query("EARNINGS", symbol="AAPL")

    def test_benign_information_does_not_raise(self):
        """Not every 'Information' field is a quota signal — AV occasionally
        emits informational text alongside valid data. Only raise when the
        text matches a known rate-limit / api-key / premium phrase."""
        from alphalens_pipeline.data.alt_data.alphavantage_client import AlphaVantageClient

        body = json.dumps(
            {
                "Information": "Data retrieved successfully.",
                "Symbol": "AAPL",
            }
        )
        urlopen_fn = _fake_urlopen_with_body(body)

        client = AlphaVantageClient(api_key="DEMO", urlopen_fn=urlopen_fn)
        result = client.query("OVERVIEW", symbol="AAPL")
        self.assertEqual(result["Symbol"], "AAPL")


class TestSchemaValidation(unittest.TestCase):
    def test_non_json_body_raises(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import (
            AlphaVantageClient,
            AVSchemaError,
        )

        urlopen_fn = _fake_urlopen_with_body("<html>503 Service Unavailable</html>")
        client = AlphaVantageClient(api_key="DEMO", urlopen_fn=urlopen_fn)
        with self.assertRaises(AVSchemaError):
            client.query("OVERVIEW", symbol="AAPL")

    def test_non_dict_body_raises(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import (
            AlphaVantageClient,
            AVSchemaError,
        )

        # AV always returns dicts at top level; a list response is a vendor
        # contract violation we should refuse rather than silently degrade.
        urlopen_fn = _fake_urlopen_with_body(json.dumps([1, 2, 3]))
        client = AlphaVantageClient(api_key="DEMO", urlopen_fn=urlopen_fn)
        with self.assertRaises(AVSchemaError):
            client.query("OVERVIEW", symbol="AAPL")

    def test_error_message_raises(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import (
            AlphaVantageClient,
            AVSchemaError,
        )

        body = json.dumps(
            {"Error Message": "Invalid API call. Please retry or visit the documentation."}
        )
        urlopen_fn = _fake_urlopen_with_body(body)

        client = AlphaVantageClient(api_key="DEMO", urlopen_fn=urlopen_fn)
        with self.assertRaises(AVSchemaError):
            client.query("OVERVIEW", symbol="NOSUCHTICKER")


class TestThrottling(unittest.TestCase):
    def test_throttle_sleeps_between_calls_when_configured(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import AlphaVantageClient

        urlopen_fn = _fake_urlopen_with_body(json.dumps({"Symbol": "AAPL"}))
        sleep_fn = MagicMock()

        client = AlphaVantageClient(
            api_key="DEMO",
            urlopen_fn=urlopen_fn,
            throttle_seconds=1.5,
            sleep_fn=sleep_fn,
        )

        client.query("OVERVIEW", symbol="AAPL")
        client.query("OVERVIEW", symbol="MSFT")
        client.query("OVERVIEW", symbol="GOOG")

        # Three calls → sleep before the 2nd and 3rd (not before the 1st).
        self.assertEqual(sleep_fn.call_count, 2)
        # Sleep duration is (throttle - elapsed_since_last_call); real
        # monotonic adds a few microseconds of noise per call. ±0.01s is
        # generous and still validates the throttle is in the right ballpark.
        for call in sleep_fn.call_args_list:
            self.assertAlmostEqual(call.args[0], 1.5, places=2)

    def test_no_throttle_when_disabled(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import AlphaVantageClient

        urlopen_fn = _fake_urlopen_with_body(json.dumps({"Symbol": "AAPL"}))
        sleep_fn = MagicMock()

        client = AlphaVantageClient(
            api_key="DEMO",
            urlopen_fn=urlopen_fn,
            throttle_seconds=0,
            sleep_fn=sleep_fn,
        )
        client.query("OVERVIEW", symbol="AAPL")
        client.query("OVERVIEW", symbol="MSFT")

        sleep_fn.assert_not_called()


class TestDefaultClientSingleton(unittest.TestCase):
    def setUp(self):
        from alphalens_pipeline.data.alt_data import alphavantage_client as mod

        mod._reset_default_client_for_tests()

    def tearDown(self):
        from alphalens_pipeline.data.alt_data import alphavantage_client as mod

        mod._reset_default_client_for_tests()

    def test_get_default_returns_same_instance(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import get_default_av_client

        with patch.dict("os.environ", {"ALPHA_VANTAGE_API_KEY": "envkey"}, clear=False):
            c1 = get_default_av_client()
            c2 = get_default_av_client()
            self.assertIs(c1, c2)

    def test_get_default_raises_without_env(self):
        from alphalens_pipeline.data.alt_data.alphavantage_client import get_default_av_client

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                get_default_av_client()


if __name__ == "__main__":
    unittest.main()
