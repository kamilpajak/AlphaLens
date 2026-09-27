"""The bar walk: what a document would have done against real bars.

ENGINE module: stdlib, ``broker_contract`` and this package only.

One pass over the bars, in the per-bar order spec section 4.4 fixes. Nothing
here is configurable: the intra-bar tie convention is named in the spec and is
not a switch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from broker_contract.exit_geometry.registry import resolve_declared_policy
from broker_contract.stop_decision import StopDecisionView, decide_stop

from intent_replay.bars import Bar
from intent_replay.config import Costs, RunConfig
from intent_replay.cost_gate import clears_cost
from intent_replay.interpreter import DeclaredTranche, PendingEntry, Plan
from intent_replay.trace import (
    EntryExpired,
    EntryFilled,
    HorizonOpen,
    PositionClosed,
    StopMoved,
    StopPlaced,
    TpFired,
    TraceEvent,
)

__all__ = ["WalkResult", "walk"]

# Which outcome a close reason produces. One mapping so the two words cannot
# drift apart in separate branches.
_OUTCOME_OF_CLOSE = {
    "stop": "closed_stop",
    "tp_complete": "closed_tp",
    "time_stop": "closed_time_stop",
}

# How close the units sold must come to the units held for the position to count
# as closed. RELATIVE, not absolute: three fractions of one third leave one unit
# in the last place behind, which is 1.49e-08 units on a ladder of 7.35e07 and
# would read as an open position under an absolute 1e-09. A ladder that
# deliberately leaves a runner sits at 0.30 of the position, seven orders of
# magnitude away, so nothing is closed by accident.
_DUST_REL_TOL = 1e-9


@dataclass(frozen=True, slots=True)
class WalkResult:
    """What the walk knows. MEASURES are not here: the envelope derives them
    from the trace, so the summary and the trace cannot disagree."""

    events: tuple[TraceEvent, ...]
    outcome: str
    ambiguous_bars: int
    ladder: str
    intended_units: float
    units_filled: float
    notional_spent: float
    filled_fraction: float
    avg_entry_price: float | None
    peak_price: float | None
    trough_price: float | None


def _held(state: _WalkState) -> float:
    """Units still in the position: everything filled, less everything a
    take-profit tranche has already sold."""
    return state.units - state.units_sold


def _outcome(state: _WalkState, *, filled_any: bool) -> str:
    if state.closed is not None:
        return _OUTCOME_OF_CLOSE[state.closed]
    return "open" if filled_any else "no_fill"


@dataclass(slots=True)
class _WalkState:
    pending: dict[int, PendingEntry]
    units: float = 0.0
    units_sold: float = 0.0
    fired: set[int] = field(default_factory=set)
    cash: float = 0.0
    committed: float = 0.0
    stop: float | None = None
    peak: float | None = None
    trough: float | None = None
    last_trailed_level: float | None = None
    latched_avg: float | None = None
    ambiguous: int = 0
    closed: str | None = None
    events: list[TraceEvent] = field(default_factory=list)


def _expire(state: _WalkState, bar: Bar, *, deadline: int) -> None:
    """The entry ladder's deadline, checked before the bar is matched.

    The comparison is NOT strict, matching `/edge`, which blocks a fill at
    ``ts >= entry_expiry_ms``: a bar stamped exactly at the deadline is already
    outside the ladder's life. One event carries every rung still waiting,
    because they died of one cause at one instant - and ``pending`` is EMPTIED,
    so a later bar reaching those levels fills nothing and skips nothing.
    """
    if bar.t < deadline or not state.pending:
        return
    tiers = tuple(sorted(state.pending))
    state.pending.clear()
    state.events.append(EntryExpired(t=bar.t, tiers=tiers, reason="deadline"))


def _time_stop(state: _WalkState, bar: Bar, *, at: int | None) -> None:
    """The replay's own horizon, at the bar's CLOSE.

    It is a research construction: no live venue here carries a time stop, so it
    cannot have traded intra-bar and it loses to everything that did - the
    resting stop and a completed take-profit both run before it. Firing on the
    very bar that filled IS allowed: a document with a one-bar horizon meant it.
    """
    if at is None or bar.t < at or _held(state) <= 0.0:
        return
    state.events.append(
        PositionClosed(t=bar.t, reason="time_stop", price=bar.close, units=_held(state))
    )
    state.closed = "time_stop"


def _fill_entries(state: _WalkState, bar: Bar, plan: Plan) -> None:
    """Every rung the bar reached, shallowest first.

    The fill price is ``min(open, limit)``: a bar that opens already through a
    resting limit order fills at the open, because the open is the first trade
    and there is nothing ambiguous about it.
    """
    for index in sorted(state.pending):
        rung = state.pending[index]
        # A rung at or below the resting stop is SKIPPED, not cancelled: the
        # price cannot reach it without passing the stop, so a fill would book a
        # purchase at a price this bar has already sold at. At equality,
        # reaching the rung IS reaching the stop. The rung stays pending,
        # because the re-anchor arm can put the stop back below it.
        if state.stop is not None and rung.limit_price <= state.stop:
            continue
        if bar.low > rung.limit_price:
            continue
        price = min(bar.open, rung.limit_price)
        units = rung.notional / rung.limit_price
        del state.pending[index]
        state.units += units
        state.cash += units * price
        state.committed += rung.notional
        state.events.append(
            EntryFilled(t=bar.t, tier_index=index, price=price, units=units, cash=units * price)
        )
        if state.stop is None:
            state.stop = plan.declared_floor
            state.events.append(StopPlaced(t=bar.t, level=plan.declared_floor))


def _track_extremes(state: _WalkState, bar: Bar) -> None:
    """The high-water and low-water marks since the first fill, this bar
    included — the position was live during it, so its extremes belong to the
    excursion even when this is the bar that closes the position."""
    if state.units <= 0.0:
        return
    state.peak = bar.high if state.peak is None else max(state.peak, bar.high)
    state.trough = bar.low if state.trough is None else min(state.trough, bar.low)


def _exit_on_stop(
    state: _WalkState,
    bar: Bar,
    *,
    ladder: tuple[DeclaredTranche, ...],
    intended: float,
    costs: Costs,
) -> None:
    """The resting stop, if the bar reached it. The stop RESTS at the broker, so
    a bar that opens already through it executes at the open.

    Row 1 of the section 4.4 table: a bar that reached both the stop and a
    take-profit does not say which came first. The walk takes the stop - the
    conservative reading - and counts the bar, so a reader of the summary knows
    the run answered a question the tape could not.
    """
    if state.stop is None or bar.low > state.stop:
        return
    if _a_tranche_would_have_fired(state, bar, ladder=ladder, intended=intended, costs=costs):
        state.ambiguous += 1
    price = min(bar.open, state.stop)
    state.events.append(PositionClosed(t=bar.t, reason="stop", price=price, units=_held(state)))
    state.closed = "stop"


def _a_tranche_would_have_fired(
    state: _WalkState,
    bar: Bar,
    *,
    ladder: tuple[DeclaredTranche, ...],
    intended: float,
    costs: Costs,
) -> bool:
    """Whether ANY tranche still unfired was both reached and affordable.

    ANY, not the shallowest: the schema does not order the ladder
    (``_tp_violations`` checks positive percentages, the one-sided sum,
    duplicate prices and above-blend, and no ordering), and ``/edge`` asks the
    same question of any unhit tranche. A tranche the cost gate would decline
    could not have fired first either way, so a bar carrying only those decided
    nothing and is not counted.
    """
    if _held(state) <= 0.0:
        return False
    return any(
        bar.high >= tranche.price and _clears(state, tranche, intended=intended, costs=costs)
        for tranche in ladder
        if tranche.tranche_index not in state.fired
    )


def _clears(state: _WalkState, tranche: DeclaredTranche, *, intended: float, costs: Costs) -> bool:
    return clears_cost(
        price=tranche.price,
        entry_price=state.cash / state.units,
        units=min(tranche.fraction * intended, _held(state)),
        costs=costs,
    )


def _fire_tranches(
    state: _WalkState,
    bar: Bar,
    *,
    ladder: tuple[DeclaredTranche, ...],
    ladder_name: str,
    intended: float,
    costs: Costs,
) -> None:
    """Every unfired tranche the bar reached, in ladder order.

    An untouched tranche is SKIPPED and the review continues, exactly as the
    daemon's ``plan_tranche_exits`` does; the only early exit is a cost-gate
    refusal, because every tranche behind a level too cheap to sell at is
    cheaper still. Nothing here assumes the ladder rises.

    The quantity is a fraction of the INTENDED ladder, as in the daemon, and the
    clamp to what is actually held runs CUMULATIVELY down the tranches, so a
    half-filled entry ladder cannot sell more than it bought.

    A take-profit does not rest at the broker in this model, so the gap rule of
    section 4.4 does not reach it: it fills AT its level, and the distance
    between that and where a live fill would have landed is the
    ``take_profit_observation_time`` divergence the envelope reports.
    """
    # Nothing to sell and, more to the point, no entry price for the gate to
    # measure against until a rung has filled.
    if _held(state) <= 0.0:
        return
    for tranche in ladder:
        if tranche.tranche_index in state.fired or bar.high < tranche.price:
            continue
        units = min(tranche.fraction * intended, _held(state))
        if not _clears(state, tranche, intended=intended, costs=costs):
            return
        state.fired.add(tranche.tranche_index)
        state.units_sold += units
        state.events.append(
            TpFired(
                t=bar.t,
                tranche_index=tranche.tranche_index,
                price=tranche.price,
                units=units,
                proceeds=units * tranche.price,
                ladder=ladder_name,
            )
        )
        if math.isclose(state.units_sold, state.units, rel_tol=_DUST_REL_TOL):
            # A MARKER, not a sale: the cash came from the tranche above. Section
            # 4.6 publishes no `stop_filled` kind, so `position_closed` carries
            # the sale for every other reason - the asymmetry has to be written
            # down or the envelope counts this tranche twice.
            state.events.append(
                PositionClosed(t=bar.t, reason="tp_complete", price=tranche.price, units=0.0)
            )
            state.closed = "tp_complete"
            return


def _decide_stop(state: _WalkState, bar: Bar, plan: Plan, *, trails: bool) -> None:
    """The daemon's protection pass, at the end of the bar.

    ``last_price`` is the bar's HIGH, not its close. The minimum-distance clamp
    is anchored on that price, so the choice moves the level: a peak of 70.60
    with an average of 68.00 answers 69.56 at a last price of 70.60 and 67.365
    at a last price of 67.50. The daemon polls many times inside one bar and its
    ratchet keeps the best level any poll produced, and the best available in
    this bar is the one at its high — every lower price in the same bar yields a
    level the ratchet would refuse anyway.

    ``already_reanchored`` is the daemon's own predicate, computed here from the
    trace: the latch holds the average it fired on, so any fill that CHANGES the
    average releases it without a release step of its own.
    """
    if state.closed is not None or state.units <= 0.0 or state.stop is None:
        return
    average = state.cash / state.units
    level = decide_stop(
        StopDecisionView(
            avg_price=average,
            peak=state.peak,
            last_price=bar.high,
            plan_stop=plan.declared_floor,
            reaction=plan.reaction,
            has_sole_standalone_stop=True,
            amend_in_backoff=False,
            last_trailed_level=state.last_trailed_level,
            already_reanchored=state.latched_avg is not None and state.latched_avg == average,
        )
    )
    # Any level DIFFERENT from the one resting, in either direction. Monotonicity
    # is a property of the clamp and the ratchet inside the decision, not a rule
    # of the walk: a never-down rule here would ship a policy no deployment has.
    if level is None or level == state.stop:
        return
    state.events.append(
        StopMoved(
            t=bar.t,
            reason="trail" if trails else "reanchor-on-fill",
            before=state.stop,
            after=level,
        )
    )
    state.stop = level
    if trails:
        state.last_trailed_level = level
    else:
        state.latched_avg = average


def _resolve_ladder(plan: Plan) -> tuple[str, tuple[DeclaredTranche, ...]]:
    """Which take-profit ladder the run replays (section 5.1).

    A document that placed its own bracket is replayed against THAT instruction:
    one tranche for the whole position at the level it published. The author's
    own ladder does not fire, because it never reached the broker.
    """
    if plan.declared_take_profit is not None:
        single = DeclaredTranche(tranche_index=0, price=plan.declared_take_profit, fraction=1.0)
        return "initial_levels", (single,)
    return "tp_tranches", plan.declared_tranches


def walk(plan: Plan, config: RunConfig, bars: tuple[Bar, ...]) -> WalkResult:
    """Walk ``bars`` from ``config.walk_start`` and report what happened."""
    state = _WalkState(pending={rung.tier_index: rung for rung in plan.entries})
    ladder_name, ladder = _resolve_ladder(plan)
    # Resolved ONCE, with the function ``decide_stop`` itself routes on, so the
    # reason a trace event carries cannot disagree with the arm that produced it.
    trails = resolve_declared_policy(plan.reaction).trails
    intended = sum(rung.notional / rung.limit_price for rung in plan.entries)
    last_t: int | None = None
    for bar in bars:
        if bar.t < config.walk_start.value:
            continue
        last_t = bar.t
        _expire(state, bar, deadline=config.entry_deadline.value)
        _fill_entries(state, bar, plan)
        _track_extremes(state, bar)
        _exit_on_stop(state, bar, ladder=ladder, intended=intended, costs=config.costs)
        if state.closed is None:
            _fire_tranches(
                state,
                bar,
                ladder=ladder,
                ladder_name=ladder_name,
                intended=intended,
                costs=config.costs,
            )
        if state.closed is None:
            _time_stop(state, bar, at=config.time_stop_t)
        if state.closed is not None:
            break
        _decide_stop(state, bar, plan, trails=trails)
    filled_any = state.units > 0.0
    if filled_any and state.closed is None and last_t is not None:
        state.events.append(HorizonOpen(t=last_t, units=_held(state)))
    return WalkResult(
        events=tuple(state.events),
        outcome=_outcome(state, filled_any=filled_any),
        ambiguous_bars=state.ambiguous,
        ladder=ladder_name,
        intended_units=intended,
        units_filled=state.units,
        notional_spent=state.cash,
        filled_fraction=state.committed / plan.notional if plan.notional else 0.0,
        avg_entry_price=state.cash / state.units if filled_any else None,
        peak_price=state.peak,
        trough_price=state.trough,
    )
