"""HTTP-core + redaction-boundary tests for :class:`SaxoClient`.

The single CRITICAL contract this file pins: the ``/token`` request body +
headers and any response body NEVER reach an exception message or a log line
raw. Only ``status_code`` + the parsed OAuth ``error`` / ``error_description``
fields are surfaced. A leaked refresh token in journald is a live-brokerage
secret exposure (secret-leak Finding 1).

Tests use ``httpx.MockTransport`` (the canonical ``_transport`` seam) so no
network is touched.
"""

from __future__ import annotations

import logging
import unittest

import httpx
from alphalens_pipeline.data.alt_data.saxo_client import (
    SaxoClient,
    SaxoConfigError,
    SaxoEnvironmentMismatchError,
)

SENTINEL_RT = "SENTINEL_REFRESH_TOKEN_zzz999"
SENTINEL_AT = "SENTINEL_ACCESS_TOKEN_aaa111"


def _client(handler, *, environment: str = "sim") -> SaxoClient:
    transport = httpx.MockTransport(handler)
    return SaxoClient(
        app_key="APPKEY",
        redirect_uri="https://example.invalid/cb",
        environment=environment,
        _transport=transport,
    )


class TestSaxoClientEndpoints(unittest.TestCase):
    def test_sim_and_live_endpoints_are_hardcoded_per_env(self) -> None:
        sim = _client(lambda r: httpx.Response(200, json={}), environment="sim")
        live = _client(lambda r: httpx.Response(200, json={}), environment="live")
        self.assertIn("sim", sim.auth_base_url + sim.gateway_base_url)
        self.assertNotIn("sim", live.auth_base_url)
        # Hosts are the Saxo OpenAPI hosts, never derived from an env string.
        self.assertIn("logonvalidation.net", sim.auth_base_url)
        self.assertIn("saxobank.com", sim.gateway_base_url)

    def test_unknown_environment_rejected(self) -> None:
        with self.assertRaises(SaxoConfigError):
            _client(lambda r: httpx.Response(200, json={}), environment="prod")

    def test_empty_environment_rejected_not_defaulted(self) -> None:
        with self.assertRaises(SaxoConfigError):
            _client(lambda r: httpx.Response(200, json={}), environment="")


class TestSaxoClientRefresh(unittest.TestCase):
    def test_refresh_returns_parsed_token_payload(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": SENTINEL_AT,
                    "refresh_token": SENTINEL_RT,
                    "expires_in": 1200,
                    "refresh_token_expires_in": 2400,
                },
            )

        client = _client(handler)
        payload = client.refresh_token(refresh_token="OLD_RT")
        self.assertEqual(payload["access_token"], SENTINEL_AT)
        self.assertEqual(payload["refresh_token"], SENTINEL_RT)
        self.assertEqual(payload["expires_in"], 1200)

    def test_invalid_grant_is_classified_permanent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        client = _client(handler)
        from alphalens_pipeline.data.alt_data.saxo_client import classify_token_error

        resp = client._http.post("/token", data={"x": "y"})  # internal probe
        self.assertEqual(classify_token_error(resp), "permanent")

    def test_503_is_classified_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="<html>upstream</html>")

        client = _client(handler)
        from alphalens_pipeline.data.alt_data.saxo_client import classify_token_error

        resp = client._http.post("/token", data={"x": "y"})
        self.assertEqual(classify_token_error(resp), "transient")

    def test_400_with_html_body_is_transient_not_permanent(self) -> None:
        # Off-shape (non-JSON) body must NOT be misclassified permanent —
        # that would self-inflict a halt on a proxy error page.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="<html>blocked</html>")

        client = _client(handler)
        from alphalens_pipeline.data.alt_data.saxo_client import classify_token_error

        resp = client._http.post("/token", data={"x": "y"})
        self.assertEqual(classify_token_error(resp), "transient")

    def test_400_empty_body_is_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="")

        client = _client(handler)
        from alphalens_pipeline.data.alt_data.saxo_client import classify_token_error

        resp = client._http.post("/token", data={"x": "y"})
        self.assertEqual(classify_token_error(resp), "transient")


class TestSaxoClientRedaction(unittest.TestCase):
    """The load-bearing security contract: no token material ever leaks."""

    def _leaky_handler(self, request: httpx.Request) -> httpx.Response:
        # The error response echoes the refresh token back in the body AND a
        # Bearer header — exactly the worst case for a naive raise idiom.
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": f"token refresh_token={SENTINEL_RT} rejected",
            },
            headers={"Authorization": f"Bearer {SENTINEL_RT}"},
        )

    def test_exception_str_contains_no_token_material(self) -> None:
        client = _client(self._leaky_handler)
        with self.assertRaises(Exception) as ctx:
            client.refresh_token(refresh_token=SENTINEL_RT)
        msg = str(ctx.exception)
        self.assertNotIn(SENTINEL_RT, msg, "exception must not echo the refresh token")
        self.assertNotIn("refresh_token=", msg, "exception must not echo the raw body")
        # The redacted summary keeps the actionable OAuth error name.
        self.assertIn("invalid_grant", msg)

    def test_logs_contain_no_token_material(self) -> None:
        import alphalens_pipeline.data.alt_data.saxo_client as mod

        records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        handler = _Capture()
        mod.logger.addHandler(handler)
        mod.logger.setLevel(logging.DEBUG)
        try:
            client = _client(self._leaky_handler)
            with self.assertRaises(Exception):
                client.refresh_token(refresh_token=SENTINEL_RT)
        finally:
            mod.logger.removeHandler(handler)
        joined = "\n".join(records)
        self.assertNotIn(SENTINEL_RT, joined, "no log line may contain the token")
        self.assertNotIn("refresh_token=", joined)

    def test_positive_control_unredacted_build_would_fail(self) -> None:
        # Prove the redaction test has teeth: feed the SENTINEL through the
        # redaction helper directly and assert it is scrubbed. If _redact ever
        # rots to identity, this fails loud.
        from alphalens_pipeline.data.alt_data.saxo_client import _redact

        leaky = f"body refresh_token={SENTINEL_RT} access_token={SENTINEL_AT}"
        scrubbed = _redact(leaky)
        self.assertNotIn(SENTINEL_RT, scrubbed)
        self.assertNotIn(SENTINEL_AT, scrubbed)
        # Sanity: an un-redacted string DOES contain it (control on the input).
        self.assertIn(SENTINEL_RT, leaky)


class TestSaxoClientProbe(unittest.TestCase):
    def test_probe_users_me_returns_payload(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(request.url.path.endswith("/port/v1/users/me"))
            self.assertEqual(request.headers.get("Authorization"), f"Bearer {SENTINEL_AT}")
            return httpx.Response(200, json={"UserId": "U123"})

        client = _client(handler)
        payload = client.get_user_me(access_token=SENTINEL_AT)
        self.assertEqual(payload["UserId"], "U123")

    def test_probe_401_raises_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"Message": "Unauthorized"})

        client = _client(handler)
        from alphalens_pipeline.data.alt_data.saxo_client import SaxoAuthError

        with self.assertRaises(SaxoAuthError):
            client.get_user_me(access_token="STALE")


class TestSaxoClientEnvHelpers(unittest.TestCase):
    def test_environment_mismatch_error_is_distinct_type(self) -> None:
        # The exception type the manager raises on a record/env interlock break
        # must exist and subclass the client base error.
        from alphalens_pipeline.data.alt_data.saxo_client import SaxoClientError

        self.assertTrue(issubclass(SaxoEnvironmentMismatchError, SaxoClientError))


if __name__ == "__main__":
    unittest.main()
