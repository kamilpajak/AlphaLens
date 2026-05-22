"""Alpha Vantage EARNINGS bulk-fetch client with disk cache and throttle.

Used by paradigm-14 PEAD v2 (`alphalens_research/screeners/event_drift/`) to obtain
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
The default fetcher delegates to
:class:`alphalens_research.data.alt_data.alphavantage_client.AlphaVantageClient`,
which is the canonical HTTP wrapper for every AV call in the repo (one
client → one quota tracker → one rate-limit-detection code path).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
from collections.abc import Callable
from pathlib import Path

from alphalens_research.data.alt_data.alphavantage_client import (
    AVRateLimitError,
    AVSchemaError,
    get_default_av_client,
)

logger = logging.getLogger(__name__)

_REQUIRED_QUARTERLY_FIELDS = (
    "fiscalDateEnding",
    "reportedDate",
    "reportedEPS",
    "estimatedEPS",
)
# reportTime is consumed by PEAD entry_offset (pre/post-market) but is NOT
# in the strict schema gate above: AV historical data legitimately lacks
# reportTime for pre-2010 filings, and rejecting an entire ticker payload
# because a single 2003 quarter is missing the field would drop otherwise-
# valid IS-period data. Consumers (PEAD scorer) must default missing
# reportTime to post-market (conservative: entry at close(t)+1 not close(t)).

FetcherFn = Callable[[str], dict]
SleepFn = Callable[[float], None]

# Re-export so downstream callers that catch these errors keep importing
# from the EARNINGS module unchanged.
__all__ = [
    "AVRateLimitError",
    "AVSchemaError",
    "FetcherFn",
    "SleepFn",
    "fetch_earnings",
    "fetch_earnings_batch",
    "load_earnings",
]


def _default_fetcher(ticker: str) -> dict:
    """Default EARNINGS fetcher — routes through the canonical AV client.

    The throttle / cache / batch orchestration in this module remains
    EARNINGS-specific (3-week backfill, quota-window resumption). The
    canonical client owns the HTTP request + auth + per-response
    schema/rate-limit detection.
    """
    return get_default_av_client().query("EARNINGS", symbol=ticker)


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
    # Atomic write: a SIGKILL or OOM mid-write would otherwise leave a
    # partial-JSON file at `path`. tmp + rename guarantees `path` always
    # holds either the prior valid payload or the new complete one.
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)
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
        except urllib.error.HTTPError as exc:
            # Permanent auth/access errors (401/403/404) must fail-fast — a
            # bad API key would otherwise silently mark all 510 tickers as
            # "failed". 429 is rate-limit, which is already handled inside
            # fetch_earnings via AVRateLimitError; an HTTP-level 429 here
            # means the per-ticker retry was exhausted, so abort the batch.
            if 400 <= exc.code < 500:
                logger.exception("Permanent HTTP %s for %s", exc.code, ticker)
                raise
            logger.warning("HTTP %s for %s: %s", exc.code, ticker, exc)
            statuses[ticker] = "failed"
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # Transient network blips during a 3-week overnight batch must not
            # abort the run. Mark failed and continue; operator can re-run to
            # retry the failed subset.
            logger.warning("Network error for %s: %s", ticker, exc)
            statuses[ticker] = "failed"
            continue
        # AVRateLimitError after retry: propagate, preserving partial progress.

        statuses[ticker] = "fetched"
        if throttle_seconds > 0:
            sleep(throttle_seconds)
    return statuses
