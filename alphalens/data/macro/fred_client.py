"""FRED (St. Louis Fed) HTTP client with disk cache and 5xx retry.

Used by Tactical Sector Rotation (Layer 2e) for yield curve (DGS10, DGS2),
VIX (VIXCLS), and other macro series. Free tier provides 120 req/min, which
is more than enough — we cache aggressively to disk so every unique series
is fetched once per day at most.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


class FREDError(RuntimeError):
    """Non-transient FRED failure (4xx, exhausted retries)."""


class FREDAuthError(FREDError):
    """Missing or invalid API key."""


class FREDClient:
    def __init__(
        self,
        *,
        api_key: str,
        cache_dir: Path,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        retry_backoff_s: float = 2.0,
    ):
        if not api_key:
            raise FREDAuthError("FRED API key is required (set FRED_API_KEY env var)")
        self._api_key = api_key
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = session or requests.Session()
        self._sleep = sleep
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s

    @classmethod
    def from_env(cls, *, cache_dir: Path | None = None) -> FREDClient:
        api_key = os.environ.get("FRED_API_KEY", "")
        cache_dir = cache_dir or Path.home() / ".alphalens" / "macro"
        return cls(api_key=api_key, cache_dir=cache_dir)

    def _cache_path(self, series_id: str) -> Path:
        return self._cache_dir / f"FRED_{series_id}.parquet"

    def fetch_series(self, series_id: str) -> pd.Series:
        """Fetch a FRED series as pd.Series[float] indexed by DatetimeIndex.

        Disk-cached; FRED sentinel "." values (missing observations) are dropped.
        """
        cache = self._cache_path(series_id)
        if cache.exists():
            return pd.read_parquet(cache).iloc[:, 0]

        url = f"{_BASE_URL}?series_id={series_id}&api_key={self._api_key}&file_type=json"
        payload = self._get_with_retry(url, series_id)
        series = _parse_observations(payload, series_id)

        # Persist as a 1-column DataFrame (Parquet doesn't round-trip Series cleanly).
        series.to_frame(name=series_id).to_parquet(cache)
        return series

    def _get_with_retry(self, url: str, series_id: str) -> dict:
        attempt = 0
        while True:
            resp = self._session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if 400 <= resp.status_code < 500:
                raise FREDError(f"FRED returned {resp.status_code} for series {series_id}")
            # 5xx: retry
            if attempt >= self._max_retries:
                raise FREDError(
                    f"FRED {resp.status_code} persisted after {attempt} retries "
                    f"for series {series_id}"
                )
            backoff = self._retry_backoff_s * (2**attempt)
            logger.warning(
                "FRED %s %s; retrying in %.1fs (attempt %d/%d)",
                resp.status_code,
                series_id,
                backoff,
                attempt + 1,
                self._max_retries,
            )
            self._sleep(backoff)
            attempt += 1


def _parse_observations(payload: dict, series_id: str) -> pd.Series:
    obs = payload.get("observations", [])
    if not obs:
        raise FREDError(f"FRED returned empty observations for {series_id}")
    dates, values = [], []
    for row in obs:
        raw = row.get("value", ".")
        if raw in (".", "", None):
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
        dates.append(pd.Timestamp(row["date"]))
    if not values:
        raise FREDError(f"no valid observations for {series_id}")
    return pd.Series(values, index=pd.DatetimeIndex(dates, name="date"), name=series_id)
