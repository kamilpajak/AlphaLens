from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager.live_exit_engine import ManagedExit, run_live_exits
from broker_contract.price_feed import PricePoint
from broker_contract.sizing import TpTranchePlan

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker


class _FakeFeed:
    def __init__(self, prices):
        self._p = prices  # {uic: price|None}

    def latest(self, uic):
        px = self._p.get(uic)
        return (
            None
            if px is None
            else PricePoint(
                uic=uic,
                bid=px,
                ask=px,
                event_time=dt.datetime(2026, 8, 5, tzinfo=dt.UTC),
                received_at=dt.datetime(2026, 8, 5, tzinfo=dt.UTC),
                source="test",
            )
        )


def _tr(index, target, pct):
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_pct=pct,
        r_multiple=1.0,
        tag=f"tp{index + 1}",
    )


class TestRunLiveExits(unittest.TestCase):
    def _mk(self, price):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.set_position("KO", 100, avg_price=15.0)
        b.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        feed = _FakeFeed({uic: price})
        managed = [
            ManagedExit(
                uic=uic,
                tp_tranches=(_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3)),
                reference_qty=100,
                stop_price=13.0,
                already_fired=frozenset(),
            )
        ]
        return b, uic, feed, managed

    def test_touch_fires_tranche_and_shrinks_sl(self):
        b, uic, feed, managed = self._mk(price=16.5)
        n = run_live_exits(b, feed, managed)
        self.assertEqual(n, 1)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 50.0)

    def test_stale_price_vetoes_all_fires(self):
        b, uic, feed, managed = self._mk(price=None)  # feed.latest -> None
        n = run_live_exits(b, feed, managed)
        self.assertEqual(n, 0)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 100.0)

    def test_gap_through_fires_both_and_sl_tracks_remaining_owned(self):
        # price crosses tp1(16, 50%) AND tp2(18, 30%) of ref 100 in ONE pass.
        # Guards the batch bug: the 2nd amend must use LIVE owned, not a stale
        # captured sl_leg.amount (which would set the SL to 100-30=70, over-hedged).
        b, uic, feed, managed = self._mk(price=18.5)
        n = run_live_exits(b, feed, managed)
        self.assertEqual(n, 2)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 20.0)  # sold 50 + 30
        sl_now = next(o for o in b.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl_now.amount, 20.0)  # SL tracks remaining owned, not stale 70


if __name__ == "__main__":
    unittest.main()
