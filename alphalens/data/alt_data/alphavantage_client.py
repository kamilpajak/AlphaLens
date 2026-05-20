"""Canonical Alpha Vantage HTTP client.

Single source of truth for every Alpha Vantage REST call in the project.
Both the fundamentals fetcher (OVERVIEW / BALANCE_SHEET / CASH_FLOW /
INCOME_STATEMENT) and the EARNINGS bulk-cache route through this client.

Why a single client: AV's free-tier quota (25 req/day) is per-API-key,
so any uncoordinated shadow caller eats quota from every other consumer
in the process. Centralising auth + rate-limit detection + (optional)
throttle here means a single fix lands once when AV changes the format
of its "Information" rate-limit signal, and one set of tests covers
every vendor-specific edge case.

What this client does NOT do:
- Disk caching. Per-ticker JSON cache is an EARNINGS-specific concern
  (resumable 3-week backfill, day-boundary quota resets) and lives in
  ``av_earnings_client``. Fundamentals fetcher consumers do their own
  ``fundamentals/cache.py`` layering.
- PIT filtering on ``fiscalDateEnding``. Vendor-agnostic concern owned
  by the fundamentals adapter.
- Retry orchestration around quota refills. ``av_earnings_client`` owns
  the "sleep N seconds and retry once" loop because only batch callers
  can decide whether to burn the quota window or wait.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

logger = logging.getLogger(__name__)

_AV_BASE_URL = "https://www.alphavantage.co/query"
API_KEY_ENV = "ALPHA_VANTAGE_API_KEY"

# Phrases that, when found in AV's ``Information`` field, indicate the
# response is a quota / auth signal rather than valid data. Matched
# case-insensitively as a substring scan over the lower-cased field.
_RATE_LIMIT_PHRASES = ("rate limit", "api key", "premium")

UrlopenFn = Callable[..., Any]
SleepFn = Callable[[float], None]

__all__ = [
    "API_KEY_ENV",
    "AVRateLimitError",
    "AVSchemaError",
    "AlphaVantageClient",
    "get_default_av_client",
]


class AVRateLimitError(RuntimeError):
    """Alpha Vantage signalled rate-limit / quota / auth exhaustion.

    Distinct from generic fetch failures so batch callers can branch on it
    (sleep + retry vs abort the batch).
    """


class AVSchemaError(ValueError):
    """Alpha Vantage returned a malformed or vendor-rejected response.

    Covers non-JSON bodies, non-dict top-level structures, and explicit
    ``Error Message`` payloads (invalid ticker, malformed call). Adapters
    that want soft-fail semantics (return ``{}`` instead) catch this.
    """


class AlphaVantageClient:
    """Thin REST wrapper over the AV ``query`` endpoint.

    The client is intentionally state-light: it owns the API key, the
    optional throttle, and the urlopen fn. Caching and retry orchestration
    are caller concerns (see :mod:`av_earnings_client`).
    """

    def __init__(
        self,
        api_key: str,
        *,
        throttle_seconds: float = 0.0,
        timeout: float = 30.0,
        urlopen_fn: UrlopenFn = urlopen,
        sleep_fn: SleepFn = time.sleep,
    ):
        if not api_key:
            raise ValueError(f"Alpha Vantage requires a non-empty API key (env {API_KEY_ENV})")
        self._api_key = api_key
        self._throttle_seconds = max(0.0, float(throttle_seconds))
        self._timeout = timeout
        self._urlopen = urlopen_fn
        self._sleep = sleep_fn
        # monotonic ts of the most recent call; 0.0 means "no calls yet" so the
        # first query() skips the throttle.
        self._last_call_ts: float = 0.0

    @classmethod
    def from_env(
        cls,
        *,
        throttle_seconds: float = 0.0,
        timeout: float = 30.0,
        urlopen_fn: UrlopenFn = urlopen,
        sleep_fn: SleepFn = time.sleep,
    ) -> AlphaVantageClient:
        """Build a client reading the API key from ``ALPHA_VANTAGE_API_KEY``."""
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise ValueError(f"{API_KEY_ENV} environment variable is not set.")
        return cls(
            api_key=api_key,
            throttle_seconds=throttle_seconds,
            timeout=timeout,
            urlopen_fn=urlopen_fn,
            sleep_fn=sleep_fn,
        )

    def query(self, function: str, **params: str) -> dict[str, Any]:
        """Call the AV ``query`` endpoint and return the parsed JSON dict.

        ``function`` is the AV function name (OVERVIEW, EARNINGS, ...).
        Any keyword params are URL-encoded alongside ``function`` and the
        injected API key.

        Raises:
            AVRateLimitError: AV ``Information`` field matched a known
                rate-limit / api-key / premium signal.
            AVSchemaError: body was not JSON, not a dict, or contained an
                ``Error Message``.
            urllib.error.HTTPError / urllib.error.URLError: network
                failure — caller decides whether to retry.
        """
        self._throttle()
        # Canonical keys win: putting them after **params spread means a caller
        # that accidentally passes `function=...` or `apikey=...` via kwargs
        # cannot shadow the injected values.
        full_params = {**params, "function": function, "apikey": self._api_key}
        url = f"{_AV_BASE_URL}?{urlencode(full_params)}"
        with self._urlopen(url, timeout=self._timeout) as resp:
            body = resp.read().decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AVSchemaError(
                f"Alpha Vantage returned non-JSON body for {function}: {body[:200]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise AVSchemaError(
                f"Alpha Vantage returned non-dict top-level for {function}: {type(data).__name__}"
            )

        info = data.get("Information")
        if isinstance(info, str):
            info_lower = info.lower()
            if any(phrase in info_lower for phrase in _RATE_LIMIT_PHRASES):
                raise AVRateLimitError(f"Alpha Vantage rate-limited on {function}: {info}")

        if "Error Message" in data:
            raise AVSchemaError(f"Alpha Vantage error for {function}: {data['Error Message']}")

        return data

    def _throttle(self) -> None:
        if self._throttle_seconds <= 0 or self._last_call_ts == 0.0:
            self._last_call_ts = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._throttle_seconds:
            self._sleep(self._throttle_seconds - elapsed)
        self._last_call_ts = time.monotonic()


# Module-level lazy singleton — one AlphaVantageClient shared by every
# module-level helper that doesn't have its own injected client (fundamentals
# fetcher module functions, av_earnings default fetcher). First call reads
# ALPHA_VANTAGE_API_KEY from the environment; tests reset via
# _reset_default_client_for_tests().
_DEFAULT_CLIENT: AlphaVantageClient | None = None


def get_default_av_client() -> AlphaVantageClient:
    """Return the process-wide default AlphaVantageClient (lazy-initialized).

    Raises ``ValueError`` if ``ALPHA_VANTAGE_API_KEY`` is unset at first
    call. Subsequent calls return the same instance — the throttle state
    is shared across every caller in the process, matching the AV
    quota's per-key semantics.
    """
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = AlphaVantageClient.from_env()
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None
