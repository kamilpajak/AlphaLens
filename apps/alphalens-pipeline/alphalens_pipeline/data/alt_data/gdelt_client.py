"""GDELT 2.0 DOC API client — single canonical entry point for every GDELT call.

GDELT is keyless and free (~1 req/5s soft limit), so there is no API key to
centralise — but the canonical-client doctrine still applies: one shared HTTP +
retry + permanent-vs-transient seam so the live thematic news ingest (1 of 4
sources) can't accidentally grow a second, uncoordinated ``urlopen`` against the
DOC API. The thematic source module (``thematic/sources/gdelt.py``) owns the
domain logic (theme buckets, transform to the unified news schema, per-day cache
+ dedup, the inter-bucket rate-limit sleep); this client owns ONLY the raw HTTP:
URL building, the ``urlopen`` fetch, and the retry / error classification.

Permanent vs transient (the GDELT-specific wrinkle): the DOC API signals a
malformed query with HTTP 200 + a plain-text body (e.g. "The specified phrase is
too short." for single-word quoted phrases), so a non-JSON body is a PERMANENT
``GdeltQueryError`` — retrying just burns the 5s/req soft cap of the next bucket.
An empty body (soft rate-limit signal) or an ``HTTPError`` is transient and
retried with exponential backoff.

The module-level :func:`_http_get_json` is the URL-keyed seam the golden-ingest
cassette patches (``tests/golden/url_cassette.py``) — keep its ``(url, **kwargs)``
signature stable.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_MAXRECORDS = 100


class GdeltQueryError(Exception):
    """Permanent error returned by GDELT — query is malformed, do not retry.

    Triggered when the API responds 200 OK with a non-JSON body, e.g.
    ``"The specified phrase is too short."`` for queries containing
    single-word quoted phrases. Retrying just burns the rate-limit budget
    of subsequent buckets without ever succeeding.
    """


class GdeltMaxRetriesError(Exception):
    """Transient retries exhausted (empty body, HTTPError) — bucket dropped."""


def build_query_url(
    *,
    query: str,
    startdatetime: str | None = None,
    enddatetime: str | None = None,
    maxrecords: int = DEFAULT_MAXRECORDS,
    sort: str = "datedesc",
) -> str:
    """Build a GDELT DOC API URL with optional explicit UTC bounds.

    ``startdatetime`` / ``enddatetime`` are GDELT ``YYYYMMDDHHMMSS`` strings
    (absolute, UTC). They are only emitted when BOTH are provided; the legacy
    relative ``timespan`` parameter is gone (P1a strict single-day window).
    """
    params: dict[str, object] = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": maxrecords,
        "sort": sort,
    }
    if startdatetime is not None and enddatetime is not None:
        params["startdatetime"] = startdatetime
        params["enddatetime"] = enddatetime
    return f"{ENDPOINT}?{urllib.parse.urlencode(params)}"


def _http_get_json(
    url: str,
    *,
    timeout: float = 20.0,
    max_attempts: int = 3,
    backoff_sec: float = 10.0,
) -> dict:
    """Fetch JSON from GDELT, distinguishing permanent vs transient errors.

    Transient (retried with exponential backoff): empty body (soft rate-limit
    signal) and ``HTTPError`` (including real 429).

    Permanent (raised immediately as ``GdeltQueryError``): non-empty body that
    does not look like JSON. GDELT signals malformed queries with HTTP 200 and
    a plain-text message body, so retrying is wasted wall time AND triggers
    GDELT's 5s/req soft cap for the bucket that comes next.

    This is the URL-keyed seam the golden-ingest cassette patches — keep the
    ``(url, **kwargs)`` signature stable.
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AlphaLens-thematic/0.1"})
            # URL built from gdelt-base constant + querystring; file:// not reachable.
            with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
                body = r.read()
            if not body:
                raise json.JSONDecodeError("empty body (likely rate-limited)", "", 0)
            if body.lstrip()[:1] not in (b"{", b"["):
                snippet = body[:200].decode("utf-8", errors="replace").strip()
                raise GdeltQueryError(f"GDELT permanent error: {snippet}")
            return json.loads(body)
        except GdeltQueryError:
            raise
        except (json.JSONDecodeError, urllib.error.HTTPError) as e:
            last_err = e
            if attempt + 1 < max_attempts:
                time.sleep(backoff_sec * (2**attempt))
    raise GdeltMaxRetriesError(f"GDELT fetch failed after {max_attempts} attempts: {last_err}")


class GdeltClient:
    """Canonical client for the GDELT DOC API.

    Holds the HTTP config (timeout / retry budget) and exposes :meth:`fetch_doc`,
    which builds the query URL and fetches the JSON payload. The raw ``urlopen``
    lives in the module-level :func:`_http_get_json` (the golden cassette's
    URL-keyed patch seam); this class just threads its config through.
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_attempts: int = 3,
        backoff_sec: float = 10.0,
    ):
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_sec = backoff_sec

    def fetch_doc(
        self,
        *,
        query: str,
        startdatetime: str | None = None,
        enddatetime: str | None = None,
        maxrecords: int = DEFAULT_MAXRECORDS,
        sort: str = "datedesc",
    ) -> dict:
        """Build the DOC API URL for ``query`` over the bounds and return the
        parsed JSON payload. Raises :class:`GdeltQueryError` (permanent) or
        :class:`GdeltMaxRetriesError` (transient retries exhausted)."""
        url = build_query_url(
            query=query,
            startdatetime=startdatetime,
            enddatetime=enddatetime,
            maxrecords=maxrecords,
            sort=sort,
        )
        return _http_get_json(
            url,
            timeout=self._timeout,
            max_attempts=self._max_attempts,
            backoff_sec=self._backoff_sec,
        )


_DEFAULT_CLIENT: GdeltClient | None = None
_DEFAULT_CLIENT_LOCK = threading.Lock()


def get_default_gdelt_client() -> GdeltClient:
    """Return the process-wide default GdeltClient (lazy-initialized, thread-safe
    via double-checked locking — mirrors the other canonical clients)."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        with _DEFAULT_CLIENT_LOCK:
            if _DEFAULT_CLIENT is None:
                _DEFAULT_CLIENT = GdeltClient()
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None


__all__ = [
    "DEFAULT_MAXRECORDS",
    "ENDPOINT",
    "GdeltClient",
    "GdeltMaxRetriesError",
    "GdeltQueryError",
    "build_query_url",
    "get_default_gdelt_client",
]
