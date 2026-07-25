"""GUARANTEE 3 — It never sells more than it owns.

In plain terms: the total amount of "sell" resting at the broker to protect a
position never exceeds the number of shares actually held. If a stop is somehow
too large, it is brought back to the owned size. A two-legged OCO exit (a stop
OR a take-profit — only one can ever fire) counts as one commitment, not two.
This is what stops the manager from accidentally flipping a long into a short.
"""

from __future__ import annotations

import unittest

from .world import ManagerWorld


class ItNeverSellsMoreThanItOwns(unittest.TestCase):
    def test_an_oversized_stop_is_brought_back_to_the_owned_size(self) -> None:
        world = ManagerWorld(self)
        # GIVEN 100 KO owned but a stale stop resting for 200 shares
        world.entry_fills("KO", shares=100)
        world.has_resting_stop("KO", shares=200)

        # WHEN the manager runs a tick
        world.run_tick()

        # THEN it is not committed to sell more than the 100 it owns, and stays protected
        world.assert_not_oversold("KO")
        world.assert_protected("KO")

    def test_an_oco_pair_counts_as_one_commitment_not_two(self) -> None:
        world = ManagerWorld(self)
        world.oco_is_enabled()
        # GIVEN a fresh 100 KO fill that the manager protects with its own OCO exit
        world.entry_fills("KO", shares=100)
        world.run_tick()
        world.assert_protected_by_oco("KO")  # the manager placed a stop+take-profit pair
        before = world.resting_order_count()

        # WHEN the manager runs another tick over the now-protected position
        world.run_tick()

        # THEN the OCO pair reads as ONE 100-share commitment (not 200) — not oversold,
        # and the healthy pair is left alone (no churn)
        world.assert_not_oversold("KO")
        world.assert_no_new_orders(before)


if __name__ == "__main__":
    unittest.main()
