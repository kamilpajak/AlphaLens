"""Hermetic tests for the canonical SaxoClient (``brokers/saxo/client.py``).

Everything runs against a recording fake session — no network, no token. Pins
the transport contract the house one-canonical-client doctrine cares about:

- Bearer auth in the header ONLY (token never leaks into URLs or params)
- ``x-request-id`` on every request, stable across retries of one logical
  request, unique across logical requests (P2's duplicate-order dedup seam)
- proactive 0.5s min-interval throttle via the injected ``sleep``
- 429 -> Retry-After honored, clamped into [1, 120]
- 5xx -> (5, 15, 30) backoffs, then ``SaxoError``
- 401 -> ``invalidate()`` + retry once with a fresh token, then ``SaxoAuthError``
- ``StaticTokenProvider.from_env`` without ``SAXO_SIM_TOKEN`` -> ``SaxoAuthError``
"""

from __future__ import annotations

import unittest
import uuid
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.saxo.client import (
    SIM_BASE_URL,
    SaxoAuthError,
    SaxoClient,
    SaxoError,
    SaxoRateLimitError,
)
from alphalens_pipeline.brokers.saxo.tokens import TOKEN_ENV, StaticTokenProvider


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingSession:
    """Returns queued responses in order; repeats the last one when drained."""

    def __init__(self, responses: list[_FakeResponse] | None = None):
        self.responses = list(responses or [_FakeResponse()])
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.calls.append(
            {"url": url, "headers": dict(headers or {}), "params": params, "timeout": timeout}
        )
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class _StubTokenProvider:
    """Static token by default; a queue of tokens simulates a refresh."""

    def __init__(self, tokens: tuple[str, ...] = ("tok-static",)):
        self._tokens = list(tokens)
        self.invalidations = 0
        self.issued: list[str] = []

    def get_access_token(self) -> str:
        token = self._tokens[0] if len(self._tokens) == 1 else self._tokens.pop(0)
        self.issued.append(token)
        return token

    def invalidate(self) -> None:
        self.invalidations += 1


def _make_client(
    session: _RecordingSession,
    *,
    tokens: tuple[str, ...] = ("tok-static",),
) -> tuple[SaxoClient, _StubTokenProvider, list[float]]:
    provider = _StubTokenProvider(tokens)
    sleeps: list[float] = []
    client = SaxoClient(
        provider,
        session=session,  # type: ignore[arg-type]
        sleep=sleeps.append,
    )
    return client, provider, sleeps


class TestAuthHeaderDiscipline(unittest.TestCase):
    def test_bearer_header_only_token_never_in_url_or_params(self):
        session = _RecordingSession()
        client, _, _ = _make_client(session)

        client.get_user()

        (call,) = session.calls
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/port/v1/users/me")
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok-static")
        self.assertNotIn("tok-static", call["url"])
        self.assertTrue(call["params"] is None or "tok-static" not in str(call["params"]))

    def test_x_request_id_present_and_unique_per_logical_request(self):
        session = _RecordingSession()
        client, _, _ = _make_client(session)

        client.get_user()
        client.get_user()

        ids = [call["headers"]["x-request-id"] for call in session.calls]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        for request_id in ids:
            uuid.UUID(request_id)  # raises if not a valid uuid

    def test_x_request_id_stable_across_retries_of_one_request(self):
        """Retries of ONE logical request reuse its id — the P2 dedup seam."""
        session = _RecordingSession([_FakeResponse(500), _FakeResponse(200)])
        client, _, _ = _make_client(session)

        client.get_user()

        ids = {call["headers"]["x-request-id"] for call in session.calls}
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(ids), 1)


class TestThrottle(unittest.TestCase):
    def test_second_immediate_call_sleeps_min_interval(self):
        session = _RecordingSession()
        client, _, sleeps = _make_client(session)

        client.get_user()
        client.get_user()

        self.assertEqual(len(sleeps), 1, f"expected exactly one throttle sleep, got {sleeps}")
        self.assertGreater(sleeps[0], 0.0)
        self.assertLessEqual(sleeps[0], 0.5)


class TestRateLimitHandling(unittest.TestCase):
    def test_429_honors_retry_after_clamped_to_ceiling(self):
        session = _RecordingSession(
            [_FakeResponse(429, headers={"Retry-After": "999"}), _FakeResponse(200)]
        )
        client, _, sleeps = _make_client(session)

        payload = client.get_user()

        self.assertEqual(payload, {"ok": True})
        self.assertIn(120.0, [float(s) for s in sleeps], "Retry-After must clamp to 120s ceiling")

    def test_429_missing_retry_after_uses_floor(self):
        session = _RecordingSession([_FakeResponse(429), _FakeResponse(200)])
        client, _, sleeps = _make_client(session)

        client.get_user()

        self.assertIn(1.0, [float(s) for s in sleeps], "missing Retry-After must use 1s floor")

    def test_429_exhausted_raises_rate_limit_error(self):
        session = _RecordingSession([_FakeResponse(429)])
        client, _, _ = _make_client(session)

        with self.assertRaises(SaxoRateLimitError):
            client.get_user()
        self.assertEqual(len(session.calls), 4, "4 attempts = 1 + 3 retries")


class TestServerErrorHandling(unittest.TestCase):
    def test_5xx_backoffs_then_saxo_error(self):
        session = _RecordingSession([_FakeResponse(503)])
        client, _, sleeps = _make_client(session)

        with self.assertRaises(SaxoError):
            client.get_user()

        backoff_sleeps = [s for s in sleeps if s >= 5]
        self.assertEqual(backoff_sleeps, [5, 15, 30])

    def test_permanent_4xx_raises_immediately_no_retry(self):
        session = _RecordingSession([_FakeResponse(404, text="not found")])
        client, _, _ = _make_client(session)

        with self.assertRaises(SaxoError):
            client.get_user()
        self.assertEqual(len(session.calls), 1, "permanent 4xx must not be retried")


class TestAuthRefreshSeam(unittest.TestCase):
    def test_401_invalidates_and_retries_once_with_fresh_token(self):
        session = _RecordingSession([_FakeResponse(401), _FakeResponse(200)])
        client, provider, _ = _make_client(session, tokens=("tok-expired", "tok-fresh"))

        payload = client.get_user()

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(provider.invalidations, 1)
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer tok-expired")
        self.assertEqual(session.calls[1]["headers"]["Authorization"], "Bearer tok-fresh")

    def test_second_401_raises_auth_error_after_single_refresh(self):
        session = _RecordingSession([_FakeResponse(401)])
        client, provider, _ = _make_client(session)

        with self.assertRaises(SaxoAuthError):
            client.get_user()
        self.assertEqual(provider.invalidations, 1, "invalidate exactly once, then give up")
        self.assertEqual(len(session.calls), 2, "one refresh retry only")


class TestStaticTokenProviderFromEnv(unittest.TestCase):
    def test_missing_env_token_raises_auth_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SaxoAuthError):
                StaticTokenProvider.from_env()

    def test_env_token_round_trips(self):
        with mock.patch.dict("os.environ", {TOKEN_ENV: "tok-from-env"}):
            provider = StaticTokenProvider.from_env()
            self.assertEqual(provider.get_access_token(), "tok-from-env")
            provider.invalidate()  # no-op — must not raise
            self.assertEqual(provider.get_access_token(), "tok-from-env")


class TestEndpointWrappers(unittest.TestCase):
    def test_client_info_cached_after_first_call(self):
        session = _RecordingSession([_FakeResponse(200, payload={"ClientKey": "CK-1"})])
        client, _, _ = _make_client(session)

        first = client.get_client_info()
        second = client.get_client_info()

        self.assertEqual(first, {"ClientKey": "CK-1"})
        self.assertEqual(second, first)
        self.assertEqual(len(session.calls), 1, "second get_client_info must hit the cache")

    def test_search_instruments_params(self):
        session = _RecordingSession([_FakeResponse(200, payload={"Data": []})])
        client, _, _ = _make_client(session)

        client.search_instruments("AAPL", exchange_id="NASDAQ")

        (call,) = session.calls
        self.assertTrue(call["url"].endswith("/ref/v1/instruments"))
        self.assertEqual(
            call["params"],
            {"Keywords": "AAPL", "AssetTypes": "Stock", "ExchangeId": "NASDAQ"},
        )

    def test_get_json_escape_hatch_joins_base_url(self):
        session = _RecordingSession()
        client, _, _ = _make_client(session)

        client.get_json("/port/v1/orders/me", params={"Status": "Working"})

        (call,) = session.calls
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/port/v1/orders/me")
        self.assertEqual(call["params"], {"Status": "Working"})


if __name__ == "__main__":
    unittest.main()
