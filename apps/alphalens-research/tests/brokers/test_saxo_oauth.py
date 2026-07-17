"""Hermetic tests for P4 Saxo OAuth: transport, token store, refreshing provider.

Everything runs offline — the token endpoint is a recording fake session fed
canned JSON matching the documented response shape, clocks are injected
(monotonic ``now`` + wall ``wall_now``), and store paths live in per-test
``tempfile.TemporaryDirectory``. No real credentials appear anywhere.

Pins the P4 contract:

- ``SaxoAuthClient`` posts the exact grant bodies with Basic auth, refuses any
  non-SIM auth base URL (equality rail), never retries a 4xx (chain lost), and
  retries transport/5xx exactly once.
- ``TokenStore`` writes atomically with 0600, refuses corrupt / foreign /
  non-sim stores with an actionable message, and serializes rotation through a
  sibling ``.lock`` flock.
- ``OAuthTokenProvider`` refreshes proactively at ``deadline − 120 s`` on the
  monotonic clock, persists the rotated pair BEFORE returning the new token,
  adopts a sibling process's fresh token instead of burning a rotation, and
  treats ANY refresh failure as chain loss (single alert + re-auth message).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import stat
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

import requests
from alphalens_pipeline.brokers.saxo.client import _LIVE_URL_MARKERS
from alphalens_pipeline.brokers.saxo.client import SIM_AUTH_BASE_URL as CLIENT_SIM_AUTH_BASE_URL
from alphalens_pipeline.brokers.saxo.errors import (
    SaxoAuthError,
    SaxoLiveEnvironmentBlockedError,
)
from alphalens_pipeline.brokers.saxo.oauth import (
    SIM_AUTH_BASE_URL,
    SaxoAuthClient,
    TokenBundle,
    generate_state,
)
from alphalens_pipeline.brokers.saxo.tokens import (
    APP_KEY_ENV,
    APP_SECRET_ENV,
    REDIRECT_URL_ENV,
    REFRESH_MARGIN_S,
    TOKEN_STORE_PATH_ENV,
    OAuthTokenProvider,
    TokenStore,
    app_key_fingerprint,
    resolve_token_store_path,
)

_REAUTH_HINT = "alphalens broker auth"

# Documented token-endpoint response shape (values are fakes).
_CANNED_TOKEN_RESPONSE: dict[str, Any] = {
    "access_token": "acc-token-1",
    "expires_in": 1200,
    "token_type": "Bearer",
    "refresh_token": "refresh-token-1",
    "refresh_token_expires_in": 2400,
}

_REDIRECT_URI = "http://localhost:8765/callback"  # NOSONAR — OAuth loopback redirect, never fetched


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else dict(_CANNED_TOKEN_RESPONSE)
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingAuthSession:
    """Queued responses in order; an Exception item is raised instead."""

    def __init__(self, queue: list[Any] | None = None):
        self.queue = list(queue or [])
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        item = self.queue.pop(0) if self.queue else _FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item


def _make_auth_client(
    session: _RecordingAuthSession | None = None,
) -> tuple[SaxoAuthClient, _RecordingAuthSession]:
    session = session or _RecordingAuthSession()
    client = SaxoAuthClient(
        "app-key-x",
        "app-secret-x",
        session=session,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    return client, session


class _FakeMonotonic:
    def __init__(self, start: float = 0.0):
        self.value = start

    def __call__(self) -> float:
        return self.value


class _FakeWallClock:
    def __init__(self, start: dt.datetime | None = None):
        self.value = start or dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.value


class _FakeAuthTransport:
    """SaxoAuthClient double for provider tests — counts refreshes."""

    def __init__(self, bundles: list[TokenBundle | Exception] | None = None):
        self.app_key = "app-key-x"
        self.refresh_calls: list[tuple[str, str]] = []
        self._bundles = list(bundles or [])
        self._seq = 0

    def refresh(self, refresh_token: str, redirect_uri: str) -> TokenBundle:
        self.refresh_calls.append((refresh_token, redirect_uri))
        if self._bundles:
            item = self._bundles.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        self._seq += 1
        return TokenBundle(
            access_token=f"acc-{self._seq}",
            expires_in=1200,
            refresh_token=f"refresh-{self._seq}",
            refresh_token_expires_in=2400,
        )


class _BombTransport:
    """Fails the test if any refresh is attempted (adopt-not-burn assertions)."""

    def __init__(self):
        self.app_key = "app-key-x"

    def refresh(self, refresh_token: str, redirect_uri: str) -> TokenBundle:
        raise AssertionError("refresh must not be called — the on-disk token should be adopted")


def _bundle(seq: int = 1, *, expires_in: int = 1200, refresh_expires_in: int = 2400) -> TokenBundle:
    return TokenBundle(
        access_token=f"acc-{seq}",
        expires_in=expires_in,
        refresh_token=f"refresh-{seq}",
        refresh_token_expires_in=refresh_expires_in,
    )


class _ProviderHarness:
    def __init__(self, tmp: Path, transport: Any | None = None):
        self.store = TokenStore(tmp / "token_store.json")
        self.transport = transport or _FakeAuthTransport()
        self.mono = _FakeMonotonic()
        self.wall = _FakeWallClock()
        self.alerts: list[str] = []
        self.provider = OAuthTokenProvider(
            self.transport,
            self.store,
            redirect_uri=_REDIRECT_URI,
            now=self.mono,
            wall_now=self.wall,
            alert=self.alerts.append,
        )

    def seed(self, seq: int = 1, **kw: int) -> None:
        self.store.save_bundle(_bundle(seq, **kw), app_key="app-key-x", wall_now=self.wall)


class TestSaxoAuthClientTransport(unittest.TestCase):
    def test_exchange_code_posts_authorization_code_grant_with_basic_auth(self):
        client, session = _make_auth_client()

        bundle = client.exchange_code("the-code", _REDIRECT_URI)

        call = session.calls[0]
        self.assertEqual(call["url"], f"{SIM_AUTH_BASE_URL}/token")
        self.assertEqual(
            call["data"],
            {
                "grant_type": "authorization_code",
                "code": "the-code",
                "redirect_uri": _REDIRECT_URI,
            },
        )
        self.assertEqual(call["auth"], ("app-key-x", "app-secret-x"))
        self.assertEqual(bundle.access_token, "acc-token-1")
        self.assertEqual(bundle.refresh_token, "refresh-token-1")

    def test_refresh_posts_refresh_grant_including_redirect_uri(self):
        client, session = _make_auth_client()

        client.refresh("refresh-token-0", _REDIRECT_URI)

        call = session.calls[0]
        self.assertEqual(
            call["data"],
            {
                "grant_type": "refresh_token",
                "refresh_token": "refresh-token-0",
                "redirect_uri": _REDIRECT_URI,
            },
        )
        self.assertEqual(call["auth"], ("app-key-x", "app-secret-x"))

    def test_4xx_raises_auth_error_immediately_without_retry(self):
        client, session = _make_auth_client(_RecordingAuthSession([_FakeResponse(401, {})]))

        with self.assertRaises(SaxoAuthError) as ctx:
            client.refresh("refresh-token-0", _REDIRECT_URI)
        self.assertEqual(len(session.calls), 1, "4xx = chain lost; never retried")
        self.assertIn(_REAUTH_HINT, str(ctx.exception))

    def test_5xx_retried_exactly_once_then_auth_error(self):
        client, session = _make_auth_client(
            _RecordingAuthSession([_FakeResponse(503, {}), _FakeResponse(503, {})])
        )

        with self.assertRaises(SaxoAuthError):
            client.refresh("refresh-token-0", _REDIRECT_URI)
        self.assertEqual(len(session.calls), 2)

    def test_connect_timeout_retried_once_then_succeeds(self):
        client, session = _make_auth_client(
            _RecordingAuthSession([requests.exceptions.ConnectTimeout("boom"), _FakeResponse()])
        )

        bundle = client.refresh("refresh-token-0", _REDIRECT_URI)

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(bundle.access_token, "acc-token-1")

    def test_expiry_fields_read_from_every_response_not_hardcoded(self):
        payload = dict(_CANNED_TOKEN_RESPONSE, expires_in=999, refresh_token_expires_in=1234)
        client, _ = _make_auth_client(_RecordingAuthSession([_FakeResponse(200, payload)]))

        bundle = client.exchange_code("c", _REDIRECT_URI)

        self.assertEqual(bundle.expires_in, 999)
        self.assertEqual(bundle.refresh_token_expires_in, 1234)

    def test_missing_response_fields_raise_auth_error_without_token_leak(self):
        client, _ = _make_auth_client(
            _RecordingAuthSession([_FakeResponse(200, {"access_token": "leaky-secret"})])
        )

        with self.assertRaises(SaxoAuthError) as ctx:
            client.exchange_code("c", _REDIRECT_URI)
        self.assertNotIn("leaky-secret", str(ctx.exception))


class TestAuthorizeUrl(unittest.TestCase):
    def test_authorize_url_carries_code_grant_params_and_no_scope(self):
        client, _ = _make_auth_client()

        url = client.build_authorize_url(_REDIRECT_URI, "state-xyz")

        parts = urllib.parse.urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}", SIM_AUTH_BASE_URL)
        self.assertEqual(parts.path, "/authorize")
        params = urllib.parse.parse_qs(parts.query)
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["client_id"], ["app-key-x"])
        self.assertEqual(params["redirect_uri"], [_REDIRECT_URI])
        self.assertEqual(params["state"], ["state-xyz"])
        self.assertNotIn("scope", params)

    def test_generate_state_is_long_and_random(self):
        first, second = generate_state(), generate_state()
        self.assertGreaterEqual(len(first), 32)
        self.assertNotEqual(first, second)


class TestOAuthSimRail(unittest.TestCase):
    def test_live_auth_base_url_is_refused(self):
        live_marker = next(m for m in _LIVE_URL_MARKERS if "logonvalidation" in m)
        with self.assertRaises(SaxoLiveEnvironmentBlockedError):
            SaxoAuthClient("k", "s", auth_base_url=f"https://{live_marker}")

    def test_any_non_sim_auth_base_url_is_refused(self):
        with self.assertRaises(SaxoLiveEnvironmentBlockedError):
            SaxoAuthClient("k", "s", auth_base_url="https://example.com")

    def test_default_is_sim_and_matches_client_constant(self):
        client, _ = _make_auth_client()
        self.assertIsInstance(client, SaxoAuthClient)
        self.assertEqual(SIM_AUTH_BASE_URL, CLIENT_SIM_AUTH_BASE_URL)


class TestProactiveRefreshMargin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = _ProviderHarness(Path(self._tmp.name))
        self.h.seed(1)

    def test_first_get_adopts_valid_on_disk_token_without_refresh(self):
        token = self.h.provider.get_access_token()
        self.assertEqual(token, "acc-1")
        self.assertEqual(self.h.transport.refresh_calls, [])

    def test_token_cached_inside_margin_no_refresh(self):
        self.h.provider.get_access_token()
        self.h.mono.value = 1200 - REFRESH_MARGIN_S - 0.1
        self.assertEqual(self.h.provider.get_access_token(), "acc-1")
        self.assertEqual(self.h.transport.refresh_calls, [])

    def test_refresh_triggered_exactly_at_deadline_minus_margin(self):
        self.h.provider.get_access_token()
        self.h.mono.value = 1200 - REFRESH_MARGIN_S
        token = self.h.provider.get_access_token()
        self.assertEqual(len(self.h.transport.refresh_calls), 1)
        self.assertEqual(token, "acc-1")  # transport seq restarts at 1
        self.assertEqual(self.h.transport.refresh_calls[0], ("refresh-1", _REDIRECT_URI))

    def test_invalidate_forces_refresh_even_with_valid_disk_token(self):
        self.h.provider.get_access_token()
        self.h.provider.invalidate()
        self.h.provider.get_access_token()
        self.assertEqual(
            len(self.h.transport.refresh_calls),
            1,
            "a 401-rejected token must NOT be re-adopted from disk",
        )

    def test_refresh_failure_raises_auth_error_with_reauth_hint(self):
        self.h.provider.get_access_token()
        self.h.transport._bundles = [SaxoAuthError("token endpoint said no")]
        self.h.provider.invalidate()
        with self.assertRaises(SaxoAuthError) as ctx:
            self.h.provider.get_access_token()
        self.assertIn(_REAUTH_HINT, str(ctx.exception))

    def test_refresh_now_rotates_even_with_a_valid_disk_token(self):
        """The keep-alive primitive must EXTEND the chain, never adopt."""
        self.h.seed(10)  # distinct seed so rotation is observable on disk
        token = self.h.provider.refresh_now()
        self.assertEqual(len(self.h.transport.refresh_calls), 1)
        self.assertEqual(token, "acc-1")
        on_disk = json.loads(self.h.store.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["refresh_token"], "refresh-1")

    def test_dead_refresh_chain_fails_fast_without_network(self):
        self.h.wall.value += dt.timedelta(seconds=2401)  # past refresh_token_expires_in
        self.h.mono.value = 5000.0
        with self.assertRaises(SaxoAuthError) as ctx:
            self.h.provider.get_access_token()
        self.assertEqual(self.h.transport.refresh_calls, [], "dead chain = no doomed HTTP call")
        self.assertIn(_REAUTH_HINT, str(ctx.exception))


class TestRefreshRotationPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = _ProviderHarness(Path(self._tmp.name))
        self.h.seed(1)

    def _on_disk(self) -> dict[str, Any]:
        return json.loads(self.h.store.path.read_text(encoding="utf-8"))

    def test_disk_holds_newest_refresh_token_after_each_rotation(self):
        self.h.provider.get_access_token()  # adopt seeded acc-1
        for expected_seq in (1, 2):
            self.h.provider.invalidate()
            self.h.provider.get_access_token()
            self.assertEqual(self._on_disk()["refresh_token"], f"refresh-{expected_seq}")

    def test_new_pair_persisted_before_provider_returns(self):
        self.h.provider.get_access_token()  # adopt the seeded token first
        self.h.provider.invalidate()
        with mock.patch.object(
            self.h.store, "save_bundle", side_effect=OSError("disk full")
        ) as save:
            with self.assertRaises(OSError):
                self.h.provider.get_access_token()
        self.assertEqual(save.call_count, 1, "persistence must precede returning the new token")

    def test_crashed_atomic_write_leaves_original_store_intact(self):
        before = self._on_disk()
        with mock.patch("os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.h.store.save_bundle(_bundle(9), app_key="app-key-x", wall_now=self.h.wall)
        self.assertEqual(self._on_disk(), before)
        leftovers = [p for p in self.h.store.path.parent.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [], "failed replace must unlink its temp file")


class TestTokenStorePerms(unittest.TestCase):
    def test_store_file_is_owner_rw_only_and_parent_auto_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "nested" / "deeper" / "token_store.json")
            store.save_bundle(_bundle(), app_key="app-key-x")
            mode = stat.S_IMODE(store.path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_store_path_env_override_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = str(Path(tmp) / "custom.json")
            with mock.patch.dict(os.environ, {TOKEN_STORE_PATH_ENV: override}):
                self.assertEqual(resolve_token_store_path(), Path(override))

    def test_default_store_path_is_saxo_auth_dir(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            path = resolve_token_store_path()
        self.assertEqual(path.parent.name, "saxo_auth")
        self.assertEqual(path.name, "token_store.json")


class TestTokenStoreCorruption(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TokenStore(Path(self._tmp.name) / "token_store.json")

    def _assert_corrupt(self):
        with self.assertRaises(SaxoAuthError) as ctx:
            self.store.load(expected_fingerprint=app_key_fingerprint("app-key-x"))
        message = str(ctx.exception)
        self.assertIn(str(self.store.path), message)
        self.assertIn(_REAUTH_HINT, message)
        return message

    def test_absent_store_loads_as_none(self):
        self.assertIsNone(self.store.load())

    def test_garbage_json_is_corrupt(self):
        self.store.path.write_text("{not json", encoding="utf-8")
        self._assert_corrupt()

    def test_wrong_schema_version_is_corrupt(self):
        self.store.save_bundle(_bundle(), app_key="app-key-x")
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        payload["schema_version"] = 99
        self.store.path.write_text(json.dumps(payload), encoding="utf-8")
        self._assert_corrupt()

    def test_non_sim_environment_is_refused(self):
        self.store.save_bundle(_bundle(), app_key="app-key-x")
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        payload["environment"] = "live"
        self.store.path.write_text(json.dumps(payload), encoding="utf-8")
        self._assert_corrupt()

    def test_foreign_app_fingerprint_is_refused(self):
        self.store.save_bundle(_bundle(), app_key="some-other-app-key")
        message = self._assert_corrupt()
        self.assertNotIn("some-other-app-key", message, "never echo the app key")


class TestCrossProcessAdoption(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_second_provider_adopts_instead_of_burning_a_rotation(self):
        a = _ProviderHarness(self.tmp)
        a.seed(10)
        a.provider.get_access_token()  # adopt seeded acc-10
        a.provider.invalidate()  # 401 hint: force A to rotate acc-10 -> acc-1
        a.provider.get_access_token()
        self.assertEqual(len(a.transport.refresh_calls), 1)

        b = _ProviderHarness(self.tmp, transport=_BombTransport())
        token = b.provider.get_access_token()

        self.assertEqual(token, a.provider.get_access_token())

    def test_stuck_lock_times_out_with_actionable_error(self):
        store = TokenStore(
            self.tmp / "token_store.json",
            lock_timeout_s=0.05,
            lock_poll_interval_s=0.01,
        )
        store.save_bundle(_bundle(), app_key="app-key-x")
        fd = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(SaxoAuthError) as ctx:
                with store.exclusive_lock():
                    pass
            self.assertIn("stuck", str(ctx.exception))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_lock_file_is_a_sibling_not_the_store_inode(self):
        store = TokenStore(self.tmp / "token_store.json")
        self.assertEqual(store.lock_path, self.tmp / "token_store.lock")


class TestChainLossAlert(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = _ProviderHarness(Path(self._tmp.name))

    def test_dead_chain_alerts_exactly_once_per_process(self):
        # BOTH tokens expired: >40 min gap kills the chain AND the access token.
        self.h.seed(1, expires_in=0, refresh_expires_in=0)
        self.h.wall.value += dt.timedelta(seconds=1)
        for _ in range(3):
            with self.assertRaises(SaxoAuthError):
                self.h.provider.get_access_token()
        self.assertEqual(len(self.h.alerts), 1)
        self.assertIn(_REAUTH_HINT, self.h.alerts[0])

    def test_alert_exceptions_are_swallowed(self):
        def _boom(_msg: str) -> None:
            raise RuntimeError("telegram down")

        harness = _ProviderHarness(Path(self._tmp.name) / "sub")
        harness.provider = OAuthTokenProvider(
            harness.transport,
            harness.store,
            redirect_uri=_REDIRECT_URI,
            now=harness.mono,
            wall_now=harness.wall,
            alert=_boom,
        )
        with self.assertRaises(SaxoAuthError) as ctx:
            harness.provider.get_access_token()  # absent store = dead chain
        self.assertIn(_REAUTH_HINT, str(ctx.exception))


class TestOAuthProviderFromEnv(unittest.TestCase):
    def test_missing_app_key_raises_naming_the_var(self):
        env = {APP_SECRET_ENV: "s", REDIRECT_URL_ENV: _REDIRECT_URI}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SaxoAuthError) as ctx:
                OAuthTokenProvider.from_env()
        self.assertIn(APP_KEY_ENV, str(ctx.exception))
        self.assertNotIn("s", str(ctx.exception).split(APP_KEY_ENV)[-1][:2])

    def test_missing_secret_raises_naming_the_var_without_echoing_values(self):
        env = {APP_KEY_ENV: "value-of-key", REDIRECT_URL_ENV: _REDIRECT_URI}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SaxoAuthError) as ctx:
                OAuthTokenProvider.from_env()
        self.assertIn(APP_SECRET_ENV, str(ctx.exception))
        self.assertNotIn("value-of-key", str(ctx.exception))

    def test_from_env_builds_a_provider_with_store_at_resolved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = str(Path(tmp) / "store.json")
            env = {
                APP_KEY_ENV: "k",
                APP_SECRET_ENV: "s",
                REDIRECT_URL_ENV: _REDIRECT_URI,
                TOKEN_STORE_PATH_ENV: override,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                provider = OAuthTokenProvider.from_env()
        self.assertIsInstance(provider, OAuthTokenProvider)


if __name__ == "__main__":
    unittest.main()
