"""GUARANTEE 1 — The safety rails are respected.

In plain terms: the manager will not open new risk when it shouldn't. A master
"orders off" switch, a cap on how many positions can be open at once, a gross
exposure cap, a daily-loss cutoff, and an emergency KILL file each stop new
orders. The KILL switch is special: it stops NEW orders but never stops the
manager from protecting positions it already holds.
"""

from __future__ import annotations

import unittest

from .world import ManagerWorld


class TheSafetyRailsAreRespected(unittest.TestCase):
    def test_orders_flow_when_the_master_switch_is_on(self) -> None:
        world = ManagerWorld(self)  # orders enabled by default
        self.assertTrue(world.safety_allows_a_new_pick())

    def test_no_orders_when_the_master_switch_is_off(self) -> None:
        world = ManagerWorld(self)
        world.orders_are_disabled()
        self.assertFalse(world.safety_allows_a_new_pick())

    def test_no_new_pick_once_the_open_position_limit_is_reached(self) -> None:
        world = ManagerWorld(self)
        # The default cap is 3 open positions/brackets.
        self.assertFalse(world.safety_allows_a_new_pick(open_positions=3))

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
