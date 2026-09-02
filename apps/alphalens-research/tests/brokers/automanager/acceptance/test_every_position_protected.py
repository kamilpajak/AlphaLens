"""GUARANTEE 2 — Every position is protected (never naked).

In plain terms: the moment the manager can see that it holds shares, it makes
sure a protective stop covers ALL of them. It works this out from what the broker
actually reports, not from its own notes — so a position it never placed itself
still gets protected. It never leaves shares exposed, and it never sells more than
it owns while doing so.
"""

from __future__ import annotations

import unittest

from .world import ManagerWorld


class EveryPositionIsProtected(unittest.TestCase):
    def test_a_fresh_fill_gets_a_protective_stop(self) -> None:
        world = ManagerWorld(self)
        # GIVEN a KO trade opened — 100 shares filled, with its stop plan on record
        world.entry_fills("KO", shares=100, stop=44.0)

        # WHEN the manager runs one tick
        world.run_tick()

        # THEN all 100 shares are covered by a resting protective stop
        world.assert_protected("KO")

    def test_a_position_the_manager_never_placed_still_gets_protected(self) -> None:
        world = ManagerWorld(self)
        # GIVEN the account already holds 100 KO with a plan the manager can read
        world.entry_fills("KO", shares=100, stop=44.0)

        # WHEN the manager runs a tick (it discovers the position from broker truth)
        world.run_tick()

        # THEN the discovered position is protected
        world.assert_protected("KO")

    def test_an_already_protected_position_is_left_alone(self) -> None:
        world = ManagerWorld(self)
        # GIVEN 100 KO that already carries a stop covering all 100 shares
        world.entry_fills("KO", shares=100, stop=44.0)
        world.has_resting_stop("KO", shares=100)
        before = world.resting_order_count()

        # WHEN the manager runs a tick
        world.run_tick()

        # THEN it neither doubles up the protection nor oversells
        world.assert_no_new_orders(before)
        world.assert_protected("KO")
        world.assert_not_oversold("KO")

    def test_a_position_with_conflicting_plans_still_gets_a_stop(self) -> None:
        # #1249 — a stale plan from an earlier pick collides with the live one on
        # the netted uic. The merge stays refused, but never-naked wins: the
        # fresh fill is still covered on the first tick.
        world = ManagerWorld(self)
        # GIVEN a KO trade opened, and a stale second plan governing the same uic
        world.entry_fills("KO", shares=100, stop=44.0)
        world.a_stale_plan_also_governs("KO", stop=40.0)

        # WHEN the manager runs one tick
        world.run_tick()

        # THEN all 100 shares are covered despite the plan conflict
        world.assert_protected("KO")

    def test_a_position_that_grows_has_its_protection_grown(self) -> None:
        world = ManagerWorld(self)
        # GIVEN 100 KO, protected on the first tick
        world.entry_fills("KO", shares=100, stop=44.0)
        world.run_tick()
        world.assert_protected("KO")

        # WHEN a second tranche fills and the position grows to 200
        world.grows_position_to("KO", shares=200)
        world.run_tick()

        # THEN all 200 shares are covered, still without overselling
        world.assert_protected("KO")
        world.assert_not_oversold("KO")

    def test_if_the_broker_refuses_an_oco_the_manager_falls_back_to_a_plain_stop(self) -> None:
        world = ManagerWorld(self)
        # GIVEN the richer OCO exit is enabled but the broker will refuse it
        world.oco_is_enabled()
        world.broker_rejects_oco(code="TooFarFromMarket")
        world.entry_fills("KO", shares=100, stop=44.0)

        # WHEN the manager runs a tick
        world.run_tick()

        # THEN it degrades to a plain protective stop — the position is still covered
        world.assert_protected("KO")


if __name__ == "__main__":
    unittest.main()
