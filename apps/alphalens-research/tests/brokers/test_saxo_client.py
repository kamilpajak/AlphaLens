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

import tempfile
import unittest
import uuid
from pathlib import Path
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

    def patch(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        return self._next(
            {
                "method": "patch",
                "url": url,
                "headers": dict(headers or {}),
                "params": params,
                "json": json,
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

        with self.assertRaises(SaxoAuthError) as ctx:
            client.get_user()
        self.assertEqual(provider.invalidations, 1, "invalidate exactly once, then give up")
        self.assertEqual(len(session.calls), 2, "one refresh retry only")
        # Provider-agnostic operator guidance (P4: OAuth + static co-exist).
        self.assertIn("alphalens broker auth", str(ctx.exception))
        self.assertIn("SAXO_SIM_TOKEN", str(ctx.exception))

    def test_second_401_on_write_carries_provider_agnostic_guidance(self):
        session = _RecordingSession([_FakeResponse(401)])
        client, _, _ = _make_client(session)

        with self.assertRaises(SaxoAuthError) as ctx:
            client.precheck_order({"Uic": 1})
        self.assertIn("alphalens broker auth", str(ctx.exception))
        self.assertIn("SAXO_SIM_TOKEN", str(ctx.exception))


class TestFromEnvProviderSelection(unittest.TestCase):
    """P4: an existing OAuth token store beats SAXO_SIM_TOKEN; no store keeps
    the static path byte-identical; missing OAuth creds with a store present
    is a hard error, never a silent static fallback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "token_store.json"

    def _oauth_env(self, **extra: str) -> dict[str, str]:
        return {
            "SAXO_TOKEN_STORE_PATH": str(self.store_path),
            "SAXO_APP_KEY": "app-key-x",
            "SAXO_APP_SECRET": "app-secret-x",
            "SAXO_AUTH_REDIRECT_URL": "http://localhost:8765/callback",  # NOSONAR — loopback
            **extra,
        }

    def _write_store(self) -> None:
        from alphalens_pipeline.brokers.saxo.oauth import TokenBundle
        from alphalens_pipeline.brokers.saxo.tokens import TokenStore

        TokenStore(self.store_path).save_bundle(
            TokenBundle("acc", 1200, "ref", 2400), app_key="app-key-x"
        )

    def test_store_present_selects_oauth_provider(self):
        from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

        self._write_store()
        with mock.patch.dict("os.environ", self._oauth_env(), clear=True):
            client = SaxoClient.from_env()
        self.assertIsInstance(client._token_provider, OAuthTokenProvider)

    def test_store_present_beats_a_set_static_token(self):
        from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

        self._write_store()
        with mock.patch.dict("os.environ", self._oauth_env(SAXO_SIM_TOKEN="tok"), clear=True):
            client = SaxoClient.from_env()
        self.assertIsInstance(client._token_provider, OAuthTokenProvider)

    def test_store_present_but_missing_app_key_is_a_hard_error(self):
        self._write_store()
        env = self._oauth_env(SAXO_SIM_TOKEN="tok")
        env.pop("SAXO_APP_KEY")
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(SaxoAuthError) as ctx:
                SaxoClient.from_env()
        self.assertIn("SAXO_APP_KEY", str(ctx.exception))

    def test_no_store_keeps_static_provider_from_sim_token(self):
        with mock.patch.dict(
            "os.environ",
            {"SAXO_TOKEN_STORE_PATH": str(self.store_path), TOKEN_ENV: "tok"},
            clear=True,
        ):
            client = SaxoClient.from_env()
        self.assertIsInstance(client._token_provider, StaticTokenProvider)

    def test_saxo_env_guard_still_fires_first_even_with_store(self):
        from alphalens_pipeline.brokers.saxo.client import SaxoLiveEnvironmentBlockedError

        self._write_store()
        with mock.patch.dict("os.environ", self._oauth_env(SAXO_ENV="live"), clear=True):
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                SaxoClient.from_env()


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


class TestWriteTransportPatch(unittest.TestCase):
    """PATCH shares the POST-safe never-blind-retry lane (idempotent=False):
    a body is attached like POST, but an ambiguous 5xx raises immediately
    carrying the x-request-id rather than blind-hammering the resize."""

    _PATCH_BODY = {"OrderId": "O-1", "Uic": 307, "AssetType": "Stock", "Amount": 7}

    def test_send_write_allows_patch_verb(self):
        # The allow-list guard must accept PATCH and dispatch session.patch.
        session = _RecordingSession([_FakeResponse(200, payload={"OrderId": "O-1"})])
        client, _, _ = _make_client(session)

        status, body = client.amend_order(self._PATCH_BODY, request_id="rid-patch")

        (call,) = session.calls
        self.assertEqual(call["method"], "patch")
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/trade/v2/orders")
        self.assertEqual((status, body), (200, {"OrderId": "O-1"}))

    def test_patch_attaches_json_body(self):
        # A bodiless PATCH 400s at Saxo — the body MUST be attached (like POST).
        session = _RecordingSession([_FakeResponse(200, payload={"OrderId": "O-1"})])
        client, _, _ = _make_client(session)

        client.amend_order(self._PATCH_BODY, request_id="rid-patch-body")

        (call,) = session.calls
        self.assertEqual(call["json"], self._PATCH_BODY)
        self.assertEqual(call["headers"]["x-request-id"], "rid-patch-body")

    def test_patch_not_idempotent_never_blind_retries_5xx(self):
        # A 5xx after a PATCH is ambiguous (the resize may have landed), so it
        # raises immediately carrying the x-request-id — NOT the DELETE ladder.
        session = _RecordingSession([_FakeResponse(500, text="server error")])
        client, _, _ = _make_client(session)

        with self.assertRaises(SaxoError) as ctx:
            client.amend_order(self._PATCH_BODY, request_id="rid-patch-5xx")

        self.assertEqual(len(session.calls), 1, "an ambiguous PATCH 5xx must NOT be retried")
        self.assertIn("rid-patch-5xx", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, SaxoRateLimitError)

    def test_patch_provably_unsent_retries_same_request_id(self):
        # ConnectTimeout = the TCP connection never opened, so the PATCH was
        # provably not sent — the one network shape safe to retry, and it must
        # reuse the SAME x-request-id (verb-agnostic provably-unsent lane).
        session = _RecordingSession(
            [
                requests.exceptions.ConnectTimeout("connect timed out"),
                _FakeResponse(200, payload={"OrderId": "O-1"}),
            ]
        )
        client, _, _ = _make_client(session)

        status, _ = client.amend_order(self._PATCH_BODY, request_id="rid-patch-unsent")

        self.assertEqual(status, 200)
        self.assertEqual(len(session.calls), 2)
        ids = {call["headers"]["x-request-id"] for call in session.calls}
        self.assertEqual(ids, {"rid-patch-unsent"})

    def test_patch_429_reuses_same_request_id(self):
        # A 429 means the PATCH was provably not accepted, so it retries after
        # Retry-After — with the SAME x-request-id (verb-agnostic 429 lane).
        session = _RecordingSession(
            [
                _FakeResponse(429, headers={"Retry-After": "2"}),
                _FakeResponse(200, payload={"OrderId": "O-1"}),
            ]
        )
        client, _, sleeps = _make_client(session)

        status, _ = client.amend_order(self._PATCH_BODY, request_id="rid-patch-429")

        self.assertEqual(status, 200)
        self.assertEqual(len(session.calls), 2)
        ids = {call["headers"]["x-request-id"] for call in session.calls}
        self.assertEqual(ids, {"rid-patch-429"}, "429 retry must reuse the SAME x-request-id")
        self.assertIn(2.0, [float(s) for s in sleeps])

    def test_amend_order_returns_status_and_json(self):
        session = _RecordingSession([_FakeResponse(200, payload={"OrderId": "O-1"})])
        client, _, _ = _make_client(session)

        status, body = client.amend_order(self._PATCH_BODY, request_id="rid-amend")

        self.assertEqual((status, body), (200, {"OrderId": "O-1"}))


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


class TestAuditActivitiesWrapper(unittest.TestCase):
    """P3 wrapper for GET /cs/v1/audit/orderactivities (terminal resolution)."""

    def test_param_passthrough_order_id_entry_type_client_key(self):
        session = _RecordingSession([_FakeResponse(200, payload={"Data": [], "__count": 0})])
        client, _, _ = _make_client(session)

        payload = client.get_order_activities("CK-1", order_id="5039272886")

        (call,) = session.calls
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/cs/v1/audit/orderactivities")
        self.assertEqual(
            call["params"],
            {"ClientKey": "CK-1", "EntryType": "Last", "OrderId": "5039272886"},
        )
        self.assertEqual(payload, {"Data": [], "__count": 0})

    def test_optional_from_datetime_and_top_params(self):
        session = _RecordingSession([_FakeResponse(200, payload={"Data": []})])
        client, _, _ = _make_client(session)

        client.get_order_activities(
            "CK-1",
            entry_type="All",
            from_datetime="2026-07-01T00:00:00Z",
            top=500,
        )

        (call,) = session.calls
        self.assertEqual(
            call["params"],
            {
                "ClientKey": "CK-1",
                "EntryType": "All",
                "FromDateTime": "2026-07-01T00:00:00Z",
                "$top": 500,
            },
        )

    def test_next_url_with_port_443_is_normalized_and_followed(self):
        # Saxo pagination URLs embed ":443" in the absolute form, which would
        # fail _join_url's SIM-prefix rail — the wrapper must strip to the
        # path after /sim/openapi before re-requesting.
        next_absolute = (
            "https://gateway.saxobank.com:443/sim/openapi"
            "/cs/v1/audit/orderactivities?ClientKey=CK-1&$skiptoken=abc"
        )
        session = _RecordingSession(
            [
                _FakeResponse(
                    200,
                    payload={"Data": [{"LogId": 1}], "__next": next_absolute},
                ),
                _FakeResponse(200, payload={"Data": [{"LogId": 2}]}),
            ]
        )
        client, _, _ = _make_client(session)

        payload = client.get_order_activities("CK-1", order_id="1")

        self.assertEqual(len(session.calls), 2)
        follow_up = session.calls[1]["url"]
        self.assertTrue(
            follow_up.startswith(f"{SIM_BASE_URL}/cs/v1/audit/orderactivities"),
            f"follow-up must pass the SIM rail as a joined path, got {follow_up!r}",
        )
        self.assertNotIn(":443", follow_up)
        self.assertEqual([row["LogId"] for row in payload["Data"]], [1, 2])
        self.assertNotIn("__next", payload, "merged payload must not re-expose pagination")

    def test_next_poll_cursor_is_not_followed_single_snapshot(self):
        # LIVE 2026-07-20 (first-fill experiment): the audit endpoint ALWAYS
        # returns a __nextPoll cursor — a subscription-style live-poll
        # continuation for FUTURE activities, NOT pagination of the current
        # snapshot. It is present even when __count == len(Data) (page 1 is
        # already complete). Following it immediately returns HTTP 429
        # ("poll too soon"). The wrapper must take page 1's Data and ignore
        # __nextPoll entirely — additional current-snapshot rows only ever
        # arrive via __next. The second queued page proves a wrong follow-up
        # (it would leak in and blow the single-GET count).
        session = _RecordingSession(
            [
                _FakeResponse(
                    200,
                    payload={
                        "__count": 3,
                        "Data": [{"LogId": 1}, {"LogId": 2}, {"LogId": 3}],
                        "__nextPoll": (
                            "https://gateway.saxobank.com:443/sim/openapi"
                            "/cs/v1/audit/orderactivities?ClientKey=CK-1&$skiptoken=poll"
                        ),
                    },
                ),
                _FakeResponse(200, payload={"Data": [{"LogId": 999}]}),
            ]
        )
        client, _, _ = _make_client(session)

        payload = client.get_order_activities("CK-1", order_id="1")

        self.assertEqual(len(session.calls), 1, "__nextPoll must NOT be fetched")
        self.assertEqual([row["LogId"] for row in payload["Data"]], [1, 2, 3])
        self.assertNotIn("__nextPoll", payload, "poll cursor must not leak into the envelope")

    def test_next_pagination_still_followed_when_present(self):
        # Positive control: a genuine __next cursor (pagination of the CURRENT
        # snapshot) is still followed unconditionally — the fix narrows ONLY
        # __nextPoll, never __next.
        next_url = (
            "https://gateway.saxobank.com:443/sim/openapi"
            "/cs/v1/audit/orderactivities?ClientKey=CK-1&$skiptoken=page2"
        )
        session = _RecordingSession(
            [
                _FakeResponse(
                    200,
                    payload={
                        "__count": 4,
                        "Data": [{"LogId": 1}, {"LogId": 2}],
                        "__next": next_url,
                    },
                ),
                _FakeResponse(200, payload={"Data": [{"LogId": 3}, {"LogId": 4}]}),
            ]
        )
        client, _, _ = _make_client(session)

        payload = client.get_order_activities("CK-1", order_id="1")

        self.assertEqual(len(session.calls), 2, "__next pagination must be followed")
        self.assertEqual([row["LogId"] for row in payload["Data"]], [1, 2, 3, 4])
        self.assertNotIn("__next", payload)


class TestClosedPositionsWrapper(unittest.TestCase):
    """P3 wrapper for GET /port/v1/closedpositions (fill cross-check)."""

    def test_envelope_shape_passes_through_with_client_key_param(self):
        session = _RecordingSession(
            [_FakeResponse(200, payload={"__count": 1, "Data": [{"ClosedPosition": {}}]})]
        )
        client, _, _ = _make_client(session)

        payload = client.get_closed_positions("CK-1")

        (call,) = session.calls
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/port/v1/closedpositions")
        self.assertEqual(call["params"], {"ClientKey": "CK-1"})
        self.assertEqual(payload["__count"], 1)
        self.assertEqual(len(payload["Data"]), 1)

    def test_bare_array_shape_is_normalized_to_envelope(self):
        # Live-verified empty-account shape: the endpoint answers with a bare
        # JSON array, not the {__count, Data} envelope.
        session = _RecordingSession([_FakeResponse(200, payload=[])])  # type: ignore[arg-type]
        client, _, _ = _make_client(session)

        payload = client.get_closed_positions("CK-1")

        self.assertEqual(payload, {"__count": 0, "Data": []})

    def test_next_url_is_normalized_and_followed(self):
        # closedpositions paginates the CURRENT snapshot via __next (like the
        # audit endpoint), so a genuine __next cursor is followed. This test
        # previously mocked __nextPoll here and asserted it was followed — that
        # encoded the bug (see client._get_paged_json: __nextPoll is a
        # live-poll continuation cursor, NOT pagination, and following it 429s).
        # Rewritten to __next to keep pagination coverage without the bug.
        next_url = (
            "https://gateway.saxobank.com:443/sim/openapi"
            "/port/v1/closedpositions?ClientKey=CK-1&$skiptoken=xyz"
        )
        session = _RecordingSession(
            [
                _FakeResponse(200, payload={"Data": [{"A": 1}], "__next": next_url}),
                _FakeResponse(200, payload={"Data": [{"A": 2}]}),
            ]
        )
        client, _, _ = _make_client(session)

        payload = client.get_closed_positions("CK-1")

        self.assertEqual(len(session.calls), 2)
        self.assertNotIn(":443", session.calls[1]["url"])
        self.assertEqual(len(payload["Data"]), 2)
        self.assertNotIn("__next", payload)

    def test_next_poll_cursor_is_not_followed(self):
        # A __nextPoll on closedpositions is likewise a live-poll cursor: take
        # the current page, do not fetch it, strip it from the envelope.
        next_poll = (
            "https://gateway.saxobank.com:443/sim/openapi"
            "/port/v1/closedpositions?ClientKey=CK-1&$skiptoken=poll"
        )
        session = _RecordingSession(
            [
                _FakeResponse(
                    200, payload={"__count": 1, "Data": [{"A": 1}], "__nextPoll": next_poll}
                ),
                _FakeResponse(200, payload={"Data": [{"A": 2}]}),
            ]
        )
        client, _, _ = _make_client(session)

        payload = client.get_closed_positions("CK-1")

        self.assertEqual(len(session.calls), 1, "__nextPoll must NOT be fetched")
        self.assertEqual(len(payload["Data"]), 1)
        self.assertNotIn("__nextPoll", payload)


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
