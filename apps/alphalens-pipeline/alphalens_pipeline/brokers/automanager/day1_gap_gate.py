"""The day-1 gap gate: refuse a pick that gapped away from its own levels.

Step 5 of the ``control_loop`` partition. A pick is armed against levels its
author chose on the previous session. If the instrument opens far from them, the
levels no longer describe the trade, so the gate defers the pick instead of
placing it.

``_day1_gap_gate_defers`` is the whole decision as ``control_loop`` sees it;
below it, ``_day1_gap_gate_enabled`` reads the env rail,
``_day1_gap_gate_session_info`` establishes which session the gate is judging
and how far into it the tick is, and ``_day1_gap_gate_decision`` and
``_evaluate_day1_gap_gate`` answer with a ``Day1GapGateVerdict``.

What moved is CLOSED under every module-level name it uses, so nothing here
names anything left behind in ``control_loop`` and there is no import cycle.
``control_loop`` reaches it through the module prefix
(``day1_gap_gate.<name>``), never by a ``from`` import.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Callable
from typing import Any, Literal

logger = logging.getLogger(__name__)


_DAY1_GAP_GATE_ENV = "ALPHALENS_BROKER_DAY1_GAP_GATE"

_DAY1_GAP_GATE_OPEN_GRACE_S = 300

Day1GapGateVerdict = Literal["pass", "defer_preopen", "defer_no_price", "defer_below_e1"]


def _day1_gap_gate_enabled() -> bool:
    """Whether the day-1 gap gate is armed (read at call time, mirrors
    ``_live_market_exits_enabled`` / ``_saxo_live_prices_enabled`` above).
    Defaults OFF — unset means ``_place_pick`` never evaluates the gate,
    byte-identical to today."""
    return os.environ.get(_DAY1_GAP_GATE_ENV) == "1"


def _day1_gap_gate_session_info(
    trade_date: dt.date, exchange_mic: str, *, day1_includes_trade_date: bool = False
) -> tuple[dt.date, dt.datetime] | None:
    """``(day1 session date, day1 session open UTC)`` for ``trade_date`` on
    ``exchange_mic``, or ``None`` when the calendar cannot resolve it (e.g. an
    unrecognised exchange MIC) — never raises. NOTE the consequence: ``None``
    makes ``_day1_gap_gate_decision`` return "pass", so an unknown-to-calendar
    MIC DISABLES the gate for that pick — visible only through the WARNING
    below, never a refusal (#1238).

    The anchor depends on the pick's provenance (#1246):

    - default (brief picks): ``day1`` is the first trading session STRICTLY
      AFTER ``trade_date`` — a brief holds T-1 data and trades the next
      session (a Monday brief's day1 is Tuesday, a Friday brief's day1 is the
      following Monday; a weekend/holiday ``trade_date`` lands one session
      past the weekend's first session).
    - ``day1_includes_trade_date=True`` (manual picks, whose ``trade_date``
      IS the arm date): ``day1`` is the session ON-OR-AFTER ``trade_date`` —
      an "as of now" operator decision trades its own arm day, not the next
      one (a weekend arm still rolls to the next session).

    Both anchors are one ``advance_trading_sessions`` call (``n=0`` is
    documented as session-on-or-after). Pure calendar math, no I/O — shared
    by ``_day1_gap_gate_decision`` and the placer's probe-gating check
    (``_evaluate_day1_gap_gate``) so the two never disagree on what "day1"
    means."""
    try:
        from alphalens_pipeline.market.calendar import advance_trading_sessions, session_open_utc

        step = 0 if day1_includes_trade_date else 1
        day1 = advance_trading_sessions(trade_date, step, exchange=exchange_mic)
        return day1, session_open_utc(day1, exchange=exchange_mic)
    except Exception:
        logger.warning(
            "day1 gap gate: calendar resolution failed for trade_date=%s exchange_mic=%s",
            trade_date,
            exchange_mic,
            exc_info=True,
        )
        return None


def _day1_gap_gate_decision(
    now_utc: dt.datetime,
    trade_date: dt.date,
    e1_limit: float | None,
    probe_price: float | None,
    exchange_mic: str,
    *,
    source: str = "brief",
) -> Day1GapGateVerdict:
    """Pure day-1 gap gate verdict — no I/O, total (never raises on weird
    inputs). ``source`` picks the day-1 anchor (#1246): ``"manual"`` anchors
    day 1 on the session on-or-after ``trade_date`` (the arm date itself),
    anything else on the session strictly after it — see
    ``_day1_gap_gate_session_info``.

    - ``e1_limit is None`` (a pick the gate cannot evaluate) -> "pass" with a
      WARNING log — a doubt about GATING must never itself become a
      placement refusal.
    - A calendar resolution failure (see ``_day1_gap_gate_session_info``) ->
      "pass" — same reasoning, already logged there.
    - ``now_utc`` on a date AFTER day1 -> "pass" (later-day gaps are benign
      by the population data, so the gate is inert from day 2 on).
    - Before day1's open + ``_DAY1_GAP_GATE_OPEN_GRACE_S`` -> "defer_preopen"
      (covers every pre-day1 tick too — day1's open is always in the future
      then).
    - Within day1, at/after the grace window, and ``probe_price is None`` ->
      "defer_no_price" (fail-safe: no price, no day-1 placement).
    - Within day1, at/after the grace window, and ``probe_price < e1_limit``
      -> "defer_below_e1".
    - Otherwise -> "pass"."""
    if e1_limit is None:
        logger.warning("day1 gap gate: pick carries no E1 limit — gate cannot evaluate, passing")
        return "pass"
    info = _day1_gap_gate_session_info(
        trade_date,
        exchange_mic,
        day1_includes_trade_date=source == "manual",  # LEGACY(source_brief)
    )
    if info is None:
        return "pass"
    day1, day1_open = info
    if now_utc.date() > day1:
        return "pass"
    if now_utc < day1_open + dt.timedelta(seconds=_DAY1_GAP_GATE_OPEN_GRACE_S):
        return "defer_preopen"
    if probe_price is None:
        return "defer_no_price"
    if probe_price < e1_limit:
        return "defer_below_e1"
    return "pass"


def _evaluate_day1_gap_gate(
    ticker: str,
    trade_date: dt.date,
    spec: Any,
    exchange_mic: str,
    probe: Callable[[str, str], float | None] | None,
    *,
    source: str = "brief",
) -> Day1GapGateVerdict:
    """Orchestrates the gate for one pick: resolves E1 (the first PULLBACK
    tier — ``ladder.build_entry_tiers`` returns tiers strictly descending, so
    the first pullback tier is the shallowest/highest resting limit; a
    leading immediate "now" tier (#1247) is EXCLUDED — its cap is not a
    pullback rung and must not redefine the gate's threshold), calls the
    price probe ONLY when the pick is within its day1 session at/after the
    open+grace window — every other phase (pre-day1, pre-open, day 2+) needs
    no price at all, and the probe is a real network round-trip — then
    delegates the full verdict to the pure ``_day1_gap_gate_decision``.
    ``source`` threads the day-1 anchor choice (#1246) into BOTH the
    probe-gating check here and the decision, so the two can never disagree
    on what "day1" means. A now-ONLY pick has no pullback rung: the gate is
    not applicable (the group's decision IS the timing, memo §2) — pass
    without probing."""
    tiers = spec.entry_tiers or ()
    e1_limit = next(
        (t.limit_price for t in tiers if getattr(t, "entry_mode", "pullback") == "pullback"),
        None,
    )
    if e1_limit is None and any(getattr(t, "entry_mode", "pullback") == "immediate" for t in tiers):
        logger.info(
            "day1 gap gate: %s carries only an immediate tier — gate not applicable", ticker
        )
        return "pass"
    now_utc = dt.datetime.now(dt.UTC)
    probe_price: float | None = None
    if e1_limit is not None and probe is not None:
        info = _day1_gap_gate_session_info(
            trade_date,
            exchange_mic,
            day1_includes_trade_date=source == "manual",  # LEGACY(source_brief)
        )
        if info is not None:
            day1, day1_open = info
            grace_open = day1_open + dt.timedelta(seconds=_DAY1_GAP_GATE_OPEN_GRACE_S)
            if now_utc.date() <= day1 and now_utc >= grace_open:
                probe_price = probe(ticker, exchange_mic)
    return _day1_gap_gate_decision(
        now_utc, trade_date, e1_limit, probe_price, exchange_mic, source=source
    )


def _day1_gap_gate_defers(
    ticker: str,
    trade_date: dt.date,
    spec: Any,
    exchange_mic: str,
    probe: Callable[[str, str], float | None] | None,
    alert_throttled: Callable[[str, str], bool] | None,
    *,
    source: str,
) -> bool:
    """True iff the day-1 gap gate is enabled AND defers this pick (the
    ``_place_pick`` early-return). Pages the operator (throttled) for the
    actionable below-E1 verdict AND for the no-price verdict (an
    INFRASTRUCTURE failure — the probe could not produce a price at all;
    real incident 2026-08-12: LAC's resolve failure silently deferred its
    whole day 1 at DEBUG); "defer_preopen" stays a DEBUG line (expected,
    high-frequency).

    ``source`` (no default — the one production caller must be explicit)
    picks the day-1 anchor: ``"manual"`` gates on the arm date's own session,
    ``"brief"`` on the next session (#1246)."""
    if not _day1_gap_gate_enabled():
        return False
    gate_verdict = _evaluate_day1_gap_gate(
        ticker, trade_date, spec, exchange_mic, probe, source=source
    )
    if gate_verdict == "pass":
        return False
    if gate_verdict == "defer_no_price":
        # Ride the alert throttle for the WARNING too (zen pre-merge finding):
        # the probe can fail every ~45s tick all day, and hundreds of
        # identical journald WARNINGs would crowd out real signals. One
        # WARNING per throttle window (or per tick when no alert sink is
        # wired — unit tests, ad-hoc runs); suppressed repeats log at DEBUG.
        sent = alert_throttled is None or alert_throttled(
            f"day1 gap gate: {ticker} day-1 PRICE PROBE failed — an "
            "infrastructure problem, not a market condition; check "
            "instrument resolution / marketdata chain (the pick stays "
            "deferred all of day 1 until a price arrives)",
            f"day1-gap-noprice:{ticker}",
        )
        log = logger.warning if sent else logger.debug
        log(
            "place_pick %s: day1 gap gate deferred (defer_no_price) — the PRICE "
            "PROBE returned no price (infrastructure problem, not a market "
            "condition); check instrument resolution / marketdata chain",
            ticker,
        )
        return True
    logger.debug("place_pick %s: day1 gap gate deferred (%s)", ticker, gate_verdict)
    if gate_verdict == "defer_below_e1" and alert_throttled is not None:
        alert_throttled(
            f"day1 gap gate: {ticker} trading below E1 at the day-1 open — "
            "entry deferred to day 2+",
            f"day1-gap:{ticker}",
        )
    return True
