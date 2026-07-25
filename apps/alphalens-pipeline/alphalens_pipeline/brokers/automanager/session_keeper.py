"""Session-keeper — OAuth token-chain liveness at the top of every tick.

Thin wrapper over the shipped OAuthTokenProvider. ensure_alive touches
get_access_token, which self-refreshes the access token at expires_in - 120s on
the provider's own clock (the keeper never re-implements that schedule).
keep_alive is the idle-timer primitive (the alphalens-saxo-refresh unit),
forcing an unconditional refresh_now during no-bracket stretches so the ~40min
refresh window never lapses. A lost chain raises a BrokerAuthError inside the
provider (the Saxo provider raises SaxoAuthError, a BrokerAuthError subclass,
and also fires the Telegram _chain_lost alert); the keeper catches the generic
base and TRANSLATES it into ChainStatus(alive=False, reason=...) so the loop
reads a verdict and stops placing, never crashes mid-tick. Catching the generic
type keeps this module broker-agnostic (no concrete-Saxo import).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from alphalens_pipeline.brokers.contract import BrokerAuthError


@dataclass(frozen=True)
class ChainStatus:
    alive: bool
    reason: str | None = None


@runtime_checkable
class _SessionProvider(Protocol):
    def get_access_token(self) -> str: ...
    def refresh_now(self) -> str: ...


class SessionKeeper:
    """Per-tick + idle-timer liveness gate over the OAuth token chain."""

    def __init__(self, provider: _SessionProvider):
        self._provider = provider

    def ensure_alive(self) -> ChainStatus:
        try:
            self._provider.get_access_token()
        except BrokerAuthError as exc:
            return ChainStatus(alive=False, reason=str(exc))
        return ChainStatus(alive=True, reason=None)

    def keep_alive(self) -> ChainStatus:
        try:
            self._provider.refresh_now()
        except BrokerAuthError as exc:
            return ChainStatus(alive=False, reason=str(exc))
        return ChainStatus(alive=True, reason=None)


__all__ = ["ChainStatus", "SessionKeeper"]
