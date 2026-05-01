"""Polygon /stocks/v1/short-interest REST client.

Bi-monthly short interest sourced from FINRA via Polygon's $29/mo Stocks Starter tier.
Verified 2026-04-30: AAPL coverage 2017-12-29 -> 2026-04-15 = 200 settlement records.
Schema per record: {settlement_date, ticker, short_interest, avg_daily_volume,
days_to_cover}. Pagination via `next_url` field on the response.

Replaces the v1 FINRA Daily Short Sale Volume design which was infra-blocked: cdn.finra.org
returns 403 to all programmatic access; FINRA Query API has only ~10mo trailing depth.
v2 supersession documented at docs/research/v4_alt_data_pit_audit_2026_04_30.md.

PIT contract — at asof t, only settlements where (settlement_date + 8 trading days) <= t
are visible. The 8 BD lag matches FINRA Rule 4560 (settlement on 15th + last BD of month;
public dissemination 8 BD later). Polygon's `days_to_cover` field uses FINRA's 20-day
trailing avg_daily_volume window through settlement, so it carries the same lag.

Used by `alphalens/screeners/alt_data/features.py` to compute three pre-registered v2
features: short_interest_pct_float_change_60d, rank_short_interest_pct_float, and
log1p_days_to_cover.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.polygon.io/stocks/v1/short-interest"
_DISSEMINATION_LAG_BD = 8


class PolygonShortInterestError(RuntimeError):
    """Non-transient Polygon failure (4xx other than 401, exhausted retries)."""


class PolygonShortInterestAuthError(PolygonShortInterestError):
    """401: missing or invalid API key."""


@dataclass(frozen=True)
class ShortInterestRecord:
    settlement_date: date
    ticker: str
    short_interest: int
    avg_daily_volume: int
    days_to_cover: float


def _add_business_days(d: date, n: int) -> date:
    """Add ``n`` business days (Mon-Fri) to ``d``. Does NOT account for holidays.
    Sufficient for FINRA dissemination-lag tests at the granularity used here:
    short-interest features have a 60-calendar-day comparison window so a small
    holiday miscount cannot cross multiple settlement boundaries.
    """
    cur = d
    added = 0
    while added < n:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def _is_available_at(*, asof: date, settlement: date) -> bool:
    """True iff the FINRA dissemination of `settlement` is on or before `asof`."""
    return _add_business_days(settlement, _DISSEMINATION_LAG_BD) <= asof


class PolygonShortInterestClient:
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
            raise PolygonShortInterestAuthError(
                "POLYGON_API_KEY is required (set env var or pass api_key=)"
            )
        self._api_key = api_key
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = session or requests.Session()
        self._sleep = sleep
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s

    @classmethod
    def from_env(cls, *, cache_dir: Path | None = None) -> PolygonShortInterestClient:
        api_key = os.environ.get("POLYGON_API_KEY", "")
        cache_dir = cache_dir or Path.home() / ".alphalens" / "polygon_short_interest"
        return cls(api_key=api_key, cache_dir=cache_dir)

    def _cache_path(self, ticker: str) -> Path:
        return self._cache_dir / f"{ticker.upper()}.parquet"

    def fetch_ticker(self, ticker: str, *, refresh: bool = False) -> pd.DataFrame:
        """Full short-interest history for a ticker.

        Returns DataFrame indexed by ``settlement_date`` (DatetimeIndex, ascending)
        with columns ``[short_interest, avg_daily_volume, days_to_cover]``. Empty
        DataFrame (with the right schema) when Polygon has no data.

        First call hits the API and caches to ``{cache_dir}/{TICKER}.parquet``.
        Subsequent calls read the cache unless ``refresh=True``.
        """
        cache = self._cache_path(ticker)
        if cache.exists() and not refresh:
            return pd.read_parquet(cache)

        all_rows: list[dict] = []
        url = f"{_BASE_URL}?ticker={ticker.upper()}&limit=500&order=asc"
        while url:
            payload = self._get_with_retry(url)
            rows = payload.get("results", []) or []
            all_rows.extend(rows)
            next_url = payload.get("next_url")
            if not next_url:
                break
            # Polygon's next_url may or may not include apiKey; preserve auth via header
            url = next_url

        df = self._rows_to_frame(all_rows)
        df.to_parquet(cache)
        return df

    @staticmethod
    def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
        cols = ["short_interest", "avg_daily_volume", "days_to_cover"]
        if not rows:
            empty = pd.DataFrame(columns=cols)
            empty.index = pd.DatetimeIndex([], name="settlement_date")
            return empty
        df = pd.DataFrame(rows)
        df["settlement_date"] = pd.to_datetime(df["settlement_date"])
        df = df.set_index("settlement_date").sort_index()
        out = df[cols].copy()
        out["short_interest"] = out["short_interest"].astype("int64")
        out["avg_daily_volume"] = out["avg_daily_volume"].astype("int64")
        out["days_to_cover"] = out["days_to_cover"].astype(float)
        return out

    def _get_with_retry(self, url: str) -> dict:
        attempt = 0
        headers = {"Authorization": f"Bearer {self._api_key}"}
        while True:
            resp = self._session.get(url, timeout=30, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                raise PolygonShortInterestAuthError("Polygon returned 401: API key rejected")
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise PolygonShortInterestError(
                    f"Polygon returned {resp.status_code}: {resp.text[:200]}"
                )
            # 429 / 5xx: retry
            if attempt >= self._max_retries:
                raise PolygonShortInterestError(
                    f"Polygon {resp.status_code} persisted after {attempt} retries"
                )
            backoff = self._retry_backoff_s * (2**attempt)
            logger.warning(
                "Polygon short-interest %s; retrying in %.1fs (attempt %d/%d)",
                resp.status_code,
                backoff,
                attempt + 1,
                self._max_retries,
            )
            self._sleep(backoff)
            attempt += 1

    def features_as_of(self, ticker: str, asof: date) -> ShortInterestRecord | None:
        """Most recent record with `(settlement_date + 8 BD) <= asof`. None if none."""
        df = self.fetch_ticker(ticker)
        if df.empty:
            return None
        asof_ts = pd.Timestamp(asof)
        # Eligible == settlement + 8 BD <= asof
        # Cheap approximation: filter index <= asof - 8 BD upper-bound
        eligible_mask = np.array(
            [_is_available_at(asof=asof, settlement=ts.date()) for ts in df.index]
        )
        eligible = df[eligible_mask]
        if eligible.empty:
            return None
        last = eligible.iloc[-1]
        return ShortInterestRecord(
            settlement_date=eligible.index[-1].date(),
            ticker=ticker.upper(),
            short_interest=int(last["short_interest"]),
            avg_daily_volume=int(last["avg_daily_volume"]),
            days_to_cover=float(last["days_to_cover"]),
        )
