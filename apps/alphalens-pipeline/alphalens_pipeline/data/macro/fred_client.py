"""FRED (St. Louis Fed) HTTP client with disk cache and 5xx retry.

Used for the yield curve (DGS10, DGS2), VIX (VIXCLS), and other macro series.
Free tier provides 120 req/min, so a refetch is effectively free.

CACHE LIFETIME (#1524). The cache used to have NONE: a series was written once
and returned forever. `market_state` consequently stamped a 2026-07-01 VIX print
onto 79 daily brief rows, because its PIT truncation `vix[vix.index <= asof]` is
a no-op when the cached series ENDS BEFORE asof, and nothing downstream could
tell that apart from a quiet market.

Callers now opt in to freshness with `through=<date>`, and the check has TWO
thresholds because the two jobs pull opposite ways:

* REFETCH when the cached series does not reach one session before `through`.
  Tight, because a refetch costs a request nobody is counting, and because this
  is what stops a cache from ever freezing again.
* FAIL (`FREDStaleError`) only when, AFTER refetching, the series still does not
  reach `_MAX_LAG_SESSIONS` before `through`. Loose, because at that point FRED
  itself is behind and the caller's usual response is to degrade a display.

One session of lag is the NORMAL case, not an anomaly: FRED publishes a
session's value the next business morning (~13:40 UTC), and the pipeline runs at
00:30 / 04:30 / 08:30 UTC, all before that.

`through=None` keeps the original behaviour exactly, which is what a research
script wanting a frozen, reproducible series relies on.

POINT-IN-TIME (still latent, do not assume it away). This hits the plain FRED
endpoint, not ALFRED, so a refetch returns the CURRENT vintage of every past
observation. Verified 2026-09-24 for VIXCLS only: the 2026-07-01..07-10 window
read under the 2026-09-01 and 2026-09-24 vintages is byte-identical, so its
3939 vintages are one per new observation rather than per revision. That is a
fact about VIXCLS, NOT a property of this client. Any other series must be
checked before a caller relies on a refetch preserving history.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

#: Sessions of lag tolerated before a cached series is REFETCHED. One, because
#: one is the normal publication lag and anything older means the cache is
#: drifting. A refetch is nearly free, so this end is deliberately tight.
_REFETCH_LAG = 1

#: Sessions of lag tolerated before a refetched series is REFUSED. Five, matching
#: ``data/rs_history.py::LISTING_STATUS_MAX_LAG_SESSIONS``, which does the same
#: job for the grouped-daily store. With the tight refetch above in front of it,
#: this slack cannot hide a frozen cache; it only absorbs a real FRED outage.
_MAX_LAG_SESSIONS = 5


def _last_observation(series: pd.Series) -> dt.date | None:
    return series.index[-1].date() if len(series) else None


def _reaches(series: pd.Series, through: dt.date, lag_sessions: int) -> bool:
    """Does ``series`` extend to within ``lag_sessions`` trading sessions of ``through``?

    Counted in SESSIONS, not calendar days: a weekend or a market holiday is not
    missing data, and a calendar rule would refetch every Monday and then refuse
    every long weekend. ``n_sessions_before`` rolls a non-session ``through`` back
    to the prior session first, which is what makes a Saturday ``asof`` behave.
    """
    from alphalens_pipeline.paper.calendar import n_sessions_before

    last = _last_observation(series)
    return last is not None and last >= n_sessions_before(through, lag_sessions)


class FREDError(RuntimeError):
    """Non-transient FRED failure (4xx, exhausted retries)."""


class FREDAuthError(FREDError):
    """Missing or invalid API key."""


class FREDStaleError(FREDError):
    """The series does not reach the date the caller asked for, even after a refetch.

    Distinct from a transport failure: the request succeeded and FRED simply has
    no data that recent. Callers that can degrade (a display label) should catch
    it; callers that cannot should let it surface.
    """


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

    def fetch_series(self, series_id: str, *, through: dt.date | None = None) -> pd.Series:
        """Fetch a FRED series as pd.Series[float] indexed by DatetimeIndex.

        Disk-cached; FRED sentinel "." values (missing observations) are dropped.

        ``through`` is the date the caller needs covered. Omit it and the cache is
        returned whatever its age — the original behaviour, kept for reproducible
        research reads. Supply it and the module docstring's two thresholds apply:
        a cache short of one session before ``through`` is refetched, and a series
        still short of :data:`_MAX_LAG_SESSIONS` before it raises
        :class:`FREDStaleError`.
        """
        cache = self._cache_path(series_id)
        cached = self._read_cache(cache)

        if cached is not None and (through is None or _reaches(cached, through, _REFETCH_LAG)):
            return cached

        series = self._download(series_id)
        self._write_cache(series, series_id, cache)

        if through is not None and not _reaches(series, through, _MAX_LAG_SESSIONS):
            last = _last_observation(series)
            raise FREDStaleError(
                f"FRED series {series_id} ends {last} but {through} was requested "
                f"(tolerance {_MAX_LAG_SESSIONS} sessions). The refetch did not help, "
                f"so FRED itself is behind."
            )
        return series

    @staticmethod
    def _read_cache(cache: Path) -> pd.Series | None:
        """The cached series, or None if there is not a usable one.

        A damaged file counts as a miss rather than an exception. This file is
        now rewritten by a live pipeline instead of being written once and left,
        so corruption is a state the system can reach — and because
        ``market_state.enrich`` is fail-soft, an unguarded read would not crash
        the build, it would stamp 'unknown' every day until a human noticed.
        Refetching costs one request and repairs the file on the way past.
        """
        if not cache.exists():
            return None
        try:
            return pd.read_parquet(cache).iloc[:, 0]
        except (OSError, ValueError) as exc:
            logger.warning("FRED cache %s is unreadable (%s); refetching.", cache, exc)
            return None

    def _download(self, series_id: str) -> pd.Series:
        url = f"{_BASE_URL}?series_id={series_id}&api_key={self._api_key}&file_type=json"
        return _parse_observations(self._get_with_retry(url, series_id), series_id)

    @staticmethod
    def _write_cache(series: pd.Series, series_id: str, cache: Path) -> None:
        """Persist atomically: write a sibling temp file, then rename over the target.

        A 1-column DataFrame because Parquet does not round-trip a Series cleanly.
        The rename matters now that a live pipeline overwrites a file other
        processes read: Parquet keeps its metadata at the END, so a torn write is
        unreadable rather than merely short, and an in-place write has a window
        where exactly that is on disk.

        The cache inherits ``mkstemp``'s 0600 rather than the umask default this
        file used to be created at. That is a deliberate tightening, not an
        oversight: every reader — the pipeline container, which runs as the same
        uid via ``--user``, and the Mac-side research scripts — is the owning
        user, so nothing needs group or world read. An explicit chmod back to
        0644 was written first and then removed: CodeQL flagged it as an overly
        permissive mask, and it was right, because "keep the old behaviour" here
        meant keeping an umask rather than a requirement. A future consumer
        running as another user will fail loudly on permissions instead of
        silently reading a cache it should not own.
        """
        fd, tmp_name = tempfile.mkstemp(dir=str(cache.parent), suffix=".parquet.tmp")
        os.close(fd)
        try:
            series.to_frame(name=series_id).to_parquet(tmp_name)
            os.replace(tmp_name, cache)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

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
    # Sorted, so that "the last row" and "the newest observation" are the same
    # thing by construction. Every freshness check here, and
    # `refresh_vix_cache`'s "last non-null observation", reads `.iloc[-1]`. FRED
    # returns ascending today; trusting a latent property of an upstream feed is
    # what #1524 was about, and an out-of-order response would otherwise look
    # like a stale series and pick the wrong value.
    return pd.Series(
        values, index=pd.DatetimeIndex(dates, name="date"), name=series_id
    ).sort_index()
