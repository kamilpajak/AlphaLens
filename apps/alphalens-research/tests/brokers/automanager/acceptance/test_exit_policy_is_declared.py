"""Promise 7: a pick's stop is managed the way the PICK asked, or not at all.

Before this, "may the daemon move this stop" was a side effect — true when the
plan happened to carry a volatility blob — and *which* move happened came from
one setting shared by every position. A pick could not say what it wanted, and
two picks could not want different things.

These four sentences are the promise, and they read without any of that
machinery. As everywhere in this folder, the REAL manager runs against a fake,
non-Saxo broker, so nothing broker-specific can be hiding in the answer.
"""

from __future__ import annotations

import unittest

from broker_contract.trade_intent.schema import TrailingStop

from .world import ManagerWorld

_ENTRY = 50.0
_STOP = 44.0  # 1R = 6.00, so a 0.5R trail arms at 53.00
_TRAIL = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)


class TestAPickThatAsksForNothingKeepsItsStop(unittest.TestCase):
    def test_a_pick_that_declares_nothing_never_has_its_stop_moved(self):
        world = ManagerWorld(self)
        world.amend_is_enabled()
        world.entry_fills("KO", shares=100, price=_ENTRY, stop=_STOP)  # GIVEN no request
        world.has_resting_stop("KO", shares=100, price=_STOP)
        world.price_rises_to("KO", 70.0)  # GIVEN the price runs away upward
        world.run_ticks(3)  # WHEN cycles run
        world.assert_stop_did_not_move("KO", from_price=_STOP)  # THEN it is where it was

    def test_positive_control_the_same_run_up_does_move_a_stop_that_asked(self):
        """Without this, the promise above would hold on a manager that could not
        move a stop at all."""
        world = ManagerWorld(self)
        world.amend_is_enabled()
        world.entry_fills("KO", shares=100, price=_ENTRY, stop=_STOP, exit_policy=_TRAIL)
        world.has_resting_stop("KO", shares=100, price=_STOP)
        world.price_rises_to("KO", 70.0)
        world.run_ticks(3)
        world.assert_stop_at("KO", _ENTRY + 0.6 * (70.0 - _ENTRY))  # 62.0


class TestAPickThatAsksToTrailDoesTrail(unittest.TestCase):
    def test_a_declared_trail_follows_the_move_with_no_geometry_supplied(self):
        """The sentence that could not be written before. The pick supplies no
        levels to place — only a statement about how its stop should move — and
        the stop follows the move."""
        world = ManagerWorld(self)
        world.amend_is_enabled()
        world.entry_fills("KO", shares=100, price=_ENTRY, stop=_STOP, exit_policy=_TRAIL)
        world.has_resting_stop("KO", shares=100, price=_STOP)

        world.price_rises_to("KO", 60.0)  # +1.67R
        world.run_tick()
        world.assert_stop_at("KO", 56.0)  # 50 + 0.6*10

        world.price_rises_to("KO", 80.0)  # the move continues
        world.run_tick()
        world.assert_stop_at("KO", 68.0)  # 50 + 0.6*30

    def test_the_stop_does_not_follow_the_price_back_down(self):
        world = ManagerWorld(self)
        world.amend_is_enabled()
        world.entry_fills("KO", shares=100, price=_ENTRY, stop=_STOP, exit_policy=_TRAIL)
        world.has_resting_stop("KO", shares=100, price=_STOP)
        world.price_rises_to("KO", 80.0)
        world.run_tick()
        world.assert_stop_at("KO", 68.0)

        world.price_rises_to("KO", 62.0)  # a pullback
        world.run_tick()
        world.assert_stop_at("KO", 68.0)  # THEN the stop held its ground


class TestTwoPicksCanWantDifferentThings(unittest.TestCase):
    def test_one_position_trails_while_another_keeps_its_stop(self):
        """The property the old shared setting made impossible: what happens to a
        stop is a fact about the pick, not about the deployment."""
        world = ManagerWorld(self)
        world.amend_is_enabled()
        world.entry_fills("KO", shares=100, price=_ENTRY, stop=_STOP, exit_policy=_TRAIL)
        world.has_resting_stop("KO", shares=100, price=_STOP)
        world.entry_fills("PEP", shares=100, price=_ENTRY, stop=_STOP)  # asks for nothing
        world.has_resting_stop("PEP", shares=100, price=_STOP)

        world.price_rises_to("KO", 80.0)
        world.price_rises_to("PEP", 80.0)  # the same move on both
        world.run_tick()

        world.assert_stop_at("KO", 68.0)
        world.assert_stop_did_not_move("PEP", from_price=_STOP)


class TestWhereAPickCameFromIsNotHowItIsManaged(unittest.TestCase):
    def test_the_same_request_behaves_the_same_from_either_source(self):
        """Provenance is not policy. A pick parsed from a research brief and one
        typed in by a human are managed by what they ASK FOR, and nothing in the
        manager may branch on which it was."""
        stops = {}
        for source in ("brief", "manual"):
            with self.subTest(source=source):
                world = ManagerWorld(self)
                world.amend_is_enabled()
                world.arm("KO", exit_policy=_TRAIL, source=source)
                world.entry_fills("KO", shares=100, price=_ENTRY, stop=_STOP, exit_policy=_TRAIL)
                world.has_resting_stop("KO", shares=100, price=_STOP)
                world.price_rises_to("KO", 80.0)
                world.run_tick()
                world.assert_stop_at("KO", 68.0)
                stops[source] = world.stop_price("KO")
        # Compare the STOP PRICE, which is the thing provenance could have
        # changed. The first version compared the resting sell quantity — a
        # constant 100 either way, so it could not fail for the reason the test
        # names.
        self.assertEqual(stops["brief"], stops["manual"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
