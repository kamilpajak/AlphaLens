"""Hermetic tests for the orphan-sweeper (place-before-journal crash detector).

On start it flags any open ORDER whose id the journal never recorded (entry +
exit ids) and any open POSITION whose ExternalReference is absent from the
journal's client_request_ids. Strictly read-only + alert-only; degrades to an
order-only sweep when the broker lacks the position-reference capability.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.orphan_sweeper import Orphan, sweep
from broker_contract.contract import OrderState, OrderStatus


def _order_state(order_id: str, *, side: str | None = None) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=0.0,
        raw_status="",
        side=side,
    )


class _FullStubBroker:
    name = "stub-full"

    def __init__(self, *, open_orders: list[OrderState], open_refs: list[str]) -> None:
        self._open_orders = open_orders
        self._open_refs = open_refs

    def list_open_orders(self) -> list[OrderState]:
        return list(self._open_orders)

    def get_open_position_references(self) -> list[str]:
        return list(self._open_refs)

    def get_closed_position_rows(self) -> list[dict[str, Any]]:
        return []


class _OrdersOnlyStubBroker:
    name = "stub-orders-only"

    def __init__(self, *, open_orders: list[OrderState]) -> None:
        self._open_orders = open_orders

    def list_open_orders(self) -> list[OrderState]:
        return list(self._open_orders)


def _record() -> dict[str, Any]:
    return {
        "brackets": [
            {
                "client_request_id": "rid-1",
                "entry_order_id": "E-1",
                "exit_order_ids": ["T-1", "S-1"],
                "qty": 2,
            }
        ]
    }


class OrphanSweeperTests(unittest.TestCase):
    def test_flags_unjournaled_order_and_position(self) -> None:
        broker = _FullStubBroker(
            open_orders=[_order_state("E-1"), _order_state("X-9")],
            open_refs=["rid-1", "rid-orphan"],
        )
        orphans = sweep(broker, [_record()])
        self.assertIn(Orphan(order_id="X-9", external_reference="", kind="order"), orphans)
        self.assertIn(
            Orphan(order_id="", external_reference="rid-orphan", kind="position"), orphans
        )
        self.assertEqual(len(orphans), 2)

    def test_all_known_ids_yield_no_orphans(self) -> None:
        broker = _FullStubBroker(
            open_orders=[_order_state("E-1"), _order_state("T-1"), _order_state("S-1")],
            open_refs=["rid-1"],
        )
        self.assertEqual(sweep(broker, [_record()]), [])

    def test_degrades_to_orders_only_without_position_capability(self) -> None:
        broker = _OrdersOnlyStubBroker(open_orders=[_order_state("X-9")])
        self.assertEqual(
            sweep(broker, [_record()]),
            [Orphan(order_id="X-9", external_reference="", kind="order")],
        )

    def test_protective_sell_legs_are_never_orphans(self) -> None:
        # A protective SELL exit leg (a standalone stop or an OCO leg) is placed by
        # the protection pass, journaled separately from the entry submission
        # journal, and reconciled from live broker state every tick — so it can
        # never be a place-before-journal orphan of the entry flow. Without this,
        # every restart while protection is resting alerts on the daemon's OWN legs.
        broker = _OrdersOnlyStubBroker(
            open_orders=[
                _order_state("SELL-STOP-1", side="SELL"),  # protection leg — not journaled here
                _order_state("BUY-ENTRY-9", side="BUY"),  # a genuine unjournaled entry
            ]
        )
        # Only the unjournaled BUY entry is an orphan; the SELL protection leg is not.
        self.assertEqual(
            sweep(broker, [_record()]),
            [Orphan(order_id="BUY-ENTRY-9", external_reference="", kind="order")],
        )


if __name__ == "__main__":
    unittest.main()
