"""The bar walk (spec sections 4.4, 4.6, 5.1 and 6.3).

Fixtures are built from the PUBLISHED template `pullback-trailing-stop.json`
wherever a number is load-bearing: rungs 68.00 and 66.50 carrying 60% and 40%
of a 1500 budget, a disaster stop of 63.00, and `trailing_stop` with
`arm_trigger_r` 0.5 and `trail_frac` 0.6. The configuration is the section 5.2
block `test_config.CANONICAL`, so no value here is invented.
"""

from __future__ import annotations

import copy
import math
import unittest
from typing import Any

from broker_contract.trade_intent.schema import ReanchorOnFill, TrailingStop
from intent_replay.bars import Bar
from intent_replay.config import RunConfig
from intent_replay.interpreter import DeclaredTranche, PendingEntry, Plan
from intent_replay.measures import summarise
from intent_replay.trace import KINDS
from intent_replay.walk import (
    _a_tranche_would_have_fired,
    _fire_tranches,
    _held,
    _WalkState,
    _would_fill,
    walk,
)

from tests.intent_replay.test_config import ACCOUNT_CURRENCY, CANONICAL, SAME_CURRENCY

WALK_START = 1_790_170_200_000
MINUTE = 60_000
DEADLINE = 1_791_230_400_000

# The published template's ladder: 60 / 40 of 1500.
RUNGS = (
    PendingEntry(tier_index=0, limit_price=68.0, notional=900.0),
    PendingEntry(tier_index=1, limit_price=66.5, notional=600.0),
)
FLOOR = 63.0

# The published template's declaration, and the re-anchor primitive the spec's
# section 6.3 measurement uses.
TRAIL = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)
REANCHOR = ReanchorOnFill(k_atr=1.5, atr=1.20)

# R = 68.00 - 63.00 = 5.00, so the trail arms at a peak of 70.50 and not before.
ARMED_PEAK = 70.6
TRAILED_LEVEL = 69.56


def _config(**overrides: Any) -> RunConfig:
    """The SAME-CURRENCY block (``test_config.SAME_CURRENCY``).

    The walk's own arithmetic is currency-blind: it divides a notional by a
    price and compares prices against prices. What a stated conversion changes
    is the cost gate's notional and the budget the interpreter splits, and this
    file builds its plans by hand rather than through the interpreter. So the
    same-currency block is the honest default here -- every number below
    predates #1592 and is unmoved by it, which is what makes a leaking rate or
    a leaking buffer visible rather than absorbed. The rows that exercise the
    conversion state it themselves (:func:`_cross_config`).
    """
    data = copy.deepcopy(dict(SAME_CURRENCY))
    data.update(overrides)
    return RunConfig.from_jsonable(data, account_currency=ACCOUNT_CURRENCY)


def _cross_config(**overrides: Any) -> RunConfig:
    """The CROSS-currency block of the section 5 example: a EUR budget on a USD
    instrument, with the rate, the round-trip cost rate and the sizing buffer
    the caller stated."""
    data = copy.deepcopy(dict(CANONICAL))
    data.update(overrides)
    return RunConfig.from_jsonable(data, account_currency=ACCOUNT_CURRENCY)


# The same ladder under a 1 per cent sizing buffer: the figures the interpreter
# produces from the published template once the conversion is stated. One per
# cent of 1500 is exact in binary, so these are literals and not near-misses.
BUFFERED_RUNGS = (
    PendingEntry(tier_index=0, limit_price=68.0, notional=891.0),
    PendingEntry(tier_index=1, limit_price=66.5, notional=594.0),
)
BUFFERED_TOTAL = 1485.0


def _plan(
    *,
    entries: tuple[PendingEntry, ...] = RUNGS,
    notional: float = 1500.0,
    sizing_notional: float | None = None,
    floor: float = FLOOR,
    take_profit: float | None = None,
    tranches: tuple[DeclaredTranche, ...] = (),
    reaction: Any = None,
) -> Plan:
    """``sizing_notional`` defaults to ``notional`` — the same-currency case,
    where no buffer applies and the ladder splits the stated budget."""
    return Plan(
        entries=entries,
        notional=notional,
        sizing_notional=notional if sizing_notional is None else sizing_notional,
        account_currency=ACCOUNT_CURRENCY,
        declared_floor=floor,
        declared_stop=None,
        declared_take_profit=take_profit,
        declared_tranches=tranches,
        reaction=reaction,
        read=frozenset(),
    )


def _bar(t: int, open_: float, high: float, low: float, close: float | None = None) -> Bar:
    return Bar(t=t, open=open_, high=high, low=low, close=close if close is not None else open_)


def _kinds(result: Any) -> list[str]:
    return [event.kind for event in result.events]


class NothingFillsTest(unittest.TestCase):
    def test_bars_that_touch_no_rung_report_no_fill(self) -> None:
        result = walk(_plan(), _config(), (_bar(WALK_START, 70.0, 71.0, 69.0),))
        self.assertEqual(result.outcome, "no_fill")
        self.assertEqual(result.units_filled, 0.0)
        self.assertEqual(result.notional_spent, 0.0)
        self.assertEqual(result.filled_fraction, 0.0)
        self.assertIsNone(result.avg_entry_price)
        self.assertEqual(_kinds(result), [])

    def test_a_bar_before_walk_start_is_not_walked(self) -> None:
        # The ladder was not live yet; a fill there would describe a session the
        # caller did not ask about.
        result = walk(
            _plan(),
            _config(),
            (_bar(WALK_START - MINUTE, 68.0, 68.0, 60.0), _bar(WALK_START, 70.0, 71.0, 69.0)),
        )
        self.assertEqual(result.outcome, "no_fill")
        self.assertEqual(_kinds(result), [])


class EntriesFillTest(unittest.TestCase):
    def test_a_touched_rung_fills_at_its_limit_and_places_the_stop(self) -> None:
        result = walk(_plan(), _config(), (_bar(WALK_START, 68.5, 68.6, 67.9),))
        self.assertEqual(_kinds(result), ["entry_filled", "stop_placed", "horizon_open"])
        filled = result.events[0]
        self.assertEqual(filled.tier_index, 0)
        self.assertEqual(filled.price, 68.0)
        self.assertEqual(filled.units, 900.0 / 68.0)
        self.assertEqual(filled.cash, 900.0)
        self.assertEqual(result.events[1].level, FLOOR)

    def test_the_units_are_the_budget_divided_by_the_limit(self) -> None:
        # The quantity is fixed when the order is COMPOSED, as in the drain, not
        # recomputed from the fill price.
        result = walk(_plan(), _config(), (_bar(WALK_START, 60.0, 68.6, 60.0),))
        self.assertEqual(result.units_filled, 900.0 / 68.0 + 600.0 / 66.5)
        self.assertEqual(result.intended_units, 900.0 / 68.0 + 600.0 / 66.5)

    def test_a_bar_that_opens_below_a_rung_fills_at_the_open(self) -> None:
        # A resting limit order fills at the first trade when the bar gaps
        # through it. The open is that trade (spec section 4.4).
        result = walk(_plan(entries=RUNGS[:1]), _config(), (_bar(WALK_START, 65.0, 66.0, 64.0),))
        self.assertEqual(result.events[0].price, 65.0)
        self.assertEqual(result.events[0].units, 900.0 / 68.0)
        self.assertEqual(result.events[0].cash, 900.0 / 68.0 * 65.0)

    def test_the_filled_fraction_is_allocation_over_budget(self) -> None:
        # NOT cash over budget: a gap fill spends less than the rung's share and
        # the ladder is still 60% filled. `ladder_replay._filled_frac` already
        # defines the name this way.
        # Open 67.00 gaps through the 68.00 rung and stays above the 66.50 one,
        # so 60% of the ladder filled while the cash spent is 886.76, not 900.
        result = walk(_plan(), _config(), (_bar(WALK_START, 67.0, 68.2, 66.6),))
        self.assertEqual(result.filled_fraction, 0.6)
        self.assertEqual(result.notional_spent, 900.0 / 68.0 * 67.0)

    def test_the_position_left_open_reports_the_horizon(self) -> None:
        result = walk(_plan(entries=RUNGS[:1]), _config(), (_bar(WALK_START, 68.0, 68.6, 67.9),))
        self.assertEqual(result.outcome, "open")
        self.assertEqual(result.events[-1].kind, "horizon_open")
        self.assertEqual(result.events[-1].units, 900.0 / 68.0)

    def test_the_horizon_mark_is_the_close_of_the_last_bar_the_walk_saw(self) -> None:
        # Section 4.6: the mark values the units at the LAST bar's close, which
        # is not the bar's open and not the last fill price.
        bars = (
            _bar(WALK_START, 68.0, 68.6, 67.9, close=68.4),
            _bar(WALK_START + MINUTE, 68.5, 69.0, 68.2, close=68.9),
        )
        result = walk(_plan(entries=RUNGS[:1]), _config(), bars)
        self.assertEqual(result.events[-1].kind, "horizon_open")
        self.assertEqual(result.events[-1].price, 68.9)

    def test_a_bar_before_walk_start_does_not_supply_the_horizon_mark(self) -> None:
        # The skipped bar is not one the walk saw, so its close must not become
        # the valuation of an open position.
        bars = (
            _bar(WALK_START - MINUTE, 50.0, 50.0, 50.0, close=50.0),
            _bar(WALK_START, 68.0, 68.6, 67.9, close=68.4),
        )
        result = walk(_plan(entries=RUNGS[:1]), _config(), bars)
        self.assertEqual(result.events[-1].price, 68.4)


class EntryTrailTest(unittest.TestCase):
    """The native entry trail (spec sections 3.3, 4.4 row 3 and 8).

    With a stated distance a rung no longer rests as a limit. The first bar to
    touch it ARMS a trailing trigger that ratchets down with the running low
    and fires on a rebound of that distance. What is modelled is the BROKER's
    order type: the server owns the ratchet and the fire, so there is no local
    implementation to compare against and the run says so through the
    ``native_entry_trail_is_a_broker_model`` divergence.

    ``d`` is 50 bps throughout, so a limit of 68.00 arms at a reference of
    68.00 and fires at 68.34.
    """

    LIMIT = RUNGS[0].limit_price
    BUDGET = RUNGS[0].notional

    def _walk(self, *bars: Bar, bps: int = 50) -> Any:
        plan = _plan(entries=RUNGS[:1], notional=self.BUDGET)
        return walk(plan, _config(entry_trail_bps=bps), bars)

    def test_a_touched_rung_no_longer_fills_at_its_limit(self) -> None:
        # The bar reaches 68.00 and rebounds to 68.20, which is short of the
        # 68.34 trigger. As a resting limit this rung fills at 68.00; as a
        # trail it arms and waits.
        result = self._walk(_bar(WALK_START, 68.05, 68.2, 67.9))
        self.assertEqual(_kinds(result), [])
        self.assertEqual(result.outcome, "no_fill")

    def test_the_arming_bar_fires_when_the_rebound_reaches_the_trigger(self) -> None:
        # Section 4.4 keeps the worse resolution where one is well defined, and
        # on the arming bar it is: the trigger computed from the touch
        # reference, before the bar's own low could ratchet it down. Live the
        # geometry is computed AT the touch and the order rests from that
        # instant, so the same bar can fire it - every recorded live fire
        # happened in the session of its touch.
        result = self._walk(_bar(WALK_START, 68.05, 68.4, 67.9))
        self.assertEqual(_kinds(result), ["entry_filled", "stop_placed", "horizon_open"])
        filled = result.events[0]
        self.assertEqual(filled.price, 68.34)
        self.assertEqual(filled.units, self.BUDGET / self.LIMIT)
        self.assertEqual(filled.cash, self.BUDGET / self.LIMIT * 68.34)
        self.assertEqual(result.events[1].level, FLOOR)

    def test_a_bar_that_gaps_below_the_rung_arms_on_its_OPEN(self) -> None:
        # The touch happens at the first print, so the reference is the open and
        # not the level. Measured on 20 sessions of real daily bars: at a 1-2%
        # pullback 23-33% of arming bars open below their rung, with a median
        # gap of 86-131 bps - and the arming-bar trigger moves by the whole gap.
        # Referenced on the LIMIT this bar would fire at 68.34, which its high
        # never reaches, so the two readings differ by a fill.
        result = self._walk(_bar(WALK_START, 67.0, 67.5, 66.9))
        self.assertEqual(result.events[0].price, 67.0 * 1.005)
        self.assertEqual(result.events[0].units, self.BUDGET / self.LIMIT)

    def test_the_distance_is_frozen_and_ABSOLUTE_not_a_fraction_of_the_trough(self) -> None:
        # The wire field is a price distance to the market, computed once from
        # the arming reference, so the server's trigger is `trough + distance`.
        # The two forms coincide at the touch and separate as the trough falls:
        # at a trough of 60.00 the additive trigger is 60.34 and the
        # proportional one 60.30, so bar 3 fires under one reading and not the
        # other. Its high sits between them on purpose.
        bars = (
            _bar(WALK_START, 68.05, 68.2, 67.9),
            _bar(WALK_START + MINUTE, 67.0, 67.5, 60.0),
            _bar(WALK_START + 2 * MINUTE, 60.1, 60.32, 60.0),
            _bar(WALK_START + 3 * MINUTE, 60.1, 60.4, 60.0),
        )
        result = self._walk(*bars)
        filled = [event for event in result.events if event.kind == "entry_filled"]
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0].t, WALK_START + 3 * MINUTE)
        self.assertAlmostEqual(filled[0].price, 60.34, places=10)

    def test_a_LATER_bar_that_opens_above_the_trigger_fills_at_the_open(self) -> None:
        # By then the order RESTS at the broker, so the gap rule of section 4.4
        # reaches it: the open is the first print and nothing about that is in
        # question. Bar 1 arms and ratchets the trough to 67.90, leaving a
        # 68.24 trigger; bar 2 opens above it.
        bars = (
            _bar(WALK_START, 68.05, 68.2, 67.9),
            _bar(WALK_START + MINUTE, 68.5, 68.6, 68.3),
        )
        result = self._walk(*bars)
        filled = [event for event in result.events if event.kind == "entry_filled"]
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0].price, 68.5)

    def test_the_ARMING_bar_never_fills_at_its_open_however_high_that_is(self) -> None:
        # The gap rule reaches the legs that REST there, and on the arming bar
        # this one did not: it is placed at the touch, which cannot precede the
        # open. So a bar opening at 68.40, dipping to the 68.00 rung and
        # reaching 68.50 fires at the 68.34 trigger, not at 68.40 - the open is
        # a price the order was not yet there to take.
        result = self._walk(_bar(WALK_START, 68.4, 68.5, 67.9))
        self.assertEqual(result.events[0].price, 68.34)

    def test_a_bar_whose_low_could_have_fired_it_cheaper_is_counted(self) -> None:
        # Row 3 of section 4.4, and the criterion at :831 is that both orderings
        # are consistent with the bar and lead to different money. Here they do:
        # the declared reading tests the 68.34 trigger and does not fire, while
        # "the low came first" tests 67.50 + 0.34 = 67.84 and does. One fill
        # against none is the largest difference the rule can make.
        result = self._walk(_bar(WALK_START, 68.05, 68.1, 67.5))
        self.assertEqual(_kinds(result), [])
        self.assertEqual(result.snu_bars, 1)

    def test_a_bar_whose_low_IS_the_touch_decides_nothing(self) -> None:
        # With the low at the reference the trough cannot move inside the bar,
        # so the trigger is the same under either ordering - forced by the
        # levels, exactly the ground on which section 4.4 excludes row 2.
        result = self._walk(_bar(WALK_START, 68.0, 68.4, 68.0))
        self.assertEqual(result.events[0].price, 68.34)
        self.assertEqual(result.snu_bars, 0)

    def test_an_armed_bar_that_makes_a_new_low_and_retraces_is_counted(self) -> None:
        bars = (
            _bar(WALK_START, 68.05, 68.1, 67.9),
            _bar(WALK_START + MINUTE, 67.4, 67.5, 67.0),
        )
        result = self._walk(*bars)
        self.assertEqual(_kinds(result), [])
        self.assertEqual(result.snu_bars, 1)

    def test_a_gap_through_the_trigger_decides_nothing_even_on_a_new_low(self) -> None:
        # The gap row wins over the new-low row: the open is the first print, so
        # the fill is at the open under either ordering and the money is the
        # same. Counting it would put a bar where the rule changed nothing into
        # a number whose published meaning is how much came from the rule.
        bars = (
            _bar(WALK_START, 68.05, 68.1, 67.9),
            _bar(WALK_START + MINUTE, 68.3, 68.4, 67.0),
        )
        result = self._walk(*bars)
        self.assertEqual(result.events[0].price, 68.3)
        self.assertEqual(result.snu_bars, 0)

    def _laddered(self, *bars: Bar) -> Any:
        """Both published rungs, so a later one can arm while a stop rests."""
        return walk(_plan(), _config(entry_trail_bps=50), bars)

    def test_a_bar_that_opens_ABOVE_the_stop_fires_and_then_stops_out(self) -> None:
        # Section 4.4 keeps the worse resolution where one is well defined, and
        # a fill followed by a stop-out IS the worse one: without it there is no
        # loss. The open is 66.00, above the 63.00 stop, so the price reached the
        # 66.33 trigger before it reached the stop and both legs executed.
        bars = (
            _bar(WALK_START, 68.05, 68.4, 67.9),
            _bar(WALK_START + MINUTE, 66.0, 66.4, 62.0),
        )
        result = self._laddered(*bars)
        self.assertEqual(
            _kinds(result), ["entry_filled", "stop_placed", "entry_filled", "position_closed"]
        )
        self.assertEqual(result.events[0].price, 68.34)
        self.assertEqual(result.events[2].price, 66.0 * 1.005)
        self.assertEqual(result.events[3].price, FLOOR)

    def test_a_bar_that_opens_BELOW_the_stop_does_not_fire_the_trail(self) -> None:
        # Continuity settles this one rather than a convention: the open is
        # already through the stop, so the position closed at the first print and
        # a purchase after it is a RE-ENTRY - a second position this tool does
        # not model (section 4.4). The trigger being above the stop does not
        # help; nothing was there to buy for.
        bars = (
            _bar(WALK_START, 68.05, 68.4, 67.9),
            _bar(WALK_START + MINUTE, 62.5, 66.4, 62.0),
        )
        result = self._laddered(*bars)
        self.assertEqual(_kinds(result), ["entry_filled", "stop_placed", "position_closed"])
        self.assertEqual(result.events[2].price, 62.5)

    def test_a_bar_that_gaps_past_the_NEXT_rung_bars_the_shallower_one(self) -> None:
        # The live depth suspend, measured against the engine on 2026-09-29: it
        # fires only when the FIRST price at or below a rung is already below the
        # next one, because the wire arms on the touch tick and an armed tier is
        # terminal for the watcher. In bar terms that first price is the open.
        # A deeper move is the next rung's job; the shallower one never arms.
        result = self._laddered(_bar(WALK_START, 66.0, 68.0, 65.0))
        filled = [event for event in result.events if event.kind == "entry_filled"]
        self.assertEqual([event.tier_index for event in filled], [1])
        self.assertEqual(filled[0].price, 66.0 * 1.005)

    def test_the_same_depth_on_a_LATER_bar_does_not_bar_an_armed_rung(self) -> None:
        # Measured on the live engine: a price walking DOWN through the rung arms
        # on the touch and a later depth never suspends it, because the watcher
        # is already terminal. Barring it here would starve fills the deployed
        # path makes.
        bars = (
            _bar(WALK_START, 68.05, 68.1, 67.9),
            _bar(WALK_START + MINUTE, 67.0, 68.5, 65.0),
        )
        result = self._laddered(*bars)
        filled = [event for event in result.events if event.kind == "entry_filled"]
        self.assertIn(0, [event.tier_index for event in filled])

    def test_the_last_listed_rung_never_bars_itself(self) -> None:
        # There is no next rung to hand the move to. `validate_intent` does not
        # require the ladder to descend, so the rule is about the rung LISTED
        # last, not the cheapest one.
        result = self._walk(_bar(WALK_START, 60.0, 68.5, 59.0))
        self.assertEqual(result.events[0].price, 60.0 * 1.005)

    def test_an_arming_bar_whose_OPEN_is_above_the_trigger_still_counts(self) -> None:
        # The "opened through the trigger" guard belongs to a RESTING order: for
        # one, both readings fill at the open and nothing is assumed. On the
        # ARMING bar the order does not exist at the open - it is placed when the
        # price falls to the rung - so an open above the trigger settles nothing.
        # Here the reference is 68.00 and the trigger 68.34, the open is 68.40,
        # and the low-first reading would fill at 67.90 + 0.34 = 68.24 instead.
        result = self._walk(_bar(WALK_START, 68.4, 68.5, 67.9))
        self.assertEqual(result.events[0].price, 68.34)
        self.assertEqual(result.snu_bars, 1)

    def test_a_barred_rung_stays_barred_on_a_LATER_bar_that_would_arm_it(self) -> None:
        # "Never arms" is the whole content of the depth rule, and it is a claim
        # about every LATER bar, not about the bar that barred the rung. Without
        # this the rule was only tested on the bar it fired on, and the second
        # bar here is one that would otherwise arm at 68.00 and fire at 68.34.
        bars = (
            _bar(WALK_START, 66.0, 68.0, 65.0),
            _bar(WALK_START + MINUTE, 68.05, 68.5, 67.9),
        )
        result = self._laddered(*bars)
        filled = [event for event in result.events if event.kind == "entry_filled"]
        self.assertEqual([event.tier_index for event in filled], [1])

    def test_the_classifier_reports_no_fill_once_the_open_is_through_the_stop(self) -> None:
        """``_would_fill`` answers "does this bar fill the rung", and on a bar
        whose OPEN is already through the resting stop the answer is no: the
        position closed at the first print and buying after it is a re-entry.

        Asserted on the function rather than through a walk on purpose. Measured
        over 8000 random ladders, 2239 of them ending on the stop, teaching the
        classifier this changes neither ``snu_bars`` nor the cash - so no walk
        can carry the assertion, and without one the function would go on
        promising a fill that ``_advance_trails`` refuses to book.
        """
        state = _WalkState(pending={1: RUNGS[1]})
        state.stop = FLOOR
        state.units, state.cash = 13.17, 900.0
        bar = _bar(WALK_START, FLOOR - 0.5, 69.5, FLOOR - 1.0)
        answer = _would_fill(
            state, bar, RUNGS[1], 1, config=_config(entry_trail_bps=50), next_limits={}
        )
        self.assertIsNone(answer)

    def test_a_trail_SNU_survives_the_stop_closing_the_same_bar(self) -> None:
        """A bar can carry a trailing-entry SNU and then be closed by the stop.

        The count used to be dropped there, on the ground that row 1 had already
        asked the bar its question - but row 1 only counts a bar that also
        reached an affordable take-profit, and this document declares no ladder
        at all. Before the trail there was no SNU source without a tranche, so
        the premise held; the trail is the first one.

        The first bar arms rung 0 with its low AT the reference, so its trigger
        cannot move and it contributes nothing of its own. On the second bar
        rung 1 arms at 66.50 and fires at 66.8325, and the low then reaches the
        stop. Read low-first, the price passes the stop at 63.00 before the
        trigger, so that rung is never bought at all - the two readings differ
        over whether a tranche was purchased, which is as different as money
        gets.
        """
        bars = (
            _bar(WALK_START, 68.05, 68.4, 68.0),
            _bar(WALK_START + MINUTE, 66.6, 67.2, 62.9),
        )
        result = self._laddered(*bars)
        self.assertEqual(
            _kinds(result), ["entry_filled", "stop_placed", "entry_filled", "position_closed"]
        )
        self.assertEqual(result.events[2].price, 66.8325)
        self.assertEqual(result.events[3].price, FLOOR)
        self.assertEqual(result.snu_bars, 1)

    def test_the_fourth_situation_is_classified_by_the_TRAIL_not_by_the_limit(self) -> None:
        """Section 4.4's fourth situation asks whether a rung filled INSIDE the
        bar while a take-profit was also reached. For a resting limit the test is
        ``limit < open``; for a buy STOP the sign inverts, because a trigger at or
        below the open is the one already through at the first print.

        Reading it off the LIMIT under a trail counts the wrong bars. This ladder
        is one of three in six thousand random trailing runs where the two
        readings reach a different ``snu_bars``, found by search rather than by
        guessing a shape: the limit reading reports two bars and the trail
        reading one. The cash is asserted beside it because it must NOT move -
        the detector feeds ``snu_bars`` alone and can never change what was
        bought, which is why the count is the only thing that can catch it.

        The ladder was re-drawn on 2026-09-30: teaching ``_trail_snu`` about
        arming bars made the previous one report two bars under BOTH readings,
        so it no longer discriminated. A golden case that stops discriminating
        is worse than none, because it still passes.
        """
        rungs = (
            PendingEntry(tier_index=0, limit_price=45.8149, notional=1050.0),
            PendingEntry(tier_index=1, limit_price=44.8421, notional=450.0),
        )
        tranches = (
            DeclaredTranche(tranche_index=0, price=46.1709, fraction=0.333333),
            DeclaredTranche(tranche_index=1, price=46.045, fraction=0.333333),
            DeclaredTranche(tranche_index=2, price=47.2309, fraction=0.333333),
        )
        bars = (
            _bar(WALK_START, 45.9079, 45.9934, 44.8313, close=45.9542),
            _bar(WALK_START + MINUTE, 45.9542, 48.073, 44.3412, close=45.372),
            _bar(WALK_START + 2 * MINUTE, 45.372, 47.0921, 43.4173, close=45.0753),
            _bar(WALK_START + 3 * MINUTE, 45.0753, 47.1778, 43.8848, close=44.4752),
        )
        plan = _plan(entries=rungs, notional=1500.0, floor=42.5307, tranches=tranches)
        result = walk(plan, _config(entry_trail_bps=50), bars)
        self.assertEqual(result.snu_bars, 1)
        self.assertAlmostEqual(result.notional_spent, 1505.4425, places=4)

    def _to_deadline(self, *bars: Bar) -> Any:
        config = _config(entry_trail_bps=50, entry_deadline=_deadline(WALK_START + MINUTE))
        return walk(_plan(), config, bars)

    def test_an_armed_rung_expires_at_the_deadline_like_any_other(self) -> None:
        # A resting trailing order dies with the ladder's window. It is still an
        # unfilled rung, so it leaves through the one published cause rather
        # than through a second kind of event.
        result = self._to_deadline(
            _bar(WALK_START, 68.05, 68.1, 67.9),
            _bar(WALK_START + MINUTE, 68.0, 68.1, 67.95),
        )
        self.assertEqual(_kinds(result), ["entry_expired"])
        self.assertEqual(result.events[0].tiers, (0, 1))
        self.assertEqual(result.events[0].reason, "deadline")

    def test_a_barred_rung_expires_too_rather_than_vanishing(self) -> None:
        # The depth rule retires a rung without an event of its own, so the
        # deadline is where the trace accounts for it. Dropping it from
        # ``pending`` instead would leave a rung the trace never mentions.
        result = self._to_deadline(
            _bar(WALK_START, 66.0, 68.0, 65.0),
            _bar(WALK_START + MINUTE, 66.5, 66.6, 66.4),
        )
        expired = [event for event in result.events if event.kind == "entry_expired"]
        self.assertEqual([event.tiers for event in expired], [(0,)])


class RestingStopTest(unittest.TestCase):
    def test_the_resting_stop_closes_the_position_at_its_level(self) -> None:
        bars = (_bar(WALK_START, 68.0, 68.6, 67.9), _bar(WALK_START + MINUTE, 67.0, 67.2, 62.0))
        result = walk(_plan(entries=RUNGS[:1]), _config(), bars)
        self.assertEqual(result.outcome, "closed_stop")
        closed = result.events[-1]
        self.assertEqual(closed.kind, "position_closed")
        self.assertEqual(closed.reason, "stop")
        self.assertEqual(closed.price, FLOOR)
        self.assertEqual(closed.units, 900.0 / 68.0)

    def test_a_bar_that_opens_below_the_stop_exits_at_the_open(self) -> None:
        # The stop RESTS at the broker, so the gap rule reaches it: a stop at
        # 63.00 on a bar opening at 60.00 executes near 60.00, not at 63.00.
        bars = (_bar(WALK_START, 68.0, 68.6, 67.9), _bar(WALK_START + MINUTE, 60.0, 61.0, 59.0))
        result = walk(_plan(entries=RUNGS[:1]), _config(), bars)
        self.assertEqual(result.events[-1].price, 60.0)

    def test_a_rung_and_the_stop_in_one_bar_fill_first_then_stop_out(self) -> None:
        # validate_intent keeps the disaster stop below every rung, so reaching
        # the rung is on the way to the stop: the order is forced by the levels
        # and the bar is NOT counted as ambiguous.
        result = walk(_plan(entries=RUNGS[:1]), _config(), (_bar(WALK_START, 68.0, 68.2, 62.0),))
        self.assertEqual(_kinds(result), ["entry_filled", "stop_placed", "position_closed"])
        self.assertEqual(result.snu_bars, 0)

    def test_bars_after_the_close_are_not_walked(self) -> None:
        # A fill at or after the close is a RE-ENTRY, a second position this tool
        # does not model (spec section 4.4), so the walk ends at the close and a
        # further bar changes nothing.
        closing = (_bar(WALK_START, 68.0, 68.2, 67.9), _bar(WALK_START + MINUTE, 67.0, 67.2, 62.0))
        after = (*closing, _bar(WALK_START + 2 * MINUTE, 66.0, 70.0, 60.0))
        plan, config = _plan(), _config()
        self.assertEqual(walk(plan, config, after).events, walk(plan, config, closing).events)
        self.assertEqual(walk(plan, config, after).outcome, "closed_stop")

    def test_the_bar_that_reaches_the_stop_fills_the_deeper_rung_first(self) -> None:
        # Row 2 of the section 4.4 table: the disaster stop sits below every rung,
        # so a bar reaching the stop has already reached them. Without the fill
        # there would be no loss to report.
        bars = (_bar(WALK_START, 68.0, 68.2, 67.9), _bar(WALK_START + MINUTE, 67.0, 67.2, 62.0))
        result = walk(_plan(), _config(), bars)
        self.assertEqual(
            _kinds(result), ["entry_filled", "stop_placed", "entry_filled", "position_closed"]
        )
        self.assertEqual(result.units_filled, 900.0 / 68.0 + 600.0 / 66.5)
        self.assertEqual(result.events[-1].units, result.units_filled)
        self.assertEqual(result.snu_bars, 0)

    def test_the_extremes_cover_the_bar_that_closes_the_position(self) -> None:
        # The position was live during that bar, so its high and low belong to
        # the excursion; and the trough can then never sit above the exit.
        result = walk(_plan(entries=RUNGS[:1]), _config(), (_bar(WALK_START, 68.0, 69.5, 62.0),))
        self.assertEqual(result.peak_price, 69.5)
        self.assertEqual(result.trough_price, 62.0)
        self.assertLessEqual(result.trough_price, result.events[-1].price)

    def test_the_extremes_start_at_the_bar_of_the_first_fill(self) -> None:
        bars = (_bar(WALK_START, 80.0, 90.0, 69.0), _bar(WALK_START + MINUTE, 68.0, 68.5, 67.0))
        result = walk(_plan(entries=RUNGS[:1]), _config(), bars)
        self.assertEqual(result.peak_price, 68.5)
        self.assertEqual(result.trough_price, 67.0)


class StopDecisionTest(unittest.TestCase):
    """The decision is taken at the bar's HIGH. The minimum-distance clamp in
    ``stop_decision`` is anchored on ``last_price``, so the choice changes the
    level: the same peak of 70.60 returns 69.56 at a last price of 70.60 and
    67.365 at a last price of 67.50. Spec section 4.4 publishes 69.56, and the
    daemon polls many times inside a minute with a ratchet that keeps the best
    level any poll produced - the highest one available in this bar is the one
    at its high."""

    def _trailed(self, bar: Bar) -> Any:
        return walk(_plan(entries=RUNGS[:1], reaction=TRAIL), _config(), (bar,))

    def test_the_decision_reads_the_bar_high_and_not_its_close(self) -> None:
        result = self._trailed(_bar(WALK_START, 68.0, ARMED_PEAK, 67.5, close=67.5))
        moved = [event for event in result.events if event.kind == "stop_moved"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].reason, "trail")
        self.assertEqual(moved[0].before, FLOOR)
        self.assertEqual(moved[0].after, TRAILED_LEVEL)
        self.assertNotEqual(moved[0].after, 67.365)

    def test_the_trail_stays_dark_below_its_trigger(self) -> None:
        result = self._trailed(_bar(WALK_START, 68.0, 70.4, 67.5))
        self.assertEqual([e.kind for e in result.events if e.kind == "stop_moved"], [])

    def test_the_trail_arms_at_its_trigger(self) -> None:
        result = self._trailed(_bar(WALK_START, 68.0, 70.5, 67.5))
        self.assertEqual(result.events[2].after, 69.5)

    def test_a_document_with_no_reaction_never_moves_the_stop(self) -> None:
        # exit: null resolves to the inert policy, which answers None on every
        # view, so the property costs the walk no branch of its own.
        result = walk(_plan(entries=RUNGS[:1]), _config(), (_bar(WALK_START, 68.0, 71.0, 67.5),))
        self.assertEqual([e.kind for e in result.events if e.kind == "stop_moved"], [])

    def test_the_ratchet_refuses_a_step_smaller_than_its_epsilon(self) -> None:
        bars = (
            _bar(WALK_START, 68.0, ARMED_PEAK, 67.5),
            _bar(WALK_START + MINUTE, 70.0, 70.61, 69.9),
        )
        result = walk(_plan(entries=RUNGS[:1], reaction=TRAIL), _config(), bars)
        self.assertEqual(
            [e.after for e in result.events if e.kind == "stop_moved"], [TRAILED_LEVEL]
        )

    def test_the_ratchet_lets_a_big_enough_step_through(self) -> None:
        bars = (
            _bar(WALK_START, 68.0, ARMED_PEAK, 67.5),
            _bar(WALK_START + MINUTE, 70.0, 71.0, 69.9),
        )
        result = walk(_plan(entries=RUNGS[:1], reaction=TRAIL), _config(), bars)
        self.assertEqual(
            [e.after for e in result.events if e.kind == "stop_moved"], [TRAILED_LEVEL, 69.8]
        )


class SkippedRungTest(unittest.TestCase):
    """A rung below the resting stop is SKIPPED on every bar the stop sits above
    it, and never cancelled (spec section 4.4). Filling it books a purchase at a
    price the same bar had already sold at; a first implementation did that and
    reported 48.26 where the honest answer was 20.65."""

    def test_a_rung_under_the_moved_stop_never_fills(self) -> None:
        bars = (
            _bar(WALK_START, 68.0, ARMED_PEAK, 67.5, close=67.5),
            _bar(WALK_START + MINUTE, 67.0, 67.5, 66.0),
        )
        result = walk(_plan(reaction=TRAIL), _config(), bars)
        self.assertEqual(
            _kinds(result), ["entry_filled", "stop_placed", "stop_moved", "position_closed"]
        )
        self.assertEqual(result.units_filled, 900.0 / 68.0)
        self.assertEqual(result.notional_spent, 900.0)
        # The exit is the gap price, not the stop level: the bar opened below it.
        self.assertEqual(result.events[-1].price, 67.0)

    def test_a_skipped_rung_does_not_expire(self) -> None:
        bars = (
            _bar(WALK_START, 68.0, ARMED_PEAK, 67.5, close=67.5),
            _bar(WALK_START + MINUTE, 67.0, 67.5, 66.0),
        )
        result = walk(_plan(reaction=TRAIL), _config(), bars)
        self.assertNotIn("entry_expired", _kinds(result))

    def test_a_rung_exactly_at_the_resting_stop_is_skipped(self) -> None:
        # Non-strict on purpose: at equality, reaching the rung IS reaching the
        # stop, so a fill would be a re-entry at the price that just sold.
        rungs = (
            PendingEntry(tier_index=0, limit_price=68.0, notional=900.0),
            PendingEntry(tier_index=1, limit_price=66.2, notional=600.0),
        )
        bars = (
            _bar(WALK_START, 68.0, 68.2, 67.5),
            _bar(WALK_START + MINUTE, 66.5, 66.6, 66.1),
        )
        result = walk(_plan(entries=rungs, reaction=REANCHOR), _config(), bars)
        self.assertEqual([e.after for e in result.events if e.kind == "stop_moved"], [66.2])
        self.assertEqual(result.units_filled, 900.0 / 68.0)

    def test_a_skipped_rung_is_live_again_once_the_stop_drops_below_it(self) -> None:
        # Skipped, not cancelled: the re-anchor arm clamps against the brief
        # floor rather than the standing level, so a later fill can put the stop
        # back under a rung it had passed.
        rungs = (
            PendingEntry(tier_index=0, limit_price=68.0, notional=500.0),
            PendingEntry(tier_index=1, limit_price=67.5, notional=500.0),
            PendingEntry(tier_index=2, limit_price=66.0, notional=500.0),
        )
        bars = (
            _bar(WALK_START, 68.0, 68.2, 67.6),  # rung 0; stop -> 66.20
            _bar(WALK_START + MINUTE, 67.8, 67.9, 66.5),  # rung 1; stop -> 65.9491
            _bar(WALK_START + 2 * MINUTE, 66.2, 66.3, 65.96),  # rung 2 is live again
        )
        result = walk(_plan(entries=rungs, reaction=REANCHOR), _config(), bars)
        self.assertEqual(
            [e.after for e in result.events if e.kind == "stop_moved"],
            [66.2, 65.94907749077491, 65.35584127687875],
        )
        self.assertEqual(
            [e.tier_index for e in result.events if e.kind == "entry_filled"], [0, 1, 2]
        )
        self.assertEqual(result.outcome, "open")


class ReanchorTest(unittest.TestCase):
    def test_a_later_rung_fill_reanchors_LOWER(self) -> None:
        # The clamp compares the target with the brief floor, never with the
        # level standing, so the stop moves DOWN here. The replay reproduces the
        # arm rather than adding a never-down rule of its own.
        rungs = (
            PendingEntry(tier_index=0, limit_price=68.0, notional=900.0),
            PendingEntry(tier_index=1, limit_price=67.0, notional=600.0),
        )
        bars = (
            _bar(WALK_START, 68.0, 68.2, 67.5),
            _bar(WALK_START + MINUTE, 67.5, 67.6, 66.9),
        )
        result = walk(_plan(entries=rungs, reaction=REANCHOR), _config(), bars)
        moved = [e.after for e in result.events if e.kind == "stop_moved"]
        # 65.796... and not the spec's 65.70: that figure is the equal-UNITS
        # one, and the walk weights by budget over limit.
        self.assertEqual(moved, [66.2, 65.79643916913948])
        self.assertLess(moved[1], moved[0])

    def test_the_latch_stops_a_second_reanchor_on_the_same_average(self) -> None:
        bars = (
            _bar(WALK_START, 68.0, 68.2, 67.5),
            _bar(WALK_START + MINUTE, 67.9, 68.1, 67.6),
        )
        result = walk(_plan(entries=RUNGS[:1], reaction=REANCHOR), _config(), bars)
        self.assertEqual([e.after for e in result.events if e.kind == "stop_moved"], [66.2])


# Measured 2026-09-27 against `cost_gate.min_profitable_exit_price` with the
# section 5.2 costs (8 bps ad valorem, a 1.00 per-fill minimum that applies, a
# 5 bps edge) and an entry of 68.00. The threshold RISES as the tranche shrinks,
# because the per-fill minimum is a flat fee spread over fewer units.
THRESHOLDS = {1.0: 68.18511111111111, 0.5: 68.33622222222222, 0.4: 68.41177777777777}
UNITS = 900.0 / 68.0

# A tranche level between the 100% threshold and the entry: touched, and refused.
REFUSED_LEVEL = 68.1


class TakeProfitTest(unittest.TestCase):
    """The ladder is reviewed WHOLE on every bar, in ladder order. Nothing here
    assumes the ladder rises: `_tp_violations` checks positive percentages, the
    one-sided sum, duplicate prices and above-blend, and no ordering at all."""

    FILL = _bar(WALK_START, 68.0, 68.2, 67.9)

    def _walk(
        self,
        tranches: tuple[DeclaredTranche, ...],
        *bars: Bar,
        entries: tuple[PendingEntry, ...] = RUNGS[:1],
        notional: float = 900.0,
        take_profit: float | None = None,
    ) -> Any:
        plan = _plan(entries=entries, notional=notional, tranches=tranches, take_profit=take_profit)
        return walk(plan, _config(), (self.FILL, *bars))

    def _fired(self, result: Any) -> list[Any]:
        return [event for event in result.events if event.kind == "tp_fired"]

    def test_a_touched_tranche_fires_at_its_level(self) -> None:
        result = self._walk(
            (DeclaredTranche(tranche_index=0, price=70.0, fraction=0.5),),
            _bar(WALK_START + MINUTE, 69.0, 70.2, 68.8),
        )
        fired = self._fired(result)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].tranche_index, 0)
        self.assertEqual(fired[0].price, 70.0)
        self.assertEqual(fired[0].units, 0.5 * UNITS)
        self.assertEqual(fired[0].proceeds, 0.5 * UNITS * 70.0)
        self.assertEqual(fired[0].ladder, "tp_tranches")
        self.assertEqual(result.outcome, "open")

    def test_a_bar_that_gaps_above_a_tranche_still_fills_at_the_level(self) -> None:
        # The gap rule of section 4.4 reaches only the legs that REST at the
        # broker - a rung and the disaster stop. A take-profit in this model is
        # observed, not resting, so it fills AT its level; the difference between
        # that and the open is the `take_profit_observation_time` divergence.
        result = self._walk(
            (DeclaredTranche(tranche_index=0, price=70.0, fraction=0.5),),
            _bar(WALK_START + MINUTE, 71.0, 71.5, 70.8),
        )
        self.assertEqual(self._fired(result)[0].price, 70.0)

    def test_an_untouched_tranche_is_skipped_and_a_deeper_one_fires(self) -> None:
        # The review CONTINUES past an untouched tranche, as the daemon does
        # (`live_exit_engine.plan_tranche_exits`); stopping at the first one would
        # silence every tranche behind a level the bar never reached.
        tranches = (
            DeclaredTranche(tranche_index=0, price=80.0, fraction=0.5),
            DeclaredTranche(tranche_index=1, price=74.0, fraction=0.5),
        )
        result = self._walk(tranches, _bar(WALK_START + MINUTE, 72.0, 74.5, 71.0))
        fired = self._fired(result)
        self.assertEqual([event.tranche_index for event in fired], [1])
        self.assertEqual(fired[0].price, 74.0)

    def test_the_first_tranche_the_cost_gate_refuses_ends_the_batch(self) -> None:
        # A refusal is the ONLY reason the review stops early: the daemon breaks
        # there because every tranche behind it is cheaper still.
        tranches = (
            DeclaredTranche(tranche_index=0, price=70.0, fraction=0.4),
            DeclaredTranche(tranche_index=1, price=REFUSED_LEVEL, fraction=0.4),
        )
        result = self._walk(tranches, _bar(WALK_START + MINUTE, 69.0, 70.5, 68.5))
        self.assertEqual([event.price for event in self._fired(result)], [70.0])
        self.assertLess(REFUSED_LEVEL, THRESHOLDS[0.4])

    def test_the_tranche_units_come_from_the_whole_ladder_and_clamp_cumulatively(self) -> None:
        # The base is the INTENDED ladder, as in the daemon (`control_loop` uses
        # the reference quantity), so a half-filled entry ladder cannot sell more
        # than it holds - the clamp runs cumulatively down the tranches.
        tranches = (
            DeclaredTranche(tranche_index=0, price=70.0, fraction=0.5),
            DeclaredTranche(tranche_index=1, price=71.0, fraction=0.5),
        )
        result = self._walk(
            tranches, _bar(WALK_START + MINUTE, 69.0, 71.2, 68.8), entries=RUNGS, notional=1500.0
        )
        fired = self._fired(result)
        self.assertEqual([event.units for event in fired], [11.12892525431225, 2.106368863334808])
        self.assertEqual(sum(event.units for event in fired), result.units_filled)
        closed = result.events[-1]
        self.assertEqual((closed.kind, closed.reason), ("position_closed", "tp_complete"))
        self.assertEqual(closed.units, 0.0)
        self.assertEqual(result.outcome, "closed_tp")

    def test_a_ladder_that_leaves_a_runner_leaves_the_position_open(self) -> None:
        # The percentage sum is ONE-SIDED (`validate.py:340`: under 100 means "the
        # pick deliberately leaves a runner"), so a 70% ladder never closes the
        # position and the walk ends on the horizon.
        result = self._walk(
            (DeclaredTranche(tranche_index=0, price=70.0, fraction=0.7),),
            _bar(WALK_START + MINUTE, 69.0, 70.2, 68.8),
        )
        self.assertEqual(result.outcome, "open")
        self.assertEqual(result.events[-1].kind, "horizon_open")
        self.assertEqual(result.events[-1].units, UNITS - 0.7 * UNITS)

    def test_a_full_ladder_closes_the_position_despite_float_dust(self) -> None:
        # Three fractions of one third leave a residue that an ABSOLUTE 1e-9
        # tolerance would read as an open position. Measured on this fixture:
        # 1.4901161193847656e-08 units left of 73529411.76470588, which is one
        # unit in the last place at that size, so the tolerance has to be
        # RELATIVE. A 70% ladder sits at 0.30, seven orders away.
        big = (PendingEntry(tier_index=0, limit_price=68.0, notional=5_000_000_000.0),)
        third = 1.0 / 3.0
        tranches = tuple(
            DeclaredTranche(tranche_index=index, price=price, fraction=third)
            for index, price in enumerate((70.0, 70.5, 71.0))
        )
        result = self._walk(
            tranches,
            _bar(WALK_START + MINUTE, 69.0, 71.2, 68.8),
            entries=big,
            notional=5_000_000_000.0,
        )
        sold = sum(event.units for event in self._fired(result))
        self.assertEqual(result.units_filled - sold, 1.4901161193847656e-08)
        self.assertEqual(result.outcome, "closed_tp")

    def test_a_tranche_reached_before_any_fill_does_nothing(self) -> None:
        # The gate needs an entry price, and there is none until a rung fills:
        # a bar that trades through a take-profit level while the ladder is
        # still waiting must be a no-op, not a division by zero units.
        plan = _plan(
            entries=RUNGS[:1],
            notional=900.0,
            tranches=(DeclaredTranche(tranche_index=0, price=70.0, fraction=1.0),),
        )
        result = walk(plan, _config(), (_bar(WALK_START, 70.0, 71.0, 69.0),))
        self.assertEqual(_kinds(result), [])
        self.assertEqual(result.outcome, "no_fill")

    def test_an_empty_ladder_never_fires(self) -> None:
        # Legitimate by the published schema: a pick with no take-profit that
        # runs to its disaster stop.
        result = self._walk((), _bar(WALK_START + MINUTE, 69.0, 90.0, 62.0))
        self.assertEqual(self._fired(result), [])
        self.assertEqual(result.outcome, "closed_stop")

    def test_the_initial_levels_ladder_is_one_tranche_at_its_take_profit(self) -> None:
        # Section 5.1: a document that placed its bracket itself is replayed
        # against THAT instruction - one tranche for the whole position.
        result = self._walk((), _bar(WALK_START + MINUTE, 69.0, 70.2, 68.8), take_profit=70.0)
        fired = self._fired(result)
        self.assertEqual(result.ladder, "initial_levels")
        self.assertEqual(len(fired), 1)
        self.assertEqual((fired[0].price, fired[0].units), (70.0, UNITS))
        self.assertEqual(fired[0].ladder, "initial_levels")
        self.assertEqual(result.outcome, "closed_tp")

    def test_the_authors_ladder_does_not_fire_when_initial_levels_won(self) -> None:
        tranches = (
            DeclaredTranche(tranche_index=0, price=74.0, fraction=0.5),
            DeclaredTranche(tranche_index=1, price=76.0, fraction=0.5),
        )
        result = self._walk(tranches, _bar(WALK_START + MINUTE, 69.0, 77.0, 68.8), take_profit=70.0)
        self.assertEqual([event.price for event in self._fired(result)], [70.0])


class AmbiguousBarsTest(unittest.TestCase):
    """Row 1 of the section 4.4 table: one bar reaching both the stop and a
    take-profit does not say which came first. The walk takes the stop, the
    conservative reading, and COUNTS the bar so the reader knows a coin was
    flipped."""

    FILL = _bar(WALK_START, 68.0, 68.2, 67.9)

    def _walk(self, tranches: tuple[DeclaredTranche, ...], *bars: Bar) -> Any:
        plan = _plan(entries=RUNGS[:1], notional=900.0, tranches=tranches)
        return walk(plan, _config(), (self.FILL, *bars))

    def test_a_stop_and_a_tranche_in_one_bar_take_the_stop_and_count_the_bar(self) -> None:
        result = self._walk(
            (DeclaredTranche(tranche_index=0, price=70.0, fraction=1.0),),
            _bar(WALK_START + MINUTE, 67.0, 70.5, 62.0),
        )
        self.assertEqual(result.events[-1].reason, "stop")
        self.assertEqual([e for e in result.events if e.kind == "tp_fired"], [])
        self.assertEqual(result.snu_bars, 1)

    def test_the_bar_is_not_ambiguous_when_the_gate_refuses_the_tranche(self) -> None:
        # The counter must measure what it NAMES. A tranche the cost gate would
        # decline could not have fired first, so both orders book the same cash
        # and the bar decided nothing.
        result = self._walk(
            (DeclaredTranche(tranche_index=0, price=REFUSED_LEVEL, fraction=1.0),),
            _bar(WALK_START + MINUTE, 67.0, 68.5, 62.0),
        )
        self.assertLess(REFUSED_LEVEL, THRESHOLDS[1.0])
        self.assertEqual(result.snu_bars, 0)

    def test_an_untouched_shallower_tranche_does_not_block_a_reached_deeper_one(self) -> None:
        # What this pins is SKIP-untouched, not break-on-first: tranche 0 sits at
        # 80.00, which the bar never reaches, so the review continues to tranche
        # 1 exactly as `_fire_tranches` does. The ladder need not rise, so the
        # walk reads it as GIVEN rather than by price. Renamed 2026-09-30: the
        # old name claimed ANY unfired tranche counts, which is the reading
        # `_fire_tranches` has never had -- a REACHED tranche the gate refuses
        # blocks the ones behind it
        # (`test_a_refused_tranche_blocks_the_reached_ones_behind_it_and_nothing_fires`).
        tranches = (
            DeclaredTranche(tranche_index=0, price=80.0, fraction=0.5),
            DeclaredTranche(tranche_index=1, price=74.0, fraction=0.5),
        )
        result = self._walk(tranches, _bar(WALK_START + MINUTE, 72.0, 74.5, 62.0))
        self.assertEqual(result.snu_bars, 1)

    def test_a_refused_tranche_blocks_the_reached_ones_behind_it_and_nothing_fires(self) -> None:
        # `_fire_tranches` walks the ladder in ITS order and RETURNS on the first
        # cost-gate refusal, so a level behind one the gate declines cannot fire
        # in either reading. Both tranches share the 0.5 threshold, so the bar
        # reaching both fires NOTHING under the take-profit-first order, and a
        # bar where no order moved money is not an SNU. Spec section 4.4,
        # decided 2026-09-26: counting it would put bars where the rule changed
        # nothing into a number whose published meaning is the opposite.
        tranches = (
            DeclaredTranche(tranche_index=0, price=REFUSED_LEVEL, fraction=0.5),
            DeclaredTranche(tranche_index=1, price=74.0, fraction=0.5),
        )
        result = self._walk(tranches, _bar(WALK_START + MINUTE, 72.0, 74.5, 62.0))
        self.assertLess(REFUSED_LEVEL, THRESHOLDS[0.5])
        self.assertGreater(74.0, THRESHOLDS[0.5])
        # Not vacuous: row 1 was REACHED. Without these two the test would pass
        # on a walk that never asked the question at all.
        self.assertEqual(result.events[-1].reason, "stop")
        self.assertEqual([e for e in result.events if e.kind == "tp_fired"], [])
        self.assertEqual(result.snu_bars, 0)

    def test_a_tranche_that_already_fired_leaves_a_later_stop_bar_unambiguous(self) -> None:
        result = self._walk(
            (DeclaredTranche(tranche_index=0, price=70.0, fraction=0.4),),
            _bar(WALK_START + MINUTE, 69.0, 70.2, 68.8),
            _bar(WALK_START + 2 * MINUTE, 68.0, 70.5, 62.0),
        )
        self.assertEqual(len([e for e in result.events if e.kind == "tp_fired"]), 1)
        self.assertEqual(result.events[-1].reason, "stop")
        self.assertEqual(result.snu_bars, 0)


class RowOneAgreesWithTheLadderPassTest(unittest.TestCase):
    """`_a_tranche_would_have_fired` answers True exactly when `_fire_tranches`
    would sell something from the same state.

    Row 1 is a COUNTERFACTUAL: on a stop bar `_walk_one_bar` returns before
    `_advance_state`, so the ladder pass never runs and nothing checks that the
    two agree. #1629 is exactly that drift -- the helper asked `any(reached and
    affordable)` while the pass stops at the first refusal -- and it survived
    because no behavioural test put a REFUSED tranche in front of a clearing
    one. A bar shape cannot pin this; only asking both functions can.

    The domain is `_held(state) > 0`. `_fire_tranches` carries a `_held <= 0`
    guard the helper does not, and the helper's docstring argues it is never
    asked with nothing held (a stop RESTS only after a fill). A twin guard on
    the helper would be unreachable code, so the carve-out lives here instead.
    """

    INTENDED = 900.0 / 68.0

    def _state(self, *, fired: frozenset[int] = frozenset(), sold: float = 0.0) -> _WalkState:
        state = _WalkState(pending={})
        state.units = self.INTENDED
        state.cash = 900.0
        state.units_sold = sold
        state.fired = set(fired)
        state.stop = FLOOR
        return state

    # Each case is (name, ladder, bar, already-fired, units already sold). The
    # boundary case is not optional: a mutant reading `bar.high <= price`
    # survives the whole suite and is caught ONLY by a tranche priced exactly at
    # a bar's high.
    CASES = (
        (
            "rising, first reached is refused, deeper one clears",
            ((0, REFUSED_LEVEL, 0.5), (1, 74.0, 0.5)),
            (72.0, 74.5, 62.0),
            frozenset(),
            0.0,
        ),
        (
            "rising, first reached clears",
            ((0, 70.0, 0.5), (1, 74.0, 0.5)),
            (72.0, 74.5, 62.0),
            frozenset(),
            0.0,
        ),
        (
            "ladder given in DESCENDING order",
            ((0, 74.0, 0.5), (1, REFUSED_LEVEL, 0.5)),
            (72.0, 74.5, 62.0),
            frozenset(),
            0.0,
        ),
        (
            "first tranche is never reached, second clears",
            ((0, 80.0, 0.5), (1, 74.0, 0.5)),
            (72.0, 74.5, 62.0),
            frozenset(),
            0.0,
        ),
        (
            "the only reached tranche already fired",
            ((0, 70.0, 0.4),),
            (68.0, 70.5, 62.0),
            frozenset({0}),
            0.4 * INTENDED,
        ),
        (
            "tranche priced EXACTLY at the bar high",
            ((0, 70.0, 1.0),),
            (67.0, 70.0, 62.0),
            frozenset(),
            0.0,
        ),
        (
            "refused tranche priced exactly at the bar high",
            ((0, REFUSED_LEVEL, 1.0),),
            (67.0, REFUSED_LEVEL, 62.0),
            frozenset(),
            0.0,
        ),
        (
            "no tranche reached at all",
            ((0, 90.0, 0.5), (1, 95.0, 0.5)),
            (72.0, 74.5, 62.0),
            frozenset(),
            0.0,
        ),
        (
            "empty ladder",
            (),
            (72.0, 74.5, 62.0),
            frozenset(),
            0.0,
        ),
        (
            "part of the position already sold, so held < units",
            ((0, 70.0, 0.4), (1, 74.0, 0.6)),
            (72.0, 74.5, 62.0),
            frozenset({0}),
            0.4 * INTENDED,
        ),
    )

    def test_the_helper_and_the_ladder_pass_agree_on_every_shape(self) -> None:
        for name, rungs, (open_, high, low), fired, sold in self.CASES:
            with self.subTest(case=name):
                ladder = tuple(
                    DeclaredTranche(tranche_index=i, price=price, fraction=frac)
                    for i, price, frac in rungs
                )
                bar = _bar(WALK_START + MINUTE, open_, high, low)
                asked = self._state(fired=fired, sold=sold)
                self.assertGreater(_held(asked), 0.0, "case is outside the stated domain")
                answer = _a_tranche_would_have_fired(
                    asked,
                    bar,
                    ladder=ladder,
                    intended=self.INTENDED,
                    costs=_config().costs,
                )
                acted = copy.deepcopy(self._state(fired=fired, sold=sold))
                _fire_tranches(
                    acted,
                    bar,
                    ladder=ladder,
                    ladder_name="tp_tranches",
                    intended=self.INTENDED,
                    costs=_config().costs,
                )
                sold_something = any(e.kind == "tp_fired" for e in acted.events)
                self.assertEqual(answer, sold_something)


class RungAndTakeProfitSnuTest(unittest.TestCase):
    """Section 4.4's fourth situation: a rung BELOW the bar's open and a
    take-profit on the same bar.

    The walk does NOT resolve it. No resolution is a bound for a laddered
    document, so the declared rule stands - every touched rung fills, then the
    ladder is reviewed - and `snu_bars` says when that rule decided money.
    """

    TIE = _bar(WALK_START, 67.0, 68.6, 66.4)
    TRANCHE = 68.5

    def _walk(self, *tranches: DeclaredTranche, first: Bar | None = None, then: Bar | None = None):
        plan = _plan(entries=RUNGS, notional=1500.0, tranches=tranches)
        bars = ((first or self.TIE),) + ((then,) if then else ())
        return walk(plan, _config(), bars)

    def test_the_declared_rule_fills_every_touched_rung_first_and_counts_the_bar(self) -> None:
        # Measured 2026-09-28: this reading nets +37.898054 and the other
        # +19.852941, a gap of exactly rung 2's profit. The rule takes the
        # first; the COUNT is what tells the reader a rule decided 18.045113.
        result = self._walk(DeclaredTranche(tranche_index=0, price=self.TRANCHE, fraction=1.0))
        self.assertEqual(
            _kinds(result),
            ["entry_filled", "stop_placed", "entry_filled", "tp_fired", "position_closed"],
        )
        fired = next(event for event in result.events if event.kind == "tp_fired")
        self.assertEqual(fired.units, 900.0 / 68.0 + 600.0 / 66.5)
        self.assertEqual(result.snu_bars, 1)

    def test_a_ladder_that_wants_less_than_is_held_decides_nothing(self) -> None:
        # The counter must measure what it NAMES. When the touched ladder wants
        # fewer units than are already held, the clamp cannot bind either way,
        # the deep rung fills in both readings, and the bar decided nothing.
        # The second tranche sits ABOVE this bar's high, so it is untouched and
        # must not feed the appetite: a level the bar never reached cannot have
        # fired in either reading.
        result = self._walk(
            DeclaredTranche(tranche_index=0, price=self.TRANCHE, fraction=0.5),
            DeclaredTranche(tranche_index=1, price=80.0, fraction=0.5),
        )
        self.assertEqual(result.snu_bars, 0)
        self.assertEqual(result.units_filled, 900.0 / 68.0 + 600.0 / 66.5)

    def test_a_ladder_wanting_exactly_what_is_held_still_decides_the_money(self) -> None:
        # The boundary: at equality the clamp does not bind, but the CLOSURE
        # does. Selling precisely what is held ends the walk, so the deep rung
        # never fills in that reading while it does in this one.
        equal = (
            PendingEntry(tier_index=0, limit_price=100.0, notional=1000.0),
            PendingEntry(tier_index=1, limit_price=50.0, notional=500.0),
        )
        plan = _plan(
            entries=equal,
            notional=1500.0,
            floor=40.0,
            tranches=(DeclaredTranche(tranche_index=0, price=110.0, fraction=0.5),),
        )
        result = walk(plan, _config(), (_bar(WALK_START, 60.0, 110.5, 49.0),))
        self.assertEqual(result.snu_bars, 1)

    def test_a_rung_at_or_above_the_open_is_never_in_question(self) -> None:
        # Both rungs are through at the first print, so no fill is in question.
        result = self._walk(
            DeclaredTranche(tranche_index=0, price=self.TRANCHE, fraction=1.0),
            first=_bar(WALK_START, 66.0, 68.6, 65.9),
        )
        self.assertEqual(result.snu_bars, 0)
        self.assertEqual(result.outcome, "closed_tp")

    def test_a_bar_the_stop_closed_is_counted_once_by_row_one(self) -> None:
        # Rows 1 and 4 both look at this bar. It is ONE bar, so it counts once,
        # and row 1 is the one that asks - the stop settles the bar.
        result = self._walk(
            DeclaredTranche(tranche_index=0, price=self.TRANCHE, fraction=1.0),
            first=_bar(WALK_START, 67.0, 68.6, 62.0),
        )
        self.assertEqual(result.outcome, "closed_stop")
        self.assertEqual(result.snu_bars, 1)

    def test_the_bar_counts_when_the_cost_gate_verdict_turns_on_the_order(self) -> None:
        # The deep fill lowers the average entry and every threshold with it, so
        # a tranche refused on the small position is afforded on the blended
        # one: it fires in one reading and not the other. Measured 2026-09-29 at
        # this fraction, the gate's threshold is 68.258640 before the deep rung
        # and 67.650288 after, so 67.70 flips. The clamp never binds here
        # (0.4 x intended is 8.90 against 13.24 held), so the appetite arm
        # cannot catch it and only the gate arm can.
        result = self._walk(
            DeclaredTranche(tranche_index=0, price=67.7, fraction=0.4),
            first=_bar(WALK_START, 68.0, 68.0, 67.99),
            then=_bar(WALK_START + MINUTE, 67.0, 68.0, 66.4),
        )
        self.assertEqual(result.snu_bars, 1)

    def test_a_ladder_blocked_by_a_refused_tranche_decides_nothing(self) -> None:
        # `_fire_tranches` RETURNS on the first refusal, so a level behind one
        # the gate declines cannot fire in either reading and must not feed the
        # appetite. Counting it would report a bar where nothing moved.
        result = self._walk(
            DeclaredTranche(tranche_index=0, price=REFUSED_LEVEL, fraction=0.05),
            DeclaredTranche(tranche_index=1, price=self.TRANCHE, fraction=1.0),
            first=_bar(WALK_START, 68.0, 68.0, 67.99),
            then=_bar(WALK_START + MINUTE, 67.0, 68.6, 66.4),
        )
        self.assertEqual(result.snu_bars, 0)

    def test_a_bar_that_opens_the_position_is_a_known_blind_spot(self) -> None:
        # Section 6.3 names this one: with nothing held the cost gate has no
        # entry price to measure a tranche against, so the replay cannot say
        # whether the tranche would have fired. Measured worth: 18.045113.
        # Pinned so the gap is discoverable rather than folklore.
        plan = _plan(
            entries=(PendingEntry(tier_index=0, limit_price=66.5, notional=600.0),),
            notional=600.0,
            tranches=(DeclaredTranche(tranche_index=0, price=self.TRANCHE, fraction=1.0),),
        )
        result = walk(plan, _config(), (self.TIE,))
        self.assertEqual(result.units_filled, 600.0 / 66.5)
        self.assertEqual(result.snu_bars, 0)


def _deadline(value: int) -> dict[str, Any]:
    """The section 5.2 deadline block with a different instant. Everything else
    stays as published, so the test moves one number and nothing else."""
    block = copy.deepcopy(dict(CANONICAL["entry_deadline"]))
    block["value"] = value
    return block


class DeadlineTest(unittest.TestCase):
    """The entry ladder has a life: once `entry_deadline` is reached, the rungs
    that never filled are gone. `/edge` blocks a fill at `ts >= expiry`, so the
    comparison is NOT strict, and a bar at exactly the deadline expires."""

    MISS = _bar(WALK_START, 70.0, 71.0, 69.0)
    DEEP = _bar(WALK_START + MINUTE, 60.0, 68.6, 59.0)

    def test_the_deadline_expires_every_unfilled_rung_in_one_event(self) -> None:
        # ONE event carrying the list, not one per rung: the rungs died of the
        # same cause at the same instant.
        result = walk(
            _plan(),
            _config(entry_deadline=_deadline(WALK_START + MINUTE)),
            (self.MISS, self.DEEP, _bar(WALK_START + 2 * MINUTE, 60.0, 68.6, 59.0)),
        )
        self.assertEqual(_kinds(result), ["entry_expired"])
        expired = result.events[0]
        self.assertEqual(expired.t, WALK_START + MINUTE)
        self.assertEqual(expired.tiers, (0, 1))
        self.assertEqual(expired.reason, "deadline")
        self.assertEqual(result.outcome, "no_fill")

    def test_a_bar_at_the_deadline_expires_a_rung_it_also_reaches(self) -> None:
        # The boundary that decides money, stated on its own. `DEEP` has a low
        # of 59.00, under BOTH rungs, and is stamped exactly at the deadline.
        # It expires them instead of filling them, because `_expire` runs
        # before `_fill_entries` and the comparison is not strict. A strict
        # comparison here would buy a ladder the live side had already pulled.
        deadline = WALK_START + MINUTE
        self.assertEqual(self.DEEP.t, deadline)
        self.assertLess(self.DEEP.low, RUNGS[1].limit_price)
        result = walk(_plan(), _config(entry_deadline=_deadline(deadline)), (self.DEEP,))
        self.assertEqual(_kinds(result), ["entry_expired"])
        self.assertEqual(result.outcome, "no_fill")
        self.assertEqual(result.units_filled, 0.0)

    def test_a_bar_before_the_deadline_still_fills(self) -> None:
        result = walk(
            _plan(),
            _config(entry_deadline=_deadline(WALK_START + 2 * MINUTE)),
            (self.MISS, self.DEEP),
        )
        self.assertNotIn("entry_expired", _kinds(result))
        self.assertEqual(result.units_filled, 900.0 / 68.0 + 600.0 / 66.5)

    def test_only_the_rungs_still_waiting_expire(self) -> None:
        result = walk(
            _plan(),
            _config(entry_deadline=_deadline(WALK_START + MINUTE)),
            (_bar(WALK_START, 68.0, 68.2, 67.9), _bar(WALK_START + MINUTE, 67.0, 67.2, 66.0)),
        )
        self.assertEqual(
            _kinds(result), ["entry_filled", "stop_placed", "entry_expired", "horizon_open"]
        )
        self.assertEqual(result.events[2].tiers, (1,))
        self.assertEqual(result.outcome, "open")

    def test_a_full_ladder_leaves_nothing_to_expire(self) -> None:
        # Both rungs fill on a bar that opens under the deeper one and holds
        # above the disaster stop, so the deadline finds nothing waiting.
        result = walk(
            _plan(),
            _config(entry_deadline=_deadline(WALK_START + MINUTE)),
            (_bar(WALK_START, 66.0, 68.6, 65.9), _bar(WALK_START + MINUTE, 67.0, 67.2, 66.9)),
        )
        self.assertEqual(
            _kinds(result), ["entry_filled", "stop_placed", "entry_filled", "horizon_open"]
        )

    def test_a_deadline_already_past_expires_on_the_first_walked_bar(self) -> None:
        # The event is stamped with the BAR, not with the deadline: the walk
        # observed the expiry there, and no earlier bar was read.
        result = walk(
            _plan(),
            _config(entry_deadline=_deadline(WALK_START - MINUTE)),
            (_bar(WALK_START, 60.0, 68.6, 59.0),),
        )
        self.assertEqual(_kinds(result), ["entry_expired"])
        self.assertEqual(result.events[0].t, WALK_START)
        self.assertEqual(result.outcome, "no_fill")


class TimeStopTest(unittest.TestCase):
    """The time stop is a REPLAY construction - Saxo has none (see
    `feedback_saxo_no_live_time_stop_by_design`). It fires after the bar has
    finished, at the close, and loses to anything the tape actually reached."""

    FILL = _bar(WALK_START, 68.0, 68.2, 67.9)

    def _walk(
        self, time_stop_t: int | None, *bars: Bar, tranches: tuple[DeclaredTranche, ...] = ()
    ) -> Any:
        plan = _plan(entries=RUNGS[:1], notional=900.0, tranches=tranches)
        return walk(plan, _config(time_stop_t=time_stop_t), (self.FILL, *bars))

    def test_the_time_stop_closes_the_position_at_the_close(self) -> None:
        result = self._walk(
            WALK_START + MINUTE, _bar(WALK_START + MINUTE, 67.5, 67.6, 67.0, close=67.2)
        )
        closed = result.events[-1]
        self.assertEqual((closed.kind, closed.reason), ("position_closed", "time_stop"))
        self.assertEqual(closed.price, 67.2)
        self.assertEqual(closed.units, 900.0 / 68.0)
        self.assertEqual(result.outcome, "closed_time_stop")

    def test_the_resting_stop_wins_on_the_same_bar(self) -> None:
        # The stop is a real order that really traded; the time stop is the
        # replay's own idea of a horizon.
        result = self._walk(
            WALK_START + MINUTE, _bar(WALK_START + MINUTE, 67.0, 67.2, 62.0, close=62.5)
        )
        self.assertEqual(result.events[-1].reason, "stop")
        self.assertEqual(result.outcome, "closed_stop")

    def test_a_completed_take_profit_wins_on_the_same_bar(self) -> None:
        result = self._walk(
            WALK_START + MINUTE,
            _bar(WALK_START + MINUTE, 69.0, 70.5, 68.8, close=70.0),
            tranches=(DeclaredTranche(tranche_index=0, price=70.0, fraction=1.0),),
        )
        self.assertEqual(result.events[-1].reason, "tp_complete")
        self.assertEqual(result.outcome, "closed_tp")

    def test_the_time_stop_may_fire_on_the_bar_that_filled(self) -> None:
        # Buying and selling inside one bar is allowed: the fill happened first
        # by construction, and a horizon that cannot end on day one would report
        # a position the document never wanted held.
        result = self._walk(WALK_START)
        self.assertEqual(_kinds(result), ["entry_filled", "stop_placed", "position_closed"])
        self.assertEqual(result.events[-1].reason, "time_stop")

    def test_the_time_stop_needs_a_position_to_close(self) -> None:
        plan = _plan(entries=RUNGS[:1], notional=900.0)
        result = walk(plan, _config(time_stop_t=WALK_START), (_bar(WALK_START, 70.0, 71.0, 69.0),))
        self.assertEqual(_kinds(result), [])
        self.assertEqual(result.outcome, "no_fill")

    def test_a_config_that_states_no_time_stop_never_closes_on_one(self) -> None:
        result = self._walk(None, _bar(WALK_START + MINUTE, 67.5, 67.6, 67.0))
        self.assertEqual(result.outcome, "open")
        self.assertEqual(result.events[-1].kind, "horizon_open")


class WholeWalkInvariantsTest(unittest.TestCase):
    """Properties every run has, whatever the document said."""

    SCENARIOS: tuple[tuple[str, Plan, RunConfig, tuple[Bar, ...]], ...] = ()

    @classmethod
    def setUpClass(cls) -> None:
        tranches = (
            DeclaredTranche(tranche_index=0, price=70.0, fraction=0.5),
            DeclaredTranche(tranche_index=1, price=71.0, fraction=0.5),
        )
        cls.SCENARIOS = (
            (
                "stopped out after a trail",
                _plan(reaction=TRAIL),
                _config(),
                (
                    _bar(WALK_START, 68.0, ARMED_PEAK, 67.5, close=67.5),
                    _bar(WALK_START + MINUTE, 67.0, 67.5, 62.0),
                ),
            ),
            (
                "take-profit ladder completed",
                _plan(entries=RUNGS[:1], notional=900.0, tranches=tranches),
                _config(),
                (_bar(WALK_START, 68.0, 68.2, 67.9), _bar(WALK_START + MINUTE, 69.0, 71.5, 68.8)),
            ),
            (
                "ladder expired unfilled",
                _plan(),
                _config(entry_deadline=_deadline(WALK_START)),
                (_bar(WALK_START, 60.0, 68.6, 59.0),),
            ),
            (
                "closed on the horizon",
                _plan(entries=RUNGS[:1], notional=900.0),
                _config(time_stop_t=WALK_START + MINUTE),
                (_bar(WALK_START, 68.0, 68.2, 67.9), _bar(WALK_START + MINUTE, 68.1, 68.3, 67.8)),
            ),
        )

    def test_every_event_kind_is_one_of_the_seven_published_kinds(self) -> None:
        # Section 4.6 publishes a CLOSED list. A walk that invented an eighth
        # kind would be editing a locked spec from an implementation PR.
        for label, plan, config, bars in self.SCENARIOS:
            with self.subTest(label):
                result = walk(plan, config, bars)
                self.assertTrue(result.events, "a scenario with no events tests nothing")
                self.assertLessEqual(set(_kinds(result)), set(KINDS))

    def test_two_walks_over_one_input_agree(self) -> None:
        for label, plan, config, bars in self.SCENARIOS:
            with self.subTest(label):
                self.assertEqual(walk(plan, config, bars), walk(plan, config, bars))

    def test_the_walk_does_not_mutate_its_inputs(self) -> None:
        # The pending ladder is the walk's own copy; consuming a rung must not
        # reach back into the plan, or a second run would see a shorter ladder.
        for label, plan, config, bars in self.SCENARIOS:
            with self.subTest(label):
                before = (copy.deepcopy(plan), copy.deepcopy(config), copy.deepcopy(bars))
                walk(plan, config, bars)
                self.assertEqual((plan, config, bars), before)

    def test_the_stop_sells_only_what_the_take_profits_left(self) -> None:
        # Otherwise the envelope books the same units twice: once as realised
        # proceeds and once as the loss on the close.
        result = walk(
            _plan(
                entries=RUNGS[:1],
                notional=900.0,
                tranches=(DeclaredTranche(tranche_index=0, price=70.0, fraction=0.4),),
            ),
            _config(),
            (
                _bar(WALK_START, 68.0, 68.2, 67.9),
                _bar(WALK_START + MINUTE, 69.0, 70.2, 68.8),
                _bar(WALK_START + 2 * MINUTE, 68.0, 68.1, 62.0),
            ),
        )
        sold = next(event for event in result.events if event.kind == "tp_fired").units
        closed = result.events[-1]
        self.assertEqual(closed.reason, "stop")
        self.assertEqual(closed.units, result.units_filled - sold)


class TheGoldenCasesAtWalkLevelTest(unittest.TestCase):
    """Spec section 6.2 rows 1 and 2, asserted as the EVENTS the column names.

    The four golden cases already exist in
    `tests/brokers/test_stop_decision.py::GoldenCasesTest`, where they assert
    the level `decide_stop` RETURNS. Section 6.2's expectation column is written
    in `stop_moved` events, which is a walk-level fact: a leaf returning the
    right level and a walk that drops the event are the same green there.

    Only two of the four rows are new here. Rows 3 and 4 (the trail at its
    trigger and a tenth below it) are already asserted at this level by
    `StopDecisionTest.test_the_decision_reads_the_bar_high_and_not_its_close`
    and `test_the_trail_stays_dark_below_its_trigger`; restating them with
    section 6.2's own numbers beside the template's would buy nothing.

    Section 6.1 is why these matter more than their size suggests: the parity
    test holding the leaf against the daemon "must be retired in step 2", and
    the PR that retires it "puts behaviour-pinning golden cases in its place".

    The section's "anchor 68.00" is NOT a rung. It is the value the floor is
    derived from -- 68.00 - 1.5 x 1.20 = 66.20, which is exactly the
    `plan_stop` of `_reanchor_view()` in the leaf's own fixture. So the rung
    goes at the average fill the row names, and no entry trail is needed.
    """

    FLOOR = 66.20

    def test_an_average_fill_of_68_50_reanchors_the_stop_to_66_70(self) -> None:
        result = walk(
            _plan(
                entries=(PendingEntry(tier_index=0, limit_price=68.50, notional=1500.0),),
                floor=self.FLOOR,
                reaction=REANCHOR,
            ),
            _config(),
            (_bar(WALK_START, 68.50, 68.60, 68.40),),
        )
        self.assertEqual(result.avg_entry_price, 68.50)
        self.assertEqual([e.after for e in result.events if e.kind == "stop_moved"], [66.70])

    def test_a_fill_better_than_the_anchor_moves_no_stop(self) -> None:
        # 67.50 - 1.5 x 1.20 = 65.70, under the 66.20 floor, so the clamp
        # refuses and the walk emits nothing rather than lowering the stop.
        result = walk(
            _plan(
                entries=(PendingEntry(tier_index=0, limit_price=67.50, notional=1500.0),),
                floor=self.FLOOR,
                reaction=REANCHOR,
            ),
            _config(),
            (_bar(WALK_START, 67.50, 67.60, 67.40),),
        )
        self.assertEqual(result.avg_entry_price, 67.50)
        self.assertEqual([e.kind for e in result.events if e.kind == "stop_moved"], [])


class TheFloorDecidesHowManyReanchorsFireTest(unittest.TestCase):
    """Spec section 6.3: the floor is part of the re-anchor measurement.

    The section says a floor in `(65.70, 66.20]` fires only the FIRST
    re-anchor. Measured on the section's own PR-6 fixture, BOTH endpoints are
    wrong:

        floor 63.0                 -> 2 moves  [66.2, 65.79643916913948]
        floor 65.79643916913948    -> 2 moves  (the section says 1)
        floor 65.7964391691395     -> 1 move
        floor 66.1999              -> 1 move
        floor 66.20                -> 0 moves  (the section says 1)

    So the interval is `(65.79643916913948, 66.20)`, open at both ends. The left
    number is the one `test_a_later_rung_fill_reanchors_LOWER` already records
    as the budget-weighted level the walk produces, against the section's
    equal-units 65.70. The right end closes because `_decide_stop` returns early
    when the target equals the stop standing, and at that floor it does.
    """

    RUNGS = (
        PendingEntry(tier_index=0, limit_price=68.0, notional=900.0),
        PendingEntry(tier_index=1, limit_price=67.0, notional=600.0),
    )
    SECOND_LEVEL = 65.79643916913948

    def _moves(self, floor: float) -> list[float]:
        result = walk(
            _plan(entries=self.RUNGS, floor=floor, reaction=REANCHOR),
            _config(),
            (_bar(WALK_START, 68.0, 68.2, 67.5), _bar(WALK_START + MINUTE, 67.5, 67.6, 66.9)),
        )
        return [event.after for event in result.events if event.kind == "stop_moved"]

    def test_a_floor_below_the_second_level_fires_both(self) -> None:
        self.assertEqual(self._moves(63.0), [66.2, self.SECOND_LEVEL])

    def test_a_floor_exactly_at_the_second_level_still_fires_both(self) -> None:
        # The left end is OPEN: equality does not refuse.
        self.assertEqual(self._moves(self.SECOND_LEVEL), [66.2, self.SECOND_LEVEL])

    def test_one_ulp_above_the_second_level_fires_only_the_first(self) -> None:
        self.assertEqual(self._moves(math.nextafter(self.SECOND_LEVEL, 1e9)), [66.2])

    def test_a_floor_just_under_the_first_level_fires_only_the_first(self) -> None:
        self.assertEqual(self._moves(66.1999), [66.2])

    def test_a_floor_exactly_at_the_first_level_fires_neither(self) -> None:
        # The right end is OPEN too: the target equals the stop standing, and
        # the decision returns early rather than re-emitting it.
        self.assertEqual(self._moves(66.20), [])


class TheFilledFractionIsAFractionOfWhatTheLadderSplitsTest(unittest.TestCase):
    """The numerator and the denominator must be buffered together (#1592).

    ``state.committed`` accumulates each rung's ``notional``, which is a share
    of the BUFFERED budget once a conversion is stated. Dividing that by the
    stated budget publishes 0.594 where the ladder is 60 per cent filled -- a
    number that is wrong by exactly the buffer and reads as plausible. No test
    could catch it before this issue, because the buffer was 0.0 on every
    reachable path and the two readings agreed.
    """

    BAR = (_bar(WALK_START, 68.5, 68.6, 67.9),)

    def test_a_buffered_ladder_reports_the_allocation_and_not_the_buffer(self) -> None:
        # The 68.00 rung fills and the 66.50 one does not: 891 of 1485 is 0.6,
        # and 891 of the stated 1500 would be 0.594.
        result = walk(
            _plan(entries=BUFFERED_RUNGS, sizing_notional=BUFFERED_TOTAL), _config(), self.BAR
        )
        self.assertEqual(result.notional_spent, 891.0)
        self.assertEqual(result.filled_fraction, 0.6)

    def test_the_unbuffered_denominator_is_the_number_this_rules_out(self) -> None:
        # The existence control, stated as arithmetic rather than as a claim:
        # the two readings really do differ on this fixture, so the row above
        # has something to discriminate.
        self.assertEqual(891.0 / 1500.0, 0.594)
        self.assertNotEqual(891.0 / BUFFERED_TOTAL, 891.0 / 1500.0)

    def test_a_same_currency_ladder_is_unmoved(self) -> None:
        # The preimage: with no buffer the two readings agree, which is why
        # every other fraction in this file is still 0.6 or 0.0.
        result = walk(_plan(), _config(), self.BAR)
        self.assertEqual(result.filled_fraction, 0.6)

    def test_a_ladder_that_splits_nothing_divides_by_nothing(self) -> None:
        # The guard is on the DENOMINATOR the walk divides by, so it has to be
        # the buffered one there too.
        #
        # Measured 2026-10-02, so the next reader does not delete it as dead:
        # the door refuses ``spec.size.notional_acct: 0`` and the configuration
        # refuses a buffer at 100, so NO document reaches zero here. ``walk``
        # is public and takes a hand-built ``Plan``, which is the path this row
        # exercises -- and the pre-#1592 guard on ``plan.notional`` had exactly
        # the same reachability.
        result = walk(_plan(entries=(), notional=1500.0, sizing_notional=0.0), _config(), self.BAR)
        self.assertEqual(result.filled_fraction, 0.0)


class TheRatchetFloorIsTheHigherOfTheTwoFloorsTheWalkHoldsTest(unittest.TestCase):
    """The walk holds TWO floors and hands the leaf one of them (#1581).

    ``state.stop`` is the level resting at the broker and ``state.last_trailed_level``
    is the level a trail last moved it to. The live daemon ratchets a proposal
    against the HIGHER of those two (``position_manager.py:985-991``, citing
    #1514: the journaled level can lag the resting one after a lost marker or an
    owner-raised stop). The walk passed only the second, so before the first
    trail move -- when the trailed level is still ``None`` and the resting stop is
    the declared floor -- it had no floor at all.

    The window is narrow and that is the clamp's doing, not luck: the clamp
    already refuses anything below ``plan_stop``, which IS the resting stop at
    that moment, so the gap is confined to the ``TRAIL_STEP_EPS`` band of 0.02
    above the floor. Measured 2026-10-02 on the leaf: 15 of 200 000 draws, worst
    distance 1.57e-02. Narrow is not zero, and the same composition is what makes
    the daemon safe to rewire onto this leaf.

    Both rows below are measured, and the second is the existence control: a
    composition that vetoed every move would satisfy the first row alone.
    """

    # R = 63.08 - 63.00 = 0.08, so the trail arms at 63.08 + 0.5R = 63.1200 and
    # the bar's 63.13 high reaches it. The level the leaf then proposes is
    # 63.00374, which is 0.00374 above the floor -- inside the 0.02 band.
    TIGHT_FLOOR, TIGHT_RUNG = 63.00, 63.08
    TIGHT_BAR = _bar(WALK_START, 63.08, 63.13, 63.01, 63.10)
    # R = 3.08, arming at 64.6200, and the proposal lands 4.832 above the floor.
    WIDE_FLOOR, WIDE_RUNG = 60.00, 63.08
    WIDE_BAR = _bar(WALK_START, 63.08, 66.00, 63.01, 65.00)

    def _moves(self, floor: float, rung: float, bar: Bar) -> list[Any]:
        plan = _plan(
            entries=(PendingEntry(tier_index=0, limit_price=rung, notional=1000.0),),
            notional=1000.0,
            floor=floor,
            reaction=TRAIL,
        )
        return [
            event for event in walk(plan, _config(), (bar,)).events if event.kind == "stop_moved"
        ]

    def test_a_proposal_inside_the_band_above_the_resting_stop_is_vetoed(self) -> None:
        # Without the composition the walk emits ``stop_moved 63.00 -> 63.00374``
        # and the daemon, handed the same view with the stop resting at 63.00,
        # answers None. The resting stop is a floor, so there is nothing to move.
        self.assertEqual(self._moves(self.TIGHT_FLOOR, self.TIGHT_RUNG, self.TIGHT_BAR), [])

    def test_a_proposal_clear_of_the_band_still_moves_the_stop(self) -> None:
        # The existence control. Composing the floor must not turn the ratchet
        # into a blanket refusal; a proposal 4.832 above the floor clears it.
        moves = self._moves(self.WIDE_FLOOR, self.WIDE_RUNG, self.WIDE_BAR)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].before, self.WIDE_FLOOR)
        self.assertAlmostEqual(moves[0].after, 64.832, places=9)


class AnSnuBarHasNotNecessarilyChangedTheMoneyTest(unittest.TestCase):
    """``snu_bars`` counts a bar-LOCAL question, and the money is terminal.

    Section 4.4's predicate asks whether this bar's rung-against-take-profit
    order COULD change the money, and it is evaluated before the fills because
    afterwards the deep rung has left ``pending``. A continuation can then erase
    the difference it flagged, which is what this pins: a bar the walk counts,
    on which the two admissible readings of the tape end with the SAME cash.

    The pair of tapes is the literature's ``ex`` mode (arXiv:1412.5558): two
    finer series that aggregate to one coarse bar and differ only in whether the
    low or the high came first. The aggregation is asserted, not assumed.

    The ladder is the published template's (``RUNGS``, ``FLOOR``) and the tranche
    shape is 33.33 / 33.33 / 33.34, which 11 documents in the LIVE journal
    declare. The shallow tranche sits at 67.70 on purpose: it is REFUSED against
    the partial position's average of 67.50 and clears against the blended
    67.3920, so the readings disagree about whether it fires on the ambiguous
    bar. They still agree on the terminal cash, because the refusal only delays
    the tranche one bar and it then fires at the same declared price.
    """

    TRANCHES = (
        DeclaredTranche(tranche_index=0, price=67.70, fraction=0.3333),
        DeclaredTranche(tranche_index=1, price=73.00, fraction=0.3333),
        DeclaredTranche(tranche_index=2, price=75.00, fraction=0.3334),
    )
    COARSE = (WALK_START, 67.50, 68.20, 66.40, 68.00)
    LOW_FIRST = ((67.50, 67.50, 66.40, 66.40), (66.40, 68.20, 66.40, 68.00))
    HIGH_FIRST = ((67.50, 68.20, 67.50, 68.20), (68.20, 68.20, 66.40, 68.00))
    RISING = (68.00, 73.50, 67.50, 73.20)
    COLLAPSING = (68.00, 68.10, 62.50, 62.80)

    def _tape(
        self, readings: tuple[tuple[float, ...], ...], tail: tuple[float, ...]
    ) -> tuple[Bar, ...]:
        bars = [_bar(WALK_START + i * MINUTE, *row) for i, row in enumerate(readings)]
        bars.append(_bar(WALK_START + len(readings) * MINUTE, *tail))
        return tuple(bars)

    def _walk(self, tape: tuple[Bar, ...]) -> Any:
        return walk(_plan(tranches=self.TRANCHES), _config(), tape)

    def test_both_readings_aggregate_to_the_one_coarse_bar(self) -> None:
        # Without this the pair is not two readings of one bar, and the rest of
        # the class would be comparing two different tapes.
        _, open_, high, low, close = self.COARSE
        for name, reading in (("low first", self.LOW_FIRST), ("high first", self.HIGH_FIRST)):
            with self.subTest(name):
                self.assertEqual(reading[0][0], open_)
                self.assertEqual(max(row[1] for row in reading), high)
                self.assertEqual(min(row[2] for row in reading), low)
                self.assertEqual(reading[-1][3], close)

    def test_the_walk_counts_the_coarse_bar_as_an_snu(self) -> None:
        result = self._walk((_bar(*self.COARSE), _bar(WALK_START + MINUTE, *self.RISING)))
        self.assertEqual(result.snu_bars, 1)

    def test_the_reading_that_refuses_the_tranche_is_the_counted_one(self) -> None:
        # The count follows the pending rung, so it lands on the reading whose
        # deep rung is still unfilled when the high reaches the tranche.
        high = self._walk(self._tape(self.HIGH_FIRST, self.RISING))
        low = self._walk(self._tape(self.LOW_FIRST, self.RISING))
        self.assertEqual((high.snu_bars, low.snu_bars), (1, 0))

    def _fired(self, tape: tuple[Bar, ...]) -> list[tuple[int, int]]:
        return [
            (event.t, event.tranche_index)
            for event in self._walk(tape).events
            if event.kind == "tp_fired"
        ]

    def test_the_high_first_reading_reaches_the_tranche_and_refuses_it(self) -> None:
        # The divergence this class rests on, asserted directly rather than
        # inferred. On the first bar of the high-first reading the tape has
        # already reached 67.70, and the gate refuses it against the partial
        # position's average; the low-first reading never faces that question,
        # because its first bar tops out at 67.50.
        self.assertGreaterEqual(self.HIGH_FIRST[0][1], self.TRANCHES[0].price)
        self.assertLess(self.LOW_FIRST[0][1], self.TRANCHES[0].price)
        fired = self._fired(self._tape(self.HIGH_FIRST, self.RISING))
        self.assertNotIn(WALK_START, [t for t, _ in fired])

    def test_the_counted_bar_cost_nothing_on_either_continuation(self) -> None:
        # The claim this class exists for: the high-first reading met a refusal
        # the low-first reading never met, the walk counted its bar, and both
        # end with the same cash under a continuation that rises and one that
        # collapses to the stop. So a non-zero count is not a measured loss.
        #
        # NO DEMONSTRATED MUTATION POWER, stated rather than implied. Four
        # mutations were tried (the SNU predicate to False, the cost gate to
        # always-clear, `_staged` skipping the deep stage, the staged fill price
        # to the bar close) and none broke this equality, because under this
        # construction the two readings are structurally identical in money:
        # the deep rung fills at its own limit either way and both tranches fire
        # at their declared prices on the same bars. The equality is a property
        # of the fixture, not a coincidence a mutant can disturb. It is kept as
        # the recorded observation behind the locality claim, and as a tripwire
        # if the gate or the count ever starts moving the cash; it is not
        # evidence on its own.
        for name, tail in (("rising", self.RISING), ("collapsing", self.COLLAPSING)):
            with self.subTest(name):
                high = summarise(
                    self._walk(self._tape(self.HIGH_FIRST, tail)), declared_floor=FLOOR
                )
                low = summarise(self._walk(self._tape(self.LOW_FIRST, tail)), declared_floor=FLOOR)
                self.assertAlmostEqual(high.pnl_cash, low.pnl_cash, places=9)

    def test_the_two_continuations_do_not_both_end_in_the_same_place(self) -> None:
        # The existence control for the test above. If both tails produced one
        # outcome, equal cash across readings would prove nothing about the
        # continuation mattering at all.
        rising = summarise(
            self._walk(self._tape(self.LOW_FIRST, self.RISING)), declared_floor=FLOOR
        )
        collapsing = summarise(
            self._walk(self._tape(self.LOW_FIRST, self.COLLAPSING)), declared_floor=FLOOR
        )
        self.assertGreater(rising.pnl_cash, 0.0)
        self.assertLess(collapsing.pnl_cash, 0.0)


if __name__ == "__main__":
    unittest.main()
