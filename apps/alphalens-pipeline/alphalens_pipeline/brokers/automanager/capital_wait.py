"""An armed pick that does not fit in free capital waits; its window ends it (#1734).

Before #1734 the gross cap and the cash floor refused such a pick for good. Now
the pick stays ``armed`` and the drain tries it again on every tick, in armed
order, until it fits or its validity window ends (``pick_window.py``). The
window is the same one a placed entry rests in, anchored on the trade date, so
waiting never starts a new one.

This module owns what happens around the wait, so ``control_loop`` only decides:

* :func:`record_capital_wait` — the gates said "does not fit". Journal the wait
  (``pick_waits.jsonl``) when it starts and when the binding gate changes, page
  ONCE per pick for its whole life (a restart reads the journal and does not
  page again), log at WARNING the first time per process and at DEBUG after.
* :func:`hold_on_state` — a gate could not value something (``state``). The pick
  is held, bounded by the same window, with the throttled alert the fail-closed
  refusal used to send. Not journaled as a wait: nobody knows how much capital
  is free, so "waiting for capital" would be a claim this process cannot make.
* :func:`note_hold` — any other reason the drain left a pick unplaced this tick
  (the day-1 gate, a safety rail). Remembered only to explain an expiry.
* :func:`expire` — the window ended with the pick unplaced: one terminal
  ``expired`` line in ``picks.jsonl`` and one alert.

The in-process sets reset on restart by design: they are observability only,
and the alert de-duplication that must survive a restart reads the journal.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from alphalens_pipeline.brokers.automanager import pick_waits, picks
from alphalens_pipeline.brokers.automanager.pick_window import PickWindow

logger = logging.getLogger(__name__)

# A second wait line for the same pick only when the binding gate changed AND
# the last line is at least this old. Marks move every tick, so the gate
# holding a pick can flip between gross cap and cash floor on consecutive
# ticks; one line per flip would grow the journal by thousands of lines over a
# seven-session window and tell the reader nothing new.
WAIT_LINE_MIN_INTERVAL = dt.timedelta(minutes=30)

# More picks than this waiting in one tick is logged once per process: each
# waiting pick repeats its broker reads every tick (orders, positions, account,
# FX), so a long queue costs request budget the operator should know about.
MANY_WAITING = 3

UNPLACED_AT_WINDOW_END = "unplaced at window end"

_wait_logged: set[str] = set()
_hold_reasons: dict[str, str] = {}
_waiting_this_tick: set[str] = set()
_many_waiting_logged: list[bool] = [False]


def _reset_for_tests() -> None:
    """Forget every in-process mark, as a daemon restart does."""
    _wait_logged.clear()
    _hold_reasons.clear()
    _waiting_this_tick.clear()
    _many_waiting_logged[0] = False


def note_hold(pick_key: str, reason: str) -> None:
    """Remember why the drain left ``pick_key`` unplaced on this tick."""
    _hold_reasons[pick_key] = reason


def clear_hold(pick_key: str) -> None:
    """The pick was admitted: forget why it was held."""
    _hold_reasons.pop(pick_key, None)
    _wait_logged.discard(pick_key)


def begin_tick() -> None:
    _waiting_this_tick.clear()


def end_tick() -> None:
    """Log once per process when more than :data:`MANY_WAITING` picks waited."""
    if len(_waiting_this_tick) > MANY_WAITING and not _many_waiting_logged[0]:
        _many_waiting_logged[0] = True
        logger.warning(
            "%d picks are waiting for capital (%s) — each repeats its broker reads every "
            "tick until it fits or its window ends",
            len(_waiting_this_tick),
            ", ".join(sorted(_waiting_this_tick)),
        )


def _parse_ts(raw: str) -> dt.datetime | None:
    try:
        stamp = dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=dt.UTC)


def _wants_line(previous: pick_waits.PickWait | None, gate: str, now: dt.datetime) -> bool:
    if previous is None:
        return True
    if previous.gate == gate:
        return False
    stamp = _parse_ts(previous.ts)
    return stamp is None or now - stamp >= WAIT_LINE_MIN_INTERVAL


def record_capital_wait(
    *,
    intent: Any,
    pick_key: str,
    gate: str,
    message: str,
    window: PickWindow,
    alert_throttled: Callable[[str, str], bool] | None,
    now: dt.datetime,
) -> None:
    """The gates found no room for ``pick_key``: it stays armed and waits."""
    ticker = str(intent.instrument.ticker).upper()
    window_end = window.window_end.isoformat(timespec="seconds")
    _hold_reasons[pick_key] = f"waiting for capital: {message}"
    _waiting_this_tick.add(pick_key)

    log = logger.debug
    if pick_key not in _wait_logged:
        _wait_logged.add(pick_key)
        log = logger.warning
    log("place_pick %s: %s — stays armed until %s", ticker, message, window_end)

    try:
        previous = pick_waits.read_waits().latest.get(pick_key)
    except OSError as exc:
        logger.warning("place_pick %s: pick-wait journal unreadable: %s", ticker, exc)
        return
    if _wants_line(previous, gate, now):
        try:
            pick_waits.append_wait(
                {
                    "pick_key": pick_key,
                    "ticker": ticker,
                    "date": str(intent.meta.trade_date),
                    "generation": picks._pick_generation(intent),
                    "gate": gate,
                    "message": message,
                    "window_end": window_end,
                },
                now=now,
            )
        except OSError as exc:
            logger.warning("place_pick %s: pick-wait append failed: %s", ticker, exc)
    # ONE page per pick for its whole life, keyed on the pick only: the gate
    # holding it may change, the decision the operator has to make does not.
    # Throttled, so a journal that cannot be written does not page every tick.
    if previous is None and alert_throttled is not None:
        alert_throttled(
            f"{ticker} @ {picks.identity_token(str(intent.meta.trade_date), picks._pick_generation(intent))}"
            f" waits for capital ({message}); stays armed and is placed as soon as it "
            f"fits, until its window ends {window_end}",
            f"capital-wait:{pick_key}",
        )


def hold_on_state(
    *,
    ticker: str,
    pick_key: str,
    gate: str,
    message: str,
    alert_throttled: Callable[[str, str], bool] | None,
) -> None:
    """A gate could not value the book: hold the pick, page as the refusal did."""
    _hold_reasons[pick_key] = f"blocked: {message}"
    log = logger.debug
    if pick_key not in _wait_logged:
        _wait_logged.add(pick_key)
        log = logger.warning
    log("place_pick %s: %s — stays armed, retried next tick", ticker, message)
    if alert_throttled is not None:
        key = "gross-cap-state" if gate == pick_waits.GATE_GROSS_CAP else "cash-floor-state"
        alert_throttled(f"{message} — the pick stays armed, retried next tick", f"{key}:{ticker}")


def expire(
    *,
    intent: Any,
    pick_key: str,
    window: PickWindow,
    alert_throttled: Callable[[str, str], bool] | None,
) -> None:
    """Retire an armed pick still unplaced at the end of its window.

    The ``expired`` append is fallible I/O and must never crash the drain: on
    ``OSError`` the pick stays armed and expires on the next tick, and the
    alert is throttled so that retry does not page every tick."""
    ticker = str(intent.instrument.ticker).upper()
    generation = picks._pick_generation(intent)
    token = picks.identity_token(str(intent.meta.trade_date), generation)
    reason = _hold_reasons.get(pick_key) or UNPLACED_AT_WINDOW_END
    window_end = window.window_end.isoformat(timespec="seconds")
    message = (
        f"{ticker} @ {token}: expired unplaced — its window ended {window_end} ({reason}); "
        "arm a new document if the signal still stands"
    )
    logger.warning("place_pick %s: %s", ticker, message)
    if alert_throttled is not None:
        alert_throttled(message, f"pick-expired:{pick_key}")
    try:
        picks.mark_expired(
            ticker,
            dt.date.fromisoformat(str(intent.meta.trade_date)),
            window_end=window.window_end,
            reason=reason,
            generation=generation,
        )
    except OSError as exc:
        logger.warning(
            "place_pick %s: expired-line append failed (pick stays armed): %s", ticker, exc
        )
        return
    _hold_reasons.pop(pick_key, None)
    _wait_logged.discard(pick_key)


__all__ = [
    "MANY_WAITING",
    "UNPLACED_AT_WINDOW_END",
    "WAIT_LINE_MIN_INTERVAL",
    "begin_tick",
    "clear_hold",
    "end_tick",
    "expire",
    "hold_on_state",
    "note_hold",
    "record_capital_wait",
]
