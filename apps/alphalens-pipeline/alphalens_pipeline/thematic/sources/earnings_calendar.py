"""Forward-looking earnings-calendar lookup via yfinance.

Returns the next confirmed earnings date strictly AFTER ``asof`` (PIT
guard so an operator running on date T doesn't see a past earnings as
"next").

Per Perplexity research + zen review (2026-05-17): earnings date is a
staleness signal, NOT a forecast trigger. The Phase E prompt constraint
explicitly forbids the LLM from speculating on earnings outcomes;
``earnings_calendar.fetch_next_earnings`` simply surfaces the date so
the brief can render "next earnings YYYY-MM-DD" factually.

Defensive: any yfinance failure (handled by the canonical
``YFinanceClient`` — throttle + retry, then swallow to None) or unexpected
payload shape → return None (graceful degradation; brief omits the line).
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client

logger = logging.getLogger(__name__)

_EARNINGS_DATE_KEY = "Earnings Date"


def _extract_earnings_dates(calendar) -> list[dt.date]:
    """Coerce yfinance.calendar (dict or DataFrame) to a list of date objects."""
    if calendar is None:
        return []
    raw_values: list = []
    if isinstance(calendar, dict):
        raw_values = calendar.get(_EARNINGS_DATE_KEY, []) or []
    elif isinstance(calendar, pd.DataFrame):
        if _EARNINGS_DATE_KEY in calendar.columns:
            raw_values = list(calendar[_EARNINGS_DATE_KEY])
        elif _EARNINGS_DATE_KEY in calendar.index:
            raw_values = list(calendar.loc[_EARNINGS_DATE_KEY])
    else:
        return []
    out: list[dt.date] = []
    for v in raw_values:
        if isinstance(v, dt.datetime):
            out.append(v.date())
        elif isinstance(v, dt.date):
            out.append(v)
        elif isinstance(v, pd.Timestamp):
            out.append(v.date())
    return out


_FRESHNESS_WINDOW = dt.timedelta(days=7)


def fetch_next_earnings(
    *, ticker: str, asof: dt.date, today: dt.date | None = None
) -> dt.date | None:
    """Return the next confirmed earnings date strictly AFTER ``asof``.

    Freshness-window contract. ``yfinance.calendar`` only exposes the CURRENT
    forward earnings schedule (re-fetched each call), not a historical "as-of"
    calendar — so for a 2020-06-15 replay it would happily return 2026-07-30,
    a 6-year look-ahead leak.

    But the daily pipeline always runs at ``asof == today - 1`` (T-1), which
    is the live/recent operator workflow, NOT a historical replay. The next
    earnings date is always FORWARD (e.g. 2026-08-05), so surfacing it for a
    recent ``asof`` carries no meaningful leak. We therefore surface the date
    when ``asof`` is within ``_FRESHNESS_WINDOW`` (7 days) of ``today`` — wide
    enough to absorb weekend/holiday lag (T-2/T-3) — and suppress ONLY when
    ``asof`` is genuinely far in the past (a real historical replay), where
    yfinance's forward-only calendar would leak. Historical replay needs a
    different data source (AV EARNINGS cache or SEC 8-K parsing); until that
    is wired we suppress the field there so the operator never reads a leaked
    future date as factual.

    ``today`` is injectable for testing — production callers omit it and the
    real clock is used. The default is the **UTC** date, matching the pipeline's
    own ``asof = now(UTC).date() - 1`` so the window is computed in one timezone
    (a local-clock default could drift a day from a UTC asof; harmless inside a
    7-day window but inconsistent).

    Note: if the daily pipeline ever lags more than 7 days behind real time
    (long backfill / queue stall), widen ``_FRESHNESS_WINDOW``.
    """
    today = today or dt.datetime.now(dt.UTC).date()
    if today - asof > _FRESHNESS_WINDOW:
        return None
    # The canonical client owns throttle + retry; it swallows permanent /
    # exhausted failures to None (graceful degradation — the brief omits the
    # earnings line) just like the legacy raw call did.
    calendar = get_default_yfinance_client().next_earnings(ticker)
    dates = _extract_earnings_dates(calendar)
    future = sorted(d for d in dates if d > asof)
    return future[0] if future else None


__all__ = ["fetch_next_earnings"]
