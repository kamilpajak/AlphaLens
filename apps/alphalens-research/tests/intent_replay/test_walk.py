"""The bar walk (spec sections 4.4, 4.6, 5.1 and 6.3).

Fixtures are built from the PUBLISHED template `pullback-trailing-stop.json`
wherever a number is load-bearing: rungs 68.00 and 66.50 carrying 60% and 40%
of a 1500 budget, a disaster stop of 63.00, and `trailing_stop` with
`arm_trigger_r` 0.5 and `trail_frac` 0.6. The configuration is the section 5.2
block `test_config.CANONICAL`, so no value here is invented.
"""

from __future__ import annotations

import copy
import unittest
from typing import Any

from broker_contract.trade_intent.schema import ReanchorOnFill, TrailingStop
from intent_replay.bars import Bar
from intent_replay.config import RunConfig
from intent_replay.interpreter import DeclaredTranche, PendingEntry, Plan
from intent_replay.trace import KINDS
from intent_replay.walk import walk

from tests.intent_replay.test_config import CANONICAL

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
    data = copy.deepcopy(dict(CANONICAL))
    data.update(overrides)
    return RunConfig.from_jsonable(data)


def _plan(
    *,
    entries: tuple[PendingEntry, ...] = RUNGS,
    notional: float = 1500.0,
    floor: float = FLOOR,
    take_profit: float | None = None,
    tranches: tuple[DeclaredTranche, ...] = (),
    reaction: Any = None,
) -> Plan:
    return Plan(
        entries=entries,
        notional=notional,
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
        self.assertEqual(result.ambiguous_bars, 0)

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
        self.assertEqual(result.ambiguous_bars, 0)

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
        self.assertEqual(result.ambiguous_bars, 1)

    def test_the_bar_is_not_ambiguous_when_the_gate_refuses_the_tranche(self) -> None:
        # The counter must measure what it NAMES. A tranche the cost gate would
        # decline could not have fired first, so both orders book the same cash
        # and the bar decided nothing.
        result = self._walk(
            (DeclaredTranche(tranche_index=0, price=REFUSED_LEVEL, fraction=1.0),),
            _bar(WALK_START + MINUTE, 67.0, 68.5, 62.0),
        )
        self.assertLess(REFUSED_LEVEL, THRESHOLDS[1.0])
        self.assertEqual(result.ambiguous_bars, 0)

    def test_any_unfired_tranche_makes_the_bar_ambiguous_not_only_the_leading_one(self) -> None:
        # The ladder need not rise, so "the leading tranche" is not a thing to
        # test. `/edge` asks the same question of ANY unhit tranche
        # (`ladder_replay.py:957-959`).
        tranches = (
            DeclaredTranche(tranche_index=0, price=80.0, fraction=0.5),
            DeclaredTranche(tranche_index=1, price=74.0, fraction=0.5),
        )
        result = self._walk(tranches, _bar(WALK_START + MINUTE, 72.0, 74.5, 62.0))
        self.assertEqual(result.ambiguous_bars, 1)

    def test_a_tranche_that_already_fired_leaves_a_later_stop_bar_unambiguous(self) -> None:
        result = self._walk(
            (DeclaredTranche(tranche_index=0, price=70.0, fraction=0.4),),
            _bar(WALK_START + MINUTE, 69.0, 70.2, 68.8),
            _bar(WALK_START + 2 * MINUTE, 68.0, 70.5, 62.0),
        )
        self.assertEqual(len([e for e in result.events if e.kind == "tp_fired"]), 1)
        self.assertEqual(result.events[-1].reason, "stop")
        self.assertEqual(result.ambiguous_bars, 0)


class RungAndTakeProfitTieTest(unittest.TestCase):
    """Row 4 of the section 4.4 table: a rung BELOW the bar's open and a
    take-profit on the same bar.

    A rung at or above the open is already through at the first print, so its
    fill is not in question. A rung below the open fills somewhere inside the
    bar, and the tape does not say whether that happened before or after the
    high reached a tranche. The convention takes the worse reading: the tranche
    fires on the position held BEFORE that rung filled.
    """

    # The published template's ladder, one tranche above the blend, and a bar
    # whose open sits BETWEEN the two rungs so rung 1 is the deep one.
    TIE = _bar(WALK_START, 67.0, 68.6, 66.4)
    TRANCHE = 68.5

    def _walk(self, fraction: float, *bars: Bar, first: Bar | None = None) -> Any:
        plan = _plan(
            entries=RUNGS,
            notional=1500.0,
            tranches=(DeclaredTranche(tranche_index=0, price=self.TRANCHE, fraction=fraction),),
        )
        return walk(plan, _config(), ((first or self.TIE), *bars))

    def test_the_tranche_fires_on_what_was_held_before_the_deep_rung(self) -> None:
        # Measured 2026-09-28: taking the other reading nets +37.898054, which is
        # this one plus exactly rung 1's profit (9.0226 units x 2.00 = 18.045113).
        result = self._walk(1.0, _bar(WALK_START + MINUTE, 66.0, 66.5, 62.0))
        self.assertEqual(
            _kinds(result), ["entry_filled", "stop_placed", "tp_fired", "position_closed"]
        )
        fired = next(event for event in result.events if event.kind == "tp_fired")
        self.assertEqual(fired.units, 900.0 / 68.0)
        self.assertEqual(result.units_filled, 900.0 / 68.0)
        self.assertEqual(result.outcome, "closed_tp")
        self.assertEqual(result.ambiguous_bars, 1)

    def test_a_ladder_that_wants_less_than_is_held_decides_nothing(self) -> None:
        # The counter must measure what it NAMES. When the whole touched ladder
        # wants fewer units than are already held, the clamp cannot bind, so both
        # orders sell the same units at the same price and the bar decided
        # nothing - the same argument that keeps row 2 out of the count.
        result = self._walk(0.5)
        # The tranche still fires BEFORE the deep rung - that order is the rule,
        # not the exception. What differs is that here it changes nothing.
        self.assertEqual(
            _kinds(result),
            ["entry_filled", "stop_placed", "tp_fired", "entry_filled", "horizon_open"],
        )
        self.assertEqual(result.ambiguous_bars, 0)
        self.assertEqual(result.units_filled, 900.0 / 68.0 + 600.0 / 66.5)

    def test_a_rung_at_or_above_the_open_is_never_in_question(self) -> None:
        # Both rungs are through at the open here, so there is no intra-bar fill
        # to order against the tranche.
        result = self._walk(1.0, first=_bar(WALK_START, 66.0, 68.6, 65.9))
        self.assertEqual(result.ambiguous_bars, 0)
        self.assertEqual(result.units_filled, 900.0 / 68.0 + 600.0 / 66.5)
        self.assertEqual(result.outcome, "closed_tp")

    def test_a_bar_that_also_reaches_the_stop_fills_every_rung_and_stops_out(self) -> None:
        # Rows 1, 2 and 4 are pairwise inconsistent on a bar that touches all
        # three levels, and the convention settles it: filling both rungs and
        # then stopping out is the worst reading, so the stop dominates and row 4
        # does not apply. Without this the deep rung would escape the loss.
        result = self._walk(1.0, first=_bar(WALK_START, 67.0, 68.6, 62.0))
        self.assertEqual(
            _kinds(result), ["entry_filled", "stop_placed", "entry_filled", "position_closed"]
        )
        closed = result.events[-1]
        self.assertEqual((closed.reason, closed.price), ("stop", FLOOR))
        self.assertEqual(closed.units, 900.0 / 68.0 + 600.0 / 66.5)
        self.assertEqual(result.outcome, "closed_stop")
        self.assertEqual(result.ambiguous_bars, 1)


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


if __name__ == "__main__":
    unittest.main()
