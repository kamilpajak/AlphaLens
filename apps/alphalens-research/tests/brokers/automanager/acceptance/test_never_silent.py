"""GUARANTEE 5 — It never fails silently.

In plain terms: whenever something goes wrong or degrades — it cannot place
protection, the broker's records disagree with its own, or the broker session
dies — the manager raises an alert. The operator is always told; problems are
never swallowed.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager import position_manager as pm
from alphalens_pipeline.brokers.automanager.position_manager import PlannedExit, ProtectionView

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

from .world import ManagerWorld, a_divergence, order_cancelled

_UIC = 43070


def _unrestored_trail_message(*, plan_stop: float, trailed: float) -> str:
    """The REAL text of the lost-trailed-level alert, taken from the production
    function rather than written out, so a rewording of that alert moves this
    fixture with it.

    It is the one failure alert whose words overlap the protected-confirmation
    page ("naked stop placed at the plan level ..."), which is what made a
    substring exclusion in :meth:`ManagerWorld.assert_silent` swallow it."""
    plan = PlannedExit(
        uic=_UIC,
        entry_crid="crid-KO-0",
        side="SELL",
        stop_price=plan_stop,
        tp_price=None,
        conflicting=False,
        n_plans=1,
    )
    view = ProtectionView(
        long_positions={},
        all_positions={},
        sell_legs_by_uic={},
        planned_by_uic={_UIC: plan},
        oco_unsupported=frozenset(),
        amend_recently_failed=frozenset(),
        peak_by_uic={},
        last_price_by_uic={},
        trailed_stop_by_uic={_UIC: trailed},
    )
    actions = pm._unrestored_trail_alert(_UIC, plan, view)
    assert len(actions) == 1, f"the production alert did not fire: {actions}"
    return actions[0].reason


class ItNeverFailsSilently(IsolatedHomeTestCase):
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

    def test_conflicting_plans_still_page_the_operator(self) -> None:
        # #1249 — the conservative cover must not silence the ambiguity: the
        # merge-refusal alert still reaches the operator alongside the stop.
        world = ManagerWorld(self)
        world.entry_fills("KO", shares=100, stop=44.0)
        world.a_stale_plan_also_governs("KO", stop=40.0)

        world.run_tick()

        world.assert_alerted(containing="refusing to merge")

    def test_a_stale_plans_terminal_verdict_heals_the_conflict(self) -> None:
        # #1249 part 2, end-to-end healing: the stale plan's bracket resolves
        # CANCELLED without a fill, so the verdict phase retracts its planned
        # line BEFORE the protection pass (run_once ordering) — the conflict
        # never reaches the merge guard, the position is protected off the one
        # remaining plan, and the tick stays quiet.
        world = ManagerWorld(self)
        world.entry_fills("KO", shares=100, stop=44.0)
        world.a_stale_plan_also_governs("KO", stop=40.0)
        world.broker_reports(order_cancelled("KO", crid="crid-KO-stale"))

        world.run_tick()

        world.assert_protected("KO")
        world.assert_silent()

    def test_the_quiet_check_itself_still_catches_a_real_failure(self) -> None:
        # Positive control for assert_silent. It now excludes ONE message by
        # name — the protected-confirmation page (#1621 part 3) — and an
        # exclusion that nothing tests is an exclusion that can widen to
        # everything: emptying the filter left all 49 acceptance tests green.
        # Here a genuine failure alert is present, and the check must refuse.
        world = ManagerWorld(self)
        world.entry_fills("KO", shares=100)
        world.broker_placement_fails_on("KO")

        world.run_tick()

        with self.assertRaises(AssertionError):
            world.assert_silent()

    def test_the_quiet_check_still_sees_a_lost_trailed_level(self) -> None:
        # The exclusion's WIDTH, which the positive control above cannot test:
        # its own failure alert reads "stop placement rejected", and the word
        # that matters is "placed". `_unrestored_trail_alert` is the one
        # production alert that CONTAINS the confirmation page's phrase --
        # "uic N: naked stop placed at the plan level 44.00; trailed level 51.20
        # not restored" -- so a substring exclusion on "stop placed" hid the one
        # message that makes a lost ratchet floor visible.
        world = ManagerWorld(self)
        world._captured_alert_events.append(_unrestored_trail_message(plan_stop=44.0, trailed=51.2))

        with self.assertRaises(AssertionError):
            world.assert_silent()

    def test_a_healthy_tick_is_quiet(self) -> None:
        world = ManagerWorld(self)
        # A normal fill that protects cleanly should NOT spam alerts. Since
        # #1621 part 3 it does send exactly ONE page, the confirmation that the
        # position is now protected, and nothing else. Asserted both ways here
        # so the exclusion inside assert_silent cannot become vacuous.
        world.entry_fills("KO", shares=100)
        world.run_tick()
        world.assert_alerted(containing="stop placed")
        world.assert_silent()


if __name__ == "__main__":
    unittest.main()
