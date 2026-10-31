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
* :func:`clear_hold` — the gates passed. Forget why the pick was held and,
  when its wait is open, append ONE clearing line, so `broker status` and `broker picks` stop saying it
  waits for capital while something else (the day-1 gate, a deferral, a
  failed POST) keeps it unplaced.
* :func:`hold_on_state` — a gate could not value something (``state``). The pick
  is held, bounded by the same window, with the throttled alert the fail-closed
  refusal used to send. Not journaled as a wait: nobody knows how much capital
  is free, so "waiting for capital" would be a claim this process cannot make.
* :func:`note_hold` — any other reason the drain left a pick unplaced this tick
  (the day-1 gate, a safety rail). Remembered only to explain an expiry.
* :func:`expire` — the window ended with the pick unplaced: one terminal
  ``expired`` line in ``picks.jsonl`` and one alert. Both the reason and the
  alert branch on whether an order for the key already reached the broker
  (#1832): with one there the pick was never "unplaced", and telling the
  operator to arm a fresh document would buy the same position a second time.

The in-process sets reset on restart by design: they are observability only,
and the alert de-duplication that must survive a restart reads the journal.

The journal is folded at most ONCE per tick (between :func:`begin_tick` and
:func:`end_tick`) and the daemon's own appends are folded into that copy, the
way the drain reads ``picks.jsonl`` once per tick. Outside a tick (a direct
call) every call reads it fresh.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from typing import Any

from alphalens_pipeline.brokers.automanager import pick_waits, picks
from alphalens_pipeline.brokers.automanager.pick_window import PickWindow

logger = logging.getLogger(__name__)

# A second wait line for the same pick only when the binding gate changed AND
# the last line is at least this old. Marks move every tick, so the gate
# holding a pick can flip between gross cap and cash floor on consecutive
# ticks; one line per flip would grow the journal by thousands of lines over a
# seven-session window and tell the reader nothing new. The same bound applies
# to a wait that starts again after a clearing line: fit and no-fit can
# alternate tick by tick while something else keeps the pick unplaced.
WAIT_LINE_MIN_INTERVAL = dt.timedelta(minutes=30)

# More picks than this waiting in one tick is logged once per process: each
# waiting pick repeats its broker reads every tick (orders, positions, account,
# FX), so a long queue costs request budget the operator should know about.
MANY_WAITING = 3

UNPLACED_AT_WINDOW_END = "unplaced at window end"
# The journal reason when the window ended and the submissions journal DOES
# hold an order for the key (#1832). "Unplaced" is then simply false, and it is
# published: `broker trades` emits it as ``pick_status_reason`` and
# `broker status` prints it as the last expiry.
EXPIRED_WITH_ORDER_AT_BROKER = "window ended with an order at the broker"

# The two heads and the two tails of the expiry alert, named because they are
# NOT interchangeable prose. `alphalens broker arm` does not refuse a second
# document for an expired key — intent_door._refuse_other_armed_date counts a
# record as a blocker only while its status is `armed` — so the re-arm phrase,
# sent about a pick that already has an order, is an invitation to buy an open
# position a second time. That is the whole money-path hazard of #1832. The
# heads are constants for the same reason as the tails: the head is the first
# thing an operator reads, and the word "unplaced" over a filled order is the
# sentence this issue exists to delete.
UNPLACED_HEAD = "expired unplaced"
ORDER_AT_BROKER_HEAD = "expired with an order at the broker"
RE_ARM_PHRASE = "arm a new document if the signal still stands"
ORDER_AT_BROKER_PHRASE = (
    "an order for it ALREADY reached the broker — check the position and the resting "
    "orders before arming a new document, or the second one buys it again"
)

_wait_logged: set[str] = set()
_hold_reasons: dict[str, str] = {}
_waiting_this_tick: set[str] = set()
_many_waiting_logged: list[bool] = [False]


class _TickFold:
    """The tick's fold of ``pick_waits.jsonl``: read on first use inside a tick."""

    def __init__(self) -> None:
        self.in_tick = False
        self.fold: pick_waits.PickWaitFold | None = None

    def reset(self, *, in_tick: bool) -> None:
        self.in_tick = in_tick
        self.fold = None


_tick_fold = _TickFold()


def _reset_for_tests() -> None:
    """Forget every in-process mark, as a daemon restart does."""
    _wait_logged.clear()
    _hold_reasons.clear()
    _waiting_this_tick.clear()
    _many_waiting_logged[0] = False
    _tick_fold.reset(in_tick=False)


def _fold() -> pick_waits.PickWaitFold:
    """This tick's fold of the wait journal, read on first use (``OSError`` escapes)."""
    if not _tick_fold.in_tick:
        return pick_waits.read_waits()
    if _tick_fold.fold is None:
        _tick_fold.fold = pick_waits.read_waits()
    return _tick_fold.fold


def _folded(fold: pick_waits.PickWaitFold, line: dict[str, Any]) -> None:
    """Keep the tick's copy in step with a line this process just appended."""
    if _tick_fold.fold is fold:
        fold.apply(line)


def note_hold(pick_key: str, reason: str) -> None:
    """Remember why the drain left ``pick_key`` unplaced on this tick."""
    _hold_reasons[pick_key] = reason


def clear_hold(pick_key: str, *, ticker: str, now: dt.datetime) -> None:
    """The gates admitted the pick: forget why it was held and close its wait."""
    _hold_reasons.pop(pick_key, None)
    _wait_logged.discard(pick_key)
    _close_wait(ticker=ticker, pick_key=pick_key, now=now)


def begin_tick() -> None:
    _waiting_this_tick.clear()
    _tick_fold.reset(in_tick=True)


def end_tick() -> None:
    """Log once per process when more than :data:`MANY_WAITING` picks waited."""
    _tick_fold.reset(in_tick=False)
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


def _wants_line(fold: pick_waits.PickWaitFold, pick_key: str, gate: str, now: dt.datetime) -> bool:
    previous = fold.latest.get(pick_key)
    if previous is not None and previous.gate == gate:
        return False
    last_ts = fold.last_ts.get(pick_key)
    if last_ts is None:
        return True
    # The gate changed, or the wait starts again after a clearing line.
    stamp = _parse_ts(last_ts)
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
        fold = _fold()
    except OSError as exc:
        logger.warning("place_pick %s: pick-wait journal unreadable: %s", ticker, exc)
        return
    announced = pick_key in fold.announced
    if _wants_line(fold, pick_key, gate, now):
        try:
            line = pick_waits.append_wait(
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
        else:
            _folded(fold, line)
    # ONE page per pick for its whole life, keyed on the pick only: the gate
    # holding it may change, and a wait may close and start again, but the
    # decision the operator has to make does not. Throttled, so a journal that
    # cannot be written does not page every tick.
    if not announced and alert_throttled is not None:
        alert_throttled(
            f"{ticker} @ {picks.identity_token(str(intent.meta.trade_date), picks._pick_generation(intent))}"
            f" waits for capital ({message}); stays armed and is placed as soon as it "
            f"fits, until its window ends {window_end}",
            f"capital-wait:{pick_key}",
        )


def _close_wait(*, ticker: str, pick_key: str, now: dt.datetime) -> None:
    """Close ``pick_key``'s wait if one is open.

    One clearing line, then the fold no longer holds the wait, so a later tick
    writes nothing. Fallible I/O never crashes the drain: on ``OSError`` the
    wait stays open and the next tick tries again."""
    try:
        fold = _fold()
        if pick_key not in fold.latest:
            return
        line = pick_waits.append_cleared(pick_key, ticker=ticker, now=now)
    except OSError as exc:
        logger.warning("place_pick %s: closing the capital wait failed: %s", ticker, exc)
        return
    _folded(fold, line)


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
    at_broker: bool = False,
) -> None:
    """Retire an armed pick still unplaced at the end of its window.

    ``at_broker`` is the caller's answer to "does the submissions journal hold
    an order for this key?" (#1832). It selects the alert and the journal
    reason, because the two cases ask the operator for OPPOSITE things: with
    nothing placed a fresh document is the path back, while with an order at
    the broker a fresh document buys the same position twice. Defaults to
    False, which is the pre-#1832 message — a caller that cannot answer must
    say so explicitly rather than get the dangerous branch by accident.

    Two shapes still reach this writer with something at the broker, and both
    get the terminal line: a MIXED pick whose now half was placed and whose
    pullback half never was (correctly not retired, #1247), and a pick whose
    only record is the write-ahead ``attempt``. For those the branched message
    is the ONLY guard — the arming door does not refuse a second document once
    the status stops being ``armed``, so an operator who re-arms on a longer
    ``order_ttl_days`` buys the now half again. Branding the page does not
    close that door; refusing the second document is the door's own decision.

    ``at_broker`` is generous by construction, because the caller asks
    ``picks.keys_with_any_submission`` and that fold counts every outcome but
    ``refused_cap``. A venue ``refused_reject`` therefore also gets the
    at-broker wording, although nothing rests. That is wrong in the one
    direction that cannot cost money, and ``refused_cap`` — the single outcome
    that PROVES nothing was submitted — keeps the plain message.

    The ``expired`` append is fallible I/O and must never crash the drain: on
    ``OSError`` the pick stays armed and expires on the next tick, and the
    alert is throttled so that retry does not page every tick."""
    ticker = str(intent.instrument.ticker).upper()
    generation = picks._pick_generation(intent)
    token = picks.identity_token(str(intent.meta.trade_date), generation)
    fallback = EXPIRED_WITH_ORDER_AT_BROKER if at_broker else UNPLACED_AT_WINDOW_END
    # A recorded hold still wins: it names what actually kept the pick from
    # finishing (no capital, the day-1 gate, a rail), which is more specific
    # than either fallback. Only the default is branched.
    reason = _hold_reasons.get(pick_key) or fallback
    window_end = window.window_end.isoformat(timespec="seconds")
    head = ORDER_AT_BROKER_HEAD if at_broker else UNPLACED_HEAD
    tail = ORDER_AT_BROKER_PHRASE if at_broker else RE_ARM_PHRASE
    message = f"{ticker} @ {token}: {head} — its window ended {window_end} ({reason}); {tail}"
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


class TickAdmissions:
    """Account-currency gross admitted by the money gates earlier this tick.

    With a queue of picks waiting for capital, the tick that frees capital is
    the tick several picks are admitted together, and a later pick's broker
    reads need not show an earlier pick's order or fill yet (an order missing
    from ``list_open_orders``, an audit deferred by the shared budget, a fill
    not yet in the positions read). Each admitted pick's gross is kept here,
    by pick, and counted by the gates for the rest of the tick.

    A watch is different: the gates re-read the entry-trail fold on every
    pick, so a watch opened earlier this tick is already valued as
    "watching". :meth:`outstanding_acct` subtracts that visible reservation,
    so a watched pick is never counted twice (LIVE 2026-10-06: three picks
    waited a tick on two watches counted twice). A resting order can still be
    counted twice once it shows, which delays the next pick by one tick and
    never overspends."""

    def __init__(self) -> None:
        self._gross_by_pick: dict[str, float] = {}

    def begin_tick(self) -> None:
        self._gross_by_pick = {}

    def admit(self, pick_key: str, gross_acct: float) -> None:
        self._gross_by_pick[pick_key] = self._gross_by_pick.get(pick_key, 0.0) + max(
            0.0, float(gross_acct)
        )

    def outstanding_acct(self, watching_by_pick: Mapping[str, float]) -> float:
        """The admitted gross the gates cannot see yet: each pick's admitted
        gross less what its open watches already reserve, never below zero."""
        return sum(
            max(0.0, gross - watching_by_pick.get(pick_key, 0.0))
            for pick_key, gross in self._gross_by_pick.items()
        )


__all__ = [
    "EXPIRED_WITH_ORDER_AT_BROKER",
    "MANY_WAITING",
    "ORDER_AT_BROKER_HEAD",
    "ORDER_AT_BROKER_PHRASE",
    "RE_ARM_PHRASE",
    "UNPLACED_AT_WINDOW_END",
    "UNPLACED_HEAD",
    "WAIT_LINE_MIN_INTERVAL",
    "TickAdmissions",
    "begin_tick",
    "clear_hold",
    "end_tick",
    "expire",
    "hold_on_state",
    "note_hold",
    "record_capital_wait",
]
