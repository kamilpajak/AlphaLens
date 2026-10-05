"""GUARANTEE 1 — The safety rails are respected.

In plain terms: the manager will not open new risk when it shouldn't. A master
"orders off" switch, a gross exposure cap, a cash floor, a daily-loss cutoff,
and an emergency KILL file each stop new orders. How many positions are already
open never does by itself: free capital is the only limit on how many picks the
manager takes (#1732). The KILL switch is special: it stops NEW orders but never stops the
manager from protecting positions it already holds.
"""

from __future__ import annotations

import unittest

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

from .world import ManagerWorld


class TheSafetyRailsAreRespected(IsolatedHomeTestCase):
    def test_orders_flow_when_the_master_switch_is_on(self) -> None:
        world = ManagerWorld(self)  # orders enabled by default
        self.assertTrue(world.safety_allows_a_new_pick())

    def test_no_orders_when_the_master_switch_is_off(self) -> None:
        world = ManagerWorld(self)
        world.orders_are_disabled()
        self.assertFalse(world.safety_allows_a_new_pick())

    def test_held_positions_never_stop_a_new_pick_by_their_count(self) -> None:
        world = ManagerWorld(self)
        # GIVEN three positions already held and a fresh pick waiting
        world.entry_fills("KO", shares=100)
        world.entry_fills("MO", shares=100)
        world.entry_fills("PEP", shares=100)
        world.arm("XOM")
        # WHEN a tick runs
        world.run_tick()
        # THEN the pick is still taken: the count of positions is not a limit
        world.assert_picks_placed(1)

    def test_no_new_pick_after_the_daily_loss_cutoff(self) -> None:
        world = ManagerWorld(self)
        # The day is down more than the 3R daily-loss limit.
        self.assertFalse(world.safety_allows_a_new_pick(realized_r_today=-3.5))

    def test_the_kill_switch_refuses_new_picks(self) -> None:
        world = ManagerWorld(self)
        world.kill_switch_is_pulled()
        self.assertFalse(world.safety_allows_a_new_pick())

    def test_the_kill_switch_stops_new_orders_but_keeps_protecting(self) -> None:
        world = ManagerWorld(self)
        # GIVEN a held, protectable position AND a fresh pick waiting
        world.entry_fills("KO", shares=100)
        world.arm("MO")
        # WHEN the emergency KILL file is pulled and a tick runs
        world.kill_switch_is_pulled()
        world.run_tick()
        # THEN no new pick is opened ...
        world.assert_picks_placed(0)
        # ... but the position already held is still protected
        world.assert_protected("KO")

    def test_a_dead_broker_session_halts_new_orders_and_says_so(self) -> None:
        world = ManagerWorld(self)
        world.arm("KO")
        world.auth_chain_is_dead()
        world.run_tick()
        world.assert_picks_placed(0)
        world.assert_alerted(containing="chain")


if __name__ == "__main__":
    unittest.main()
