"""Polygon.io REST client — just the endpoints the Lean screener needs.

Targets the free "Stocks Basic" tier (5 req/min, EOD only, 2 years history).
The grouped-daily endpoint returns OHLCV for every US ticker in a single call,
so the rate limit almost never bites in the batch-screener use case.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupedBar:
    """One row from Polygon grouped-daily — OHLCV + ticker, adjusted when requested."""

    ticker: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp_ms: int


class PolygonError(RuntimeError):
    """Raised on non-transient Polygon API errors (auth, schema, permanent 4xx)."""


class PolygonClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.polygon.io",
        rate_limit_per_min: int = 5,
        session: requests.Session | None = None,
        sleep: callable = time.sleep,
    ):
        if not api_key:
            raise ValueError("POLYGON_API_KEY must be set")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._min_interval_s = 60.0 / max(rate_limit_per_min, 1)
        self._session = session or requests.Session()
        self._sleep = sleep
        self._last_call_ts: float = 0.0

    def grouped_daily(self, date: str, adjusted: bool = True) -> list[GroupedBar]:
        """All US stock bars for one trading day. One HTTP call regardless of ticker count.

        date: "YYYY-MM-DD". Returns [] when the market was closed that day.
        """
        payload = self._get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{date}",
            params={"adjusted": "true" if adjusted else "false"},
        )
        results = payload.get("results") or []
        return [
            GroupedBar(
                ticker=row["T"],
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=int(row["v"]),
                timestamp_ms=int(row["t"]),
            )
            for row in results
        ]

    def ticker_range(
        self,
        ticker: str,
        from_date: str,
        to_date: str,
        adjusted: bool = True,
    ) -> list[GroupedBar]:
        """Daily OHLCV bars for a single ticker across a date range — one HTTP call.

        from_date / to_date: "YYYY-MM-DD". Max 50k results per call is plenty for
        daily granularity across multi-year windows.
        """
        payload = self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}",
            params={
                "adjusted": "true" if adjusted else "false",
                "limit": 50000,
                "sort": "asc",
            },
        )
        results = payload.get("results") or []
        return [
            GroupedBar(
                ticker=ticker,
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=int(row["v"]),
                timestamp_ms=int(row["t"]),
            )
            for row in results
        ]

    def delisted_tickers(
        self,
        market: str = "stocks",
        type_: str = "CS",
        delisted_gte: str | None = None,
        delisted_lte: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream delisted tickers. Supports delisted_utc range filtering via query params.

        Each yielded dict has at least `ticker`, `name`, `delisted_utc`.
        """
        params: dict[str, Any] = {
            "active": "false",
            "market": market,
            "type": type_,
            "limit": 1000,
            "order": "asc",
            "sort": "delisted_utc",
        }
        if delisted_gte:
            params["delisted_utc.gte"] = delisted_gte
        if delisted_lte:
            params["delisted_utc.lte"] = delisted_lte
        yield from self._paginate("/v3/reference/tickers", params)

    def splits(self, ticker: str | None = None) -> Iterator[dict[str, Any]]:
        """Stream split events (paginated). Yields dicts with split_from/split_to/execution_date."""
        params: dict[str, Any] = {"limit": 1000, "order": "asc"}
        if ticker:
            params["ticker"] = ticker
        yield from self._paginate("/v3/reference/splits", params)

    def dividends(self, ticker: str | None = None) -> Iterator[dict[str, Any]]:
        """Stream dividend events (paginated). Yields dicts with ex_dividend_date/cash_amount."""
        params: dict[str, Any] = {"limit": 1000, "order": "asc"}
        if ticker:
            params["ticker"] = ticker
        yield from self._paginate("/v3/reference/dividends", params)

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        url: str | None = path
        current_params: dict[str, Any] | None = params
        while url:
            payload = self._get(url, params=current_params)
            yield from payload.get("results") or []
            next_url = payload.get("next_url")
            if not next_url:
                return
            url = next_url
            current_params = None  # next_url already carries all state

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._min_interval_s:
            self._sleep(self._min_interval_s - elapsed)
        self._last_call_ts = time.monotonic()

    _TRANSIENT_NET_EXCEPTIONS = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )

    def _get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._throttle()
        url = path_or_url if path_or_url.startswith("http") else f"{self._base_url}{path_or_url}"
        merged = dict(params or {})
        merged["apiKey"] = self._api_key

        resp = self._request_with_retry(url, merged)

        if resp.status_code == 429:
            logger.warning("polygon 429 rate-limited; backing off 60s")
            self._sleep(60)
            resp = self._request_with_retry(url, merged)
        if resp.status_code >= 400:
            raise PolygonError(f"{resp.status_code} {resp.text[:200]}")
        return resp.json()

    def _request_with_retry(self, url: str, params: dict[str, Any]):
        """Up to 3 attempts with 5s, 15s backoff on transient network failures."""
        backoffs = (5, 15)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return self._session.get(url, params=params, timeout=30)
            except self._TRANSIENT_NET_EXCEPTIONS as exc:
                last_exc = exc
                if attempt < len(backoffs):
                    logger.warning(
                        "polygon transient net error (attempt %d/3): %s; sleeping %ds",
                        attempt + 1,
                        exc,
                        backoffs[attempt],
                    )
                    self._sleep(backoffs[attempt])
                    continue
                break
        raise PolygonError(f"exhausted network retries: {last_exc}") from last_exc
