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

    def fetch_submissions(self, cik: str) -> dict[str, Any]:
        """Fetch a filer's submissions index. cik must be 10-digit zero-padded."""
        url = f"{_DATA_BASE}/submissions/CIK{cik}.json"
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
        cik_no_zeros = str(int(cik))
        acc_no_dashes = accession_number.replace("-", "")
        url = (
            f"{_ARCHIVES_BASE}/Archives/edgar/data/"
            f"{cik_no_zeros}/{acc_no_dashes}/{primary_doc}"
        )
        return self._get_bytes(url)

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

    def _request(self, url: str):
        self._throttle()
        resp = self._request_with_retry(url)
        if resp.status_code == 429:
            logger.warning("sec edgar 429 rate-limited; backing off 60s")
            self._sleep(60)
            resp = self._request_with_retry(url)
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
