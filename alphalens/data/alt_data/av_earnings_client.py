"""Alpha Vantage EARNINGS bulk-fetch client with disk cache and throttle.

Used by paradigm-14 PEAD v2 (`alphalens/screeners/event_drift/`) to obtain
PIT-correct quarterly EPS surprises for cross-sectional ranking. AV's
`EARNINGS` endpoint returns `estimatedEPS` (consensus at announcement),
`reportedEPS`, and `reportTime` (pre/post-market) for every quarterly
report — exactly the fields PSS = (reportedEPS - estimatedEPS) / close_t-1
needs. PIT validation against contemporaneous primary sources is logged
in `~/.alphalens/av_cache/pit_validation_2026_05_13.json` (5/5 PASS).

Free-tier quota is 25 requests/day burst; throttle defaults to 1.5s
between calls (empirically safe). Cache is one JSON file per ticker so
the ~3-week 510-name backfill is resumable across crashes / day-boundary
quota resets.

The fetcher is injected as a callable so tests never hit the network.
The default fetcher uses stdlib `urllib.request` (same pattern as
`alphalens.data.fundamentals.fetcher`).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

logger = logging.getLogger(__name__)

_AV_BASE_URL = "https://www.alphavantage.co/query"
_REQUIRED_QUARTERLY_FIELDS = ("fiscalDateEnding", "reportedDate", "reportedEPS", "estimatedEPS")

FetcherFn = Callable[[str], dict]
SleepFn = Callable[[float], None]


class AVRateLimitError(RuntimeError):
    """Alpha Vantage signalled rate-limit / quota exhaustion."""


class AVSchemaError(ValueError):
    """AV EARNINGS response missing required fields."""


def _default_fetcher(ticker: str) -> dict:
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is not set.")

    params = {"function": "EARNINGS", "symbol": ticker, "apikey": api_key}
    url = f"{_AV_BASE_URL}?{urlencode(params)}"
    with urlopen(url, timeout=30) as resp:
        body = resp.read().decode("utf-8")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AVSchemaError(f"AV returned non-JSON body for {ticker}") from exc

    if not isinstance(data, dict):
        raise AVSchemaError(f"AV returned non-dict body for {ticker}")

    if "Information" in data:
        info = str(data["Information"]).lower()
        if "rate limit" in info or "api key" in info or "premium" in info:
            raise AVRateLimitError(f"AV rate-limited on {ticker}: {data['Information']}")

    if "Error Message" in data:
        raise AVSchemaError(f"AV error for {ticker}: {data['Error Message']}")

    return data


def _validate_payload(payload: dict) -> None:
    quarterly = payload.get("quarterlyEarnings")
    if not isinstance(quarterly, list) or not quarterly:
        raise AVSchemaError("payload missing non-empty 'quarterlyEarnings' array")
    for i, entry in enumerate(quarterly):
        if not isinstance(entry, dict):
            raise AVSchemaError(f"quarterlyEarnings[{i}] is not a dict")
        missing = [f for f in _REQUIRED_QUARTERLY_FIELDS if f not in entry]
        if missing:
            raise AVSchemaError(
                f"quarterlyEarnings[{i}] missing required fields {missing}; got keys {list(entry)}"
            )


def _cache_path(cache_dir: Path, ticker: str) -> Path:
    return cache_dir / f"earnings_{ticker.upper()}.json"


def load_earnings(ticker: str, cache_dir: Path) -> dict | None:
    """Read cached AV EARNINGS payload for `ticker`. None if not cached.

    Does NOT validate the payload (callers reading historical caches can
    tolerate older minimal schemas). Use `fetch_earnings` if validation
    is required.
    """
    path = _cache_path(cache_dir, ticker)
    if not path.exists():
        return None
    with path.open() as f:
        return json.load(f)


def fetch_earnings(
    ticker: str,
    cache_dir: Path,
    *,
    fetcher: FetcherFn | None = None,
    rate_limit_sleep: float = 60.0,
    sleep_fn: SleepFn | None = None,
) -> dict:
    """Cache-aware fetch of AV EARNINGS for one ticker.

    Behaviour:
    - If `earnings_<TICKER>.json` exists in `cache_dir`, return its content
      after schema validation (catches corrupt partial writes).
    - Otherwise call `fetcher(ticker)`, validate, write to cache, return.
    - On `AVRateLimitError` from the first fetcher call, sleep for
      `rate_limit_sleep` seconds (via `sleep_fn`) and retry exactly once.
    - Invalid payloads are NEVER cached — a re-run can retry cleanly.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, ticker)

    if path.exists():
        with path.open() as f:
            cached = json.load(f)
        _validate_payload(cached)  # surface corruption from partial writes
        return cached

    fetch = fetcher or _default_fetcher
    sleep = sleep_fn or time.sleep

    try:
        payload = fetch(ticker.upper())
    except AVRateLimitError:
        sleep(rate_limit_sleep)
        payload = fetch(ticker.upper())  # propagates if it fails again

    _validate_payload(payload)
    path.write_text(json.dumps(payload))
    return payload


def fetch_earnings_batch(
    tickers: list[str],
    cache_dir: Path,
    *,
    fetcher: FetcherFn | None = None,
    throttle_seconds: float = 1.5,
    rate_limit_sleep: float = 60.0,
    sleep_fn: SleepFn | None = None,
) -> dict[str, str]:
    """Throttled, resumable batch fetch. Returns per-ticker status map.

    Status values: ``"cached"`` (already on disk), ``"fetched"`` (new
    write), ``"failed"`` (schema error — logged and skipped). On
    persistent rate-limit (retry exhausted), the exception propagates so
    the operator can resume on next quota window without burning further
    requests.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    sleep = sleep_fn or time.sleep

    statuses: dict[str, str] = {}
    for ticker in tickers:
        path = _cache_path(cache_dir, ticker)
        if path.exists():
            statuses[ticker] = "cached"
            continue

        try:
            fetch_earnings(
                ticker,
                cache_dir,
                fetcher=fetcher,
                rate_limit_sleep=rate_limit_sleep,
                sleep_fn=sleep,
            )
        except AVSchemaError as exc:
            logger.warning("AV schema error for %s: %s", ticker, exc)
            statuses[ticker] = "failed"
            continue
        # AVRateLimitError after retry: propagate, preserving partial progress.

        statuses[ticker] = "fetched"
        if throttle_seconds > 0:
            sleep(throttle_seconds)
    return statuses
