"""SEC EDGAR HTTP client — submissions index and Form 4 XML fetches.

SEC requires a descriptive ``User-Agent`` header containing a contact
(email or URL) on every request; omitting it returns 403. Polite rate is
10 req/s; the client throttles to that by default and backs off 60 s on
429 or 5xx responses.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from alphalens_pipeline.data.alt_data.sec_rate_coordinator import (
    SecRateCoordinator,
    default_coord_path,
)

logger = logging.getLogger(__name__)

_DATA_BASE = "https://data.sec.gov"
_ARCHIVES_BASE = "https://www.sec.gov"
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"

# Project-wide default UA used when SEC_EDGAR_USER_AGENT is unset. Includes the
# operator contact required by SEC's fair-access policy (must contain "@" or
# a URL or every request gets 403). Single source of truth — duplicated nowhere.
ALPHALENS_DEFAULT_USER_AGENT = "AlphaLens pajakkamil@gmail.com"


class SecEdgarError(RuntimeError):
    """Non-transient SEC EDGAR failure (auth, schema, permanent 4xx/5xx)."""


class SecForbiddenError(SecEdgarError):
    """SEC 403 — auth/UA-reject OR traffic-threshold (shared-IP rate).

    Raised immediately (a shared-IP traffic-403 will not clear inside one
    process's 3 attempts, so retrying it just burns budget). Downstream ingest
    classifies it as a transient cache-poison signal so an all-403 day does not
    persist an empty parquet that poisons later same-UTC-day runs (#379/#382/#383).
    """


def _evict_to_capacity(cache: dict, max_size: int) -> None:
    """FIFO eviction: drop oldest entries until ``len(cache) <= max_size``.

    Relies on dict insertion order (Python 3.7+). Cheap: O(1) per eviction
    via ``pop(next(iter(cache)))``. Intended to be called right before a
    new insertion to reserve space.
    """
    max_size = max(max_size, 0)
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))


class SecEdgarClient:
    def __init__(
        self,
        user_agent: str,
        rate_limit_per_sec: int = 10,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        coordinator: SecRateCoordinator | None = None,
    ):
        if not user_agent:
            raise ValueError("SEC EDGAR requires a non-empty User-Agent")
        if "@" not in user_agent and "http" not in user_agent.lower():
            raise ValueError("SEC EDGAR User-Agent must include a contact (email or URL)")
        self._user_agent = user_agent
        self._min_interval_s = 1.0 / max(rate_limit_per_sec, 1)
        self._session = session or requests.Session()
        self._sleep = sleep
        self._coordinator = coordinator  # cross-process IP-wide gate (opt-in)
        self._last_call_ts: float = 0.0
        # Per-process in-memory cache: submissions JSON and Form 4 XML payloads
        # are content-addressable and static — once fetched they never change
        # during a backtest run, so caching them eliminates the "refetch every
        # scorer call" trap that dominated Phase 3b.3 initial runtime.
        # FIFO-bounded per Zen CR (2026-04-24) so long-running prewarm
        # (32h observed) cannot grow memory without bound. Capacities tuned
        # for R2000-scale universes (~2000 tickers, ~500 Form 4/yr average).
        self._submissions_cache: dict[str, dict[str, Any]] = {}
        self._form4_xml_cache: dict[tuple[str, str], bytes] = {}
        self._xbrl_frame_cache: dict[str, dict[str, Any]] = {}
        self._submissions_cache_capacity = 5_000
        self._form4_xml_cache_capacity = 50_000
        # XBRL frames are one concept aggregated across all filers for a period;
        # a brief's whole candidate set shares the same few (concept, period)
        # frames, so an in-process cache eliminates refetch across candidates.
        self._xbrl_frame_cache_capacity = 5_000

    def fetch_submissions(self, cik: str) -> dict[str, Any]:
        """Fetch a filer's submissions index. cik must be 10-digit zero-padded."""
        cached = self._submissions_cache.get(cik)
        if cached is not None:
            return cached
        url = f"{_DATA_BASE}/submissions/CIK{cik}.json"
        data = self._get_json(url)
        _evict_to_capacity(self._submissions_cache, self._submissions_cache_capacity - 1)
        self._submissions_cache[cik] = data
        return data

    def fetch_submissions_overflow(self, name: str) -> dict[str, Any]:
        """Fetch a submissions overflow file (>1000 filings → CIK*-submissions-NNN.json).

        SEC paginates submissions index for prolific filers: the main
        ``CIK{cik}.json`` holds the most recent 1000 filings, and the
        ``filings.files`` array points to additional JSONs (e.g.
        ``CIK0000320193-submissions-001.json``) that share the same
        ``{filings: {recent: {...}}}`` shape. Cached identically to the main
        index so a long-running backfill doesn't refetch.
        """
        cached = self._submissions_cache.get(name)
        if cached is not None:
            return cached
        url = f"{_DATA_BASE}/submissions/{name}"
        data = self._get_json(url)
        _evict_to_capacity(self._submissions_cache, self._submissions_cache_capacity - 1)
        self._submissions_cache[name] = data
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

    def fetch_xbrl_frame(
        self, taxonomy: str, concept: str, unit: str, period: str
    ) -> dict[str, Any]:
        """Fetch an XBRL *frame* — one concept aggregated across all filers for a period.

        URL: ``/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json`` (e.g.
        ``ecd / PeoActuallyPaidCompAmt / USD / CY2024``) → ``{"data": [{accn, cik,
        val, end, ...}]}``. Used by the Buffett DEF 14A pay-vs-performance reader,
        whose whole candidate set shares the same few (concept, period) frames —
        hence the per-process FIFO-bounded cache (same pattern as submissions).
        """
        cache_key = f"{taxonomy}/{concept}/{unit}/{period}"
        cached = self._xbrl_frame_cache.get(cache_key)
        if cached is not None:
            return cached
        url = f"{_DATA_BASE}/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json"
        data = self._get_json(url)
        _evict_to_capacity(self._xbrl_frame_cache, self._xbrl_frame_cache_capacity - 1)
        self._xbrl_frame_cache[cache_key] = data
        return data

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
        url = f"{_ARCHIVES_BASE}/Archives/edgar/data/{cik_no_zeros}/{acc_no_dashes}/{primary_doc}"
        data = self._get_bytes(url)
        _evict_to_capacity(self._form4_xml_cache, self._form4_xml_cache_capacity - 1)
        self._form4_xml_cache[cache_key] = data
        return data

    def get_json(self, url: str) -> dict[str, Any]:
        """Fetch ``url`` and parse JSON. Public escape hatch for shadow callers
        (edgar_detector, thematic verification) that need SEC URLs not covered by
        the ``fetch_*`` convenience methods. Goes through the same throttle +
        retry + User-Agent contract.
        """
        return self._get_json(url)

    def get_bytes(self, url: str) -> bytes:
        """Fetch ``url`` and return raw bytes. Used for atom feeds, XML
        primary docs, and 10-K/8-K HTML where the caller does its own parsing.
        """
        return self._get_bytes(url)

    def get_text(self, url: str, *, encoding: str = "utf-8") -> str:
        """Fetch ``url`` and decode as text (default UTF-8). Convenience over
        ``get_bytes`` for callers that immediately decode."""
        return self._get_bytes(url).decode(encoding)

    # Catch the entire RequestException family — SSLError, InvalidURL,
    # TooManyRedirects, and the connection/timeout subclasses all surface
    # here. Narrow tuples leak unrelated requests failures to callers and
    # crash the launchd detector on otherwise-transient SSL noise.
    _TRANSIENT_NET_EXCEPTIONS = (requests.RequestException,)

    def _throttle(self) -> None:
        # Per-process smoothing only (monotonic). The cross-process IP-wide gate
        # runs once per logical request in _request (NOT per retry attempt), so a
        # 429/5xx retry sequence does not multiply the shared reservation.
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

    def _request(self, url: str) -> requests.Response:
        # Cross-process IP-wide gate ONCE per logical request (not per retry
        # attempt). No-op when no coordinator is wired or the lock dir is
        # unwritable. Uses wall time (shared across processes); the per-process
        # smoothing in _throttle stays on monotonic.
        if self._coordinator is not None:
            self._coordinator.wait_for_slot()
        resp: requests.Response | None = None
        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            self._throttle()
            resp = self._request_with_retry(url)
            if attempt == self._MAX_REQUEST_ATTEMPTS - 1:
                break
            if resp.status_code == 429:
                logger.warning(
                    "sec edgar 429 rate-limited (attempt %d/%d); sleeping %ds",
                    attempt + 1,
                    self._MAX_REQUEST_ATTEMPTS,
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
                    resp.status_code,
                    attempt + 1,
                    self._MAX_REQUEST_ATTEMPTS,
                    backoff,
                )
                self._sleep(backoff)
                continue
            break
        assert resp is not None
        if resp.status_code >= 400:
            self._raise_for_4xx(url, resp)
        return resp

    # SEC throttle-403 carries a body like "Request Rate Threshold Exceeded" and
    # may set Retry-After; a UA-reject 403 carries a different body. Surface the
    # FULL body + the triage headers (truncating at 200 chars hid the epic #379
    # 403 root cause). 403 stays a non-retried immediate raise.
    _DIAGNOSTIC_HEADERS = ("Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining")

    def _raise_for_4xx(self, url: str, resp: requests.Response) -> None:
        headers = getattr(resp, "headers", {}) or {}
        diag = {k: headers[k] for k in self._DIAGNOSTIC_HEADERS if k in headers}
        detail = f"{resp.status_code} {url} headers={diag} body={resp.text}"
        if resp.status_code == 403:
            logger.warning("sec edgar 403 forbidden: %s", detail)
            raise SecForbiddenError(detail)
        raise SecEdgarError(detail)

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


# Module-level lazy singleton — single SecEdgarClient instance shared by every
# caller that doesn't have its own injected client (edgar_detector, thematic
# verification module-level functions). Reading SEC_EDGAR_USER_AGENT once at
# first call keeps the env-var resolution centralized; tests reset via
# _reset_default_client_for_tests().
_DEFAULT_CLIENT: SecEdgarClient | None = None
# Guards first-call construction so two threads racing the first call
# don't each build a client (and a redundant SecRateCoordinator).
# Double-checked locking (same idiom as ``paper.calendar._calendar``).
_DEFAULT_CLIENT_LOCK = threading.Lock()


def get_default_sec_client() -> SecEdgarClient:
    """Return the process-wide default SecEdgarClient (lazy-initialized).

    Reads ``SEC_EDGAR_USER_AGENT`` from the environment on first call; falls
    back to :data:`ALPHALENS_DEFAULT_USER_AGENT`. Subsequent calls return the
    same instance — caches and throttle state are shared across every caller
    in the process, which is precisely the SEC fair-access contract we want.
    Construction is thread-safe via double-checked locking.
    """
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        with _DEFAULT_CLIENT_LOCK:
            if _DEFAULT_CLIENT is None:
                user_agent = os.environ.get(USER_AGENT_ENV) or ALPHALENS_DEFAULT_USER_AGENT
                rate_limit = 10
                coordinator = SecRateCoordinator(
                    path=default_coord_path(),
                    min_interval_s=1.0 / rate_limit,
                )
                _DEFAULT_CLIENT = SecEdgarClient(
                    user_agent=user_agent,
                    rate_limit_per_sec=rate_limit,
                    coordinator=coordinator,
                )
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None
