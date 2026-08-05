from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    TrancheExit,
    execute_tranche_exit,
)

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker


class TestExecuteTrancheExit(unittest.TestCase):
    def _setup(self, owned=100, sl_qty=100):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.set_position("KO", owned, avg_price=15.0)
        sl_id = b.add_resting_sell("KO", sl_qty, 13.0, order_type="StopIfTraded")
        sl = next(o for o in b.list_working_sell_orders() if o.order_id == sl_id)
        return b, uic, sl

    def test_amends_sl_down_then_sells_tranche(self):
        b, uic, sl = self._setup(owned=100, sl_qty=100)
        ok = execute_tranche_exit(
            b,
            uic=uic,
            exit=TrancheExit("tp1", 40, 16.0),
            sl_leg=sl,
            stop_price=13.0,
            request_ref="KO-g0",
        )
        self.assertTrue(ok)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 60.0)  # 100 - 40 sold
        sl_now = next(o for o in b.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl_now.amount, 60.0)  # SL shrunk 100 -> 60

    def test_sell_clamped_to_live_owned(self):
        b, uic, sl = self._setup(owned=30, sl_qty=30)
        ok = execute_tranche_exit(
            b,
            uic=uic,
            exit=TrancheExit("tp1", 50, 16.0),
            sl_leg=sl,
            stop_price=13.0,
            request_ref="KO-g0",
        )
        self.assertTrue(ok)
        self.assertEqual(
            b.get_positions_by_uic(uic).quantity, 0.0
        )  # sold min(50,30)=30, never short

    def test_noop_when_live_owned_zero(self):
        b, uic, sl = self._setup(owned=100, sl_qty=100)
        b.set_position("KO", 0, avg_price=15.0)  # closed out from under us
        ok = execute_tranche_exit(
            b,
            uic=uic,
            exit=TrancheExit("tp1", 40, 16.0),
            sl_leg=sl,
            stop_price=13.0,
            request_ref="KO-g0",
        )
        self.assertFalse(ok)

    def test_full_close_cancels_sl_not_amend_to_zero(self):
        # The last tranche closes the position -> new_sl_qty == 0. Saxo rejects a
        # zero-qty amend, so the SL is CANCELLED (the proven manual-close path).
        b, uic, sl = self._setup(owned=50, sl_qty=50)
        ok = execute_tranche_exit(
            b,
            uic=uic,
            exit=TrancheExit("tp3", 50, 20.0),
            sl_leg=sl,
            stop_price=13.0,
            request_ref="KO-g0",
        )
        self.assertTrue(ok)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 0.0)  # flat
        remaining = [o for o in b.list_working_sell_orders() if o.order_type == "StopIfTraded"]
        self.assertEqual(remaining, [], "full close cancels the SL (no zero-qty amend)")


if __name__ == "__main__":
    unittest.main()
