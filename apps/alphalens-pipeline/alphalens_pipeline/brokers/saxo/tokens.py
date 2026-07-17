"""Pluggable Saxo token providers.

Today: :class:`StaticTokenProvider` over the 24-hour Developer-Portal SIM
token (``SAXO_SIM_TOKEN``). The provider seam is exactly where P4's
``RefreshingTokenProvider`` plugs in with ZERO client changes: OAuth
authorization-code grant, rotating refresh token with atomic newest-token
persistence to ``~/.alphalens/saxo/token_store.json``, renewal well before
``refresh_token_expires_in``, and a Telegram alert on refresh-chain loss
(re-solving the job of the ``alphalens-saxo-refresh`` unit removed by
ADR 0012).

The client's contract with a provider:

- ``get_access_token()`` is called per HTTP attempt (fresh headers each time);
- on a 401 the client calls ``invalidate()`` ONCE, retries with a fresh
  ``get_access_token()``, then raises ``SaxoAuthError``.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError

# Env var holding the 24h Developer-Portal SIM token. Named for its SCOPE
# (SIM), not its lifetime — P4's OAuth tokens are also SIM-scoped, so the
# name survives the provider swap.
TOKEN_ENV = "SAXO_SIM_TOKEN"


@runtime_checkable
class TokenProvider(Protocol):
    def get_access_token(self) -> str: ...

    def invalidate(self) -> None:
        """Hint that the last token was rejected (401)."""
        ...


class StaticTokenProvider:
    """A fixed token — the 24h Developer-Portal SIM token, pasted daily."""

    def __init__(self, token: str):
        if not token:
            raise SaxoAuthError("StaticTokenProvider requires a non-empty token")
        self._token = token

    @classmethod
    def from_env(cls) -> StaticTokenProvider:
        """Construct from ``SAXO_SIM_TOKEN``. Raises ``SaxoAuthError`` if unset."""
        token = os.environ.get(TOKEN_ENV)
        if not token:
            raise SaxoAuthError(
                f"{TOKEN_ENV} environment variable not set — generate a 24h SIM "
                "token at developer.saxo (expires daily until P4 OAuth lands)"
            )
        return cls(token)

    def get_access_token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        """No-op: a rejected static token means the 24h token expired — the
        operator regenerates it at developer.saxo; there is nothing to refresh
        in-process."""


__all__ = [
    "TOKEN_ENV",
    "StaticTokenProvider",
    "TokenProvider",
]
