"""Unit tests for :mod:`alphalens_pipeline.data.alt_data.polygon_client`.

Mirrors the SecEdgar / AlphaVantage client tests in scope:
- Throttle enforces spacing between requests
- 429 retry with Retry-After parsing
- 5xx exponential backoff
- Bearer auth header on every request
- API key never appears in URL or pagination
- ``next_url`` ``apiKey`` query param stripped
- ``PolygonAuthError`` on 401, ``PolygonRateLimitError`` on persisted 429
- Lazy singleton via ``get_default_polygon_client``
- ``_reset_default_client_for_tests`` clears cached singleton
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Any
from unittest import mock

import requests
from alphalens_pipeline.data.alt_data.polygon_client import (
    PolygonAuthError,
    PolygonClient,
    PolygonError,
    PolygonRateLimitError,
    _reset_default_client_for_tests,
    _strip_apikey_from_url,
    get_default_polygon_client,
)


class _MockResponse:
    """Mimic the slice of ``requests.Response`` that PolygonClient uses."""

    def __init__(
        self,
        status_code: int,
        *,
        json_payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        text: str = "",
        content: bytes = b"",
    ):
        self.status_code = status_code
        self._json_payload = json_payload or {}
        self.headers = headers or {}
        self.text = text
        self.content = content

    def json(self) -> dict[str, Any]:
        return self._json_payload


def _ok(payload: dict[str, Any]) -> _MockResponse:
    return _MockResponse(200, json_payload=payload)


def _make_client(*, session: mock.Mock, sleep: mock.Mock | None = None) -> PolygonClient:
    """Helper: build a PolygonClient with mocked session + sleep."""
    return PolygonClient(
        "test-key",
        rate_limit_per_min=5,
        session=session,
        sleep=sleep or mock.Mock(),
    )


class StripApiKeyTests(unittest.TestCase):
    def test_apikey_removed_from_query(self):
        url = "https://api.polygon.io/v2/reference/news?cursor=abc&apiKey=secret"
        self.assertEqual(
            _strip_apikey_from_url(url),
            "https://api.polygon.io/v2/reference/news?cursor=abc",
        )

    def test_url_without_apikey_unchanged(self):
        url = "https://api.polygon.io/v2/reference/news?cursor=abc"
        self.assertEqual(_strip_apikey_from_url(url), url)

    def test_url_with_only_apikey_returns_path(self):
        url = "https://api.polygon.io/v2/reference/news?apiKey=secret"
        self.assertEqual(
            _strip_apikey_from_url(url),
            "https://api.polygon.io/v2/reference/news",
        )

    def test_url_without_query_unchanged(self):
        url = "https://api.polygon.io/v2/reference/news"
        self.assertEqual(_strip_apikey_from_url(url), url)

    def test_strip_is_case_insensitive(self):
        # Polygon currently uses ``apiKey`` exactly but third-party intermediaries
        # can echo with any casing. The stripper must catch all variants so
        # credentials never leak even on misbehaving proxies.
        for variant in ("apiKey", "apikey", "APIKEY", "ApiKey", "apiKEY"):
            url = f"https://api.polygon.io/v2/reference/news?cursor=abc&{variant}=secret"
            stripped = _strip_apikey_from_url(url)
            self.assertNotIn("secret", stripped, f"failed to strip variant {variant!r}")
            self.assertIn("cursor=abc", stripped)


class PolygonClientAuthTests(unittest.TestCase):
    def test_empty_api_key_rejected(self):
        with self.assertRaises(ValueError):
            PolygonClient("")

    def test_from_env_reads_polygon_api_key(self):
        with mock.patch.dict("os.environ", {"POLYGON_API_KEY": "abc"}, clear=False):
            client = PolygonClient.from_env()
            self.assertEqual(client._api_key, "abc")

    def test_from_env_raises_when_unset(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                PolygonClient.from_env()

    def test_bearer_header_on_every_request(self):
        session = mock.Mock()
        session.get.return_value = _ok({"results": []})
        client = _make_client(session=session)

        client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=1,
        )

        # Bearer header must be present on the actual call
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_api_key_never_in_url_or_params(self):
        session = mock.Mock()
        session.get.return_value = _ok({"results": []})
        client = _make_client(session=session)

        client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=1,
        )

        args, kwargs = session.get.call_args
        # URL string must not contain the api key
        self.assertNotIn("test-key", args[0])
        self.assertNotIn("apiKey", args[0])
        # Params dict must not contain the api key either
        params = kwargs.get("params") or {}
        self.assertNotIn("apiKey", params)
        self.assertNotIn("apikey", params)


class PolygonClientThrottleTests(unittest.TestCase):
    def test_throttle_spaces_back_to_back_requests(self):
        session = mock.Mock()
        session.get.return_value = _ok({"results": []})
        sleep = mock.Mock()
        client = PolygonClient("test-key", rate_limit_per_min=5, session=session, sleep=sleep)

        # First call should not sleep (no prior call), but subsequent calls
        # must throttle to >=12s spacing (5 req/min = 12s interval).
        for _ in range(3):
            client.get_news_range(
                start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
                end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
                max_pages=1,
            )

        # _throttle is called every request. The first will sleep 0 or near-0,
        # subsequent must sleep ~12s. Assert at least 2 non-zero sleep calls.
        positive_sleeps = [c.args[0] for c in sleep.call_args_list if c.args[0] > 1.0]
        self.assertGreaterEqual(
            len(positive_sleeps), 2, f"expected >=2 throttle sleeps, got {sleep.call_args_list}"
        )


class PolygonClientRetryTests(unittest.TestCase):
    def test_429_retries_with_retry_after_header(self):
        session = mock.Mock()
        session.get.side_effect = [
            _MockResponse(429, headers={"Retry-After": "5"}, text="rate limit"),
            _ok({"results": [{"id": "ok"}]}),
        ]
        sleep = mock.Mock()
        client = _make_client(session=session, sleep=sleep)

        items = client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=1,
        )

        self.assertEqual(items, [{"id": "ok"}])
        # Floor-clamp: Retry-After 5 should be raised to 13s minimum
        sleep_durations = [c.args[0] for c in sleep.call_args_list]
        self.assertIn(13, sleep_durations)

    def test_429_honors_retry_after_above_floor(self):
        session = mock.Mock()
        session.get.side_effect = [
            _MockResponse(429, headers={"Retry-After": "30"}, text="rate limit"),
            _ok({"results": []}),
        ]
        sleep = mock.Mock()
        client = _make_client(session=session, sleep=sleep)

        client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=1,
        )

        sleep_durations = [c.args[0] for c in sleep.call_args_list]
        # Honor the 30s signal verbatim
        self.assertIn(30, sleep_durations)

    def test_429_clamps_retry_after_at_ceiling(self):
        session = mock.Mock()
        session.get.side_effect = [
            _MockResponse(429, headers={"Retry-After": "300"}, text="rate limit"),
            _ok({"results": []}),
        ]
        sleep = mock.Mock()
        client = _make_client(session=session, sleep=sleep)

        client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=1,
        )

        sleep_durations = [c.args[0] for c in sleep.call_args_list]
        # Ceiling is 60s — 300s server signal must be clamped down
        self.assertIn(60, sleep_durations)
        self.assertNotIn(300, sleep_durations)

    def test_persistent_429_raises_polygon_rate_limit_error(self):
        session = mock.Mock()
        session.get.return_value = _MockResponse(
            429, headers={"Retry-After": "13"}, text="rate limit"
        )
        sleep = mock.Mock()
        client = _make_client(session=session, sleep=sleep)

        with self.assertRaises(PolygonRateLimitError):
            client.get_news_range(
                start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
                end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
                max_pages=1,
            )

    def test_5xx_exponential_backoff(self):
        session = mock.Mock()
        session.get.side_effect = [
            _MockResponse(500, text="server error"),
            _MockResponse(503, text="server error"),
            _ok({"results": []}),
        ]
        sleep = mock.Mock()
        client = _make_client(session=session, sleep=sleep)

        client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=1,
        )

        sleep_durations = [c.args[0] for c in sleep.call_args_list]
        # Sequential backoffs from _SERVER_ERROR_BACKOFFS = (5, 15, 30)
        self.assertIn(5, sleep_durations)
        self.assertIn(15, sleep_durations)

    def test_401_raises_polygon_auth_error_immediately(self):
        session = mock.Mock()
        session.get.return_value = _MockResponse(401, text="invalid api key")
        client = _make_client(session=session)

        with self.assertRaises(PolygonAuthError):
            client.get_news_range(
                start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
                end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
                max_pages=1,
            )

    def test_permanent_4xx_raises_polygon_error(self):
        session = mock.Mock()
        session.get.return_value = _MockResponse(403, text="forbidden")
        client = _make_client(session=session)

        with self.assertRaises(PolygonError):
            client.get_news_range(
                start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
                end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
                max_pages=1,
            )

    def test_transient_network_error_retries_then_succeeds(self):
        session = mock.Mock()
        session.get.side_effect = [
            requests.ConnectionError("transient"),
            _ok({"results": [{"id": "ok"}]}),
        ]
        sleep = mock.Mock()
        client = _make_client(session=session, sleep=sleep)

        items = client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=1,
        )
        self.assertEqual(items, [{"id": "ok"}])

    def test_exhausted_network_retries_raises_polygon_error(self):
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("never recovers")
        sleep = mock.Mock()
        client = _make_client(session=session, sleep=sleep)

        with self.assertRaises(PolygonError):
            client.get_news_range(
                start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
                end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
                max_pages=1,
            )


class PolygonClientPaginationTests(unittest.TestCase):
    def test_follows_next_url_and_strips_apikey(self):
        session = mock.Mock()
        session.get.side_effect = [
            _ok(
                {
                    "results": [{"id": "p1"}],
                    "next_url": "https://api.polygon.io/v2/reference/news?cursor=c2&apiKey=leaked",
                }
            ),
            _ok({"results": [{"id": "p2"}]}),
        ]
        client = _make_client(session=session)

        items = client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=10,
        )

        self.assertEqual(items, [{"id": "p1"}, {"id": "p2"}])
        # Second call URL must not contain the leaked apiKey
        second_call_args = session.get.call_args_list[1].args
        self.assertNotIn("apiKey", second_call_args[0])
        self.assertNotIn("leaked", second_call_args[0])

    def test_max_pages_terminates_pagination(self):
        session = mock.Mock()
        # Return a never-ending pagination cursor; client must stop at max_pages
        session.get.return_value = _ok(
            {
                "results": [{"id": "p"}],
                "next_url": "https://api.polygon.io/v2/reference/news?cursor=loop",
            }
        )
        client = _make_client(session=session)

        items = client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_pages=3,
        )

        self.assertEqual(len(session.get.call_args_list), 3)
        self.assertEqual(len(items), 3)

    def test_max_items_terminates_pagination(self):
        session = mock.Mock()
        # 5 pages × 2 items each = 10 items available, but max_items=4 → stop early
        page = _ok(
            {
                "results": [{"id": "a"}, {"id": "b"}],
                "next_url": "https://api.polygon.io/v2/reference/news?cursor=next",
            }
        )
        session.get.return_value = page
        client = _make_client(session=session)

        items = client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            max_items=4,
        )
        self.assertEqual(len(items), 4)

    def test_ticker_parameter_passed_through(self):
        session = mock.Mock()
        session.get.return_value = _ok({"results": []})
        client = _make_client(session=session)

        client.get_news_range(
            start=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
            ticker="AAPL",
            max_pages=1,
        )

        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["ticker"], "AAPL")

    def test_naive_datetime_rejected(self):
        client = _make_client(session=mock.Mock())
        with self.assertRaises(ValueError):
            client.get_news_range(
                start=dt.datetime(2026, 5, 1),  # naive, no tz
                end=dt.datetime(2026, 5, 2, tzinfo=dt.UTC),
                max_pages=1,
            )


class PolygonClientShortInterestTests(unittest.TestCase):
    def test_short_interest_paginated(self):
        session = mock.Mock()
        session.get.side_effect = [
            _ok(
                {
                    "results": [{"settlement_date": "2026-04-01", "short_interest": 100}],
                    "next_url": "https://api.polygon.io/stocks/v1/short-interest?cursor=p2",
                }
            ),
            _ok({"results": [{"settlement_date": "2026-04-15", "short_interest": 120}]}),
        ]
        client = _make_client(session=session)

        rows = client.get_short_interest(ticker="aapl", max_pages=10)
        self.assertEqual(len(rows), 2)
        # Ticker upper-cased before request
        first_params = session.get.call_args_list[0].kwargs.get("params") or {}
        self.assertEqual(first_params["ticker"], "AAPL")


class PolygonClientOptionsContractsTests(unittest.TestCase):
    def test_options_contracts_passthrough_params(self):
        session = mock.Mock()
        session.get.return_value = _ok({"results": []})
        client = _make_client(session=session)

        client.get_options_contracts(
            underlying_ticker="QUBT", expired="false", limit=100, max_pages=1
        )

        _, kwargs = session.get.call_args
        params = kwargs.get("params") or {}
        self.assertEqual(params["underlying_ticker"], "QUBT")
        self.assertEqual(params["expired"], "false")
        self.assertEqual(params["limit"], 100)


class PolygonClientGetAggRangeTests(unittest.TestCase):
    """``get_agg_range`` minute-bar aggregates (Track A v2 PR-3 shadow return)."""

    _START = dt.datetime(2026, 5, 29, 13, 30, tzinfo=dt.UTC)
    _END = dt.datetime(2026, 5, 29, 14, 0, tzinfo=dt.UTC)

    def test_builds_path_with_ms_epoch_and_uppercases_ticker(self):
        session = mock.Mock()
        session.get.return_value = _ok({"results": [{"t": 1, "c": 100.0, "v": 10}]})
        client = _make_client(session=session)

        bars = client.get_agg_range(ticker="aapl", start=self._START, end=self._END)

        self.assertEqual(bars, [{"t": 1, "c": 100.0, "v": 10}])
        url = session.get.call_args_list[0].args[0]
        # Ticker upper-cased; from/to are millisecond epochs in the path.
        from_ms = int(self._START.timestamp() * 1000)
        to_ms = int(self._END.timestamp() * 1000)
        self.assertIn(f"/v2/aggs/ticker/AAPL/range/1/minute/{from_ms}/{to_ms}", url)

    def test_default_query_params(self):
        session = mock.Mock()
        session.get.return_value = _ok({"results": []})
        client = _make_client(session=session)

        client.get_agg_range(ticker="AAPL", start=self._START, end=self._END)

        params = session.get.call_args_list[0].kwargs.get("params") or {}
        # adjusted=false (raw intraday fidelity), ascending, max page size.
        self.assertEqual(params["adjusted"], "false")
        self.assertEqual(params["sort"], "asc")
        self.assertEqual(params["limit"], 50000)

    def test_naive_datetime_rejected(self):
        client = _make_client(session=mock.Mock())
        with self.assertRaises(ValueError):
            client.get_agg_range(
                ticker="AAPL", start=dt.datetime(2026, 5, 29, 13, 30), end=self._END
            )

    def test_empty_results_returns_empty_list_not_raise(self):
        # A name with no bars in the window is a legitimate skip handled by the
        # caller — must NOT raise.
        session = mock.Mock()
        session.get.return_value = _ok({"resultsCount": 0})  # no "results" key
        client = _make_client(session=session)
        self.assertEqual(client.get_agg_range(ticker="AAPL", start=self._START, end=self._END), [])

    def test_follows_next_url_and_strips_apikey(self):
        session = mock.Mock()
        session.get.side_effect = [
            _ok(
                {
                    "results": [{"t": 1, "c": 100.0, "v": 10}],
                    "next_url": "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/minute/1/2?cursor=c2&apiKey=leaked",
                }
            ),
            _ok({"results": [{"t": 2, "c": 101.0, "v": 20}]}),
        ]
        client = _make_client(session=session)

        bars = client.get_agg_range(ticker="AAPL", start=self._START, end=self._END, max_pages=10)
        self.assertEqual(len(bars), 2)
        second_url = session.get.call_args_list[1].args[0]
        self.assertNotIn("apiKey", second_url)
        self.assertNotIn("leaked", second_url)

    def test_max_pages_terminates_pagination(self):
        session = mock.Mock()
        session.get.return_value = _ok(
            {
                "results": [{"t": 1, "c": 100.0, "v": 10}],
                "next_url": "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/minute/1/2?cursor=loop",
            }
        )
        client = _make_client(session=session)
        bars = client.get_agg_range(ticker="AAPL", start=self._START, end=self._END, max_pages=3)
        self.assertEqual(len(session.get.call_args_list), 3)
        self.assertEqual(len(bars), 3)


class PolygonClientGetJsonEscapeHatchTests(unittest.TestCase):
    def test_get_json_returns_raw_payload(self):
        session = mock.Mock()
        session.get.return_value = _ok({"status": "OK", "results": [1, 2, 3]})
        client = _make_client(session=session)

        payload = client.get_json("https://api.polygon.io/v3/reference/tickers")
        self.assertEqual(payload, {"status": "OK", "results": [1, 2, 3]})


class GetDefaultClientSingletonTests(unittest.TestCase):
    def setUp(self):
        _reset_default_client_for_tests()

    def tearDown(self):
        _reset_default_client_for_tests()

    def test_singleton_returns_same_instance(self):
        with mock.patch.dict("os.environ", {"POLYGON_API_KEY": "abc"}, clear=False):
            c1 = get_default_polygon_client()
            c2 = get_default_polygon_client()
            self.assertIs(c1, c2)

    def test_reset_clears_singleton(self):
        with mock.patch.dict("os.environ", {"POLYGON_API_KEY": "abc"}, clear=False):
            c1 = get_default_polygon_client()
            _reset_default_client_for_tests()
            c2 = get_default_polygon_client()
            self.assertIsNot(c1, c2)


if __name__ == "__main__":
    unittest.main()
