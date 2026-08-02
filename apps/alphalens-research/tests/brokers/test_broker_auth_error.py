"""SaxoAuthError is-a generic BrokerAuthError, so generic catchers work.

The session-keeper (and any broker-agnostic consumer) catches the generic
``contract.BrokerAuthError`` — the Saxo-specific ``SaxoAuthError`` must be a
subclass so a lost Saxo OAuth chain is still translated into
``ChainStatus(alive=False, ...)`` rather than crashing the tick.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.session_keeper import ChainStatus, SessionKeeper
from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError
from broker_contract.contract import BrokerAuthError


class _StubProvider:
    def __init__(self, *, error: Exception | None = None):
        self._error = error

    def get_access_token(self) -> str:
        if self._error is not None:
            raise self._error
        return "tok-access"

    def refresh_now(self) -> str:
        if self._error is not None:
            raise self._error
        return "tok-refreshed"


class SaxoAuthErrorReparentTests(unittest.TestCase):
    def test_saxo_auth_error_is_a_broker_auth_error(self) -> None:
        self.assertIsInstance(SaxoAuthError("x"), BrokerAuthError)


class GenericCatchStillCatchesSaxoTests(unittest.TestCase):
    def test_ensure_alive_reports_dead_on_saxo_auth_error(self) -> None:
        status = SessionKeeper(
            _StubProvider(error=SaxoAuthError("Saxo OAuth refresh chain lost"))
        ).ensure_alive()
        self.assertEqual(status, ChainStatus(alive=False, reason="Saxo OAuth refresh chain lost"))

    def test_keep_alive_reports_dead_on_saxo_auth_error(self) -> None:
        status = SessionKeeper(
            _StubProvider(error=SaxoAuthError("refresh token expired"))
        ).keep_alive()
        self.assertFalse(status.alive)
        self.assertEqual(status.reason, "refresh token expired")


if __name__ == "__main__":
    unittest.main()
