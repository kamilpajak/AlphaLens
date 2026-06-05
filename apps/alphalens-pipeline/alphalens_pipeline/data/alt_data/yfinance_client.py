"""yfinance HTTP client — single canonical entry point for every Yahoo call.

yfinance is unauthenticated (Yahoo, ToS-grey), so there is no API key / Bearer
header to centralise — but the canonical-client doctrine still applies: a
single shared throttle + retry seam keeps a 429 burst from draining Yahoo's
implicit per-IP rate budget for the whole daily thematic pipeline. Without it
each consumer (the OHLCV loader, the earnings-calendar lookup, the mcap filter
in PR-2) fires its own uncoordinated ``yfinance.Ticker(...).history`` and a
busy run trips Yahoo's rate limiter, silently emptying the brief.

Mirrors :mod:`alphalens_pipeline.data.alt_data.polygon_client` structurally:
flat public methods, instance-level throttle + bounded retry, module-level lazy
singleton via :func:`get_default_yfinance_client`, ``_reset_default_client_for_tests``
hook. Differences from Polygon:

* No Bearer auth (Yahoo is unauthenticated) and no ``requests.Session`` — calls
  go through ``yfinance.Ticker`` which owns its own HTTP session.
* yfinance has no canonical ``Retry-After`` header. Rate limits surface either
  as :class:`yfinance.exceptions.YFRateLimitError` or as a generic exception
  whose message contains ``429`` / ``Too Many Requests``. Classification is
  string-based, mirroring the ``run_probes`` transient/permanent contract
  (``tests/live/__init__.py``).
* NEVER crashes the batch — a final unrecoverable fetch returns an empty
  DataFrame / ``None``, matching the swallow-to-empty behaviour the consumers
  rely on today.

The OHLCV path additionally owns the disk cache that used to live in
``thematic.screening.scorer``: ``~/.alphalens/thematic_ohlcv/{TICKER}_{asof}.parquet``.
On a rate-limited / empty live fetch :meth:`cached_daily_ohlcv` falls back to the
newest existing ``{TICKER}_*.parquet`` so a Yahoo outage computes technicals off
a slightly-stale ~251-row history rather than returning nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
from yfinance.exceptions import YFRateLimitError

logger = logging.getLogger(__name__)

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_OHLCV_LOOKBACK_DAYS = 400  # ~251 trading rows — enough for MA200 + vol windows
_THEMATIC_OHLCV_CACHE = Path.home() / ".alphalens" / "thematic_ohlcv"

# A transient failure (rate limit / network blip) whose exception message
# matches one of these substrings (case-insensitive) is retried; anything
# else is permanent (404 / delisted / shape) and is NOT retried. Mirrors the
# ``run_probes`` classifier in ``tests/live/__init__.py``.
_TRANSIENT_MARKERS = ("429", "too many requests", "timeout", "timed out", "connection")


class YFinanceError(RuntimeError):
    """Non-transient yfinance failure (delist / 404 / shape / exhausted retries).

    Mirrors :class:`PolygonError`. The public methods NEVER raise this to the
    caller — they collapse to an empty DataFrame / ``None`` so a single bad
    ticker can't crash the daily batch — but it is defined so internal helpers
    have a typed signal and future strict consumers can opt in.
    """


def _is_transient(exc: Exception) -> bool:
    """Classify a yfinance exception as transient (retry) vs permanent.

    ``YFRateLimitError`` is always transient. Otherwise fall back to a
    substring match on the message (yfinance raises plain exceptions for many
    rate-limit / network cases), identical to the live-probe classifier.
    """
    if isinstance(exc, YFRateLimitError):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


class YFinanceClient:
    """Canonical client for Yahoo data via yfinance.

    Single shared throttle + retry across every consumer (thematic OHLCV
    loader, earnings-calendar lookup, and — in PR-2 — the mcap filter). The
    implicit Yahoo rate limit is respected by spacing requests, not just by
    reacting to 429s; the retry layer is the safety net for bursts that slip
    through (e.g. when scoring overlaps the earnings lookup).
    """

    _MAX_REQUEST_ATTEMPTS = 3  # 1 + 2 retries (lighter than Polygon, like SEC)
    _RATE_LIMIT_BACKOFFS = (5, 15)  # conservative; Yahoo has no Retry-After header

    def __init__(
        self,
        *,
        min_interval_s: float = 1.5,
        cache_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._min_interval_s = max(min_interval_s, 0.0)
        self._cache_dir = cache_dir or _THEMATIC_OHLCV_CACHE
        self._sleep = sleep
        self._last_call_ts: float = 0.0
        # Per-process OHLCV memo so a batch that scores the same ticker twice
        # (candidate + peer cohort) doesn't refetch within one run. Keyed by
        # (ticker, asof): a different asof has a different lookback window, so it
        # must NOT reuse an earlier asof's (possibly truncated) frame.
        self._ohlcv_memo: dict[tuple[str, dt.date], pd.DataFrame] = {}

    # ----- public flat methods -----

    def daily_ohlcv(self, ticker: str, *, start: dt.date, end: dt.date) -> pd.DataFrame:
        """Daily OHLCV bars for ``ticker`` over ``[start, end)``.

        Wraps ``yfinance.Ticker(T).history(start=, end=, auto_adjust=False)``
        (``auto_adjust=False`` matches the legacy scorer fetch — raw prices,
        split/div adjustments applied at analysis time). Returns a frame with
        lowercase ``[open, high, low, close, volume]`` columns and a tz-naive
        ``DatetimeIndex``.

        Throttled + retried on transient errors. A permanent failure (delist /
        404) or exhausted retries returns an EMPTY DataFrame — never raises —
        so a single bad ticker can't crash the batch.
        """
        upper = ticker.upper()

        def _fetch() -> pd.DataFrame:
            import yfinance as yf

            return yf.Ticker(upper).history(start=start, end=end, auto_adjust=False)

        raw = self._call_with_retry(_fetch, what=f"history({upper})", default=pd.DataFrame())
        if raw is None or raw.empty:
            return pd.DataFrame()
        return _normalize_ohlcv(raw)

    def next_earnings(self, ticker: str) -> dict | pd.DataFrame | None:
        """The raw ``yfinance.Ticker(T).calendar`` payload (dict or DataFrame).

        Returns ``None`` on a permanent failure or exhausted retries. The
        caller owns the date-extraction + PIT guard (the calendar shape is
        version-dependent — dict in newer yfinance, DataFrame in older).
        """
        upper = ticker.upper()

        def _fetch() -> dict | pd.DataFrame | None:
            import yfinance as yf

            return yf.Ticker(upper).calendar

        return self._call_with_retry(_fetch, what=f"calendar({upper})", default=None)

    def market_cap(self, ticker: str) -> float | None:
        """Live market cap via ``fast_info.market_cap``; ``None`` on failure.

        Throttled + retried like every Yahoo call. (FastInfo dict-style
        ``.get("market_cap")`` returns ``None`` — attribute access is the
        contract.) The persistent ≤14d cache fallback lives in the mcap filter,
        which composes this method.
        """
        upper = ticker.upper()

        def _fetch() -> float | None:
            import yfinance as yf

            mc = yf.Ticker(upper).fast_info.market_cap
            return float(mc) if mc is not None else None

        return self._call_with_retry(_fetch, what=f"market_cap({upper})", default=None)

    _SHARES_STALE_WARN_DAYS = 90  # warn if the PIT shares match predates asof by this much

    def shares(self, ticker: str, *, asof: dt.date | None = None) -> float | None:
        """Shares outstanding; ``None`` on failure.

        PIT (``asof`` given): the latest ``get_shares_full`` value on or before
        ``asof`` (the only dated yfinance shares series), falling back to the
        ``fast_info.shares`` snapshot. ``asof=None`` → the live snapshot.

        When ``asof`` is given, a forward-biased result (a ``get_shares_full``
        match more than ``_SHARES_STALE_WARN_DAYS`` before ``asof``, or the
        ``fast_info.shares`` snapshot fallback — today's count) logs a warning so
        the staleness is visible to a PIT analysis rather than silent.
        """
        upper = ticker.upper()

        def _fetch() -> float | None:
            import yfinance as yf

            tk = yf.Ticker(upper)
            if asof is not None:
                asof_ts = pd.Timestamp(asof)
                series = tk.get_shares_full(
                    start=(asof_ts - pd.Timedelta(days=400)).date().isoformat(),
                    end=(asof_ts + pd.Timedelta(days=1)).date().isoformat(),
                )
                if series is not None and len(series) > 0:
                    # tz_localize(None) on an already-naive index raises; build a
                    # UTC-aware index first so this is idempotent across yfinance
                    # versions (today the index is UTC-aware; a future naive index
                    # would otherwise silently kill the whole PIT shares path).
                    series.index = pd.to_datetime(series.index, utc=True).tz_localize(None)
                    pit = series[series.index <= asof_ts]
                    if not pit.empty:
                        match_ts = pit.index[-1]
                        if (asof_ts - match_ts).days > self._SHARES_STALE_WARN_DAYS:
                            logger.warning(
                                "PIT shares for %s@%s are stale: nearest get_shares_full "
                                "point is %s (%d days before asof)",
                                upper,
                                asof.isoformat(),
                                match_ts.date().isoformat(),
                                (asof_ts - match_ts).days,
                            )
                        return float(pit.iloc[-1])
                logger.warning(
                    "PIT shares for %s@%s: no get_shares_full point on or before asof; "
                    "falling back to today's fast_info.shares snapshot (forward-biased)",
                    upper,
                    asof.isoformat(),
                )
            snapshot = getattr(tk.fast_info, "shares", None)
            return float(snapshot) if snapshot else None

        return self._call_with_retry(_fetch, what=f"shares({upper})", default=None)

    def cached_daily_ohlcv(self, ticker: str, *, asof: dt.date) -> pd.DataFrame:
        """OHLCV with disk + in-process cache and a stale-fallback safety net.

        Disk cache lives at ``{cache_dir}/{TICKER}_{asof}.parquet``. The asof in
        the filename prevents silent reuse of a parquet whose tail predates the
        new evaluation date (zen review 2026-05-17 HIGH finding). Resolution
        order:

        1. in-process memo (one entry per ticker per run),
        2. exact ``{TICKER}_{asof}.parquet`` on disk,
        3. a live :meth:`daily_ohlcv` fetch (~400d lookback), persisted on
           success,
        4. on a rate-limited / empty live fetch, the NEWEST existing
           ``{TICKER}_*.parquet`` (slightly stale, still computes valid
           technicals) — better than an empty frame that drops the candidate.

        The returned frame is sliced to ``index <= asof``. An empty frame is
        returned only when every path above misses.
        """
        upper = ticker.upper()
        key = (upper, asof)
        if key in self._ohlcv_memo:
            return _slice_to_asof(self._ohlcv_memo[key], asof)

        exact = self._cache_dir / f"{upper}_{asof.isoformat()}.parquet"
        # A corrupt/unreadable exact parquet reads as empty (_read_ohlcv_cache
        # swallows), so an empty result here — whether the file is absent or
        # corrupt — falls through to the live fetch below (a successful live
        # fetch overwrites the corrupt file). This avoids a corrupt file
        # permanently pinning empty OHLCV for that (ticker, asof).
        df = _read_ohlcv_cache(exact) if exact.exists() else pd.DataFrame()
        if df.empty:
            start = asof - dt.timedelta(days=_OHLCV_LOOKBACK_DAYS)
            end = asof + dt.timedelta(days=1)
            df = self.daily_ohlcv(upper, start=start, end=end)
            if not df.empty:
                self._write_ohlcv_cache(exact, df)
            else:
                stale = self._newest_cached_parquet(upper)
                if stale is not None:
                    logger.info(
                        "yfinance live fetch empty for %s@%s; using stale cache %s",
                        upper,
                        asof.isoformat(),
                        stale.name,
                    )
                    df = _read_ohlcv_cache(stale)

        self._ohlcv_memo[key] = df
        return _slice_to_asof(df, asof)

    # ----- internals -----

    def _newest_cached_parquet(self, upper: str) -> Path | None:
        """Newest ``{upper}_*.parquet`` in the cache dir, by the asof in the
        filename (lexicographic on the ISO date suffix == chronological)."""
        candidates = sorted(self._cache_dir.glob(f"{upper}_*.parquet"))
        return candidates[-1] if candidates else None

    def _write_ohlcv_cache(self, path: Path, df: pd.DataFrame) -> None:
        """Persist OHLCV to the disk cache. NEVER raises — a disk / permission
        error must not crash the batch (the fetch already succeeded)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path)
        except Exception as exc:  # best-effort cache; a disk error is never fatal
            logger.warning("ohlcv cache write failed for %s: %s", path.name, exc)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._min_interval_s:
            self._sleep(self._min_interval_s - elapsed)
        self._last_call_ts = time.monotonic()

    def _call_with_retry(self, fn: Callable[[], Any], *, what: str, default: Any) -> Any:
        """Throttle + bounded retry around a single yfinance call.

        Transient failures (rate limit / network) retry with exponential
        backoff up to ``_MAX_REQUEST_ATTEMPTS``; permanent failures (delist /
        404 / shape) return ``default`` immediately. Exhausted retries also
        return ``default`` — this method NEVER raises so the batch survives a
        persistent Yahoo outage.
        """
        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            self._throttle()
            try:
                return fn()
            except Exception as exc:  # collapse to default, never crash the batch
                if not _is_transient(exc):
                    logger.warning("yfinance %s failed (permanent): %s", what, exc)
                    return default
                if attempt == self._MAX_REQUEST_ATTEMPTS - 1:
                    logger.warning(
                        "yfinance %s rate-limited after %d attempts; giving up",
                        what,
                        self._MAX_REQUEST_ATTEMPTS,
                    )
                    return default
                backoff = self._RATE_LIMIT_BACKOFFS[
                    min(attempt, len(self._RATE_LIMIT_BACKOFFS) - 1)
                ]
                logger.warning(
                    "yfinance %s transient error (attempt %d/%d): %s; sleeping %ds",
                    what,
                    attempt + 1,
                    self._MAX_REQUEST_ATTEMPTS,
                    exc,
                    backoff,
                )
                self._sleep(backoff)
        return default


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase OHLCV columns, drop extras, strip the index timezone.

    Mirrors the legacy ``scorer._fetch_ohlcv_via_yfinance`` normalisation:
    lowercase columns, select the canonical OHLCV set, force a tz-naive index
    so downstream date-level comparisons stay simple.
    """
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if isinstance(out.index, pd.DatetimeIndex) and out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    return out[list(_OHLCV_COLUMNS)]


def _slice_to_asof(df: pd.DataFrame, asof: dt.date) -> pd.DataFrame:
    if df.empty:
        return df
    return df[df.index <= pd.Timestamp(asof)]


def _read_ohlcv_cache(path: Path) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # a corrupt parquet is a miss, not a crash
        logger.warning("ohlcv cache read failed for %s: %s", path.name, exc)
        return pd.DataFrame()


# Module-level lazy singleton — one YFinanceClient shared by every caller, so
# the throttle budget is process-wide (the implicit Yahoo fair-access we want).
# Tests reset via _reset_default_client_for_tests().
_DEFAULT_CLIENT: YFinanceClient | None = None
_DEFAULT_CLIENT_LOCK = threading.Lock()  # double-checked locking guard


def get_default_yfinance_client() -> YFinanceClient:
    """Return the process-wide default YFinanceClient (lazy-initialized).

    Subsequent calls return the same instance so the rate-limit throttle is
    shared across every caller. Construction is thread-safe via double-checked
    locking, matching :func:`get_default_polygon_client`.
    """
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        with _DEFAULT_CLIENT_LOCK:
            if _DEFAULT_CLIENT is None:
                _DEFAULT_CLIENT = YFinanceClient()
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None


__all__ = [
    "YFinanceClient",
    "YFinanceError",
    "_reset_default_client_for_tests",
    "get_default_yfinance_client",
]
