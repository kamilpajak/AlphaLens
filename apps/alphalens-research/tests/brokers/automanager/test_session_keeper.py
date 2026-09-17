"""Hermetic tests for the auto-manager session-keeper (token-chain liveness).

ensure_alive delegates to get_access_token (provider self-refreshes at
expires_in - 120s internally). A lost
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

    def get_access_token(self) -> str:
        self.get_calls += 1
        if self._error is not None:
            raise self._error
        return "tok-access"


class SessionKeeperEnsureAliveTests(unittest.TestCase):
    def test_ensure_alive_delegates_and_reports_alive(self) -> None:
        provider = _StubProvider()
        status = SessionKeeper(provider).ensure_alive()
        self.assertEqual(status, ChainStatus(alive=True, reason=None))
        self.assertEqual(provider.get_calls, 1)

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


if __name__ == "__main__":
    unittest.main()
