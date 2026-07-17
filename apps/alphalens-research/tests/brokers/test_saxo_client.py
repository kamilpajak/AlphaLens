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

P2 write transport (``_send_write`` + wrappers): POST is NEVER blind-retried —
only provably-unsent failures (connect timeout / DNS / refused before send)
and 429 (request provably not accepted) retry, always with the SAME
``x-request-id``; ambiguous post-send errors and 5xx raise immediately
carrying the request id. DELETE is idempotent and keeps the normal ladder.
"""

from __future__ import annotations

import unittest
import uuid
from typing import Any
from unittest import mock

import requests
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
    """Returns queued responses in order; repeats the last one when drained.

    A queued item that is an ``Exception`` is RAISED instead of returned (for
    the write-transport network-failure tests).
    """

    def __init__(self, responses: list[_FakeResponse | Exception] | None = None):
        self.responses = list(responses or [_FakeResponse()])
        self.calls: list[dict[str, Any]] = []

    def _next(self, call: dict[str, Any]) -> _FakeResponse:
        self.calls.append(call)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        return self._next(
            {
                "method": "get",
                "url": url,
                "headers": dict(headers or {}),
                "params": params,
                "timeout": timeout,
            }
        )

    def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        return self._next(
            {
                "method": "post",
                "url": url,
                "headers": dict(headers or {}),
                "params": params,
                "json": json,
                "timeout": timeout,
            }
        )

    def delete(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        return self._next(
            {
                "method": "delete",
                "url": url,
                "headers": dict(headers or {}),
                "params": params,
                "timeout": timeout,
            }
        )


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


class TestWriteTransportPost(unittest.TestCase):
    """POST-safe retry policy: retry ONLY when the request provably never
    reached Saxo (connect-phase failure) or was provably not accepted (429);
    everything ambiguous raises immediately carrying the x-request-id."""

    _BODY = {"Uic": 307, "AssetType": "Stock", "Amount": 1}

    def test_post_carries_client_request_id_header_and_json_body(self):
        session = _RecordingSession([_FakeResponse(201, payload={"OrderId": "1"})])
        client, _, _ = _make_client(session)

        status, body = client.place_order(self._BODY, request_id="rid-123")

        (call,) = session.calls
        self.assertEqual(call["method"], "post")
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/trade/v2/orders")
        self.assertEqual(call["headers"]["x-request-id"], "rid-123")
        self.assertEqual(call["json"], self._BODY)
        self.assertEqual((status, body), (201, {"OrderId": "1"}))

    def test_post_retried_on_429_with_same_x_request_id(self):
        session = _RecordingSession(
            [
                _FakeResponse(429, headers={"Retry-After": "2"}),
                _FakeResponse(201, payload={"OrderId": "1"}),
            ]
        )
        client, _, sleeps = _make_client(session)

        status, _ = client.place_order(self._BODY, request_id="rid-429")

        self.assertEqual(status, 201)
        self.assertEqual(len(session.calls), 2)
        ids = {call["headers"]["x-request-id"] for call in session.calls}
        self.assertEqual(ids, {"rid-429"}, "429 retry must reuse the SAME x-request-id")
        self.assertIn(2.0, [float(s) for s in sleeps])

    def test_post_not_retried_after_ambiguous_send_error(self):
        # A connection aborted mid-request MAY have been received by Saxo —
        # blind-retrying could double-place; must raise immediately with the
        # request id so the operator can reconcile / re-run inside the 15s
        # dedup window.
        session = _RecordingSession(
            [requests.exceptions.ConnectionError("Connection aborted mid-body")]
        )
        client, _, _ = _make_client(session)

        with self.assertRaises(SaxoError) as ctx:
            client.place_order(self._BODY, request_id="rid-ambig")

        self.assertEqual(len(session.calls), 1, "an ambiguous POST failure must NOT be retried")
        self.assertIn("rid-ambig", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, SaxoRateLimitError)

    def test_post_retried_when_provably_unsent(self):
        # ConnectTimeout = the TCP connection never opened, so nothing was
        # sent — the one network shape that IS safe to retry.
        session = _RecordingSession(
            [
                requests.exceptions.ConnectTimeout("connect timed out"),
                _FakeResponse(201, payload={"OrderId": "1"}),
            ]
        )
        client, _, _ = _make_client(session)

        status, _ = client.place_order(self._BODY, request_id="rid-unsent")

        self.assertEqual(status, 201)
        self.assertEqual(len(session.calls), 2)
        ids = {call["headers"]["x-request-id"] for call in session.calls}
        self.assertEqual(ids, {"rid-unsent"})

    def test_post_5xx_raises_immediately_without_retry(self):
        # A 5xx after a POST is ambiguous (the order may exist server-side).
        session = _RecordingSession([_FakeResponse(502, text="bad gateway")])
        client, _, _ = _make_client(session)

        with self.assertRaises(SaxoError) as ctx:
            client.place_order(self._BODY, request_id="rid-5xx")

        self.assertEqual(len(session.calls), 1)
        self.assertIn("rid-5xx", str(ctx.exception))

    def test_post_401_invalidates_and_retries_once_with_same_request_id(self):
        session = _RecordingSession(
            [_FakeResponse(401), _FakeResponse(201, payload={"OrderId": "1"})]
        )
        client, provider, _ = _make_client(session, tokens=("tok-expired", "tok-fresh"))

        status, _ = client.place_order(self._BODY, request_id="rid-401")

        self.assertEqual(status, 201)
        self.assertEqual(provider.invalidations, 1)
        self.assertEqual(session.calls[1]["headers"]["Authorization"], "Bearer tok-fresh")
        ids = {call["headers"]["x-request-id"] for call in session.calls}
        self.assertEqual(ids, {"rid-401"})

    def test_post_4xx_body_returned_for_translation_not_raised(self):
        # The broker owns rejection translation (ModelState / ErrorInfo /
        # OrderId-repair) — the transport must hand back the body, not raise.
        session = _RecordingSession(
            [_FakeResponse(400, payload={"ErrorInfo": {"ErrorCode": "OrderValueToSmall"}})]
        )
        client, _, _ = _make_client(session)

        status, body = client.place_order(self._BODY, request_id="rid-400")

        self.assertEqual(status, 400)
        self.assertEqual(body["ErrorInfo"]["ErrorCode"], "OrderValueToSmall")

    def test_precheck_posts_to_precheck_endpoint(self):
        session = _RecordingSession([_FakeResponse(200, payload={"PreCheckResult": "Ok"})])
        client, _, _ = _make_client(session)

        status, body = client.precheck_order(self._BODY)

        (call,) = session.calls
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/trade/v2/orders/precheck")
        self.assertEqual(call["json"], self._BODY)
        uuid.UUID(call["headers"]["x-request-id"])
        self.assertEqual((status, body), (200, {"PreCheckResult": "Ok"}))


class TestWriteTransportDelete(unittest.TestCase):
    def test_delete_retries_and_throttles(self):
        session = _RecordingSession([_FakeResponse(503), _FakeResponse(200, payload={})])
        client, _, sleeps = _make_client(session)

        status, _ = client.cancel_order_ids("O-1", account_key="AK-1")

        self.assertEqual(status, 200)
        self.assertEqual(len(session.calls), 2, "DELETE is idempotent — 5xx retries")
        for call in session.calls:
            self.assertEqual(call["method"], "delete")
            self.assertEqual(call["url"], f"{SIM_BASE_URL}/trade/v2/orders/O-1")
            self.assertEqual(call["params"], {"AccountKey": "AK-1"})
        self.assertIn(5, [int(s) for s in sleeps if s >= 1], "5xx backoff ladder applies")

    def test_delete_retries_transient_network_errors(self):
        session = _RecordingSession(
            [
                requests.exceptions.ConnectionError("reset"),
                _FakeResponse(200, payload={}),
            ]
        )
        client, _, _ = _make_client(session)

        status, _ = client.cancel_order_ids("O-1", account_key="AK-1")

        self.assertEqual(status, 200)
        self.assertEqual(len(session.calls), 2)

    def test_delete_goes_through_shared_throttle(self):
        session = _RecordingSession([_FakeResponse(200, payload={})])
        client, _, sleeps = _make_client(session)

        client.cancel_order_ids("O-1", account_key="AK-1")
        client.cancel_order_ids("O-2", account_key="AK-1")

        throttle_sleeps = [s for s in sleeps if 0 < s <= 0.5]
        self.assertEqual(len(throttle_sleeps), 1, "second immediate DELETE must throttle")


class TestOrderReadWrappers(unittest.TestCase):
    def test_get_open_orders_hits_orders_me(self):
        session = _RecordingSession([_FakeResponse(200, payload={"Data": []})])
        client, _, _ = _make_client(session)

        payload = client.get_open_orders()

        (call,) = session.calls
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/port/v1/orders/me")
        self.assertEqual(payload, {"Data": []})

    def test_get_order_status_404_returns_none(self):
        # /port/v1/orders drops filled/cancelled/expired orders — a 404 is an
        # EXPECTED outcome the broker maps to OrderStatus.UNKNOWN, not an error.
        session = _RecordingSession([_FakeResponse(404, text="not found")])
        client, _, _ = _make_client(session)

        self.assertIsNone(client.get_order_status("CK-1", "O-GONE"))

    def test_get_order_status_present_returns_payload(self):
        session = _RecordingSession(
            [_FakeResponse(200, payload={"OrderId": "O-1", "Status": "Working"})]
        )
        client, _, _ = _make_client(session)

        payload = client.get_order_status("CK-1", "O-1")

        (call,) = session.calls
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/port/v1/orders/CK-1/O-1")
        self.assertEqual(payload, {"OrderId": "O-1", "Status": "Working"})


class TestProvablyUnsentClassifier(unittest.TestCase):
    """Pins the repr-marker classification of _is_provably_unsent: only
    provably-unsent network failures may allow a POST retry. A requests/
    urllib3 bump that changes exception reprs must trip this test, not
    silently change retry behavior."""

    def _classify(self, exc):
        from alphalens_pipeline.brokers.saxo.client import SaxoClient

        return SaxoClient._is_provably_unsent(exc)

    def test_connect_timeout_is_unsent(self):
        self.assertTrue(self._classify(requests.exceptions.ConnectTimeout("t")))

    def test_connection_phase_failures_are_unsent(self):
        for msg in (
            "HTTPSConnectionPool: Max retries exceeded (Caused by NewConnectionError('fail'))",
            "Name or service not known",
            "Failed to resolve 'gateway.saxobank.com'",
        ):
            self.assertTrue(self._classify(requests.exceptions.ConnectionError(msg)), msg)

    def test_ambiguous_failures_are_not_retried(self):
        self.assertFalse(self._classify(requests.exceptions.ReadTimeout("t")))
        self.assertFalse(
            self._classify(requests.exceptions.ConnectionError("Connection aborted mid-body"))
        )


if __name__ == "__main__":
    unittest.main()
