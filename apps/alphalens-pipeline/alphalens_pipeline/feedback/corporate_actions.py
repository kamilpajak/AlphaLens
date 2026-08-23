"""Corporate-actions lookup behind the implausible-move guard (#1090).

The population monitor's 0.60 forward-return threshold
(:data:`~alphalens_pipeline.feedback.bar_window.IMPLAUSIBLE_RETURN_THRESHOLD`)
used to be a VERDICT: any bigger move was presumed an unadjusted split and the
row was silently carried forever. Measured against Polygon's reference data,
2 of the 3 rejections in 21 production days were REAL moves (MRNA +142%, CRSR
+61.6%) and only one (MQ, 4:1 reverse split executed 2026-07-01) was the
artifact the guard exists for.

This module demotes the threshold to a TRIGGER and implements the Amendment-1
disposition tree of ``docs/research/implausible_guard_redesign_2026_08_23.md``:

```
|forward_return| > 0.60
  └─ corporate-actions lookup (splits + dividends, window −3d/+1d)
       ├─ action found   → SPLIT_INVALIDATED (terminal)
       ├─ lookup failed  → carry, counted (disposition=lookup_failed)
       └─ none found     → INDEPENDENT-VENDOR cross-check:
            window return recomputed from yfinance ADJUSTED daily closes
              ├─ agrees within 10pp → accept (disposition=extreme_validated)
              ├─ disagrees          → carry, counted (disposition=data_quality)
              └─ no data            → carry, counted (disposition=data_quality)
```

Vendors are reached ONLY through the canonical clients (one-client-per-vendor
doctrine): Polygon ``/v3/reference/{splits,dividends}`` via
:class:`~alphalens_pipeline.data.alt_data.polygon_client.PolygonClient` and the
adjusted closes via
:class:`~alphalens_pipeline.data.alt_data.yfinance_client.YFinanceClient`.
Lookups are rare (3 trips in 21 days across the population) and cached on disk:
a FOUND action is immutable (cached forever); a NONE-FOUND answer expires after
:data:`NONE_FOUND_CACHE_TTL_DAYS` because corporate-action records are
corrected and appended late.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Provenance stamp for every row the trigger touched (Amendment 2). The memo
# date, NOT ``ladder_config_version`` — that is a pooling key and the guard
# never alters a pooled value.
GUARD_CONFIG_VERSION = "2026-08-23"

# The terminal classification for a window that crosses a real corporate action
# (the "MQ class"): the ladder levels were set on pre-action prices, so the
# replay can never resolve meaningfully. Terminal stops the nightly fetch
# spend; ``realized_r`` stays null so R aggregates exclude it (the NO_FILL
# convention).
SPLIT_INVALIDATED_CLASSIFICATION = "SPLIT_INVALIDATED"

# Guard dispositions (Amendment 1). Stamped into the ``guard_disposition``
# store column and countable per sweep.
DISPOSITION_SPLIT_INVALIDATED = "split_invalidated"
DISPOSITION_LOOKUP_FAILED = "lookup_failed"
DISPOSITION_EXTREME_VALIDATED = "extreme_validated"
DISPOSITION_DATA_QUALITY = "data_quality"
GUARD_DISPOSITIONS = (
    DISPOSITION_SPLIT_INVALIDATED,
    DISPOSITION_LOOKUP_FAILED,
    DISPOSITION_EXTREME_VALIDATED,
    DISPOSITION_DATA_QUALITY,
)

# A cash dividend counts as a corporate action ONLY above this fraction of the
# PRE-EX-DATE close (Amendment 3: the denominator is the pre-ex close from our
# own raw grouped-daily store, never the trade's entry anchor). Ordinary
# quarterly dividends never come near it — and never trip the 0.60 trigger in
# the first place.
SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION = 0.10

# The independent-vendor cross-check accepts the raw-bar return when the
# yfinance ADJUSTED window return agrees within this many percentage points
# (0.10 = 10pp). Different vendor, different basis (arrival VWAP vs arrival
# close) — the band absorbs the basis gap while still separating a real +142%
# from a flat adjusted series.
CROSS_CHECK_AGREEMENT_PP = 0.10

# NONE-FOUND cache TTL: corporate-action records are corrected and appended
# late, so an absence-of-record answer is only trusted for two weeks. A FOUND
# action is immutable once executed and cached forever.
NONE_FOUND_CACHE_TTL_DAYS = 14

# Action-lookup window padding around [arrival_session, horizon_session] in
# CALENDAR days: −3 before arrival (an action executed just before the window
# still poisons the raw bars inside it), +1 after the horizon (record-date
# skew).
ACTION_WINDOW_PRE_CALENDAR_DAYS = 3
ACTION_WINDOW_POST_CALENDAR_DAYS = 1


class CorporateActionsLookupError(RuntimeError):
    """The corporate-actions source of record could not answer (fail-closed)."""


# (ticker, ex_date) -> the raw close of the session BEFORE ex_date, or None
# when unavailable. Wired by the monitor to its own raw (adjusted=false)
# grouped-daily store.
PreExCloseFn = Callable[[str, dt.date], float | None]

# (ticker, start, end) -> tz-naive adjusted-daily-closes Series over
# [start, end), or None on a permanent fetch failure. Default is the canonical
# yfinance client's ``adjusted_daily_closes``.
AdjustedClosesFetch = Callable[[str, dt.date, dt.date], "pd.Series | None"]


@dataclass(frozen=True)
class CorporateActionsAnswer:
    """The lookup's verdict for one (ticker, window): action found or not."""

    found: bool
    detail: str | None = None


class PolygonCorporateActionsLookup:
    """Splits + material-special-dividends lookup via the canonical PolygonClient.

    ``pre_ex_close`` supplies the dividend-materiality denominator (the raw
    close of the session before the ex-date). Raises
    :class:`CorporateActionsLookupError` on any vendor failure OR when a
    dividend's materiality cannot be assessed — fail-closed, the caller carries
    the prior row and retries next night.
    """

    def __init__(self, *, pre_ex_close: PreExCloseFn, client: Any | None = None) -> None:
        self._pre_ex_close = pre_ex_close
        self._client = client

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

        return get_default_polygon_client()

    def lookup(self, ticker: str, start: dt.date, end: dt.date) -> CorporateActionsAnswer:
        upper = ticker.upper()
        try:
            client = self._resolve_client()
            splits = client.get_splits(
                ticker=upper, execution_date_gte=start, execution_date_lte=end
            )
        except Exception as exc:
            raise CorporateActionsLookupError(f"splits lookup failed for {upper}: {exc}") from exc
        if splits:
            first = splits[0]
            detail = (
                f"split {first.get('split_from')}:{first.get('split_to')} "
                f"executed {first.get('execution_date')}"
            )
            return CorporateActionsAnswer(found=True, detail=detail)
        try:
            dividends = client.get_dividends(
                ticker=upper, ex_dividend_date_gte=start, ex_dividend_date_lte=end
            )
        except Exception as exc:
            raise CorporateActionsLookupError(
                f"dividends lookup failed for {upper}: {exc}"
            ) from exc
        material = self._first_material_dividend(upper, dividends)
        if material is not None:
            return CorporateActionsAnswer(found=True, detail=material)
        return CorporateActionsAnswer(found=False)

    def _first_material_dividend(self, upper: str, dividends: list[dict]) -> str | None:
        """Detail string for the first dividend above the materiality floor, else None.

        Materiality = ``cash_amount > SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION ×
        pre-ex-date close`` (Polygon's ``cash_amount`` is as-declared /
        unadjusted, the consistent pair with the raw grouped close). A missing
        pre-ex close raises — the floor cannot be applied, so fail closed.
        """
        for record in dividends:
            cash = _finite_positive(record.get("cash_amount"))
            if cash is None:
                continue
            ex_date = _parse_iso_date(record.get("ex_dividend_date"))
            if ex_date is None:
                raise CorporateActionsLookupError(
                    f"dividend for {upper} carries no parseable ex_dividend_date"
                )
            close = _finite_positive(self._pre_ex_close(upper, ex_date))
            if close is None:
                raise CorporateActionsLookupError(
                    f"no pre-ex-date close for {upper}@{ex_date.isoformat()} — "
                    "cannot assess dividend materiality"
                )
            if cash / close > SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION:
                return (
                    f"special dividend {cash:.4f}/share ex {ex_date.isoformat()} "
                    f"({cash / close:.1%} of pre-ex close {close:.2f})"
                )
        return None


class CachedCorporateActionsLookup:
    """Disk-cached wrapper: FOUND forever, NONE-FOUND for 14 days, errors never.

    JSON file keyed by ``TICKER:start:end`` (atomic tmp + ``os.replace`` write,
    same idiom as the monitor's parquet stores). The cache path is injectable
    so tests never touch ``~/.alphalens``.
    """

    def __init__(
        self,
        inner: Any,
        cache_path: Path,
        *,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._inner = inner
        self._cache_path = Path(cache_path)
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    def lookup(self, ticker: str, start: dt.date, end: dt.date) -> CorporateActionsAnswer:
        key = f"{ticker.upper()}:{start.isoformat()}:{end.isoformat()}"
        cache = self._load()
        entry = cache.get(key)
        cached = self._answer_from_entry(entry)
        if cached is not None:
            return cached
        answer = self._inner.lookup(ticker, start, end)
        cache[key] = {
            "found": bool(answer.found),
            "detail": answer.detail,
            "checked_at": self._now().isoformat(),
        }
        self._store(cache)
        return answer

    def _answer_from_entry(self, entry: dict | None) -> CorporateActionsAnswer | None:
        """A still-valid cached answer, or None (miss / expired / malformed)."""
        if not isinstance(entry, dict) or "found" not in entry:
            return None
        found = bool(entry["found"])
        if found:  # immutable once executed — never expires
            return CorporateActionsAnswer(found=True, detail=entry.get("detail"))
        checked_at = _parse_iso_datetime(entry.get("checked_at"))
        if checked_at is None:
            return None
        if self._now() - checked_at > dt.timedelta(days=NONE_FOUND_CACHE_TTL_DAYS):
            return None
        return CorporateActionsAnswer(found=False, detail=entry.get("detail"))

    def _load(self) -> dict[str, dict]:
        if not self._cache_path.exists():
            return {}
        try:
            payload = json.loads(self._cache_path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("corporate-actions cache unreadable (%s); rebuilding.", exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _store(self, cache: dict[str, dict]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(cache, indent=0, sort_keys=True))
            os.replace(tmp, self._cache_path)
        except OSError as exc:  # best-effort cache; a disk error is never fatal
            logger.warning("corporate-actions cache write failed: %s", exc)


def adjusted_window_return(closes: pd.Series | None, start: dt.date, end: dt.date) -> float | None:
    """Window return over ``[start, end]`` from an adjusted-daily-closes Series.

    First in-window close → last in-window close. ``None`` when the series is
    missing / has fewer than two in-window points (delisted / renamed / halted
    — the "no coherent series" arm of the cross-check).
    """
    if closes is None or len(closes) == 0:
        return None
    try:
        dates = pd.to_datetime(closes.index)
    except (TypeError, ValueError):
        return None
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    window = closes[mask]
    if len(window) < 2:
        return None
    first = float(window.iloc[0])
    last = float(window.iloc[-1])
    if not (math.isfinite(first) and math.isfinite(last)) or first <= 0:
        return None
    return last / first - 1.0


def resolve_guard_disposition(
    *,
    ticker: str,
    forward_return: float,
    arrival_session: dt.date,
    horizon_session: dt.date,
    lookup: Any,
    adjusted_closes: AdjustedClosesFetch,
) -> str:
    """Amendment-1 disposition tree for one implausible-move trigger.

    ``lookup`` is any object with ``.lookup(ticker, start, end) ->
    CorporateActionsAnswer`` (raising = the lookup_failed arm);
    ``adjusted_closes`` is the independent-vendor closes fetch. Returns one of
    :data:`GUARD_DISPOSITIONS`.
    """
    action_start = arrival_session - dt.timedelta(days=ACTION_WINDOW_PRE_CALENDAR_DAYS)
    action_end = horizon_session + dt.timedelta(days=ACTION_WINDOW_POST_CALENDAR_DAYS)
    try:
        answer = lookup.lookup(ticker, action_start, action_end)
    except Exception as exc:
        logger.warning(
            "population-monitor guard: corporate-actions lookup failed for %s — %s.", ticker, exc
        )
        return DISPOSITION_LOOKUP_FAILED
    if answer.found:
        logger.info(
            "population-monitor guard: corporate action for %s in window — %s.",
            ticker,
            answer.detail,
        )
        return DISPOSITION_SPLIT_INVALIDATED
    try:
        # [start, end) fetch contract (mirrors daily_ohlcv): +1 day to include
        # the horizon session itself; the return is then taken over the closed
        # [arrival, horizon] window.
        closes = adjusted_closes(ticker, arrival_session, horizon_session + dt.timedelta(days=1))
    except Exception as exc:  # a broken fetch is "no data", not a crash
        logger.warning(
            "population-monitor guard: adjusted-closes fetch failed for %s — %s.", ticker, exc
        )
        closes = None
    cross = adjusted_window_return(closes, arrival_session, horizon_session)
    if cross is None:
        return DISPOSITION_DATA_QUALITY
    if abs(forward_return - cross) <= CROSS_CHECK_AGREEMENT_PP:
        return DISPOSITION_EXTREME_VALIDATED
    return DISPOSITION_DATA_QUALITY


def default_adjusted_closes_fetch(ticker: str, start: dt.date, end: dt.date) -> pd.Series | None:
    """Production cross-check source: the canonical yfinance client (lazy)."""
    from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client

    return get_default_yfinance_client().adjusted_daily_closes(ticker, start=start, end=end)


def _finite_positive(value: Any) -> float | None:
    """Coerce to a finite positive float, else None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _parse_iso_date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_iso_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


__all__ = [
    "ACTION_WINDOW_POST_CALENDAR_DAYS",
    "ACTION_WINDOW_PRE_CALENDAR_DAYS",
    "CROSS_CHECK_AGREEMENT_PP",
    "DISPOSITION_DATA_QUALITY",
    "DISPOSITION_EXTREME_VALIDATED",
    "DISPOSITION_LOOKUP_FAILED",
    "DISPOSITION_SPLIT_INVALIDATED",
    "GUARD_CONFIG_VERSION",
    "GUARD_DISPOSITIONS",
    "NONE_FOUND_CACHE_TTL_DAYS",
    "SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION",
    "SPLIT_INVALIDATED_CLASSIFICATION",
    "AdjustedClosesFetch",
    "CachedCorporateActionsLookup",
    "CorporateActionsAnswer",
    "CorporateActionsLookupError",
    "PolygonCorporateActionsLookup",
    "PreExCloseFn",
    "adjusted_window_return",
    "default_adjusted_closes_fetch",
    "resolve_guard_disposition",
]
