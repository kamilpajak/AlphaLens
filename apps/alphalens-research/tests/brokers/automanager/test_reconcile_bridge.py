"""Hermetic tests for the reconcile-bridge adapter.

verdicts is a thin faithful delegate to reconcile_brackets — must return exactly
what the engine returns (no reclassification) and forward the today seam so the
trading-day PAST-TTL sweep is drivable from the control loop.
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.reconcile_bridge import verdicts
from alphalens_pipeline.brokers.contract import OrderState, OrderStatus
from alphalens_pipeline.brokers.reconcile import reconcile_brackets

_TS = "2026-07-06T18:00:00+00:00"
_TODAY_FRESH = dt.date(2026, 7, 8)
_TODAY_STALE = dt.date(2026, 7, 17)


def _order_state(order_id: str, status: OrderStatus) -> OrderState:
    return OrderState(
        order_id=order_id, status=status, instrument=None, filled_quantity=0.0, raw_status=""
    )


class _StubBroker:
    name = "stub"

    def __init__(self, open_orders: list[OrderState]) -> None:
        self._open_orders = open_orders

    def list_open_orders(self) -> list[OrderState]:
        return list(self._open_orders)


def _record() -> dict[str, Any]:
    return {
        "execution_config_version": "execution-v1-test",
        "ts": _TS,
        "brief_date": "2026-07-06",
        "ticker": "KO",
        "mic": "XNYS",
        "uic": "307",
        "brackets": [
            {
                "client_request_id": "rid-1",
                "entry_order_id": "E-1",
                "exit_order_ids": ["T-1", "S-1"],
                "qty": 2,
                "entry": 82.0,
                "stop": 78.0,
                "tp": 90.0,
                "ttl": 5,
            }
        ],
        "precheck": [],
    }


class ReconcileBridgeTests(unittest.TestCase):
    def test_verdicts_matches_reconcile_brackets_exactly(self) -> None:
        via_bridge = verdicts(
            [_record()], _StubBroker([_order_state("E-1", OrderStatus.WORKING)]), today=_TODAY_FRESH
        )
        direct = reconcile_brackets(
            [_record()], _StubBroker([_order_state("E-1", OrderStatus.WORKING)]), today=_TODAY_FRESH
        )
        self.assertEqual(via_bridge, direct)

    def test_today_is_forwarded_so_past_ttl_divergence_surfaces(self) -> None:
        stale = verdicts(
            [_record()], _StubBroker([_order_state("E-1", OrderStatus.WORKING)]), today=_TODAY_STALE
        )
        self.assertEqual(len(stale), 1)
        self.assertTrue(stale[0].divergence)
        self.assertIn("PAST-TTL", stale[0].verdict)


if __name__ == "__main__":
    unittest.main()
