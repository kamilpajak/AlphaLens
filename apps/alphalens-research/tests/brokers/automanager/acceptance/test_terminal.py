"""GUARANTEE 6 — It manages each position to its end.

In plain terms: when an entry fills, the position ends the tick protected; and
when an entry is cancelled or rejected, any leftover child orders attached to it
are cleaned up — no orphan orders are left behind at the broker.
"""

from __future__ import annotations

import unittest

from .world import ManagerWorld, order_cancelled


class ItManagesPositionsToTheirEnd(unittest.TestCase):
    def test_a_filled_entry_ends_the_tick_protected(self) -> None:
        world = ManagerWorld(self)
        world.entry_fills("KO", shares=100)

        world.run_tick()

        world.assert_protected("KO")

    def test_leftover_children_are_cancelled_when_the_entry_is_cancelled(self) -> None:
        world = ManagerWorld(self)
        # GIVEN a cancelled entry that still has a working child order attached
        child = world.has_resting_stop("KO", shares=100)
        world.working_children_for("entry-KO", (child,))
        world.broker_reports(order_cancelled("KO", request_id="entry-KO"))

        # WHEN the manager runs a tick
        world.run_tick()

        # THEN the orphaned child order is cleaned up
        world.assert_order_gone(child)


if __name__ == "__main__":
    unittest.main()
