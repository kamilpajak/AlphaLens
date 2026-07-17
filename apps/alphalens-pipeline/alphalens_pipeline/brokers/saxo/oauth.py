"""Saxo OAuth authorization-code transport — the ONLY token-endpoint HTTP.

:class:`SaxoAuthClient` owns the three OAuth exchanges against the SIM
authentication host (``sim.logonvalidation.net``): building the ``/authorize``
URL, exchanging the one-shot ``code`` for the first token pair, and refreshing
with the (single-use, rotating) refresh token. Everything above it —
scheduling, persistence, adoption — lives in
:class:`~alphalens_pipeline.brokers.saxo.tokens.OAuthTokenProvider`.

Layering: ``client -> tokens -> oauth -> errors`` (one-way; ``client.py``
never imports this module). Enforced conventions:

- **SIM-only structural rail (ADR 0014):** the constructor accepts ONLY
  :data:`SIM_AUTH_BASE_URL` (equality check, same doctrine as the gateway
  rail in ``client.py``). No env var or constructor path reaches LIVE.
- **Secrets hygiene:** the AppSecret travels only in the per-request Basic
  auth pair; tokens never appear in logs, URLs, or exception messages.
- **Chain-lost semantics:** ANY 4xx from the token endpoint means the refresh
  chain is dead (revoked, secret rotated, or the single-use token was already
  burned) — raised immediately as :class:`SaxoAuthError`, never retried.
  Exactly one retry is allowed for provably-transient failures (connect
  timeout / connection-phase error / 5xx).

Canonical-surface enforcement: ``tests/test_no_raw_saxo_http.py`` names this
module (alongside ``client.py``) as the only files allowed to combine a Saxo
URL with raw HTTP.
"""

from __future__ import annotations

import logging
import secrets
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from alphalens_pipeline.brokers.saxo.errors import (
    SaxoAuthError,
    SaxoLiveEnvironmentBlockedError,
)

logger = logging.getLogger(__name__)

# SIM authentication host (authorize + token endpoints). Must stay equal to
# ``client.SIM_AUTH_BASE_URL`` — pinned by tests/brokers/test_saxo_oauth.py.
SIM_AUTH_BASE_URL = "https://sim.logonvalidation.net"

_TRANSPORT_RETRY_BACKOFF_S = 2.0
_REAUTH_INSTRUCTION = "re-run `alphalens broker auth`"


def generate_state() -> str:
    """CSRF ``state`` parameter for the authorize round-trip (256-bit)."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class TokenBundle:
    """One token-endpoint response — expiries as received, never hardcoded."""

    access_token: str
    expires_in: int
    refresh_token: str
    refresh_token_expires_in: int


class SaxoAuthClient:
    """Canonical transport for the Saxo SIM OAuth endpoints (Code grant)."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        auth_base_url: str = SIM_AUTH_BASE_URL,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if auth_base_url != SIM_AUTH_BASE_URL:
            raise SaxoLiveEnvironmentBlockedError(
                f"SaxoAuthClient is SIM-only: auth_base_url must be "
                f"{SIM_AUTH_BASE_URL!r}, got {auth_base_url!r}. The LIVE "
                "authentication host is structurally unreachable; lifting the "
                "rail requires its own future ADR (see ADR 0014)."
            )
        if not app_key or not app_secret:
            raise SaxoAuthError("SaxoAuthClient requires a non-empty app key and app secret")
        self._app_key = app_key
        self._app_secret = app_secret
        self._auth_base_url = auth_base_url
        self._timeout = timeout
        self._session = session or requests.Session()
        self._sleep = sleep

    @property
    def app_key(self) -> str:
        """AppKey (public identifier) — used for client_id + store fingerprint."""
        return self._app_key

    def build_authorize_url(self, redirect_uri: str, state: str) -> str:
        """The browser URL for the attended login (no ``scope`` — Saxo has none)."""
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self._app_key,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{self._auth_base_url}/authorize?{query}"

    def exchange_code(self, code: str, redirect_uri: str) -> TokenBundle:
        """POST ``/token`` with ``grant_type=authorization_code``."""
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )

    def refresh(self, refresh_token: str, redirect_uri: str) -> TokenBundle:
        """POST ``/token`` with ``grant_type=refresh_token``.

        The body INCLUDES ``redirect_uri`` (Saxo requires it on the refresh
        grant too). The response carries a NEW single-use refresh token — the
        one passed in is dead the moment this returns.
        """
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "redirect_uri": redirect_uri,
            }
        )

    # ----- internals -----

    def _token_request(self, form: dict[str, str]) -> TokenBundle:
        url = f"{self._auth_base_url}/token"
        grant = form.get("grant_type", "?")
        last_exc: Exception | None = None
        for attempt in range(2):  # 1 + exactly one transient retry
            try:
                # Basic auth built per-request by requests from the pair —
                # the credentials never enter a URL or a log line.
                resp = self._session.post(
                    url,
                    data=form,
                    auth=(self._app_key, self._app_secret),
                    headers={"Accept": "application/json"},
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        "saxo token endpoint transport error on %s (retrying once): %s",
                        grant,
                        exc.__class__.__name__,
                    )
                    self._sleep(_TRANSPORT_RETRY_BACKOFF_S)
                    continue
                raise SaxoAuthError(
                    f"Saxo token endpoint unreachable after one retry "
                    f"({exc.__class__.__name__}) on {grant}"
                ) from exc
            if 500 <= resp.status_code < 600:
                if attempt == 0:
                    logger.warning(
                        "saxo token endpoint %d on %s (retrying once)", resp.status_code, grant
                    )
                    self._sleep(_TRANSPORT_RETRY_BACKOFF_S)
                    continue
                raise SaxoAuthError(
                    f"Saxo token endpoint {resp.status_code} persisted after one retry on {grant}"
                )
            if 400 <= resp.status_code < 500:
                # Saxo answers a revoked/burned/expired chain (and bad
                # credentials) with an undetailed 4xx — chain lost, no retry.
                raise SaxoAuthError(
                    f"Saxo token endpoint refused the {grant} request "
                    f"({resp.status_code}) — refresh chain lost or credentials "
                    f"invalid; {_REAUTH_INSTRUCTION}"
                )
            return self._parse_bundle(resp)
        raise SaxoAuthError(f"Saxo token endpoint unreachable on {grant}") from last_exc

    @staticmethod
    def _parse_bundle(resp: Any) -> TokenBundle:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SaxoAuthError("Saxo token endpoint returned a non-JSON body") from exc
        try:
            return TokenBundle(
                access_token=str(payload["access_token"]),
                expires_in=int(payload["expires_in"]),
                refresh_token=str(payload["refresh_token"]),
                refresh_token_expires_in=int(payload["refresh_token_expires_in"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # Field NAMES only — never echo the body (it may carry a token).
            raise SaxoAuthError(
                "Saxo token endpoint response is missing required fields "
                "(access_token / expires_in / refresh_token / "
                "refresh_token_expires_in)"
            ) from exc


__all__ = [
    "SIM_AUTH_BASE_URL",
    "SaxoAuthClient",
    "TokenBundle",
    "generate_state",
]
