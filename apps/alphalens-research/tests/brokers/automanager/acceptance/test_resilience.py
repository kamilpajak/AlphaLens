"""GUARANTEE 4 — It keeps going when the broker misbehaves.

In plain terms: a problem with one position must never stop the manager from
protecting the others, and a broker that refuses the richer exit must never leave
a position with no protection at all — it falls back to a plain stop.
"""

from __future__ import annotations

import unittest

from .world import ManagerWorld


class ItKeepsGoingWhenTheBrokerMisbehaves(unittest.TestCase):
    def test_a_broker_failure_on_one_ticker_does_not_starve_the_others(self) -> None:
        world = ManagerWorld(self)
        # GIVEN two positions, and the broker fails every write for KO
        world.entry_fills("KO", shares=100)
        world.entry_fills("MO", shares=100)
        world.broker_placement_fails_on("KO")

        # WHEN the manager runs a tick
        world.run_tick()

        # THEN MO is still protected despite KO failing ...
        world.assert_protected("MO")
        # ... and KO's failure is not swallowed
        world.assert_alerted(containing="placement failed")

    def test_an_oco_rejection_degrades_to_a_plain_stop_never_to_nothing(self) -> None:
        world = ManagerWorld(self)
        world.oco_is_enabled()
        world.broker_rejects_oco()
        world.entry_fills("KO", shares=100)

        world.run_tick()

        world.assert_protected("KO")


if __name__ == "__main__":
    unittest.main()
