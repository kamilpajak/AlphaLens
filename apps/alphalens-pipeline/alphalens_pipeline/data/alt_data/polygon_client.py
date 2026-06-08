"""Polygon.io HTTP client — single canonical entry point for every Polygon REST call.

Polygon Starter tier ($29/mo) caps requests at 5 req/min globally per API key.
Any uncoordinated shadow client eats budget from every other consumer in the
process — a single forgotten ``urlopen("https://api.polygon.io/...")`` can
drain the window before the daily thematic pipeline gets its first page.

Bearer authentication (``Authorization: Bearer <key>``) — the API key never
appears in URLs, ``next_url`` pagination responses, log lines, or cache file
names. Polygon's docs deprecate the ``apiKey=`` query-param form in favor of
the Authorization header.

Mirrors :mod:`alphalens_pipeline.data.alt_data.sec_edgar_client` structurally:
flat public methods, dual-layer retry (429 → Retry-After / 5xx exponential /
network transient), module-level lazy singleton via
:func:`get_default_polygon_client`, ``_reset_default_client_for_tests`` hook.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.polygon.io"
_NEWS_PATH = "/v2/reference/news"
_SHORT_INTEREST_PATH = "/stocks/v1/short-interest"
_OPTIONS_CONTRACTS_PATH = "/v3/reference/options/contracts"
_GROUPED_DAILY_PATH = "/v2/aggs/grouped/locale/us/market/stocks"

# Fields kept from each grouped-daily result row: bar time + OHLCV + VWAP. The
# symbol (``T``) is the dict KEY, never carried inside the value.
_GROUPED_DAILY_FIELDS = ("t", "o", "h", "l", "c", "v", "vw")

API_KEY_ENV = "POLYGON_API_KEY"


class PolygonError(RuntimeError):
    """Non-transient Polygon failure (auth, schema, permanent 4xx, exhausted retries)."""


class PolygonAuthError(PolygonError):
    """401 — missing or invalid API key. Distinct from PolygonError so callers
    can short-circuit instead of retrying."""


class PolygonRateLimitError(PolygonError):
    """429 persisted after all retries. Distinct so callers can soft-fail
    (record gates_unknown) instead of crashing the daily pipeline."""


def _strip_apikey_from_url(url: str) -> str:
    """Strip any ``apiKey`` query parameter from ``url``.

    Polygon's ``next_url`` pagination cursor sometimes carries the original
    ``apiKey`` query param (an artifact of older clients). Stripping it here
    keeps Bearer auth as the single source of credentials and prevents the
    key from leaking into log lines or cache filenames built from URLs.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    # Case-insensitive match — defensive against third-party intermediaries
    # echoing the param with any casing (``apiKey``, ``apikey``, ``APIKEY``).
    kept = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != "apikey"
    ]
    new_query = urlencode(kept)
    return urlunparse(parsed._replace(query=new_query))


class PolygonClient:
    """Canonical HTTP client for Polygon.io.

    Single shared throttle + retry across every consumer (thematic news ingest,
    press verification, short-interest features, options contracts). Starter
    tier rate limit (5 req/min) is enforced by the spacing between requests,
    not by reactive 429 handling alone — the latter is the safety net for
    bursts that slip through (e.g. when ingest + verification overlap).
    """

    _MAX_REQUEST_ATTEMPTS = 4  # 1 + 3 retries
    _RATE_LIMIT_FLOOR_S = 13  # Starter (5 req/min) → 12s spacing + 1s safety
    _RATE_LIMIT_CEILING_S = 60  # cap Retry-After honoring at 1 minute
    _SERVER_ERROR_BACKOFFS = (5, 15, 30)
    _NETWORK_ERROR_BACKOFFS = (5, 15)
    _TRANSIENT_NET_EXCEPTIONS = (requests.RequestException,)

    def __init__(
        self,
        api_key: str,
        *,
        rate_limit_per_min: int = 5,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("Polygon requires a non-empty api_key")
        self._api_key = api_key
        self._min_interval_s = 60.0 / max(rate_limit_per_min, 1)
        self._timeout = timeout
        self._session = session or requests.Session()
        self._sleep = sleep
        self._last_call_ts: float = 0.0

    @classmethod
    def from_env(cls, **kw: Any) -> PolygonClient:
        """Construct from ``POLYGON_API_KEY`` env var. Raises ValueError if unset."""
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise ValueError(f"{API_KEY_ENV} environment variable not set")
        return cls(api_key, **kw)

    # ----- public flat methods (mirror SecEdgar style) -----

    def get_news_range(
        self,
        *,
        start: dt.datetime,
        end: dt.datetime,
        ticker: str | None = None,
        order: str = "desc",
        sort: str = "published_utc",
        limit: int = 1000,
        max_items: int = 1000,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Polygon ``/v2/reference/news`` over ``[start, end)`` with pagination.

        Consolidates the previous ``polygon_news.fetch_news_range`` (unfiltered
        ingest) and ``recent_press.fetch_recent_news`` (per-ticker / unfiltered
        verification window). Pass ``ticker=None`` for the firehose.

        ``start`` / ``end`` MUST be tz-aware. Pagination follows ``next_url``;
        ``apiKey`` query param is stripped from each ``next_url`` because the
        Authorization header carries the credential.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        params: dict[str, Any] = {
            "published_utc.gte": start.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "published_utc.lt": end.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "order": order,
            "sort": sort,
            "limit": limit,
        }
        if ticker:
            params["ticker"] = ticker

        items: list[dict[str, Any]] = []
        url: str | None = f"{_BASE_URL}{_NEWS_PATH}"
        pages = 0
        while url and len(items) < max_items and pages < max_pages:
            payload = self._get_json(url, params=params if pages == 0 else None)
            results = payload.get("results") or []
            items.extend(results)
            next_url = payload.get("next_url")
            url = _strip_apikey_from_url(next_url) if next_url else None
            pages += 1
        return items[:max_items]

    def get_short_interest(
        self,
        *,
        ticker: str,
        limit: int = 500,
        order: str = "asc",
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Polygon ``/stocks/v1/short-interest`` paginated to exhaustion.

        Returns the raw ``results`` rows; caller (PolygonShortInterestClient)
        owns the dataclass-frame transformation.
        """
        params: dict[str, Any] = {
            "ticker": ticker.upper(),
            "limit": limit,
            "order": order,
        }
        rows: list[dict[str, Any]] = []
        url: str | None = f"{_BASE_URL}{_SHORT_INTEREST_PATH}"
        pages = 0
        while url and pages < max_pages:
            payload = self._get_json(url, params=params if pages == 0 else None)
            results = payload.get("results") or []
            rows.extend(results)
            next_url = payload.get("next_url")
            url = _strip_apikey_from_url(next_url) if next_url else None
            pages += 1
        return rows

    def get_agg_range(
        self,
        *,
        ticker: str,
        start: dt.datetime,
        end: dt.datetime,
        multiplier: int = 1,
        timespan: str = "minute",
        adjusted: bool = False,
        sort: str = "asc",
        limit: int = 50000,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Polygon ``/v2/aggs/ticker/{ticker}/range/...`` OHLCV aggregate bars.

        Used by the feedback shadow-return computation (Track A v2 PR-3) to
        pull minute bars for the arrival + horizon opening windows. Each
        returned dict is a raw Polygon agg bar: ``t`` (ms epoch, bar START),
        ``o``/``h``/``l``/``c`` (OHLC), ``v`` (volume), ``vw`` (per-bar VWAP),
        ``n`` (txn count).

        ``start`` / ``end`` MUST be tz-aware; they are converted to
        millisecond-since-epoch for the path (unambiguous for minute bars).
        ``adjusted=False`` returns raw (split/dividend-unadjusted) bars — the
        shadow-return horizon is short (days) so the caller accepts the rare
        corporate-action distortion and guards against it separately.

        Empty windows return ``[]`` (NOT an error): a name with no bars in the
        window is a legitimate skip the caller handles. Pagination follows
        ``next_url`` with the ``apiKey`` query param stripped, identical to
        :meth:`get_news_range`.

        NOTE (operational): Polygon's free / Basic plan serves only PAST-day
        minute aggregates, not the current session. Callers must request
        windows that have fully closed — the feedback shadow-return job gates
        on a matured horizon before calling this.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        from_ms = int(start.timestamp() * 1000)
        to_ms = int(end.timestamp() * 1000)
        path = f"/v2/aggs/ticker/{ticker.upper()}/range/{multiplier}/{timespan}/{from_ms}/{to_ms}"
        params: dict[str, Any] = {
            "adjusted": "true" if adjusted else "false",
            "sort": sort,
            "limit": limit,
        }
        bars: list[dict[str, Any]] = []
        url: str | None = f"{_BASE_URL}{path}"
        pages = 0
        while url and pages < max_pages:
            payload = self._get_json(url, params=params if pages == 0 else None)
            results = payload.get("results") or []
            bars.extend(results)
            next_url = payload.get("next_url")
            url = _strip_apikey_from_url(next_url) if next_url else None
            pages += 1
        return bars

    def get_grouped_daily(
        self,
        date: dt.date,
        *,
        adjusted: bool = False,
        include_otc: bool = False,
        max_pages: int = 1,
    ) -> dict[str, dict[str, Any]]:
        """Polygon ``/v2/aggs/grouped/locale/us/market/stocks/{date}`` — one call
        returns the whole-market daily OHLCV+VWAP for ``date``.

        Backbone of the population-monitor grouped-daily two-tier screen: a SINGLE
        HTTP request prices the entire US-equity market for one session, so the
        cheap daily no-touch screen costs O(days) not O(candidates).

        Returns ``{TICKER: {t,o,h,l,c,v,vw}}``. The symbol key is ALWAYS the row's
        ``T`` field (upper-cased); the per-bar ``t`` (epoch ms, bar START) is
        preserved as the bar time inside the value and is NEVER used as the key.

        ``adjusted=False`` (default) mirrors :meth:`get_agg_range` — the raw,
        split/dividend-UNADJUSTED daily H/L must match the raw minute bars and the
        absolute ladder levels (Polygon defaults this endpoint to ``true``, which
        would silently corrupt ``open_r``). ``include_otc`` defaults false.

        A weekend / holiday (``resultsCount == 0`` / ``results`` null) returns
        ``{}`` (NOT an error) so the caller treats it as "no session". Raises
        ``PolygonError`` / ``PolygonRateLimitError`` / ``PolygonAuthError``
        identically to the other methods.
        """
        params: dict[str, Any] = {
            "adjusted": "true" if adjusted else "false",
            "include_otc": "true" if include_otc else "false",
        }
        out: dict[str, dict[str, Any]] = {}
        url: str | None = f"{_BASE_URL}{_GROUPED_DAILY_PATH}/{date.isoformat()}"
        pages = 0
        while url and pages < max_pages:
            payload = self._get_json(url, params=params if pages == 0 else None)
            for row in payload.get("results") or []:
                symbol = row.get("T")
                if symbol is None:
                    continue
                out[str(symbol).upper()] = {k: row[k] for k in _GROUPED_DAILY_FIELDS if k in row}
            next_url = payload.get("next_url")
            url = _strip_apikey_from_url(next_url) if next_url else None
            pages += 1
        return out

    def get_options_contracts(
        self,
        *,
        max_pages: int = 200,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Polygon ``/v3/reference/options/contracts`` paginated.

        ``params`` is passed through as query parameters — the caller is
        responsible for keyword compatibility with the Polygon API (e.g.
        ``underlying_ticker``, ``expired``, ``limit``).
        """
        rows: list[dict[str, Any]] = []
        url: str | None = f"{_BASE_URL}{_OPTIONS_CONTRACTS_PATH}"
        pages = 0
        while url and pages < max_pages:
            payload = self._get_json(url, params=params if pages == 0 else None)
            results = payload.get("results") or []
            rows.extend(results)
            next_url = payload.get("next_url")
            url = _strip_apikey_from_url(next_url) if next_url else None
            pages += 1
        return rows

    # ----- escape hatches (mirror SecEdgar) -----

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch ``url`` and parse JSON. Public escape hatch for shadow callers
        that need Polygon endpoints not covered by the ``get_*`` convenience
        methods. Goes through the same throttle + retry + Bearer auth contract.
        """
        return self._get_json(url, params=params)

    def get_bytes(self, url: str, *, params: dict[str, Any] | None = None) -> bytes:
        """Fetch ``url`` and return raw bytes. For non-JSON endpoints (rare on
        Polygon)."""
        resp = self._request(url, params=params)
        return resp.content

    # ----- internals (mirror SecEdgar) -----

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._min_interval_s:
            self._sleep(self._min_interval_s - elapsed)
        self._last_call_ts = time.monotonic()

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._request(url, params=params)
        return resp.json()

    @staticmethod
    def _parse_retry_after(resp: requests.Response, *, floor: int, ceiling: int) -> int:
        """Clamp Polygon's ``Retry-After`` header into ``[floor, ceiling]`` seconds.

        Returns ``floor`` if the header is missing, non-integer, or below
        ``floor``. Honors the server signal up to ``ceiling`` to prevent
        pathological waits on a misconfigured proxy.
        """
        raw = resp.headers.get("Retry-After")
        if not raw:
            return floor
        try:
            value = int(raw)
        except ValueError:
            return floor
        return max(floor, min(value, ceiling))

    def _request(self, url: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        """Synchronous GET with throttle + dual-layer retry.

        Layer 1 (this method): 429 → Retry-After-aware sleep; 5xx → exponential
        backoff; 2xx/permanent-4xx → return.
        Layer 2 (``_request_with_retry``): transient network errors → bounded retries.

        Raises ``PolygonAuthError`` on 401, ``PolygonRateLimitError`` if 429
        persists, ``PolygonError`` on other permanent failures.
        """
        resp: requests.Response | None = None
        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            self._throttle()
            resp = self._request_with_retry(url, params=params)
            if attempt == self._MAX_REQUEST_ATTEMPTS - 1:
                break
            if resp.status_code == 429:
                backoff = self._parse_retry_after(
                    resp, floor=self._RATE_LIMIT_FLOOR_S, ceiling=self._RATE_LIMIT_CEILING_S
                )
                logger.warning(
                    "polygon 429 rate-limited (attempt %d/%d); sleeping %ds per Retry-After",
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
                    "polygon %d server error (attempt %d/%d); sleeping %ds",
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
            raise PolygonAuthError(f"Polygon 401: {resp.text[:200]}")
        if resp.status_code == 429:
            raise PolygonRateLimitError(
                f"Polygon 429 persisted after {self._MAX_REQUEST_ATTEMPTS} attempts"
            )
        if resp.status_code >= 400:
            raise PolygonError(f"Polygon {resp.status_code}: {resp.text[:200]}")
        return resp

    def _request_with_retry(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> requests.Response:
        """Up to 3 attempts with 5s, 15s backoff on transient network failures."""
        last_exc: Exception | None = None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "AlphaLens/0.1",
            "Accept-Encoding": "gzip, deflate",
        }
        for attempt in range(3):
            try:
                return self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            except self._TRANSIENT_NET_EXCEPTIONS as exc:
                last_exc = exc
                if attempt < len(self._NETWORK_ERROR_BACKOFFS):
                    backoff = self._NETWORK_ERROR_BACKOFFS[attempt]
                    logger.warning(
                        "polygon transient net error (attempt %d/3): %s; sleeping %ds",
                        attempt + 1,
                        exc,
                        backoff,
                    )
                    self._sleep(backoff)
                    continue
                break
        raise PolygonError(f"exhausted network retries: {last_exc}") from last_exc


# Module-level lazy singleton — single PolygonClient instance shared by every
# caller that doesn't have its own injected client (thematic news_ingest,
# verification recent_press, PolygonShortInterestClient module-level factories).
# Reading POLYGON_API_KEY once at first call keeps env resolution centralised;
# tests reset via _reset_default_client_for_tests().
_DEFAULT_CLIENT: PolygonClient | None = None
# Guards first-call construction so two threads racing the first call
# don't each build a client. Double-checked locking (same idiom as
# ``paper.calendar._calendar``).
_DEFAULT_CLIENT_LOCK = threading.Lock()


def get_default_polygon_client() -> PolygonClient:
    """Return the process-wide default PolygonClient (lazy-initialized).

    Reads ``POLYGON_API_KEY`` from the environment on first call. Subsequent
    calls return the same instance — the rate-limit budget is shared across
    every caller in the process, which is precisely the Polygon Starter
    fair-access contract we want. Construction is thread-safe via
    double-checked locking.
    """
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        with _DEFAULT_CLIENT_LOCK:
            if _DEFAULT_CLIENT is None:
                _DEFAULT_CLIENT = PolygonClient.from_env()
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None


__all__ = [
    "API_KEY_ENV",
    "PolygonAuthError",
    "PolygonClient",
    "PolygonError",
    "PolygonRateLimitError",
    "_reset_default_client_for_tests",
    "get_default_polygon_client",
]
