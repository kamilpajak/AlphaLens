"""The bar walk: what a document would have done against real bars.

ENGINE module: stdlib, ``broker_contract`` and this package only.

One pass over the bars, in the per-bar order spec section 4.4 fixes. Nothing
here is configurable: the order is one declared decision rule, named in the
spec, and not a switch.

What the rule does NOT claim is as load-bearing as what it does. It is
pessimistic only where a worse resolution is well defined for the CURRENT
period; it is not a bound on the run, and for a laddered document a unique
worst case need not exist at all (section 4.4 carries the citations). Where the
rule simply decides, ``snu_bars`` counts the bar and the summary says so.
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

__all__ = ["WalkResult", "resolve_ladder", "walk"]

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

# The stated entry-trail distance is in basis points of the arming reference.
_BPS_PER_UNIT = 10_000


def _trigger(trough: float, distance: float) -> float:
    """The level a trailing buy fires at: ADDITIVE, because the wire field is a
    price distance computed once and not a fraction of the running low.

    Named, so the quantity has one expression. The classification path and the
    fill path each derived it separately, and two expressions for one number is
    the defect this module has already paid for once.
    """
    return trough + distance


@dataclass(slots=True)
class _ArmedTrail:
    """One rung's native trailing order, as the BROKER holds it.

    ``distance`` is ABSOLUTE and frozen when the order is placed: the wire
    field is a price distance to the market, computed once from the arming
    reference, so the trigger the server ratchets is ``trough + distance`` and
    not ``trough x (1 + d)``. The two coincide at the touch and separate as the
    trough falls - by 21 bps at a 30% drawdown on a 50 bps distance.

    ``trough`` is the running low the trigger follows, and it only ever falls.
    """

    distance: float
    trough: float

    @property
    def level(self) -> float:
        return _trigger(self.trough, self.distance)


@dataclass(frozen=True, slots=True)
class WalkResult:
    """What the walk knows. MEASURES are not here: the envelope derives them
    from the trace, so the summary and the trace cannot disagree."""

    events: tuple[TraceEvent, ...]
    outcome: str
    snu_bars: int
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
    # Rungs whose trailing order rests at the broker, by tier. A rung stays in
    # ``pending`` as well: it is still unfilled, so the deadline must expire it
    # with the others (section 4.6 publishes one cause, and this is it).
    armed: dict[int, _ArmedTrail] = field(default_factory=dict)
    # Rungs the depth rule retired before they could arm. They stay in
    # ``pending`` so the deadline still accounts for them.
    barred: set[int] = field(default_factory=set)
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
    snu: int = 0
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


def _reachable(state: _WalkState, bar: Bar, rung: PendingEntry) -> bool:
    """Whether this bar fills ``rung`` at all.

    A rung at or below the resting stop is SKIPPED, not cancelled: the price
    cannot reach it without passing the stop, so a fill would book a purchase at
    a price this bar has already sold at. At equality, reaching the rung IS
    reaching the stop. The rung stays pending, because the re-anchor arm can put
    the stop back below it.
    """
    if state.stop is not None and rung.limit_price <= state.stop:
        return False
    return bar.low <= rung.limit_price


def _book_entry(
    state: _WalkState, bar: Bar, plan: Plan, index: int, rung: PendingEntry, *, price: float
) -> None:
    """Book one rung's purchase and place the stop if this is the first fill.

    The quantity comes from the rung's LIMIT, never from the price paid: the
    order is composed once, as in the drain, and a trailing fire changes when
    and at what price it executes, not how much it buys.
    """
    units = rung.notional / rung.limit_price
    del state.pending[index]
    state.armed.pop(index, None)
    state.units += units
    state.cash += units * price
    state.committed += rung.notional
    state.events.append(
        EntryFilled(t=bar.t, tier_index=index, price=price, units=units, cash=units * price)
    )
    if state.stop is None:
        state.stop = plan.declared_floor
        state.events.append(StopPlaced(t=bar.t, level=plan.declared_floor))


@dataclass(frozen=True, slots=True)
class _Prospect:
    """What a rung's trailing order would do on one bar, computed WITHOUT doing
    it, so the classification in :func:`_staged` and the fill in
    :func:`_advance_trails` answer with one function. Deriving the level twice
    is the defect this repo has already paid for once."""

    trough: float
    distance: float
    arming: bool

    @property
    def level(self) -> float:
        return _trigger(self.trough, self.distance)


def _next_listed_limits(plan: Plan) -> dict[int, float]:
    """Each rung's NEXT-LISTED sibling limit, by tier index.

    LISTED, not cheapest: ``validate_intent`` does not require the entry ladder
    to descend, and the live loop reads ``tiers[index + 1]``, so the rung with
    nowhere to hand a deeper move is the one listed last rather than the one
    priced lowest.
    """
    entries = plan.entries
    return {
        rung.tier_index: entries[position + 1].limit_price
        for position, rung in enumerate(entries[:-1])
    }


def _trail_snu(bar: Bar, trail: _ArmedTrail, *, level: float, arming: bool) -> bool:
    """Does this bar's high/low ORDER change the money for an armed rung?

    Section 4.4's criterion, applied to row 3: both readings must be consistent
    with the bar and lead to different money.

    * A bar that opens through the trigger of an order ALREADY RESTING is
      forced - the open is the first print and both readings fill there, so
      nothing is assumed. The same open settles nothing on the ARMING bar,
      because the order is not there yet: it is placed when the price reaches
      the rung, which an open above the trigger has not done. That case is
      reachable whenever the open is more than one distance above the rung.
    * A bar whose low cannot fall below the trough it already carries leaves the
      trigger where it is, so both readings test the same level.
    * Otherwise the declared reading tests ``level`` and "the low came first"
      tests ``low + distance``, which is lower. Either they fire at two prices
      or only the second fires, and both are different money.
    """
    if not arming and bar.open >= level:
        return False
    if bar.low >= trail.trough:
        return False
    return bar.high >= bar.low + trail.distance


def _hands_on_to_the_next_rung(bar: Bar, rung: PendingEntry, next_limits: dict[int, float]) -> bool:
    """The live depth rule: the bar's FIRST price is already below the rung
    listed after this one, so the move is that rung's job."""
    next_limit = next_limits.get(rung.tier_index)
    return next_limit is not None and bar.open < next_limit


def _closed_at_the_first_print(state: _WalkState, bar: Bar) -> bool:
    """The open is the first print. If it is already through the resting stop the
    position closed there, and a purchase after that is a RE-ENTRY - a second
    position this tool does not model (section 4.4).

    Continuity settles it rather than a convention, and it is why the test is the
    bar's OPEN and not its low: a bar that opens ABOVE the stop reached the
    trigger first, so the fill stands and the stop takes it out afterwards, which
    is the worse resolution and the one section 4.4 keeps.
    """
    return state.stop is not None and bar.open <= state.stop


def _prospect(
    state: _WalkState,
    bar: Bar,
    rung: PendingEntry,
    index: int,
    *,
    bps: int,
    next_limits: dict[int, float],
) -> _Prospect | None:
    """This rung's trailing order on this bar, or ``None`` if it has none."""
    if index in state.barred or _closed_at_the_first_print(state, bar):
        return None
    trail = state.armed.get(index)
    if trail is not None:
        return _Prospect(trough=trail.trough, distance=trail.distance, arming=False)
    if not _reachable(state, bar, rung) or _hands_on_to_the_next_rung(bar, rung, next_limits):
        return None
    reference = min(bar.open, rung.limit_price)
    return _Prospect(trough=reference, distance=reference * bps / _BPS_PER_UNIT, arming=True)


def _trail_fill_price(bar: Bar, level: float, *, arming: bool) -> float:
    """What a fired trailing buy pays on this bar.

    The gap rule reaches the legs that REST at the broker, and on the arming bar
    this one does not: the order is placed at the touch, which cannot precede
    the open. So an arming bar takes the trigger however high it opened, and a
    later bar that opens through the trigger takes its open, the first print.
    """
    return level if arming else max(bar.open, level)


def _would_fill(
    state: _WalkState,
    bar: Bar,
    rung: PendingEntry,
    index: int,
    *,
    config: RunConfig,
    next_limits: dict[int, float],
) -> tuple[float, bool] | None:
    """``(price, in_question)`` if this bar fills the rung, else ``None``.

    ``in_question`` is section 4.4's fourth-situation test: does the fill land
    somewhere INSIDE the bar rather than at its first print. For a resting
    limit that is ``limit < open``; for a buy STOP the sign inverts, because a
    trigger at or below the open is the one already through at the first print.
    An arming bar is always in question - its order is placed at the touch,
    which cannot be the open.
    """
    if config.entry_trail_bps is None:
        if not _reachable(state, bar, rung):
            return None
        return min(bar.open, rung.limit_price), rung.limit_price < bar.open
    found = _prospect(state, bar, rung, index, bps=config.entry_trail_bps, next_limits=next_limits)
    if found is None or bar.high < found.level:
        return None
    price = _trail_fill_price(bar, found.level, arming=found.arming)
    return price, found.arming or found.level > bar.open


def _arm(
    state: _WalkState,
    bar: Bar,
    rung: PendingEntry,
    index: int,
    *,
    bps: int,
    next_limits: dict[int, float],
) -> _ArmedTrail | None:
    """Place this rung's trailing order, or decline and say nothing.

    The reference is ``min(open, limit)``: the touch happens at the first print
    when the bar gapped through the level, and at the level otherwise. The
    distance is frozen here, once, because the wire field is an absolute price
    distance and the server ratchets from it.

    Two ways to decline. A rung the resting stop puts out of reach is skipped
    and stays pending, exactly as a limit rung is. And a bar whose FIRST price
    is already below the next-listed rung hands the move to that rung: the live
    depth rule, which lives on this bar alone because the wire arms on the touch
    tick and an armed tier is terminal for the watcher.
    """
    found = _prospect(state, bar, rung, index, bps=bps, next_limits=next_limits)
    if found is None:
        if _reachable(state, bar, rung) and _hands_on_to_the_next_rung(bar, rung, next_limits):
            state.barred.add(index)
        return None
    trail = _ArmedTrail(distance=found.distance, trough=found.trough)
    state.armed[index] = trail
    return trail


def _advance_trails(
    state: _WalkState, bar: Bar, plan: Plan, *, bps: int, next_limits: dict[int, float]
) -> bool:
    """Arm, fire and ratchet the native entry trails, shallowest rung first.

    One order of operations, and section 4.4 row 3 fixes it: the trigger is
    tested against the trough as it stood BEFORE this bar, and only then does
    the bar's own low ratchet it down. The arming bar needs no special case -
    seeding the trough at the touch reference makes its trigger
    ``reference + distance``, which is the level the live geometry computes at
    the touch instant.

    That resolution is the worse one where a worse one is well defined: had the
    low come first, the trough would already have fallen and the buy would pay
    less.

    Returns whether any rung's high/low order changed the money on this bar.
    """
    # Asked for the whole bar, because the loop reads ``state.armed`` directly
    # for a rung already armed and never consults ``_prospect`` on that path.
    if _closed_at_the_first_print(state, bar):
        return False
    decided = False
    # A barred rung stays in ``pending`` so the deadline can account for it, and
    # it is ``_prospect`` that refuses it - the one function answering whether a
    # rung has an order on this bar. A second copy of that test here enforces
    # nothing the first does not, and hides which one is load-bearing.
    for index in sorted(state.pending):
        rung = state.pending[index]
        trail = state.armed.get(index)
        arming = trail is None
        if trail is None:
            trail = _arm(state, bar, rung, index, bps=bps, next_limits=next_limits)
            if trail is None:
                continue
        level = trail.level
        # Asked BEFORE the ratchet, because it is about the trough this bar
        # INHERITED - on the arming bar, the touch reference itself.
        decided = _trail_snu(bar, trail, level=level, arming=arming) or decided
        if bar.high >= level:
            price = _trail_fill_price(bar, level, arming=arming)
            _book_entry(state, bar, plan, index, rung, price=price)
            continue
        trail.trough = min(trail.trough, bar.low)
    return decided


def _fill_entries(
    state: _WalkState,
    bar: Bar,
    plan: Plan,
    config: RunConfig,
    *,
    next_limits: dict[int, float],
) -> bool:
    """Every rung the bar reached, shallowest first.

    Without a stated trail distance the fill price is ``min(open, limit)``: a
    bar that opens already through a resting limit order fills at the open,
    because the open is the first trade and nothing about that is in question.

    With one, nothing rests at the rung and :func:`_advance_trails` decides.

    Returns whether a trailing rung's high/low order changed the money, for the
    same reason the fourth situation's flag is returned rather than applied:
    the resting stop runs after the fills and one bar is counted once.
    """
    if config.entry_trail_bps is not None:
        return _advance_trails(
            state, bar, plan, bps=config.entry_trail_bps, next_limits=next_limits
        )
    for index in sorted(state.pending):
        rung = state.pending[index]
        if not _reachable(state, bar, rung):
            continue
        _book_entry(state, bar, plan, index, rung, price=min(bar.open, rung.limit_price))
    return False


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
) -> bool:
    """The resting stop, if the bar reached it. The stop RESTS at the broker, so
    a bar that opens already through it executes at the open.

    Row 1 of the section 4.4 table: a bar that reached both the stop and a
    take-profit does not say which came first. The walk takes the stop - the
    conservative reading - and the bar is counted, so a reader of the summary
    knows the run answered a question the tape could not.

    Returns whether row 1 applies. It is REPORTED rather than counted here,
    because a bar can carry another of the three sources as well and a bar is
    counted once however many questions it raised.
    """
    if state.stop is None or bar.low > state.stop:
        return False
    row_one = _a_tranche_would_have_fired(state, bar, ladder=ladder, intended=intended, costs=costs)
    price = min(bar.open, state.stop)
    state.events.append(PositionClosed(t=bar.t, reason="stop", price=price, units=_held(state)))
    state.closed = "stop"
    return row_one


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

    Only ``_exit_on_stop`` asks, and only once a stop RESTS, which happens on
    the first fill. So something is always held here: ``units`` never shrinks,
    ``units_sold`` is clamped to what is held, and the bar on which the two
    meet ends the walk with ``tp_complete``. ``_clears`` may therefore divide
    by ``state.units`` without a guard of its own.
    """
    return any(
        bar.high >= tranche.price and _clears(state, tranche, intended=intended, costs=costs)
        for tranche in ladder
        if tranche.tranche_index not in state.fired
    )


def _staged(
    state: _WalkState,
    bar: Bar,
    *,
    config: RunConfig,
    next_limits: dict[int, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """The position this bar produces, in the two stages the SNU is about.

    A rung through at the FIRST print belongs to both readings and is not in
    question. The pair returned is therefore ``(cash, units)`` after those only,
    and then after the ones that fill somewhere INSIDE the bar as well. The
    difference between the two is the whole subject of section 4.4's fourth
    situation: it moves the held quantity, and it moves the average entry that
    every cost threshold is computed from.

    Which rungs are which is :func:`_would_fill`'s answer, not a second reading
    of the levels - under a trail the test inverts, because a buy STOP at or
    below the open is the one already through.
    """
    cash, units = state.cash, state.units
    shallow = (cash, units)
    for deep in (False, True):
        for index in sorted(state.pending):
            rung = state.pending[index]
            found = _would_fill(state, bar, rung, index, config=config, next_limits=next_limits)
            if found is None or found[1] is not deep:
                continue
            filled = rung.notional / rung.limit_price
            cash += filled * found[0]
            units += filled
        if not deep:
            shallow = (cash, units)
    return shallow, (cash, units)


def _deep_rung_snu(
    state: _WalkState,
    bar: Bar,
    *,
    ladder: tuple[DeclaredTranche, ...],
    intended: float,
    costs: Costs,
    config: RunConfig,
    next_limits: dict[int, float],
) -> bool:
    """Does this bar's rung-against-take-profit order change the MONEY?

    Section 4.4's fourth situation. A rung below the open fills somewhere inside
    the bar, and the tape does not say whether that came before or after the
    high reached a tranche. The walk does not resolve it - no resolution is a
    bound for a laddered document - so this predicate only decides whether to
    COUNT the bar, and it is evaluated BEFORE the fills, because afterwards the
    deep rung has left ``pending`` and the question cannot be asked.

    Two ways the order changes money, and the second is easy to miss. The
    cumulative clamp can bind in one reading and not the other, which is the
    appetite test at the end. And the cost GATE's own verdict can turn on the
    order, because the deep fill lowers the average entry and lowers every
    threshold with it - a tranche refused on the small position and afforded on
    the blended one fires in one reading only.

    One shape is knowingly NOT counted, and section 6.3 names it: a bar on which
    the position OPENS through a deep rung, with no shallower one to precede it.
    With nothing held the gate has no entry price to measure a tranche against,
    so the replay cannot say whether the tranche would have fired at all.
    """
    if not any(
        (found := _would_fill(state, bar, rung, index, config=config, next_limits=next_limits))
        is not None
        and found[1]
        for index, rung in state.pending.items()
    ):
        return False
    (cash_before, units_before), (cash_after, units_after) = _staged(
        state, bar, config=config, next_limits=next_limits
    )
    if units_before <= 0.0:
        return False
    held, held_after = units_before - state.units_sold, units_after - state.units_sold
    before_price, after_price = cash_before / units_before, cash_after / units_after
    appetite = 0.0
    for tranche in ladder:
        if tranche.tranche_index in state.fired or bar.high < tranche.price:
            continue
        before = clears_cost(
            price=tranche.price,
            entry_price=before_price,
            units=min(tranche.fraction * intended, held),
            costs=costs,
        )
        if before is not clears_cost(
            price=tranche.price,
            entry_price=after_price,
            units=min(tranche.fraction * intended, held_after),
            costs=costs,
        ):
            return True
        if not before:
            # ``_fire_tranches`` RETURNS on the first refusal, so nothing behind
            # this level fires in either reading and it cannot feed the appetite.
            break
        appetite += tranche.fraction
    # Inclusive, and the boundary is why: at exact equality the clamp does not
    # bind, but selling precisely what is held meets ``units_filled`` and ends
    # the walk, so the deep rung never fills. Equal units, different money.
    return appetite > 0.0 and appetite * intended >= held


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
    # One condition, because the other two cannot decide anything. The caller
    # breaks the loop before this runs whenever the position closed, and a
    # resting stop exists only after a fill, so "no units" never arrives
    # without "no stop". Measured: a raise in place of the closed arm leaves
    # the package suite green; a raise in place of the units arm turns 11
    # tests red, all of them with no stop resting either.
    if state.stop is None:
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


def resolve_ladder(plan: Plan) -> tuple[str, tuple[DeclaredTranche, ...]]:
    """Which take-profit ladder the run replays (section 5.1).

    A document that placed its own bracket is replayed against THAT instruction:
    one tranche for the whole position at the level it published. The author's
    own ladder does not fire, because it never reached the broker.

    PUBLIC because two `divergences` predicates of section 5.2 ask whether the
    RESOLVED ladder is empty, and `WalkResult.ladder` carries only the NAME
    (`tp_tranches` comes back for an empty tuple too). Re-deriving the rule in
    the envelope would put one quantity in two functions, which is the defect
    this repo has already paid for once: the day this rule changes, the
    published `divergences` list would quietly stop describing the run.
    """
    if plan.declared_take_profit is not None:
        single = DeclaredTranche(tranche_index=0, price=plan.declared_take_profit, fraction=1.0)
        return "initial_levels", (single,)
    return "tp_tranches", plan.declared_tranches


def _match_orders(
    state: _WalkState,
    bar: Bar,
    plan: Plan,
    config: RunConfig,
    *,
    ladder: tuple[DeclaredTranche, ...],
    intended: float,
) -> bool:
    """What the bar's own levels decide: the deadline, the entry fills, the
    excursion marks and the resting stop.

    Returns whether this bar is an SNU bar, from ANY of the three sources
    section 4.4 names: row 1 (the stop and a take-profit in one bar), the
    fourth situation (a rung filling inside the bar against a take-profit), and
    the trailing entry's own high/low order. One answer, because ``snu_bars``
    counts BARS and not questions.

    The count is applied by the caller, once, and BEFORE it asks whether the
    position closed - a bar the stop closed can still have been decided by one
    of the other two.
    """
    _expire(state, bar, deadline=config.entry_deadline.value)
    next_limits = _next_listed_limits(plan)
    # Asked BEFORE the fills, while the rung is still pending and the question
    # is still answerable.
    deep_snu = _deep_rung_snu(
        state,
        bar,
        ladder=ladder,
        intended=intended,
        costs=config.costs,
        config=config,
        next_limits=next_limits,
    )
    trail_snu = _fill_entries(state, bar, plan, config, next_limits=next_limits)
    _track_extremes(state, bar)
    row_one = _exit_on_stop(state, bar, ladder=ladder, intended=intended, costs=config.costs)
    return deep_snu or trail_snu or row_one


def _advance_state(
    state: _WalkState,
    bar: Bar,
    plan: Plan,
    config: RunConfig,
    *,
    ladder: tuple[DeclaredTranche, ...],
    ladder_name: str,
    intended: float,
    trails: bool,
) -> None:
    """What the position does once the bar's orders have been matched: the
    take-profit review, the replay's own horizon, and the protection pass.

    Each step can close the position, and every later step is skipped when one
    does - the walk models one position, not a re-entry.
    """
    _fire_tranches(
        state,
        bar,
        ladder=ladder,
        ladder_name=ladder_name,
        intended=intended,
        costs=config.costs,
    )
    if state.closed is not None:
        return
    _time_stop(state, bar, at=config.time_stop_t)
    if state.closed is not None:
        return
    _decide_stop(state, bar, plan, trails=trails)


def _walk_one_bar(
    state: _WalkState,
    bar: Bar,
    plan: Plan,
    config: RunConfig,
    *,
    ladder: tuple[DeclaredTranche, ...],
    ladder_name: str,
    intended: float,
    trails: bool,
) -> None:
    """One bar, in the order section 4.4 fixes: match this bar's orders, then
    advance the position. Returns as soon as the position closes, so the caller
    only has to ask whether it did.

    The two halves are separate functions because section 6.5 says so: the bar
    loop is the natural candidate to exceed the cognitive-complexity gate, and
    splitting it is a design decision rather than a rescue after a red run.
    """
    if _match_orders(state, bar, plan, config, ladder=ladder, intended=intended):
        # Once per BAR, and before the closed check: a bar the stop closed may
        # still have been decided by the fourth situation or by a trailing
        # entry, and neither of those needs a take-profit to exist.
        state.snu += 1
    if state.closed is not None:
        return
    _advance_state(
        state,
        bar,
        plan,
        config,
        ladder=ladder,
        ladder_name=ladder_name,
        intended=intended,
        trails=trails,
    )


def walk(plan: Plan, config: RunConfig, bars: tuple[Bar, ...]) -> WalkResult:
    """Walk ``bars`` from ``config.walk_start`` and report what happened."""
    state = _WalkState(pending={rung.tier_index: rung for rung in plan.entries})
    ladder_name, ladder = resolve_ladder(plan)
    # Resolved ONCE, with the function ``decide_stop`` itself routes on, so the
    # reason a trace event carries cannot disagree with the arm that produced it.
    trails = resolve_declared_policy(plan.reaction).trails
    intended = sum(rung.notional / rung.limit_price for rung in plan.entries)
    last_t: int | None = None
    # The close the horizon mark values an open position at. Set beside
    # ``last_t`` so a skipped bar can supply neither.
    last_close: float | None = None
    for bar in bars:
        if bar.t < config.walk_start.value:
            continue
        last_t = bar.t
        last_close = bar.close
        _walk_one_bar(
            state,
            bar,
            plan,
            config,
            ladder=ladder,
            ladder_name=ladder_name,
            intended=intended,
            trails=trails,
        )
        if state.closed is not None:
            break
    filled_any = state.units > 0.0
    if filled_any and state.closed is None and last_t is not None and last_close is not None:
        state.events.append(HorizonOpen(t=last_t, units=_held(state), price=last_close))
    return WalkResult(
        events=tuple(state.events),
        outcome=_outcome(state, filled_any=filled_any),
        snu_bars=state.snu,
        ladder=ladder_name,
        intended_units=intended,
        units_filled=state.units,
        notional_spent=state.cash,
        filled_fraction=state.committed / plan.notional if plan.notional else 0.0,
        avg_entry_price=state.cash / state.units if filled_any else None,
        peak_price=state.peak,
        trough_price=state.trough,
    )
