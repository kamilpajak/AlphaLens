"""SEC EDGAR HTTP client — submissions index and Form 4 XML fetches.

SEC requires a descriptive ``User-Agent`` header containing a contact
(email or URL) on every request; omitting it returns 403. Polite rate is
10 req/s; the client throttles to that by default and backs off 60 s on
429 or 5xx responses.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DATA_BASE = "https://data.sec.gov"
_ARCHIVES_BASE = "https://www.sec.gov"
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class SecEdgarError(RuntimeError):
    """Non-transient SEC EDGAR failure (auth, schema, permanent 4xx/5xx)."""


class SecEdgarClient:
    def __init__(
        self,
        user_agent: str,
        rate_limit_per_sec: int = 10,
        session: requests.Session | None = None,
        sleep: callable = time.sleep,
    ):
        if not user_agent:
            raise ValueError("SEC EDGAR requires a non-empty User-Agent")
        if "@" not in user_agent and "http" not in user_agent.lower():
            raise ValueError(
                "SEC EDGAR User-Agent must include a contact (email or URL)"
            )
        self._user_agent = user_agent
        self._min_interval_s = 1.0 / max(rate_limit_per_sec, 1)
        self._session = session or requests.Session()
        self._sleep = sleep
        self._last_call_ts: float = 0.0
        # Per-process in-memory cache: submissions JSON and Form 4 XML payloads
        # are content-addressable and static — once fetched they never change
        # during a backtest run, so caching them eliminates the "refetch every
        # scorer call" trap that dominated Phase 3b.3 initial runtime.
        self._submissions_cache: dict[str, dict[str, Any]] = {}
        self._form4_xml_cache: dict[tuple[str, str], bytes] = {}

    def fetch_submissions(self, cik: str) -> dict[str, Any]:
        """Fetch a filer's submissions index. cik must be 10-digit zero-padded."""
        cached = self._submissions_cache.get(cik)
        if cached is not None:
            return cached
        url = f"{_DATA_BASE}/submissions/CIK{cik}.json"
        data = self._get_json(url)
        self._submissions_cache[cik] = data
        return data

    def fetch_company_tickers(self) -> dict[str, Any]:
        """Fetch SEC's master ticker→CIK mapping (refreshed daily).

        Returns the raw JSON dict of ``{index: {cik_str, ticker, title}}``.
        """
        return self._get_json(_COMPANY_TICKERS_URL)

    def fetch_company_facts(self, cik: str) -> dict[str, Any]:
        """Fetch SEC XBRL companyfacts (all reported concepts ever filed)."""
        url = f"{_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
        return self._get_json(url)

    def fetch_form4_xml(
        self,
        cik: str,
        accession_number: str,
        primary_doc: str,
    ) -> bytes:
        """Fetch raw Form 4 XML bytes.

        accession_number format: ``XXXXXXXXXX-YY-NNNNNN`` (with dashes).
        EDGAR archive URLs use the CIK without leading zeros and the
        accession number without dashes.
        """
        cache_key = (cik, accession_number)
        cached = self._form4_xml_cache.get(cache_key)
        if cached is not None:
            return cached
        cik_no_zeros = str(int(cik))
        acc_no_dashes = accession_number.replace("-", "")
        url = (
            f"{_ARCHIVES_BASE}/Archives/edgar/data/"
            f"{cik_no_zeros}/{acc_no_dashes}/{primary_doc}"
        )
        data = self._get_bytes(url)
        self._form4_xml_cache[cache_key] = data
        return data

    _TRANSIENT_NET_EXCEPTIONS = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._min_interval_s:
            self._sleep(self._min_interval_s - elapsed)
        self._last_call_ts = time.monotonic()

    def _get_json(self, url: str) -> dict[str, Any]:
        resp = self._request(url)
        return resp.json()

    def _get_bytes(self, url: str) -> bytes:
        resp = self._request(url)
        return resp.content

    # Unified retry: both 429 (rate-limited) and 5xx (transient server error)
    # are retryable. 4xx-except-429 is permanent. Up to 3 total attempts.
    # 429 uses 60s backoff (SEC polite guideline); 5xx uses 10s / 30s
    # exponential backoff. Mixed sequences (e.g. 500 → 429 → 200) succeed.
    _SERVER_ERROR_BACKOFFS = (10, 30)
    _RATE_LIMIT_BACKOFF = 60
    _MAX_REQUEST_ATTEMPTS = 3

    def _request(self, url: str):
        resp = None
        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            self._throttle()
            resp = self._request_with_retry(url)
            if attempt == self._MAX_REQUEST_ATTEMPTS - 1:
                break
            if resp.status_code == 429:
                logger.warning(
                    "sec edgar 429 rate-limited (attempt %d/%d); sleeping %ds",
                    attempt + 1, self._MAX_REQUEST_ATTEMPTS,
                    self._RATE_LIMIT_BACKOFF,
                )
                self._sleep(self._RATE_LIMIT_BACKOFF)
                continue
            if 500 <= resp.status_code < 600:
                backoff = self._SERVER_ERROR_BACKOFFS[
                    min(attempt, len(self._SERVER_ERROR_BACKOFFS) - 1)
                ]
                logger.warning(
                    "sec edgar %d server error (attempt %d/%d); sleeping %ds",
                    resp.status_code, attempt + 1,
                    self._MAX_REQUEST_ATTEMPTS, backoff,
                )
                self._sleep(backoff)
                continue
            break
        if resp.status_code >= 400:
            raise SecEdgarError(f"{resp.status_code} {resp.text[:200]}")
        return resp

    def _request_with_retry(self, url: str):
        """Up to 3 attempts with 5s, 15s backoff on transient network failures."""
        backoffs = (5, 15)
        last_exc: Exception | None = None
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}
        for attempt in range(3):
            try:
                return self._session.get(url, headers=headers, timeout=30)
            except self._TRANSIENT_NET_EXCEPTIONS as exc:
                last_exc = exc
                if attempt < len(backoffs):
                    logger.warning(
                        "sec edgar transient net error (attempt %d/3): %s; sleeping %ds",
                        attempt + 1,
                        exc,
                        backoffs[attempt],
                    )
                    self._sleep(backoffs[attempt])
                    continue
                break
        raise SecEdgarError(f"exhausted network retries: {last_exc}") from last_exc
