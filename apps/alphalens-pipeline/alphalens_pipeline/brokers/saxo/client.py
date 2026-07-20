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
    SaxoNotFoundError,
    SaxoRateLimitError,
)
from alphalens_pipeline.brokers.saxo.tokens import (
    TOKEN_ENV,
    OAuthTokenProvider,
    StaticTokenProvider,
    TokenProvider,
    resolve_token_store_path,
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
        """Construct with the OAuth provider when a token store exists, else
        the :class:`StaticTokenProvider` from ``SAXO_SIM_TOKEN``.

        Selection order (P4): (1) the ``SAXO_ENV`` sim-only guard stays FIRST;
        (2) an existing OAuth token store selects
        :class:`~.tokens.OAuthTokenProvider` — missing ``SAXO_APP_KEY`` /
        ``SAXO_APP_SECRET`` at that point is a hard ``SaxoAuthError``, never a
        silent static fallback (a store without refresh credentials is a
        misconfiguration); (3) no store keeps the static path byte-identical.
        OAuth state present beats a set ``SAXO_SIM_TOKEN``.

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
        if resolve_token_store_path().is_file():
            return cls(OAuthTokenProvider.from_env(), **kw)
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

    # ----- FX-leg reads (sizing FX rate; design memo saxo_fx_leg_gpw_design) -----

    def get_currency_pairs(self) -> dict[str, Any]:
        """GET ``/ref/v1/currencypairs`` — FX pair Uic reference data.

        The lookup is one-directional (a pair is listed under its base side
        only); the broker falls back to an FxSpot Keywords search when the
        pair is not listed. Live-verified EUR->PLN = Uic 1343.
        """
        return self._get_json("/ref/v1/currencypairs")

    def get_fx_infoprice(self, uic: int | str) -> dict[str, Any]:
        """GET ``/trade/v1/infoprices`` for ONE FxSpot Uic.

        FX infoprices work on this SIM even on weekends (SBFX source, no
        exchange entitlement — unlike stock infoprices, NoAccess on the
        unlinked SIM). Returns Quote Bid/Ask/Mid + PriceTypeBid/Ask +
        MarketState; ``LastUpdated`` is NOT a data-age signal (live-probed
        to echo the request second on a CLOSED market).
        """
        return self._get_json("/trade/v1/infoprices", params={"Uic": uic, "AssetType": "FxSpot"})

    # ----- order endpoints (P2 writes + order reads; thin dict-returning) -----

    def precheck_order(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST ``/trade/v2/orders/precheck`` — validates, places NOTHING.

        Returns ``(status_code, parsed_body)``; the broker owns the
        interpretation (``PreCheckResult``/``ErrorInfo``). Precheck reserves
        nothing server-side, so it gets a fresh ``x-request-id`` per call.
        """
        resp = self._send_write(
            "POST", "/trade/v2/orders/precheck", json_body=body, request_id=str(uuid.uuid4())
        )
        return resp.status_code, self._safe_json(resp)

    def place_order(self, body: dict[str, Any], *, request_id: str) -> tuple[int, dict[str, Any]]:
        """POST ``/trade/v2/orders`` — the ONE placement call per bracket.

        ``request_id`` MUST be the bracket's ``client_request_id``: Saxo's
        ``x-request-id`` 15-second duplicate window is the real idempotency
        mechanism, so a retry of the same logical bracket must reuse it.
        Returns ``(status_code, parsed_body)`` — including 4xx/202 bodies,
        which the broker translates (ModelState / ErrorInfo / OrderId-repair).
        """
        resp = self._send_write("POST", "/trade/v2/orders", json_body=body, request_id=request_id)
        return resp.status_code, self._safe_json(resp)

    def cancel_order_ids(self, order_ids: str, *, account_key: str) -> tuple[int, dict[str, Any]]:
        """DELETE ``/trade/v2/orders/{OrderIds}`` — idempotent, normal retry.

        Cancelling an unfilled entry silently cascades to its related-order
        children — one DELETE cleans a whole bracket (do NOT delete children
        first).
        """
        resp = self._send_write(
            "DELETE",
            f"/trade/v2/orders/{order_ids}",
            params={"AccountKey": account_key},
            request_id=str(uuid.uuid4()),
        )
        return resp.status_code, self._safe_json(resp)

    def get_open_orders(self) -> dict[str, Any]:
        """GET ``/port/v1/orders/me`` — open orders (Saxo Status default Working)."""
        return self._get_json("/port/v1/orders/me")

    def get_order_status(self, client_key: str, order_id: str) -> dict[str, Any] | None:
        """GET ``/port/v1/orders/{ClientKey}/{OrderId}``; ``None`` when absent.

        The open-orders endpoint drops filled/cancelled/expired orders, so a
        404 here is an EXPECTED outcome (mapped to ``OrderStatus.UNKNOWN`` by
        the adapter), not an error.
        """
        try:
            return self._get_json(f"/port/v1/orders/{client_key}/{order_id}")
        except SaxoNotFoundError:
            return None

    # ----- audit + closed-positions reads (P3 reconciliation) -----

    def get_order_activities(
        self,
        client_key: str,
        *,
        order_id: str | None = None,
        entry_type: str = "Last",
        from_datetime: str | None = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        """GET ``/cs/v1/audit/orderactivities`` — the P3 terminal-resolution read.

        The audit store retains order activities for 2+ documented years
        (ENS is NOT used — hard 14-day cap). ``EntryType=Last`` returns the
        latest activity row per order; the (Status, SubStatus) PAIR on that
        row is what the broker's ``resolve_order_outcome`` classifies.
        Pagination (``__next``/``__nextPoll``) is followed transparently and
        the ``Data`` arrays merged.
        """
        params: dict[str, Any] = {"ClientKey": client_key, "EntryType": entry_type}
        if order_id is not None:
            params["OrderId"] = order_id
        if from_datetime is not None:
            params["FromDateTime"] = from_datetime
        if top is not None:
            params["$top"] = top
        return self._get_paged_json("/cs/v1/audit/orderactivities", params=params)

    def get_closed_positions(self, client_key: str) -> dict[str, Any]:
        """GET ``/port/v1/closedpositions`` — the P3 round-trip cross-check.

        Accepts BOTH live body shapes: the ``{__count, Data}`` envelope and
        the bare ``[]`` array (live-verified empty-account shape), always
        returning the envelope form. Pagination is followed transparently.
        """
        return self._get_paged_json("/port/v1/closedpositions", params={"ClientKey": client_key})

    @staticmethod
    def _normalize_next_url(url: str) -> str:
        """Strip an absolute ``__next``/``__nextPoll`` URL to a relative path.

        Saxo pagination URLs come back absolute WITH an explicit ``:443``
        port (``https://gateway.saxobank.com:443/sim/openapi/...``), which
        fails :meth:`_join_url`'s SIM-prefix rail (the configured base URL
        carries no port). Stripping to the path after ``/sim/openapi`` keeps
        the rail intact — the follow-up request re-joins onto the one
        allowed base URL. A URL without the marker passes through unchanged
        (a relative path already satisfies the rail; a foreign absolute URL
        still fails it loudly).
        """
        marker = "/sim/openapi"
        index = url.find(marker)
        if index >= 0:
            return url[index + len(marker) :]
        return url

    def _get_paged_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``path`` and follow ``__next``/``__nextPoll`` pages, merging ``Data``.

        Normalizes the bare-array body shape into the ``{__count, Data}``
        envelope so callers see ONE shape regardless of endpoint mood.
        """
        payload = self._get_json(path, params=params)
        if isinstance(payload, list):
            return {"__count": len(payload), "Data": list(payload)}
        merged: dict[str, Any] = dict(payload)
        data: list[Any] = list(merged.get("Data") or [])
        next_url = merged.pop("__next", None) or merged.pop("__nextPoll", None)
        while next_url:
            page = self._get_json(self._normalize_next_url(str(next_url)))
            if isinstance(page, list):
                data.extend(page)
                break
            data.extend(page.get("Data") or [])
            next_url = page.get("__next") or page.get("__nextPoll")
        merged.pop("__next", None)
        merged.pop("__nextPoll", None)
        merged["Data"] = data
        return merged

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
                "Saxo 401 persisted after one token refresh — re-authenticate "
                "with `alphalens broker auth` (OAuth) or regenerate "
                "SAXO_SIM_TOKEN at developer.saxo (static)"
            )
        if resp.status_code == 429:
            raise SaxoRateLimitError(
                f"Saxo 429 persisted after {self._MAX_REQUEST_ATTEMPTS} attempts"
            )
        if resp.status_code == 404:
            raise SaxoNotFoundError(f"Saxo 404: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise SaxoError(f"Saxo {resp.status_code}: {resp.text[:200]}")
        return resp

    # ----- write transport (P2) -----

    @staticmethod
    def _safe_json(resp: requests.Response) -> dict[str, Any]:
        """Parse a response body defensively — error bodies may be non-JSON."""
        try:
            payload = resp.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {"data": payload}

    @staticmethod
    def _is_provably_unsent(exc: requests.RequestException) -> bool:
        """True only when the request provably never reached Saxo.

        ``ConnectTimeout`` and connection-phase failures (DNS miss, refused —
        surfaced by requests as a ``ConnectionError`` wrapping urllib3's
        ``NewConnectionError`` / name-resolution errors) mean the TCP
        connection never opened, so nothing was sent and a retry is safe.
        Everything else (aborted mid-request, reset/read-timeout after send)
        is ambiguous: the order may exist server-side.
        """
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return True
        if isinstance(exc, requests.exceptions.ConnectionError):
            # Marker list validated against requests 2.x / urllib3 2.x message
            # shapes and PINNED by TestProvablyUnsentClassifier — a library
            # bump that changes the repr narrows the retry set (fail-safe:
            # fewer retries, never a double-submit), and the test catches it.
            marker = repr(exc)
            return (
                "NewConnectionError" in marker
                or "Name or service not known" in marker
                or "nodename nor servname" in marker
                or "Failed to resolve" in marker
            )
        return False

    def _send_once(self, method_lower: str, url: str, kwargs: dict[str, Any]) -> requests.Response:
        """One raw verb dispatch on the injected session (typed seam)."""
        send: Callable[..., requests.Response] = getattr(self._session, method_lower)
        return send(url, **kwargs)

    def _build_write_kwargs(
        self,
        method_lower: str,
        *,
        json_body: dict[str, Any] | None,
        params: dict[str, Any] | None,
        request_id: str,
    ) -> dict[str, Any]:
        """Assemble per-attempt request kwargs: fresh-token headers, params, POST body."""
        headers = {
            "Authorization": f"Bearer {self._token_provider.get_access_token()}",
            "Accept": "application/json",
            "User-Agent": "AlphaLens/0.1",
            "x-request-id": request_id,
        }
        kwargs: dict[str, Any] = {
            "headers": headers,
            "params": params,
            "timeout": self._timeout,
        }
        if method_lower == "post":
            kwargs["json"] = json_body
        return kwargs

    def _write_net_backoff_or_raise(
        self,
        exc: requests.RequestException,
        *,
        method: str,
        path: str,
        request_id: str,
        idempotent: bool,
        net_attempt: int,
        attempt: int,
    ) -> int:
        """Return the transient-net retry backoff for a write, or raise ``SaxoError``.

        Retries only when the verb is idempotent or the request provably never
        reached Saxo (:meth:`_is_provably_unsent`) AND the network-backoff ladder
        is not yet exhausted; the caller then advances ``net_attempt``, sleeps,
        and retries with the SAME ``x-request-id``. Otherwise raises immediately,
        carrying ``request_id`` so the operator can reconcile before any re-run.
        """
        retriable = idempotent or self._is_provably_unsent(exc)
        if retriable and net_attempt < len(self._NETWORK_ERROR_BACKOFFS):
            backoff = self._NETWORK_ERROR_BACKOFFS[net_attempt]
            logger.warning(
                "saxo %s transient net error (provably unsent, attempt %d): %s; sleeping %ds",
                method.lower(),
                attempt + 1,
                exc,
                backoff,
            )
            return backoff
        detail = (
            "network retries exhausted"
            if retriable
            else "request may have been sent — NOT retried (never blind-retry a POST)"
        )
        raise SaxoError(
            f"saxo {method.upper()} {path} network failure "
            f"(x-request-id={request_id}; {detail}): {exc}"
        ) from exc

    def _server_error_backoff_or_raise(
        self,
        resp: requests.Response,
        *,
        method: str,
        path: str,
        request_id: str,
        idempotent: bool,
        attempt: int,
    ) -> int:
        """Return the 5xx retry backoff for an idempotent write, or raise for a POST.

        A 5xx after a POST is ambiguous (the order may already be processed), so
        it raises immediately with the dedup ``x-request-id``; DELETE follows the
        normal server-error backoff ladder.
        """
        if not idempotent:
            raise SaxoError(
                f"Saxo {resp.status_code} on {method.upper()} {path} "
                f"(x-request-id={request_id}; write outcome ambiguous — "
                f"reconcile via 'broker orders' before re-running): "
                f"{resp.text[:200]}"
            )
        backoff = self._SERVER_ERROR_BACKOFFS[min(attempt, len(self._SERVER_ERROR_BACKOFFS) - 1)]
        logger.warning(
            "saxo %d on %s (attempt %d/%d); sleeping %ds",
            resp.status_code,
            method,
            attempt + 1,
            self._MAX_REQUEST_ATTEMPTS,
            backoff,
        )
        return backoff

    def _raise_for_terminal_write_status(
        self,
        resp: requests.Response,
        *,
        method: str,
        path: str,
        request_id: str,
        idempotent: bool,
    ) -> None:
        """Translate a persisted terminal status into the matching Saxo exception.

        Runs after the retry loop exhausts; returns silently for any status the
        adapter is expected to translate (4xx/202/success).
        """
        if resp.status_code == 401:
            raise SaxoAuthError(
                "Saxo 401 persisted after one token refresh — re-authenticate "
                "with `alphalens broker auth` (OAuth) or regenerate "
                "SAXO_SIM_TOKEN at developer.saxo (static)"
            )
        if resp.status_code == 429:
            raise SaxoRateLimitError(
                f"Saxo 429 persisted after {self._MAX_REQUEST_ATTEMPTS} attempts "
                f"on {method.upper()} {path}"
            )
        if not idempotent and 500 <= resp.status_code < 600:
            raise SaxoError(
                f"Saxo {resp.status_code} on {method.upper()} {path} "
                f"(x-request-id={request_id}; write outcome ambiguous — reconcile "
                f"via 'broker orders' before re-running): {resp.text[:200]}"
            )
        if idempotent and 500 <= resp.status_code < 600:
            raise SaxoError(f"Saxo {resp.status_code} on {method.upper()} {path} persisted")

    def _send_write(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        request_id: str,
    ) -> requests.Response:
        """POST/DELETE through the shared throttle with a WRITE-safe retry policy.

        Shares the 401-refresh-once and 429 Retry-After machinery with the
        read path, but the retry policy differs by verb:

        - **POST is NEVER blind-retried.** Retries happen only when the
          request provably never reached Saxo (:meth:`_is_provably_unsent`)
          or was provably not accepted (429) — always reusing the SAME
          ``x-request-id`` so Saxo's 15s duplicate window dedups. A 5xx or an
          ambiguous network error after send raises immediately, carrying
          ``request_id`` so the operator can reconcile (``broker orders``)
          before any re-run.
        - **DELETE is idempotent** — normal transient/5xx retry ladder.

        Returns the final response for every other status INCLUDING 4xx/202:
        rejection translation (ModelState / ErrorInfo / OrderId-repair) is
        the adapter's job, not the transport's.
        """
        url = self._join_url(path)
        method_lower = method.lower()
        if method_lower not in ("post", "delete"):
            raise ValueError(f"_send_write supports POST/DELETE only, got {method!r}")
        idempotent = method_lower == "delete"
        auth_retried = False
        net_attempt = 0
        resp: requests.Response | None = None
        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            self._throttle()
            kwargs = self._build_write_kwargs(
                method_lower, json_body=json_body, params=params, request_id=request_id
            )
            try:
                resp = self._send_once(method_lower, url, kwargs)
            except self._TRANSIENT_NET_EXCEPTIONS as exc:
                backoff = self._write_net_backoff_or_raise(
                    exc,
                    method=method,
                    path=path,
                    request_id=request_id,
                    idempotent=idempotent,
                    net_attempt=net_attempt,
                    attempt=attempt,
                )
                net_attempt += 1
                self._sleep(backoff)
                continue
            if resp.status_code == 401 and not auth_retried:
                logger.warning("saxo 401 on %s — invalidating token and retrying once", method)
                self._token_provider.invalidate()
                auth_retried = True
                continue
            if attempt == self._MAX_REQUEST_ATTEMPTS - 1:
                break
            if resp.status_code == 429:
                # 429 = provably not accepted; safe for POST too, SAME id.
                backoff = self._parse_retry_after(
                    resp, floor=self._RATE_LIMIT_FLOOR_S, ceiling=self._RATE_LIMIT_CEILING_S
                )
                logger.warning(
                    "saxo 429 on %s (attempt %d/%d); sleeping %ds",
                    method,
                    attempt + 1,
                    self._MAX_REQUEST_ATTEMPTS,
                    backoff,
                )
                self._sleep(backoff)
                continue
            if 500 <= resp.status_code < 600:
                backoff = self._server_error_backoff_or_raise(
                    resp,
                    method=method,
                    path=path,
                    request_id=request_id,
                    idempotent=idempotent,
                    attempt=attempt,
                )
                self._sleep(backoff)
                continue
            break
        assert resp is not None  # loop either assigned resp or raised
        self._raise_for_terminal_write_status(
            resp,
            method=method,
            path=path,
            request_id=request_id,
            idempotent=idempotent,
        )
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
    "SaxoNotFoundError",
    "SaxoRateLimitError",
    "_reset_default_client_for_tests",
    "get_default_saxo_client",
]
