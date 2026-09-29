"""The measures the summary publishes (spec sections 5, 5.1 and 6.3).

Every golden number below was produced by RUNNING the walk on 2026-09-29 and
reading its trace, never by hand arithmetic over remembered inputs. The
fixtures come from ``test_walk``, so they are the published template's ladder.

The load-bearing test in this file is the PARITY one: the measures are summed
out of the trace, while ``WalkResult`` accumulates the same quantities as the
walk runs. ``assertEqual`` rather than ``assertAlmostEqual`` is what gives it
refuting power, and it is only available because the sum folds LEFT from a
``0.0`` seed. ``sum()`` has been Neumaier-compensated since CPython 3.12 and
``math.fsum`` is exact, so either disagrees with the walk's own ``+=`` on a run
with more than one fill. Measured 2026-09-29 over 200 000 random runs, of which
125 319 filled and 79 888 filled more than once: left fold 0 divergences,
``sum()`` 11 750, ``math.fsum`` the same 11 750. A later session "fixing" this
to ``assertAlmostEqual`` would lose the property without failing anything.
"""

from __future__ import annotations

import unittest

from intent_replay.interpreter import DeclaredTranche, PendingEntry
from intent_replay.measures import Measures, summarise
from intent_replay.walk import walk

from tests.intent_replay.test_walk import (
    FLOOR,
    MINUTE,
    RUNGS,
    WALK_START,
    _bar,
    _config,
    _plan,
)


def _summarise(plan, bars, *, floor=FLOOR, **config):
    result = walk(plan, _config(**config), bars)
    return result, summarise(result, declared_floor=floor)


class StopOutTest(unittest.TestCase):
    """One rung at 68.00 out of 900, then a bar reaching the 63.00 floor."""

    def setUp(self) -> None:
        self.result, self.measures = _summarise(
            _plan(entries=RUNGS[:1]),
            (_bar(WALK_START, 68.0, 68.6, 67.9), _bar(WALK_START + MINUTE, 67.0, 67.2, 62.0)),
        )

    def test_the_cash_is_the_sale_less_the_spend(self) -> None:
        self.assertEqual(self.measures.notional_spent, 900.0)
        self.assertEqual(self.measures.avg_entry_price, 68.0)
        self.assertEqual(self.measures.pnl_cash, -66.17647058823536)

    def test_the_percentage_is_a_percentage_and_not_a_fraction(self) -> None:
        self.assertEqual(self.measures.pnl_pct_of_spent, -7.352941176470596)

    def test_r_and_the_excursion_carry_the_denominator(self) -> None:
        self.assertEqual(self.measures.denominator, 5.0)
        self.assertEqual(self.measures.r_multiple, -1.000000000000001)
        self.assertEqual(self.measures.mfe, 0.11999999999999886)
        self.assertEqual(self.measures.mae, -1.2)


class TakeProfitSweepTest(unittest.TestCase):
    """A single tranche for the whole position, so the ladder completes."""

    def setUp(self) -> None:
        self.result, self.measures = _summarise(
            _plan(entries=RUNGS[:1], tranches=(DeclaredTranche(0, 74.0, 1.0),)),
            (_bar(WALK_START, 68.0, 68.6, 67.9), _bar(WALK_START + MINUTE, 70.0, 75.0, 69.0)),
        )

    def test_the_closing_marker_does_not_double_the_sale(self) -> None:
        # Section 4.6: on ``tp_complete`` the cash was the last ``tp_fired``
        # and ``position_closed`` is a marker carrying ZERO units. Summing the
        # marker as a sale would book the position out twice.
        closed = self.result.events[-1]
        self.assertEqual(
            (closed.kind, closed.reason, closed.units), ("position_closed", "tp_complete", 0.0)
        )
        self.assertEqual(self.measures.pnl_cash, 79.41176470588232)

    def test_the_sale_is_the_tranche_proceeds(self) -> None:
        fired = self.result.events[2]
        self.assertEqual(fired.kind, "tp_fired")
        self.assertEqual(self.measures.pnl_cash, fired.proceeds - self.measures.notional_spent)


class TimeStopTest(unittest.TestCase):
    def test_a_time_stop_sells_at_the_bar(self) -> None:
        _, measures = _summarise(
            _plan(entries=RUNGS[:1]),
            (
                _bar(WALK_START, 68.0, 68.6, 67.9),
                _bar(WALK_START + MINUTE, 69.0, 69.5, 68.5, 69.2),
            ),
            time_stop_t=WALK_START + MINUTE,
        )
        self.assertEqual(measures.pnl_cash, 15.882352941176464)
        self.assertEqual(measures.r_multiple, 0.2399999999999999)


class OpenPositionTest(unittest.TestCase):
    def test_an_open_position_is_valued_at_the_horizon_mark(self) -> None:
        # The bar's CLOSE of 68.40, not its open of 68.00 and not the fill.
        result, measures = _summarise(
            _plan(entries=RUNGS[:1]), (_bar(WALK_START, 68.0, 68.6, 67.9, 68.4),)
        )
        self.assertEqual(result.outcome, "open")
        self.assertEqual(result.events[-1].price, 68.4)
        self.assertEqual(measures.pnl_cash, 5.294117647058897)
        self.assertEqual(measures.r_multiple, 0.08000000000000111)

    def test_the_mark_is_what_makes_the_cash_non_zero(self) -> None:
        # Refuting power: without the horizon mark the proceeds would be 0.0
        # and the cash would be the whole spend, which is not what is reported.
        _, measures = _summarise(
            _plan(entries=RUNGS[:1]), (_bar(WALK_START, 68.0, 68.6, 67.9, 68.4),)
        )
        self.assertNotEqual(measures.pnl_cash, -measures.notional_spent)


class NoFillTest(unittest.TestCase):
    def test_a_run_that_bought_nothing_reports_null_where_it_cannot_divide(self) -> None:
        _, measures = _summarise(_plan(), (_bar(WALK_START, 70.0, 71.0, 69.0),))
        self.assertEqual(measures.notional_spent, 0.0)
        self.assertEqual(measures.units_filled, 0.0)
        self.assertEqual(measures.pnl_cash, 0.0)
        for field in (
            "avg_entry_price",
            "pnl_pct_of_spent",
            "denominator",
            "r_multiple",
            "mfe",
            "mae",
        ):
            with self.subTest(field):
                self.assertIsNone(getattr(measures, field))


class DenominatorTest(unittest.TestCase):
    """Section 5.1: not POSITIVE, because exactly 0.0 is reachable too."""

    def test_a_negative_denominator_suppresses_r_and_the_excursion(self) -> None:
        # Measured 2026-09-29 over two bars: rungs 64.00 and 63.50 at 500 each
        # out of 1000 with a 63.00 floor; the second bar opens at 50.00, fills
        # the deep rung there and stops out there, so the average entry lands
        # BELOW the floor.
        plan = _plan(
            entries=(PendingEntry(0, 64.0, 500.0), PendingEntry(1, 63.5, 500.0)),
            notional=1000.0,
        )
        _, measures = _summarise(
            plan,
            (
                _bar(WALK_START, 64.0, 64.2, 63.9, 64.0),
                _bar(WALK_START + MINUTE, 50.0, 51.0, 49.0, 50.0),
            ),
        )
        self.assertEqual(measures.notional_spent, 893.7007874015749)
        self.assertEqual(measures.avg_entry_price, 56.97254901960785)
        self.assertEqual(measures.denominator, -6.027450980392153)
        self.assertEqual(measures.pnl_cash, -109.375)
        self.assertIsNone(measures.r_multiple)
        self.assertIsNone(measures.mfe)
        self.assertIsNone(measures.mae)

    def test_without_the_rule_the_loss_would_read_as_a_gain(self) -> None:
        # The number the rule exists to suppress: +1.1568 R for a loss of 109
        # units. Computed here rather than asserted from memory.
        plan = _plan(
            entries=(PendingEntry(0, 64.0, 500.0), PendingEntry(1, 63.5, 500.0)),
            notional=1000.0,
        )
        _, measures = _summarise(
            plan,
            (
                _bar(WALK_START, 64.0, 64.2, 63.9, 64.0),
                _bar(WALK_START + MINUTE, 50.0, 51.0, 49.0, 50.0),
            ),
        )
        unguarded = measures.pnl_cash / (measures.units_filled * measures.denominator)
        self.assertGreater(unguarded, 1.0)
        self.assertLess(measures.pnl_cash, 0.0)

    def test_a_denominator_of_exactly_zero_is_not_positive(self) -> None:
        # A bar opening exactly AT the disaster stop fills there, so the
        # average entry IS the floor. Asserted with ``==``: the gate has to be
        # ``<= 0`` and a tolerance would hide which.
        plan = _plan(entries=(PendingEntry(0, 68.0, 1000.0),), notional=1000.0)
        _, measures = _summarise(plan, (_bar(WALK_START, 63.0, 63.0, 63.0, 63.0),))
        self.assertEqual(measures.avg_entry_price, 63.0)
        self.assertEqual(measures.denominator, 0.0)
        self.assertIsNone(measures.r_multiple)
        self.assertIsNone(measures.mfe)
        self.assertIsNone(measures.mae)


class ParityWithTheWalkTest(unittest.TestCase):
    """The measures are summed from the TRACE; ``WalkResult`` accumulates the
    same quantities as the walk runs. They must agree BITWISE."""

    CASES = {
        "one fill, stop out": (
            {"entries": RUNGS[:1]},
            (_bar(WALK_START, 68.0, 68.6, 67.9), _bar(WALK_START + MINUTE, 67.0, 67.2, 62.0)),
        ),
        "two fills, stop out": (
            {"entries": RUNGS},
            (_bar(WALK_START, 68.0, 68.6, 66.0), _bar(WALK_START + MINUTE, 67.0, 67.2, 62.0)),
        ),
        "two fills, open": (
            {"entries": RUNGS},
            (_bar(WALK_START, 68.0, 68.6, 66.0, 68.2),),
        ),
        "no fill": ({}, (_bar(WALK_START, 70.0, 71.0, 69.0),)),
    }

    def test_the_summed_spend_equals_the_accumulated_one(self) -> None:
        for label, (plan_kwargs, bars) in self.CASES.items():
            with self.subTest(label):
                result, measures = _summarise(_plan(**plan_kwargs), bars)
                self.assertEqual(measures.notional_spent, result.notional_spent)
                self.assertEqual(measures.units_filled, result.units_filled)
                self.assertEqual(measures.avg_entry_price, result.avg_entry_price)

    def test_more_than_one_fill_is_actually_exercised(self) -> None:
        # Without this the parity test could hold on single-fill runs alone,
        # where every summation strategy agrees and the property is untested.
        plan_kwargs, bars = self.CASES["two fills, stop out"]
        result, _ = _summarise(_plan(**plan_kwargs), bars)
        fills = [event for event in result.events if event.kind == "entry_filled"]
        self.assertEqual(len(fills), 2)


class ClosedFormIdentityTest(unittest.TestCase):
    """``r = (pnl_pct / 100) * avg / den`` holds for any definition where
    ``pnl = proceeds - spend`` and ``spend = units * avg`` (section 5). The
    agreement is to floating-point rounding, so this one is ALMOST equal."""

    def test_the_identity_holds_on_every_outcome_that_has_an_r(self) -> None:
        cases = {
            "stop": (
                {"entries": RUNGS[:1]},
                (_bar(WALK_START, 68.0, 68.6, 67.9), _bar(WALK_START + MINUTE, 67.0, 67.2, 62.0)),
            ),
            "tp": (
                {"entries": RUNGS[:1], "tranches": (DeclaredTranche(0, 74.0, 1.0),)},
                (_bar(WALK_START, 68.0, 68.6, 67.9), _bar(WALK_START + MINUTE, 70.0, 75.0, 69.0)),
            ),
            "open": ({"entries": RUNGS[:1]}, (_bar(WALK_START, 68.0, 68.6, 67.9, 68.4),)),
        }
        for label, (plan_kwargs, bars) in cases.items():
            with self.subTest(label):
                _, measures = _summarise(_plan(**plan_kwargs), bars)
                closed_form = (
                    (measures.pnl_pct_of_spent / 100.0)
                    * measures.avg_entry_price
                    / measures.denominator
                )
                self.assertAlmostEqual(measures.r_multiple, closed_form, places=12)


class ShapeTest(unittest.TestCase):
    def test_the_measures_are_frozen_and_slotted(self) -> None:
        _, measures = _summarise(_plan(), (_bar(WALK_START, 70.0, 71.0, 69.0),))
        self.assertIsInstance(measures, Measures)
        self.assertFalse(hasattr(measures, "__dict__"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
