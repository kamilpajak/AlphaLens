"""Saxo OpenAPI HTTP client — single canonical entry point for every Saxo call.

Mirrors :mod:`alphalens_pipeline.data.alt_data.polygon_client` structurally
(which itself mirrors ``sec_edgar_client``): flat public methods, injected
``session`` + ``sleep`` DI, raw ``requests`` — deliberately NO third-party SDK
(no official Python SDK exists; the community ones are dead or stale with
ManualOrder-era drift risk). Enforced as the only Saxo HTTP surface by
``tests/test_no_raw_saxo_http.py``.

**SIM-only structural rail (ADR 0014):** the constructor accepts ONLY
:data:`SIM_BASE_URL`; there is deliberately no ``environment=`` switch and no
env var that reaches LIVE. ``LIVE_TRADING_ENABLED`` is flipped ONLY by a
future ADR lifting the rail (mirrors the research-side
``capital_deploy_clause`` — two independent gates, deliberately not
collapsed). Pinned by ``tests/brokers/test_saxo_sim_only_rail.py``.

Auth: Bearer token from a pluggable :class:`~.tokens.TokenProvider` — the
token travels ONLY in the ``Authorization`` header, never in URLs, logs, or
cache keys (polygon ``_strip_apikey_from_url`` discipline). On a 401 the
client calls ``invalidate()`` once, retries with a fresh token, then raises
:class:`SaxoAuthError`.

Throttle + retry (house dual-layer): proactive 0.5s min-interval spacing
(Saxo caps at 120 req/min per session per service group; 0.5s is the exact
ceiling, safe for a read-only daily driver), then Layer 1 status handling
(429 -> Retry-After / X-RateLimit-Session-Reset clamped to [1, 120]; 5xx ->
(5, 15, 30)), Layer 2 transient-network retries ((5, 15)). Every request
carries ``x-request-id`` (uuid4, stable across retries of one logical
request) — harmless on reads, and P2's 15-second duplicate-order 409 dedup
and never-blind-retry-a-POST rule inherit the header for free.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import requests

from alphalens_pipeline.brokers.saxo.errors import (
    SaxoAuthError,
    SaxoError,
    SaxoLiveEnvironmentBlockedError,
    SaxoRateLimitError,
)
from alphalens_pipeline.brokers.saxo.tokens import (
    TOKEN_ENV,
    StaticTokenProvider,
    TokenProvider,
)

logger = logging.getLogger(__name__)

SIM_BASE_URL = "https://gateway.saxobank.com/sim/openapi"
SIM_AUTH_BASE_URL = "https://sim.logonvalidation.net"

# LIVE hosts, named ONLY so the rail can refuse them explicitly and the rail
# test can scan for accidental reintroduction. This tuple is the single place
# a LIVE URL string may appear in the brokers package (pinned by
# tests/brokers/test_saxo_sim_only_rail.py).
_LIVE_URL_MARKERS = ("gateway.saxobank.com/openapi", "live.logonvalidation.net")

# Flipped ONLY by a future ADR lifting the SIM-only rail (see ADR 0014).
LIVE_TRADING_ENABLED = False

# Env var that must never be used to reach LIVE; from_env fails loudly on any
# value other than "sim" to catch operator .env confusion.
_SAXO_ENV_VAR = "SAXO_ENV"


class SaxoClient:
    """Canonical HTTP client for the Saxo OpenAPI SIM gateway.

    Single shared throttle + retry across every consumer; construction is
    refused for any base URL other than :data:`SIM_BASE_URL`.
    """

    _MIN_REQUEST_INTERVAL_S = 0.5  # 120 req/min ceiling per session per service group
    _MAX_REQUEST_ATTEMPTS = 4  # 1 + 3 retries
    _RATE_LIMIT_FLOOR_S = 1
    _RATE_LIMIT_CEILING_S = 120
    _SERVER_ERROR_BACKOFFS = (5, 15, 30)
    _NETWORK_ERROR_BACKOFFS = (5, 15)
    _TRANSIENT_NET_EXCEPTIONS = (requests.RequestException,)

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        base_url: str = SIM_BASE_URL,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if base_url != SIM_BASE_URL:
            raise SaxoLiveEnvironmentBlockedError(
                f"SaxoClient is SIM-only: base_url must be {SIM_BASE_URL!r}, got "
                f"{base_url!r}. The LIVE gateway is structurally unreachable; "
                "lifting the rail requires its own future ADR (see ADR 0014)."
            )
        self._token_provider = token_provider
        self._base_url = base_url
        self._timeout = timeout
        self._session = session or requests.Session()
        self._sleep = sleep
        self._last_call_ts: float = 0.0
        self._throttle_lock = threading.Lock()
        self._client_info: dict[str, Any] | None = None

    @classmethod
    def from_env(cls, **kw: Any) -> SaxoClient:
        """Construct with a :class:`StaticTokenProvider` from ``SAXO_SIM_TOKEN``.

        Defensive guard: a stray ``SAXO_ENV`` set to anything but ``"sim"``
        fails loudly — the variable is NOT an environment switch (there is
        none); its presence with a non-sim value signals operator .env
        confusion that must not be silently ignored.
        """
        env = os.environ.get(_SAXO_ENV_VAR)
        if env is not None and env.strip().lower() != "sim":
            raise SaxoLiveEnvironmentBlockedError(
                f"{_SAXO_ENV_VAR}={env!r} is set but SaxoClient is SIM-only; "
                "unset it or set it to 'sim'. Reaching LIVE requires a code "
                "change gated on a future ADR (see ADR 0014)."
            )
        return cls(StaticTokenProvider.from_env(), **kw)

    # ----- public endpoint wrappers (reads only in P1; thin dict-returning) -----

    def get_user(self) -> dict[str, Any]:
        """GET ``/port/v1/users/me`` — the live-probe round-trip target."""
        return self._get_json("/port/v1/users/me")

    def get_client_info(self) -> dict[str, Any]:
        """GET ``/port/v1/clients/me`` — ClientKey holder, cached on first call.

        The ClientKey is a required query param on almost every portfolio
        endpoint and is stable for the session, so one fetch amortizes across
        every subsequent read.
        """
        if self._client_info is None:
            self._client_info = self._get_json("/port/v1/clients/me")
        return self._client_info

    def get_accounts(self) -> dict[str, Any]:
        """GET ``/port/v1/accounts/me`` — AccountKey list under ``Data``."""
        return self._get_json("/port/v1/accounts/me")

    def get_balances(self, client_key: str, account_key: str | None = None) -> dict[str, Any]:
        """GET ``/port/v1/balances`` for the client (optionally one account)."""
        params: dict[str, Any] = {"ClientKey": client_key}
        if account_key is not None:
            params["AccountKey"] = account_key
        return self._get_json("/port/v1/balances", params=params)

    def get_positions(self, client_key: str) -> dict[str, Any]:
        """GET ``/port/v1/positions`` with the field groups the adapter maps.

        ``PositionBase`` (Amount/OpenPrice/Uic/AssetType) + ``PositionView``
        (MarketValue/ProfitLossOnTrade — may be absent on SIM NoAccess
        quotes) + ``DisplayAndFormat`` (broker Symbol, e.g. ``KO:xnys``).
        """
        params = {
            "ClientKey": client_key,
            "FieldGroups": "PositionBase,PositionView,DisplayAndFormat",
        }
        return self._get_json("/port/v1/positions", params=params)

    def search_instruments(
        self,
        keywords: str,
        *,
        asset_types: str = "Stock",
        exchange_id: str | None = None,
    ) -> dict[str, Any]:
        """GET ``/ref/v1/instruments`` — keyword search, optionally exchange-scoped."""
        params: dict[str, Any] = {"Keywords": keywords, "AssetTypes": asset_types}
        if exchange_id is not None:
            params["ExchangeId"] = exchange_id
        return self._get_json("/ref/v1/instruments", params=params)

    def get_instrument_details(self, uic: int | str, asset_type: str = "Stock") -> dict[str, Any]:
        """GET ``/ref/v1/instruments/details/{Uic}/{AssetType}``.

        Carries the tick-size scheme / decimals P2's price validation needs;
        exposed now because resolve already exercises the same service group.
        """
        return self._get_json(f"/ref/v1/instruments/details/{uic}/{asset_type}")

    def get_exchanges(self) -> dict[str, Any]:
        """GET ``/ref/v1/exchanges`` — one-time confirmation of ExchangeId codes."""
        return self._get_json("/ref/v1/exchanges")

    # ----- escape hatch (house pattern) -----

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch ``path`` (joined onto the SIM base URL) and parse JSON.

        Public escape hatch for callers that need Saxo endpoints not covered
        by the ``get_*`` convenience methods. Goes through the same throttle +
        retry + Bearer auth + SIM-rail contract; absolute URLs are refused
        unless they already point at the SIM gateway.
        """
        return self._get_json(path, params=params)

    # ----- internals (mirror polygon_client) -----

    def _throttle(self) -> None:
        # Lock: the default-client singleton is shared, so the throttle budget
        # must hold under concurrent callers (a future scheduler), not only the
        # attended CLI — an unlocked check/update would race into burst calls.
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_call_ts
            if elapsed < self._MIN_REQUEST_INTERVAL_S:
                self._sleep(self._MIN_REQUEST_INTERVAL_S - elapsed)
            self._last_call_ts = time.monotonic()

    def _join_url(self, path: str) -> str:
        # The scheme literals only DETECT an absolute URL so it can be checked
        # against the HTTPS-only SIM gateway below — nothing is ever requested
        # over plain HTTP (an http:// URL fails the base-url check and raises).
        if path.startswith(("http://", "https://")):  # NOSONAR
            if not path.startswith(self._base_url):
                raise SaxoError(
                    f"absolute URL {path!r} does not point at the SIM gateway "
                    f"{self._base_url!r}; pass an endpoint path instead"
                )
            return path
        return f"{self._base_url}{path}"

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._request(path, params=params)
        return resp.json()

    @staticmethod
    def _parse_retry_after(resp: requests.Response, *, floor: int, ceiling: int) -> int:
        """Clamp the server's rate-limit wait hint into ``[floor, ceiling]`` s.

        Honors ``Retry-After`` first, then Saxo's ``X-RateLimit-Session-Reset``;
        returns ``floor`` if both are missing or non-integer.
        """
        raw = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Session-Reset")
        if not raw:
            return floor
        try:
            value = int(raw)
        except ValueError:
            return floor
        return max(floor, min(value, ceiling))

    def _request(self, path: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        """Synchronous GET with throttle + dual-layer retry + 401 refresh seam.

        Layer 1 (this method): 401 -> ``invalidate()`` + retry ONCE with a
        fresh token; 429 -> Retry-After-aware sleep; 5xx -> exponential
        backoff; 2xx/permanent-4xx -> return.
        Layer 2 (``_request_with_retry``): transient network errors ->
        bounded retries.

        Raises ``SaxoAuthError`` on a post-refresh 401, ``SaxoRateLimitError``
        if 429 persists, ``SaxoError`` on other permanent failures.
        """
        url = self._join_url(path)
        # One id per LOGICAL request, reused across its retries — the same
        # semantics P2's POST dedup needs (a retried POST must not look new).
        request_id = str(uuid.uuid4())
        auth_retried = False
        resp: requests.Response | None = None
        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            self._throttle()
            resp = self._request_with_retry(url, params=params, request_id=request_id)
            if resp.status_code == 401 and not auth_retried:
                logger.warning("saxo 401 — invalidating token and retrying once")
                self._token_provider.invalidate()
                auth_retried = True
                continue
            if attempt == self._MAX_REQUEST_ATTEMPTS - 1:
                break
            if resp.status_code == 429:
                backoff = self._parse_retry_after(
                    resp, floor=self._RATE_LIMIT_FLOOR_S, ceiling=self._RATE_LIMIT_CEILING_S
                )
                logger.warning(
                    "saxo 429 rate-limited (attempt %d/%d); sleeping %ds",
                    attempt + 1,
                    self._MAX_REQUEST_ATTEMPTS,
                    backoff,
                )
                self._sleep(backoff)
                continue
            if 500 <= resp.status_code < 600:
                backoff = self._SERVER_ERROR_BACKOFFS[
                    min(attempt, len(self._SERVER_ERROR_BACKOFFS) - 1)
                ]
                logger.warning(
                    "saxo %d server error (attempt %d/%d); sleeping %ds",
                    resp.status_code,
                    attempt + 1,
                    self._MAX_REQUEST_ATTEMPTS,
                    backoff,
                )
                self._sleep(backoff)
                continue
            break
        assert resp is not None  # loop guarantees resp is assigned
        if resp.status_code == 401:
            raise SaxoAuthError(
                "Saxo 401 persisted after one token refresh — the 24h SIM token "
                "has likely expired; regenerate it at developer.saxo"
            )
        if resp.status_code == 429:
            raise SaxoRateLimitError(
                f"Saxo 429 persisted after {self._MAX_REQUEST_ATTEMPTS} attempts"
            )
        if resp.status_code >= 400:
            raise SaxoError(f"Saxo {resp.status_code}: {resp.text[:200]}")
        return resp

    def _request_with_retry(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        request_id: str,
    ) -> requests.Response:
        """Up to 3 attempts with 5s, 15s backoff on transient network failures.

        Headers are rebuilt per attempt so a token refreshed by the 401 seam
        is picked up without any client-side state beyond the provider.
        """
        last_exc: Exception | None = None
        for attempt in range(3):
            headers = {
                "Authorization": f"Bearer {self._token_provider.get_access_token()}",
                "Accept": "application/json",
                "User-Agent": "AlphaLens/0.1",
                "x-request-id": request_id,
            }
            try:
                return self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            except self._TRANSIENT_NET_EXCEPTIONS as exc:
                last_exc = exc
                if attempt < len(self._NETWORK_ERROR_BACKOFFS):
                    backoff = self._NETWORK_ERROR_BACKOFFS[attempt]
                    logger.warning(
                        "saxo transient net error (attempt %d/3): %s; sleeping %ds",
                        attempt + 1,
                        exc,
                        backoff,
                    )
                    self._sleep(backoff)
                    continue
                break
        raise SaxoError(f"exhausted network retries: {last_exc}") from last_exc


# Module-level lazy singleton — single SaxoClient shared by every caller that
# doesn't have its own injected client. Double-checked locking, reset hook for
# tests (same idiom as get_default_polygon_client).
_DEFAULT_CLIENT: SaxoClient | None = None
_DEFAULT_CLIENT_LOCK = threading.Lock()


def get_default_saxo_client() -> SaxoClient:
    """Return the process-wide default SaxoClient (lazy-initialized).

    Reads ``SAXO_SIM_TOKEN`` from the environment on first call; the throttle
    budget is shared across every caller in the process (the Saxo per-session
    rate-limit contract). Construction is thread-safe via double-checked
    locking.
    """
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        with _DEFAULT_CLIENT_LOCK:
            if _DEFAULT_CLIENT is None:
                _DEFAULT_CLIENT = SaxoClient.from_env()
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None


__all__ = [
    "LIVE_TRADING_ENABLED",
    "SIM_AUTH_BASE_URL",
    "SIM_BASE_URL",
    "TOKEN_ENV",
    "SaxoAuthError",
    "SaxoClient",
    "SaxoError",
    "SaxoLiveEnvironmentBlockedError",
    "SaxoRateLimitError",
    "_reset_default_client_for_tests",
    "get_default_saxo_client",
]
