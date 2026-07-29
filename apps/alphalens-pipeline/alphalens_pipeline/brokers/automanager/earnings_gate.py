"""Earnings-window gate — never rest a new entry across a known earnings date.

Overnight-drift memo 2026-07-29, exposure J (the top scheduled — not tail —
risk): a resting pullback BUY limit held over an AMC/BMO earnings release is a
free option granted to informed traders (Copeland-Galai 1983); it is most
likely to fill exactly when the report gaps the stock down through it
(Linnainmaa 2010), with the brief's T-1-frozen geometry at its most wrong.

The gate runs at DRAIN (``control_loop._place_pick``), before resolve/size:
when the ticker's next CONFIRMED earnings date falls inside the entry's
GoodTillDate window (today through the TTL expiry session, inclusive — the
order is live THROUGH the expiry session), the pick is refused for this tick.
The armed pick stays queued, so the gate SELF-HEALS: once the earnings date
passes, the next tick places it normally.

Fail-open doctrine: an unknown date (no calendar entry, yfinance outage,
lookup exception) allows placement — the gate is a risk enhancement, never an
availability rail. Operator opt-out for deliberate earnings-window entries
(e.g. a SIM soak experiment): ``ALPHALENS_BROKER_ALLOW_EARNINGS_WINDOW=1``.

The default lookup wraps the canonical
``thematic.sources.earnings_calendar.fetch_next_earnings`` (yfinance-backed,
swallows failures to None) behind a per-``(ticker, today)`` cache: the drain
retries a refused pick every ~45s tick and must not re-pay a network call per
retry.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

logger = logging.getLogger(__name__)

EARNINGS_GATE_OPT_OUT_ENV = "ALPHALENS_BROKER_ALLOW_EARNINGS_WINDOW"

# {(ticker, today_iso): next_earnings_date | None} — refreshed naturally each
# calendar day because ``today`` is part of the key.
_LOOKUP_CACHE: dict[tuple[str, str], dt.date | None] = {}


def _clear_lookup_cache_for_tests() -> None:
    _LOOKUP_CACHE.clear()


def _fetch_next_earnings(*, ticker: str, asof: dt.date) -> dt.date | None:
    """Seam over the canonical earnings-calendar helper (patchable in tests).

    Dependency note (deliberate, ADR-0013-legal): this is the trade side
    (``brokers.automanager``) READING selection-side data (``thematic.sources``)
    — the allowed direction. The enforced anti-collider rule is the REVERSE
    (``thematic`` must never import ``brokers``: execution output must never
    feed selection, ADR 0013 R2 / test_module_dependencies). Lazy import keeps
    the daemon's startup path free of thematic's import cost.
    """
    from alphalens_pipeline.thematic.sources.earnings_calendar import fetch_next_earnings

    return fetch_next_earnings(ticker=ticker, asof=asof)


def _cached_lookup(*, ticker: str, asof: dt.date) -> dt.date | None:
    key = (ticker, asof.isoformat())
    if key not in _LOOKUP_CACHE:
        _LOOKUP_CACHE[key] = _fetch_next_earnings(ticker=ticker, asof=asof)
    return _LOOKUP_CACHE[key]


def _window_end(today: dt.date, ttl_days: int) -> dt.date:
    """The GTD expiry session for an entry placed today — same helper the
    broker uses to stamp the order's GoodTillDate, so gate and order agree."""
    from alphalens_pipeline.paper.calendar import advance_trading_sessions

    return advance_trading_sessions(today, ttl_days)


def earnings_window_refusal(
    ticker: str,
    *,
    ttl_days: int,
    today: dt.date | None = None,
    lookup=None,
) -> str | None:
    """Return a refusal reason when ``ticker``'s next earnings falls inside the
    entry's GTD window; ``None`` when safe, unknown (fail-open), or opted out.
    """
    if os.environ.get(EARNINGS_GATE_OPT_OUT_ENV) == "1":
        return None
    today = today or dt.date.today()
    lookup = lookup or _cached_lookup
    try:
        next_earnings = lookup(ticker=ticker, asof=today)
    except Exception as exc:
        logger.warning(
            "earnings gate: lookup failed for %s (%s); failing open", ticker, exc, exc_info=True
        )
        return None
    if next_earnings is None:
        return None
    end = _window_end(today, ttl_days)
    if today <= next_earnings <= end:
        return (
            f"earnings {next_earnings.isoformat()} inside the {ttl_days}-session "
            f"entry TTL window (ends {end.isoformat()}) — an entry resting across "
            f"the release is adverse selection; retried after the date passes "
            f"(opt out: {EARNINGS_GATE_OPT_OUT_ENV}=1)"
        )
    return None


__all__ = ["EARNINGS_GATE_OPT_OUT_ENV", "earnings_window_refusal"]
