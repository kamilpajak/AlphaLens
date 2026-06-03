"""Canonical Saxo OpenAPI HTTP client.

Single source of truth for every Saxo Bank OpenAPI call in the project
(OAuth ``/token`` exchanges + the read-only ``/port/v1/users/me`` probe). The
order/trading surface is OUT of scope for this PR — only the auth + renewal
foundation ships (locked design 2026-06-03 §Scope).

What this client owns:

* **Per-env hardcoded endpoints.** The auth host and OpenAPI gateway base URL
  are looked up from a fixed table keyed on ``sim`` / ``live`` — NEVER derived
  from an env-var string. This blocks the SIM/LIVE mixup class (a ``-e
  SAXO_ENV`` empty-string-forward must not silently select the wrong host).
  Endpoints verified against developer.saxo/openapi/learn/environments
  2026-06-03.
* **The ``_redact`` boundary (CRITICAL, secret-leak Finding 1).** The
  ``/token`` request body + headers and ANY response body NEVER reach an
  exception message or a log line raw — only ``status_code`` + the parsed
  OAuth ``error`` / ``error_description``. This client deliberately does NOT
  mirror the polygon ``f"...{resp.text[:200]}"`` raise idiom; a leaked refresh
  token in journald is a live-brokerage secret exposure.
* **A STRICT permanent/transient classifier.** ``/token`` is permanent (=>
  manual re-auth) ONLY on ``status in {400,401}`` AND a parseable JSON body
  AND ``error in {invalid_grant, invalid_client}``. Everything else (non-JSON
  proxy bodies, 5xx, 429, connect/timeout, ``temporarily_unavailable``,
  ``invalid_request``) is TRANSIENT — retried under the manager's deadline
  budget, never sets the sticky reauth flag.
* **The ``httpx.Client`` lifecycle + ``_transport`` seam** (mirrors
  :mod:`openrouter_client`): tests inject ``httpx.MockTransport(handler)``;
  production passes nothing. ``from_env`` + a module-level singleton +
  ``atexit`` close + ``_reset_default_client_for_tests``.

The error hierarchy (``SaxoClientError`` + subclasses) is shared with
:mod:`saxo_token_manager` and :mod:`saxo_token_store`.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

APP_KEY_ENV = "SAXO_APP_KEY"
REDIRECT_URI_ENV = "SAXO_REDIRECT_URI"
ENV_ENV = "SAXO_ENV"
ALLOW_LIVE_ENV = "SAXO_ALLOW_LIVE"
APP_SECRET_ENV = "SAXO_APP_SECRET"

# Per-env endpoint table. Hardcoded — NEVER built from an env string (the
# SIM/LIVE mixup guard). Verified against
# https://www.developer.saxo/openapi/learn/environments (2026-06-03).
_ENDPOINTS: dict[str, dict[str, str]] = {
    "sim": {
        "auth_base_url": "https://sim.logonvalidation.net",
        "gateway_base_url": "https://gateway.saxobank.com/sim/openapi",
    },
    "live": {
        "auth_base_url": "https://live.logonvalidation.net",
        "gateway_base_url": "https://gateway.saxobank.com/openapi",
    },
}
VALID_ENVIRONMENTS = tuple(_ENDPOINTS)

TOKEN_PATH = "/token"  # URL path, not a secret
AUTHORIZE_PATH = "/authorize"
USERS_ME_PATH = "/port/v1/users/me"

# OAuth error codes that mean "the chain is permanently broken, a human must
# re-auth". Everything else is transient (retry under the deadline budget).
_PERMANENT_OAUTH_ERRORS = frozenset({"invalid_grant", "invalid_client"})
_PERMANENT_STATUSES = frozenset({400, 401})

# HTTP timeouts: connect 5s + read 10s = 15s total, WELL BELOW the store
# lock-acquire timeout so a hung TLS call always releases via finally-unlock
# (network-partition + rotation-race Finding 4).
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

ErrorClass = Literal["transient", "permanent"]

# Tokens are opaque, reasonably long, base64url-ish strings. The redactor
# scrubs anything that looks like a token grant field value as well as long
# opaque blobs, so even an unexpected echo path is covered.
_REDACT_FIELD_RE = re.compile(
    r"((?:access_token|refresh_token|code|client_secret|assertion)=)[^\s&\"']+",
    re.IGNORECASE,
)
_REDACT_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
# Long opaque run (>=20 non-space chars containing no obvious word break) —
# a defensive catch-all for a token echoed without a field name.
_REDACT_BLOB_RE = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")
_REDACTED = "[REDACTED]"


class SaxoClientError(RuntimeError):
    """Base class for every Saxo client/manager/store failure surfaced here."""


class SaxoTransientError(SaxoClientError):
    """A retryable failure (5xx, 429, non-JSON body, connect/timeout)."""


class SaxoReauthRequiredError(SaxoClientError):
    """The token chain is permanently broken — a human must re-auth.

    The future exit manager is CONTRACTUALLY required to treat this as
    'positions now unmanaged' (emit ``alphalens_saxo_positions_unmanaged`` +
    attempt one best-effort protective flatten with the still-valid access
    token before going dark). It must never be swallowed.
    """

    def __init__(self, message: str, *, reason: str = "server_rejected") -> None:
        super().__init__(message)
        self.reason = reason


class SaxoTokenContractError(SaxoClientError):
    """A ``/token`` 2xx response was missing a mandatory field (e.g. no
    ``refresh_token`` on a rotation)."""


class SaxoLockUnavailableError(SaxoClientError):
    """Re-exported from the store for a single import surface.

    The cross-process lock could not be acquired / created — the caller MUST
    NOT refresh (an unsynchronised ``/token`` burns the rotating token).
    """


class SaxoConfigError(SaxoClientError):
    """Missing / invalid configuration (env vars, environment name)."""


class SaxoEnvironmentMismatchError(SaxoClientError):
    """A persisted record's ``environment`` does not match the requested env."""


class SaxoBootstrapNeededError(SaxoClientError):
    """No usable token chain on disk — run ``alphalens saxo auth``."""


class SaxoAuthError(SaxoClientError):
    """A gateway call returned 401 with a token that should have been valid."""


def _redact(text: str) -> str:
    """Scrub token material from a string before it reaches a log/exception.

    Removes ``<field>=<value>`` grant params, ``Bearer <token>`` headers, and
    any long opaque blob. Idempotent. The redaction test pins that the
    SENTINEL token never survives this; the positive control proves the helper
    is not an identity function.
    """
    redacted = _REDACT_FIELD_RE.sub(rf"\1{_REDACTED}", text)
    redacted = _REDACT_BEARER_RE.sub(rf"\1{_REDACTED}", redacted)
    redacted = _REDACT_BLOB_RE.sub(_REDACTED, redacted)
    return redacted


def _parse_oauth_error(response: httpx.Response) -> tuple[str | None, str | None]:
    """Return (error, error_description) from a JSON ``/token`` error body.

    Returns (None, None) if the body is not parseable JSON — the classifier
    then treats it as transient (an off-shape proxy page, not a real
    invalid_grant).
    """
    try:
        body = response.json()
    except (ValueError, httpx.DecodingError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    desc = body.get("error_description")
    return (str(error) if error is not None else None, str(desc) if desc is not None else None)


def classify_token_error(response: httpx.Response) -> ErrorClass:
    """Classify a non-2xx ``/token`` response (network-partition Finding 2).

    Permanent ONLY on ``status in {400,401}`` AND parseable JSON AND
    ``error in {invalid_grant, invalid_client}``. Everything else transient.
    """
    if response.status_code not in _PERMANENT_STATUSES:
        return "transient"
    error, _desc = _parse_oauth_error(response)
    if error is not None and error in _PERMANENT_OAUTH_ERRORS:
        return "permanent"
    return "transient"


class SaxoClient:
    """Thin httpx wrapper around Saxo's OAuth + read-only gateway surface.

    Stateless apart from the ``httpx.Client`` (connection pool). Holds NO
    token — callers pass the refresh/access token per call so the single
    source of token truth stays the store/manager. ``_transport`` is the
    test-only seam (``httpx.MockTransport``); production passes nothing.
    """

    def __init__(
        self,
        *,
        app_key: str,
        redirect_uri: str,
        environment: str,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        if environment not in _ENDPOINTS:
            raise SaxoConfigError(
                f"{ENV_ENV} must be one of {VALID_ENVIRONMENTS} (got {environment!r}); "
                "empty / unknown values are rejected, never defaulted."
            )
        if not app_key:
            raise SaxoConfigError(f"{APP_KEY_ENV} must be a non-empty app key.")
        if not redirect_uri:
            raise SaxoConfigError(f"{REDIRECT_URI_ENV} must be a non-empty redirect URI.")
        self.app_key = app_key
        self.redirect_uri = redirect_uri
        self.environment = environment
        self.auth_base_url = _ENDPOINTS[environment]["auth_base_url"]
        self.gateway_base_url = _ENDPOINTS[environment]["gateway_base_url"]
        # One client bound to the auth host (token exchanges); gateway calls
        # pass an absolute URL so a single pool serves both.
        self._http = httpx.Client(
            base_url=self.auth_base_url,
            timeout=_DEFAULT_TIMEOUT,
            transport=_transport,
        )

    @classmethod
    def from_env(cls) -> SaxoClient:
        """Build a client from ``SAXO_*`` env vars.

        SAXO_ENV required (no silent sim default); empty string rejected; live
        requires an affirmative ``SAXO_ALLOW_LIVE``. ``SAXO_APP_SECRET`` MUST
        be absent (PKCE is mandated — no long-lived secret on disk).
        """
        environment = os.environ.get(ENV_ENV)
        if not environment:
            raise SaxoConfigError(
                f"{ENV_ENV} is required and must be non-empty (sim|live); "
                "it is never defaulted to sim."
            )
        if environment == "live" and os.environ.get(ALLOW_LIVE_ENV) not in ("1", "true", "yes"):
            raise SaxoConfigError(
                f"{ENV_ENV}=live requires an affirmative {ALLOW_LIVE_ENV}=1 "
                "(second guard against an accidental live run)."
            )
        if os.environ.get(APP_SECRET_ENV):
            raise SaxoConfigError(
                f"{APP_SECRET_ENV} is present but PKCE is mandated — the "
                "Code-grant app secret and the PKCE path are mutually "
                "exclusive. Remove the secret from the environment."
            )
        app_key = os.environ.get(APP_KEY_ENV)
        redirect_uri = os.environ.get(REDIRECT_URI_ENV)
        if not app_key:
            raise SaxoConfigError(f"{APP_KEY_ENV} environment variable is not set.")
        if not redirect_uri:
            raise SaxoConfigError(f"{REDIRECT_URI_ENV} environment variable is not set.")
        return cls(app_key=app_key, redirect_uri=redirect_uri, environment=environment)

    # --- token exchanges ----------------------------------------------------

    def refresh_token(self, *, refresh_token: str) -> dict[str, Any]:
        """POST grant_type=refresh_token; return the parsed token payload.

        Raises :class:`SaxoReauthRequiredError` on a permanent classification,
        :class:`SaxoTransientError` otherwise. The request body/headers and the
        response body are NEVER surfaced raw (the ``_redact`` boundary).
        """
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.app_key,
            }
        )

    def exchange_code(self, *, code: str, code_verifier: str) -> dict[str, Any]:
        """POST grant_type=authorization_code (PKCE); return token payload."""
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.app_key,
                "code_verifier": code_verifier,
            }
        )

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._http.post(TOKEN_PATH, data=data)
        except httpx.HTTPError as exc:
            # Connect / read / timeout — transient. _redact the message in case
            # httpx echoed the URL with query material (it does not for POST,
            # but be defensive).
            raise SaxoTransientError(f"saxo /token transport error: {_redact(str(exc))}") from None
        if response.status_code // 100 == 2:
            return self._parse_token_payload(response)
        # Non-2xx: classify, NEVER include the raw body/headers. Only the
        # short OAuth ``error`` CODE is surfaced — the free-text
        # ``error_description`` is attacker/server-controlled and Saxo echoes
        # the submitted refresh_token into it, so it is dropped entirely (even
        # _redact'd it would still carry the literal ``refresh_token=`` prefix
        # that the redaction test bans). The code alone is the actionable bit.
        error, _desc = _parse_oauth_error(response)
        summary = (
            f"saxo /token HTTP {response.status_code} "
            f"error={_redact(error) if error else 'unknown'}"
        )
        if classify_token_error(response) == "permanent":
            logger.warning("saxo refresh permanently rejected: %s", summary)
            raise SaxoReauthRequiredError(summary, reason="server_rejected")
        logger.info("saxo /token transient failure: %s", summary)
        raise SaxoTransientError(summary)

    @staticmethod
    def _parse_token_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, httpx.DecodingError) as exc:
            # A 2xx with an unparseable body is a contract break — but never
            # echo the body.
            raise SaxoTokenContractError("saxo /token returned 2xx with a non-JSON body") from exc
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise SaxoTokenContractError(
                "saxo /token 2xx payload is missing the access_token field"
            )
        return payload

    # --- read-only gateway probe -------------------------------------------

    def get_user_me(self, *, access_token: str) -> dict[str, Any]:
        """GET ``/port/v1/users/me`` — the zero-blast-radius probe.

        Proves the whole token -> bearer -> authenticated-2xx loop. A 401
        raises :class:`SaxoAuthError`; other non-2xx raise
        :class:`SaxoTransientError`. No token material is logged.
        """
        url = f"{self.gateway_base_url}{USERS_ME_PATH}"
        try:
            response = self._http.get(url, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as exc:
            raise SaxoTransientError(
                f"saxo users/me transport error: {_redact(str(exc))}"
            ) from None
        if response.status_code == 401:
            raise SaxoAuthError("saxo users/me returned 401 (access token rejected)")
        if response.status_code // 100 != 2:
            raise SaxoTransientError(f"saxo users/me HTTP {response.status_code}")
        try:
            payload = response.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise SaxoTransientError("saxo users/me returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise SaxoTransientError("saxo users/me payload is not a JSON object")
        return payload

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        """Build the PKCE/S256 ``/authorize`` URL for the bootstrap flow.

        The host comes from the hardcoded per-env table, never an env string.
        """
        from urllib.parse import urlencode

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.app_key,
                "redirect_uri": self.redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        return f"{self.auth_base_url}{AUTHORIZE_PATH}?{query}"

    def close(self) -> None:
        self._http.close()


# Module-level lazy singleton (mirrors openrouter_client).
_DEFAULT_CLIENT: SaxoClient | None = None


def get_default_saxo_client() -> SaxoClient:
    """Return the process-wide default :class:`SaxoClient` (from env)."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = SaxoClient.from_env()
        atexit.register(_DEFAULT_CLIENT.close)
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None


__all__ = [
    "ALLOW_LIVE_ENV",
    "APP_KEY_ENV",
    "APP_SECRET_ENV",
    "AUTHORIZE_PATH",
    "ENV_ENV",
    "REDIRECT_URI_ENV",
    "TOKEN_PATH",
    "USERS_ME_PATH",
    "VALID_ENVIRONMENTS",
    "SaxoAuthError",
    "SaxoBootstrapNeededError",
    "SaxoClient",
    "SaxoClientError",
    "SaxoConfigError",
    "SaxoEnvironmentMismatchError",
    "SaxoLockUnavailableError",
    "SaxoReauthRequiredError",
    "SaxoTokenContractError",
    "SaxoTransientError",
    "classify_token_error",
    "get_default_saxo_client",
]
