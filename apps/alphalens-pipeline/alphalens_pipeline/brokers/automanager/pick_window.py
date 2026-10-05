"""The one validity window of a pick (#1734).

An armed pick has ``spec.order_ttl_days`` sessions of its venue, counted from
``meta.trade_date``, to do its work. The window ends at the close of the last
of those sessions. One rule, used in three places:

* the drain expires an armed pick that is still unplaced when its window ends,
  whatever kept it unplaced (no capital, the day-1 gate, a dead feed);
* an entry-trail watch carries the window's end as its ``window_end``;
* a resting entry limit gets a GTD date of the window's last session, so a pick
  placed late gets only what is LEFT of its window, never a fresh one.

``order_ttl_days == 0`` is the document's "field absent" value and means
:data:`~broker_contract.constants.DEFAULT_ORDER_TTL_DAYS`. Every failure is a
:class:`PickWindowError` rather than a calendar exception: the drain computes
this for every armed pick on every tick, so an exception escaping it would end
the tick before the protection pass ran.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from broker_contract.constants import DEFAULT_ORDER_TTL_DAYS, MAX_ORDER_TTL_DAYS

from alphalens_pipeline.market.calendar import (
    advance_trading_sessions,
    session_close_utc,
    session_on_or_after,
    trading_days_elapsed,
)


class PickWindowError(ValueError):
    """The window of a pick cannot be computed from its document."""


@dataclass(frozen=True)
class PickWindow:
    """``last_session`` is the final session a placed entry may rest through;
    ``window_end`` is that session's close, timezone-aware UTC."""

    last_session: dt.date
    window_end: dt.datetime


def effective_ttl_days(order_ttl_days: int) -> int:
    """The session count a document's ``order_ttl_days`` stands for (0 = default)."""
    return DEFAULT_ORDER_TTL_DAYS if order_ttl_days == 0 else order_ttl_days


def pick_window(trade_date: dt.date | str, order_ttl_days: int, mic: str) -> PickWindow:
    """The window ``order_ttl_days`` sessions of ``mic`` after ``trade_date``.

    A ``trade_date`` that is not a session rolls forward to the next one first,
    the same rule the calendar applies everywhere else. Raises
    :class:`PickWindowError` for a count outside ``[0, MAX_ORDER_TTL_DAYS]``, a
    venue with no calendar, or a date the calendar does not reach."""
    if isinstance(order_ttl_days, bool) or not isinstance(order_ttl_days, int):
        raise PickWindowError(f"order_ttl_days must be an integer, got {order_ttl_days!r}")
    if not 0 <= order_ttl_days <= MAX_ORDER_TTL_DAYS:
        raise PickWindowError(f"order_ttl_days {order_ttl_days} is outside 0..{MAX_ORDER_TTL_DAYS}")
    ttl = effective_ttl_days(order_ttl_days)
    try:
        anchor = dt.date.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date
        last_session = advance_trading_sessions(anchor, ttl, exchange=mic)
        window_end = session_close_utc(last_session, exchange=mic)
    except Exception as exc:
        # Broad on purpose: an unknown MIC, a date past the calendar's end and
        # a malformed date each raise their own type, and every one of them
        # means the same thing here — this document has no computable window.
        raise PickWindowError(
            f"no validity window for trade_date {trade_date!r}, {ttl} session(s) on {mic!r}: {exc}"
        ) from exc
    return PickWindow(last_session=last_session, window_end=window_end)


def window_of(intent: Any) -> PickWindow:
    """:func:`pick_window` for an armed :class:`TradeIntent`, on the document's MIC."""
    return pick_window(
        intent.meta.trade_date, intent.spec.order_ttl_days, str(intent.instrument.mic)
    )


def remaining_sessions(window: PickWindow, today: dt.date, mic: str) -> int | None:
    """How many sessions after ``today``'s the window still has, or ``None``.

    The count ``n`` is the one for which ``advance_trading_sessions(today, n,
    mic)`` lands on ``window.last_session`` — the walk the broker adapter does
    to turn an entry TTL into a GTD date. ``None`` when ``today``'s session is
    already past the window, or when the calendar cannot answer (never an
    exception, for the same reason as :func:`pick_window`)."""
    try:
        session = session_on_or_after(today, exchange=mic)
        if session > window.last_session:
            return None
        # Sessions in (session, last_session]: `session` is itself a session,
        # so this is exactly the number of single steps from one to the other.
        return trading_days_elapsed(session, window.last_session, exchange=mic)
    except Exception:
        return None


__all__ = [
    "PickWindow",
    "PickWindowError",
    "effective_ttl_days",
    "pick_window",
    "remaining_sessions",
    "window_of",
]
