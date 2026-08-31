"""GUARANTEE 5 — It never fails silently.

In plain terms: whenever something goes wrong or degrades — it cannot place
protection, the broker's records disagree with its own, or the broker session
dies — the manager raises an alert. The operator is always told; problems are
never swallowed.
"""

from __future__ import annotations

import unittest

from .world import ManagerWorld, a_divergence


class ItNeverFailsSilently(unittest.TestCase):
    def test_it_alerts_when_it_cannot_place_protection(self) -> None:
        world = ManagerWorld(self)
        world.entry_fills("KO", shares=100)
        world.broker_placement_fails_on("KO")

        world.run_tick()

        # THEN it says specifically that placing the protective stop failed
        world.assert_alerted(containing="placement failed")

    def test_it_alerts_when_the_broker_and_its_records_disagree(self) -> None:
        world = ManagerWorld(self)
        # GIVEN the reconcile step finds a divergence between journal and broker
        world.broker_reports(a_divergence("KO"))

        world.run_tick()

        # THEN it says specifically that a divergence was found
        world.assert_alerted(containing="divergence")

    def test_it_alerts_when_the_broker_session_is_dead(self) -> None:
        world = ManagerWorld(self)
        world.arm("KO")
        world.auth_chain_is_dead()

        world.run_tick()

        world.assert_alerted(containing="chain")

    def test_a_filled_stop_is_announced(self) -> None:
        # #1219 — the gap this guarantee existed for: a real-money position
        # closed by its protective stop used to vanish without a word.
        world = ManagerWorld(self)
        world.entry_fills("KO", shares=100)
        world.run_tick()  # protection places + journals the standalone stop
        world.stop_fills("KO")

        world.run_tick()

        world.assert_alerted(containing="stop")
        world.assert_flat("KO")

    def test_a_healthy_tick_is_quiet(self) -> None:
        world = ManagerWorld(self)
        # A normal fill that protects cleanly should NOT spam alerts.
        world.entry_fills("KO", shares=100)
        world.run_tick()
        world.assert_silent()


if __name__ == "__main__":
    unittest.main()
