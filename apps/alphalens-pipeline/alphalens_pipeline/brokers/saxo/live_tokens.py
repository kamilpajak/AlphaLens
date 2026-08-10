"""LIVE order-rail token adapter (design memo §2 "Token provider").

Adapts ``data.alt_data.saxo_marketdata_auth.LiveTokenProvider`` (built for
market-data reads: adopt-before-refresh under a per-host flock, but NO
in-memory rejected-token state and NO ``invalidate``) to the two contracts a
LIVE order-rail daemon needs from a token provider:

- ``brokers/saxo/tokens.py::TokenProvider`` — ``get_access_token`` /
  ``invalidate``, the shape :class:`~alphalens_pipeline.brokers.saxo.client.
  SaxoClient` calls on every HTTP attempt and after a 401.
- ``brokers/automanager/session_keeper.py``'s duck-typed surface —
  ``get_access_token`` / ``refresh_now`` — so ``SessionKeeper`` works
  unchanged over LIVE exactly as it does over SIM's ``OAuthTokenProvider``.

**The gap this closes (design memo §2, "a real gap found in review"):**
``LiveTokenProvider.access_token()`` re-reads the on-disk store on every
call and adopts a sibling's rotation when margin remains — flock-serialized
adopt-then-refresh, not a single designated refresher. But it keeps no
in-memory state of its own, so a bare 401 on a LIVE order has nothing to
invalidate: without this adapter, the same rejected disk token would be
re-read and re-sent forever. This module remembers the last-returned token
in-memory (mirrors the SIM ``OAuthTokenProvider`` rejected-token pattern,
``tokens.py:397-404``) and forces exactly ONE ``force_refresh()`` when the
disk store still holds that rejected token.

**Dead-latch (design memo §2, "the revoked-chain case must terminate, not
storm"):** the FIRST refresh failure — any exception surfaced by either
``access_token()`` or ``force_refresh()``, including a revoked-chain
``invalid_grant`` — fires the injected :data:`NotificationPort` alert
exactly once and latches the chain permanently dead for the rest of the
process (mirrors SIM ``tokens.py::OAuthTokenProvider._chain_lost``). Every
call after the latch raises immediately WITHOUT touching the underlying
provider again and WITHOUT re-alerting: a revoked LIVE refresh chain can
never turn into a refresh storm against Saxo. Recovery requires a process
restart after the LIVE market-data auth bootstrap is re-run.

Deliberately carries ZERO ``data.alt_data`` import: the underlying provider
is injected by the composition root and referenced here only through the
local, structural :class:`_UnderlyingLiveTokenProvider` Protocol — this
module never imports telegram either, matching house doctrine (the
composition root wires the concrete alert sink).
"""

from __future__ import annotations

import logging
from typing import NoReturn, Protocol, runtime_checkable

from alphalens_pipeline.brokers.notifications import NotificationPort
from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError

logger = logging.getLogger(__name__)

_LIVE_CHAIN_LOST_MESSAGE = (
    "[live] Saxo LIVE order-rail token chain lost (revoked, secret rotated, "
    "or the refresh call itself failed) — re-run the LIVE market-data auth "
    "bootstrap and restart the daemon; it will not retry the token endpoint "
    "until then"
)


def _log_chain_loss(message: str) -> None:
    """Default alert when no ``NotificationPort`` is injected — journald
    only, mirroring ``brokers/saxo/tokens.py::_log_chain_loss``. The
    composition root wires the concrete Telegram sink; this module never
    imports it."""
    logger.warning("saxo LIVE order-rail chain-loss (no alert sink injected): %s", message)


@runtime_checkable
class _UnderlyingLiveTokenProvider(Protocol):
    """Structural shape of ``saxo_marketdata_auth.LiveTokenProvider`` — kept
    as a LOCAL Protocol (never an import) so this module carries zero
    ``data.alt_data`` dependency; the concrete object is injected by the
    caller (composition root)."""

    def access_token(self) -> str: ...

    def force_refresh(self) -> str: ...


class LiveOrderTokenProvider:
    """Adapter over an injected ``LiveTokenProvider``-compatible object —
    see module docstring for the two contracts it satisfies and the gap it
    closes."""

    def __init__(
        self,
        underlying: _UnderlyingLiveTokenProvider,
        *,
        alert: NotificationPort | None = None,
    ):
        self._underlying = underlying
        self._alert = alert if alert is not None else _log_chain_loss
        self._rejected_token: str | None = None
        self._last_token: str | None = None
        self._dead = False
        self._alerted = False

    def get_access_token(self) -> str:
        """Per-HTTP-attempt token read. Delegates to ``access_token()``; if
        the disk store still holds the last ``invalidate()``-rejected token,
        forces exactly one ``force_refresh()`` under the underlying's own
        flock and returns the fresh token."""
        if self._dead:
            raise SaxoAuthError(_LIVE_CHAIN_LOST_MESSAGE)
        try:
            token = self._underlying.access_token()
        except Exception as exc:
            self._chain_lost(cause=exc)
        if token == self._rejected_token:
            try:
                token = self._underlying.force_refresh()
            except Exception as exc:
                self._chain_lost(cause=exc)
        self._last_token = token
        return token

    def invalidate(self) -> None:
        """401 hint from a LIVE order call: remember the last-returned token
        as rejected so ``get_access_token`` can never tight-loop on the same
        dead disk token. No network here — mirrors the SIM contract
        (invalidate-then-retry; the retry's ``get_access_token()`` refreshes
        synchronously)."""
        if self._dead:
            return
        self._rejected_token = self._last_token

    def refresh_now(self) -> str:
        """Unconditional rotation — the ``SessionKeeper.keep_alive``
        idle-timer primitive. Same dead-latch semantics as
        :meth:`get_access_token`."""
        if self._dead:
            raise SaxoAuthError(_LIVE_CHAIN_LOST_MESSAGE)
        try:
            token = self._underlying.force_refresh()
        except Exception as exc:
            self._chain_lost(cause=exc)
        self._last_token = token
        return token

    def _chain_lost(self, *, cause: Exception | None) -> NoReturn:
        if not self._alerted:
            self._alerted = True
            try:
                self._alert(_LIVE_CHAIN_LOST_MESSAGE)
            except Exception:
                logger.warning(
                    "saxo LIVE order-rail chain-loss alert callable failed", exc_info=True
                )
        self._dead = True
        raise SaxoAuthError(_LIVE_CHAIN_LOST_MESSAGE) from cause


__all__ = ["LiveOrderTokenProvider"]
