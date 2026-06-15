"""Pure root-cause reconstruction for NO_FILL EDGE outcomes (no I/O).

Given a candidate's entry tiers and the daily [low, high] path over its
entry-TTL window (+ a short post-window tail), classify WHY the dip-buy entry
never filled. See docs/superpowers/specs/2026-06-15-nofill-rootcause-metric-rethink-design.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TOUCH_EPS = 0.0025  # mirrors alphalens_pipeline...population_ladder_monitor._TOUCH_EPS
GAP_UP_MARGIN = 0.03  # opening gap vs arrival anchor that counts as GAP_UP_ARRIVAL

CAUSE_DATA_GAP = "DATA_GAP"
CAUSE_AMBIGUOUS = "AMBIGUOUS"
CAUSE_TOUCHED_AFTER_TTL = "TOUCHED_AFTER_TTL"
CAUSE_GAP_UP_ARRIVAL = "GAP_UP_ARRIVAL"
CAUSE_MOMENTUM_RAN = "MOMENTUM_RAN"


@dataclass(frozen=True)
class NoFillReconstruction:
    e1: float | None
    e2: float | None
    e3: float | None
    stop: float | None
    min_low_in_window: float | None
    # touched_eN is meaningful only when the corresponding tier is not None; it is
    # False both when the price never reached the tier AND when the tier is absent.
    touched_e1: bool
    touched_e2: bool
    touched_e3: bool
    gap_to_e1: float | None
    days_to_first_touch: int | None
    arrival_drift: float | None
    window_complete: bool
    cause: str


def _tier(tiers: Sequence[float], i: int) -> float | None:
    return float(tiers[i]) if len(tiers) > i else None


def reconstruct(
    *,
    tiers: Sequence[float],
    stop: float | None,
    reference_close: float | None,
    window_lows_highs: Sequence[tuple[float, float] | None],
    first_session_open: float | None,
    tail_min_low: float | None,
    touch_eps: float = TOUCH_EPS,
    gap_up_margin: float = GAP_UP_MARGIN,
) -> NoFillReconstruction:
    e1 = _tier(tiers, 0)
    e2 = _tier(tiers, 1)
    e3 = _tier(tiers, 2)
    stop_f = float(stop) if stop is not None else None

    present = [lh for lh in window_lows_highs if lh is not None]
    window_complete = bool(window_lows_highs) and len(present) == len(window_lows_highs)
    min_low = min((lh[0] for lh in present), default=None)

    def _touched(level: float | None) -> bool:
        return level is not None and min_low is not None and min_low <= level * (1.0 + touch_eps)

    touched_e1 = _touched(e1)
    touched_e2 = _touched(e2)
    touched_e3 = _touched(e3)

    days_to_first_touch: int | None = None
    if e1 is not None:
        for i, lh in enumerate(window_lows_highs):
            if lh is not None and lh[0] <= e1 * (1.0 + touch_eps):
                days_to_first_touch = i + 1
                break

    # Divisor guards use ``> 0.0`` (prices are strictly positive) rather than
    # ``!= 0.0`` — float inequality-to-zero trips Sonar S1244 and a non-positive
    # price is invalid for these ratios anyway.
    gap_to_e1 = (
        (min_low - e1) / e1 if (min_low is not None and e1 is not None and e1 > 0.0) else None
    )
    arrival_drift = (
        (first_session_open - reference_close) / reference_close
        if (
            first_session_open is not None and reference_close is not None and reference_close > 0.0
        )
        else None
    )

    cause = _classify(
        e1=e1,
        window_complete=window_complete,
        min_low=min_low,
        tail_min_low=tail_min_low,
        arrival_drift=arrival_drift,
        touch_eps=touch_eps,
        gap_up_margin=gap_up_margin,
    )

    return NoFillReconstruction(
        e1=e1,
        e2=e2,
        e3=e3,
        stop=stop_f,
        min_low_in_window=min_low,
        touched_e1=touched_e1,
        touched_e2=touched_e2,
        touched_e3=touched_e3,
        gap_to_e1=gap_to_e1,
        days_to_first_touch=days_to_first_touch,
        arrival_drift=arrival_drift,
        window_complete=window_complete,
        cause=cause,
    )


def _classify(
    *,
    e1: float | None,
    window_complete: bool,
    min_low: float | None,
    tail_min_low: float | None,
    arrival_drift: float | None,
    touch_eps: float,
    gap_up_margin: float,
) -> str:
    if e1 is None or not window_complete or min_low is None:
        return CAUSE_DATA_GAP
    # Daily low reached E1 yet the row is NO_FILL: the daily path says it should have
    # filled but the minute-resolve monitor recorded no fill (daily-vs-minute
    # disagreement). Flag for minute-bar escalation rather than trusting either side.
    if min_low <= e1 * (1.0 + touch_eps):
        return CAUSE_AMBIGUOUS
    if tail_min_low is not None and tail_min_low <= e1 * (1.0 + touch_eps):
        return CAUSE_TOUCHED_AFTER_TTL
    if arrival_drift is not None and arrival_drift > gap_up_margin:
        return CAUSE_GAP_UP_ARRIVAL
    return CAUSE_MOMENTUM_RAN


def _bar_low_high_open(
    snapshot: Mapping[str, Mapping[str, Any]] | None, ticker: str
) -> tuple[float, float, float] | None:
    """Pull (low, high, open) for ``ticker`` from one grouped-daily snapshot.

    ``snapshot is None`` means the session is not on disk; a present snapshot
    missing the ticker means it did not trade that session. Either way -> None.
    """
    if snapshot is None:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        return float(bar["l"]), float(bar["h"]), float(bar["o"])
    except (KeyError, TypeError, ValueError):
        return None


def analyze_outcome_row(
    *,
    ticker: str,
    tiers: Sequence[float],
    stop: float | None,
    reference_close: float | None,
    window_sessions: Sequence[object],
    tail_sessions: Sequence[object],
    grouped_by_session: Mapping[object, Mapping[str, Mapping[str, Any]] | None],
    touch_eps: float = TOUCH_EPS,
    gap_up_margin: float = GAP_UP_MARGIN,
) -> NoFillReconstruction:
    """Build the window/tail price path for ``ticker`` from grouped-daily snapshots
    and classify the NO_FILL cause. Pure: ``grouped_by_session`` is already loaded."""
    window_lows_highs: list[tuple[float, float] | None] = []
    first_session_open: float | None = None
    for i, session in enumerate(window_sessions):
        lho = _bar_low_high_open(grouped_by_session.get(session), ticker)
        if lho is None:
            window_lows_highs.append(None)
            continue
        low, high, open_ = lho
        window_lows_highs.append((low, high))
        if i == 0:
            first_session_open = open_

    tail_lows: list[float] = []
    for session in tail_sessions:
        lho = _bar_low_high_open(grouped_by_session.get(session), ticker)
        if lho is not None:
            tail_lows.append(lho[0])
    tail_min_low = min(tail_lows) if tail_lows else None

    return reconstruct(
        tiers=tiers,
        stop=stop,
        reference_close=reference_close,
        window_lows_highs=window_lows_highs,
        first_session_open=first_session_open,
        tail_min_low=tail_min_low,
        touch_eps=touch_eps,
        gap_up_margin=gap_up_margin,
    )
