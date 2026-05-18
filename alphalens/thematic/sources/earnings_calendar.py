"""Forward-looking earnings-calendar lookup via yfinance.

Returns the next confirmed earnings date strictly AFTER ``asof`` (PIT
guard so an operator running on date T doesn't see a past earnings as
"next").

Per Perplexity research + zen review (2026-05-17): earnings date is a
staleness signal, NOT a forecast trigger. The Phase E prompt constraint
explicitly forbids the LLM from speculating on earnings outcomes;
``earnings_calendar.fetch_next_earnings`` simply surfaces the date so
the brief can render "next earnings YYYY-MM-DD" factually.

Defensive: any yfinance exception or unexpected payload shape → return
None (graceful degradation; brief omits the line).
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

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


def fetch_next_earnings(*, ticker: str, asof: dt.date) -> dt.date | None:
    """Return the next confirmed earnings date strictly AFTER ``asof``.

    PIT contract: when ``asof < today`` we return ``None``. ``yfinance.calendar``
    only exposes the CURRENT forward earnings schedule (re-fetched each
    call), not a historical "as-of" calendar — so for a 2020-06-15 replay
    it would happily return 2026-07-30, a 6-year look-ahead leak.

    For the live operator workflow (``asof == today``) this matches
    expectation: the next earnings is the one yfinance knows about right
    now. Historical replay needs a different data source (AV EARNINGS
    cache or SEC 8-K parsing) — until that is wired we suppress the field
    so the operator never reads a leaked future date as factual.
    """
    if asof < dt.date.today():
        return None
    try:
        import yfinance as yf

        calendar = yf.Ticker(ticker).calendar
    except Exception as exc:
        logger.warning("earnings_calendar fetch failed for %s: %s", ticker, exc)
        return None
    dates = _extract_earnings_dates(calendar)
    future = sorted(d for d in dates if d > asof)
    return future[0] if future else None


__all__ = ["fetch_next_earnings"]
