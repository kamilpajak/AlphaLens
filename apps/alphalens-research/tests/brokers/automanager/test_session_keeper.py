"""Hermetic tests for the auto-manager session-keeper (token-chain liveness).

ensure_alive delegates to get_access_token (provider self-refreshes at
expires_in - 120s internally); keep_alive delegates to refresh_now. A lost
chain surfaces as ChainStatus(alive=False, reason=...), never an exception.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.session_keeper import ChainStatus, SessionKeeper
from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError


class _StubProvider:
    def __init__(self, *, error: Exception | None = None):
        self._error = error
        self.get_calls = 0
        self.refresh_calls = 0

    def get_access_token(self) -> str:
        self.get_calls += 1
        if self._error is not None:
            raise self._error
        return "tok-access"

    def refresh_now(self) -> str:
        self.refresh_calls += 1
        if self._error is not None:
            raise self._error
        return "tok-refreshed"


class SessionKeeperEnsureAliveTests(unittest.TestCase):
    def test_ensure_alive_delegates_and_reports_alive(self) -> None:
        provider = _StubProvider()
        status = SessionKeeper(provider).ensure_alive()
        self.assertEqual(status, ChainStatus(alive=True, reason=None))
        self.assertEqual(provider.get_calls, 1)
        self.assertEqual(provider.refresh_calls, 0)

    def test_ensure_alive_dead_chain_returns_not_alive_with_reason(self) -> None:
        status = SessionKeeper(
            _StubProvider(error=SaxoAuthError("Saxo OAuth refresh chain lost"))
        ).ensure_alive()
        self.assertFalse(status.alive)
        self.assertIsNotNone(status.reason)
        self.assertIn("chain lost", status.reason)

    def test_ensure_alive_does_not_leak_saxo_auth_error(self) -> None:
        try:
            SessionKeeper(_StubProvider(error=SaxoAuthError("dead"))).ensure_alive()
        except SaxoAuthError:
            self.fail("ensure_alive must translate SaxoAuthError into ChainStatus")


class SessionKeeperKeepAliveTests(unittest.TestCase):
    def test_keep_alive_delegates_to_refresh_now(self) -> None:
        provider = _StubProvider()
        status = SessionKeeper(provider).keep_alive()
        self.assertEqual(status, ChainStatus(alive=True, reason=None))
        self.assertEqual(provider.refresh_calls, 1)
        self.assertEqual(provider.get_calls, 0)

    def test_keep_alive_dead_chain_returns_not_alive(self) -> None:
        status = SessionKeeper(
            _StubProvider(error=SaxoAuthError("refresh token expired (>40 min gap)"))
        ).keep_alive()
        self.assertFalse(status.alive)
        self.assertIn("40 min", status.reason)


if __name__ == "__main__":
    unittest.main()
