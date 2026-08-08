from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.live_exit_engine import ManagedExit, run_live_exits
from broker_contract.price_feed import PricePoint
from broker_contract.sizing import TpTranchePlan

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker

_DECISION_EVENT_TIME = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)


class _FakeFeed:
    def __init__(self, prices, *, bid=None, ask=None, source="test"):
        self._p = prices  # {uic: price|None}
        self._bid = bid
        self._ask = ask
        self._source = source

    def latest(self, uic):
        px = self._p.get(uic)
        if px is None:
            return None
        return PricePoint(
            uic=uic,
            bid=self._bid if self._bid is not None else px,
            ask=self._ask if self._ask is not None else px,
            event_time=_DECISION_EVENT_TIME,
            received_at=_DECISION_EVENT_TIME,
            source=self._source,
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
        records: list[dict] = []
        with mock.patch.object(cl, "_append_standalone_stop_journal", side_effect=records.append):
            n = run_live_exits(b, feed, managed)
        self.assertEqual(n, 2)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 20.0)  # sold 50 + 30
        sl_now = next(o for o in b.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl_now.amount, 20.0)  # SL tracks remaining owned, not stale 70
        # Each tranche carries its OWN market-SELL join key (non-None, distinct) —
        # guards against a shared/stale sell_order_id leaking across the batch.
        sell_ids = [
            r["telemetry"]["sell_order_id"] for r in records if r.get("kind") == "tranche_fired"
        ]
        self.assertEqual(len(sell_ids), 2)
        self.assertTrue(all(sell_ids))
        self.assertEqual(len(set(sell_ids)), 2)

    def test_fire_stamps_decision_telemetry_from_the_pricepoint(self):
        # (test c) A fire journals decision-side telemetry sourced from the
        # in-scope PricePoint (the BID drives the sell decision) and the
        # TrancheExit. Capture the journal line via the shared append seam.
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.set_position("KO", 100, avg_price=15.0)
        b.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        feed = _FakeFeed({uic: 16.5}, bid=16.5, ask=16.7, source="saxo-live-l1")
        managed = [
            ManagedExit(
                uic=uic,
                tp_tranches=(_tr(0, 16.0, 0.5),),
                reference_qty=100,
                stop_price=13.0,
                already_fired=frozenset(),
            )
        ]
        records: list[dict] = []
        with mock.patch.object(cl, "_append_standalone_stop_journal", side_effect=records.append):
            n = run_live_exits(b, feed, managed)
        self.assertEqual(n, 1)
        fired = [r for r in records if r.get("kind") == "tranche_fired"]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["uic"], uic)
        self.assertEqual(fired[0]["tag"], "tp1")
        # (test d) the market-SELL order id FakeBroker.place_market_order returns
        # (add_resting_sell bumps seq to 1 -> "resting-1"; the sell is seq 2).
        self.assertEqual(
            fired[0]["telemetry"],
            {
                "decision_bid": 16.5,
                "decision_ask": 16.7,
                "decision_mid": 16.6,
                "spread_abs": 16.7 - 16.5,
                "target_price": 16.0,
                "qty": 50,
                "event_time": _DECISION_EVENT_TIME.isoformat(),
                "source": "saxo-live-l1",
                "sell_order_id": "mkt-2",
            },
        )

    def test_broker_without_list_working_sell_orders_skips_the_uic_no_crash(self):
        """list_working_sell_orders is NOT part of the Broker Protocol
        (broker_contract/contract.py). Calling it unguarded would let an
        AttributeError escape the `except BrokerError` boundary and kill the
        whole tick -- one uic missing a capability must not do that. Mirrors
        the getattr(broker, "list_working_sell_orders", None) convention
        control_loop.py already uses at two sites."""
        b, uic, feed, managed = self._mk(price=16.5)  # would otherwise fire tp1

        class _BrokerWithoutListSells(FakeBroker):
            list_working_sell_orders = None

        no_cap = _BrokerWithoutListSells()
        no_cap._positions = b._positions
        no_cap._orders = b._orders

        with self.assertLogs(
            "alphalens_pipeline.brokers.automanager.live_exit_engine", level="WARNING"
        ) as cm:
            n = run_live_exits(no_cap, feed, managed)
        self.assertEqual(n, 0)
        self.assertEqual(no_cap.get_positions_by_uic(uic).quantity, 100.0)  # nothing sold
        self.assertTrue(any(str(uic) in line for line in cm.output), cm.output)


if __name__ == "__main__":
    unittest.main()
