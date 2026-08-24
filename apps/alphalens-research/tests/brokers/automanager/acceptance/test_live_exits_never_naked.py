"""GUARANTEE — the live TP-tranche exit engine (INC-5) never breaks
never-naked / no-oversell, with the flag both ON and OFF.

The critical coordination proof: ``_run_live_exits_pass`` runs IMMEDIATELY
BEFORE ``_run_protection_pass`` in ``run_once`` so a tranche sell that fails
AFTER its standalone SL was already shrunk is re-covered by the SAME tick's
protection pass, on the SAME thread — never left naked until the next poll.
"""

from __future__ import annotations

import unittest

from broker_contract.contract import BrokerError
from broker_contract.sizing import TpTranchePlan

from .world import ManagerWorld

_FILL = 15.0
"""The realised entry these fixtures fill at. Explicit (the world default is
50.0) so the tp1 target at 16.0 is a genuine +667 bps gain: the #1112 exit cost
gate measures from the realised entry, and would refuse a tranche that sits
BELOW it."""


def _tr(index: int, target: float, pct: float) -> TpTranchePlan:
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_pct=pct,
        r_multiple=1.0,
        tag=f"tp{index + 1}",
    )


class LiveExitsNeverBreakNeverNaked(unittest.TestCase):
    def test_a_touched_tranche_fires_and_stays_exactly_covered_never_oversold(self) -> None:
        world = ManagerWorld(self)
        world.live_exits_are_enabled()
        world.amend_is_enabled()  # in-place SL resize -> a single clean stop
        world.entry_fills_with_tranches(
            "KO", shares=100, price=_FILL, stop=13.0, tranches=(_tr(0, 16.0, 0.5),)
        )

        # Tick 1: a fresh fill has no resting SL yet -- the engine requires a
        # SOLE standalone stop to shrink, so the live-exits pass is a no-op
        # this tick; the protection pass places the initial covering stop.
        world.run_tick()
        world.assert_protected("KO")
        world.assert_not_oversold("KO")
        world.assert_exactly_covered("KO")

        # Tick 2: price touches the tp1 target -- the engine shrinks the SL
        # then market-sells the tranche, and the SAME tick's protection pass
        # confirms SL == remaining owned (never over-hedged, never naked).
        world.price_is("KO", 16.5)
        world.run_tick()

        self.assertAlmostEqual(world.owned("KO"), 50.0)
        world.assert_protected("KO")
        world.assert_not_oversold("KO")
        world.assert_exactly_covered("KO")

    def test_a_failed_market_sell_after_the_sl_shrink_is_re_covered_the_same_tick(self) -> None:
        # The critical coordination proof (INC-5 plan, Task 3): the fake
        # broker's amend_stop_amount succeeds (the SL shrinks to the
        # remaining-after-sell size) but the very next place_market_order call
        # is rejected. The tranche never actually sold, so owned is unchanged
        # -- but the SL is now under-sized. The SAME tick's protection pass
        # (the deficit arm) must re-grow it back to the full owned qty before
        # the tick ends -- never-naked restored within one tick, not one poll
        # cycle later.
        world = ManagerWorld(self)
        world.live_exits_are_enabled()
        world.amend_is_enabled()
        world.entry_fills_with_tranches(
            "KO", shares=100, price=_FILL, stop=13.0, tranches=(_tr(0, 16.0, 0.5),)
        )
        world.run_tick()  # establishes the initial 100-share covering SL
        world.assert_exactly_covered("KO")

        world.price_is("KO", 16.5)
        world.market_sell_fails_once(BrokerError("sim: market sell rejected"))
        world.run_tick()  # engine shrinks the SL to 50, then the sell raises

        self.assertAlmostEqual(world.owned("KO"), 100.0)  # the sell never went through
        world.assert_protected("KO")
        world.assert_not_oversold("KO")
        world.assert_exactly_covered("KO")  # the SL is back to the full 100
        # Explicit, not merely implied by assert_exactly_covered: the SL was
        # actually RE-GROWN by the deficit arm, not left untouched by chance.
        self.assertAlmostEqual(world.resting_stop_qty("KO"), 100.0)

    def test_flag_off_a_tranche_target_touch_never_fires(self) -> None:
        # live_exits_are_enabled() is deliberately never called -- the flag
        # stays off, so a touched target changes nothing (byte-identical to
        # pre-INC-5 acceptance behaviour).
        world = ManagerWorld(self)
        world.entry_fills_with_tranches(
            "KO", shares=100, price=_FILL, stop=13.0, tranches=(_tr(0, 16.0, 0.5),)
        )
        world.run_tick()
        world.price_is("KO", 16.5)  # would touch tp1 if the engine ran
        world.run_tick()

        self.assertAlmostEqual(world.owned("KO"), 100.0)
        world.assert_protected("KO")
        world.assert_not_oversold("KO")

    def test_orders_disabled_is_a_clean_no_op_even_with_the_flag_on(self) -> None:
        # ALLOW_ORDERS off means the whole live-exits pass no-ops (see
        # control_loop._live_exits_orders_allowed) -- a touched target never
        # shrinks the SL nor sells, and the position stays covered untouched.
        world = ManagerWorld(self)
        world.live_exits_are_enabled()
        world.entry_fills_with_tranches(
            "KO", shares=100, price=_FILL, stop=13.0, tranches=(_tr(0, 16.0, 0.5),)
        )
        world.run_tick()
        world.orders_are_disabled()
        world.price_is("KO", 16.5)
        world.run_tick()

        self.assertAlmostEqual(world.owned("KO"), 100.0)
        world.assert_protected("KO")
        world.assert_not_oversold("KO")


if __name__ == "__main__":
    unittest.main()
