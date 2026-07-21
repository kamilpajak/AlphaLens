"""Hermetic tests for position_manager.advance.

The flagship case drives the verdict through the shipped reconcile core with a
stub broker returning the REAL SIM FinalFill quantity (FillAmount==2.0, entry
order 5039287596 captured 2026-07-20) so the standalone stop sizes to the
REALIZED fill (2.0), never the planned qty (3). Realized-qty = design memo Risk 2.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.position_manager import (
    AlertOnly,
    BrokerView,
    CancelRemaining,
    DisasterStop,
    NoOp,
    PlaceStandaloneStop,
    advance,
)
from alphalens_pipeline.brokers.contract import OrderState, OrderStatus
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict, reconcile_brackets

_RID = "87e0ab88-c1f2-4e88-b5b8-8fbbbb6e1a6d"
_ENTRY = "5039287596"


def _record() -> dict[str, Any]:
    return {
        "execution_config_version": "execution-v2-test",
        "ts": "2026-07-20T14:00:00+00:00",
        "brief_date": "2026-07-20",
        "ticker": "KO",
        "mic": "XNYS",
        "uic": "307",
        "brackets": [
            {
                "client_request_id": _RID,
                "entry_order_id": _ENTRY,
                "exit_order_ids": [],
                "qty": 3,
                "entry": 82.86,
                "stop": 79.0,
                "tp": 90.0,
                "ttl": 7,
            }
        ],
        "precheck": [],
    }


class _FilledOpenBroker:
    name = "stub-filled-open"

    def list_open_orders(self) -> list[OrderState]:
        return []

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        return OrderState(
            order_id=order_id,
            status=OrderStatus.FILLED,
            instrument=None,
            filled_quantity=2.0,
            raw_status="FinalFill/Confirmed LogId=249519481",
        )

    def get_open_position_references(self) -> list[str]:
        return [_RID]

    def get_closed_position_rows(self) -> list[dict[str, Any]]:
        return []


def _view(**over: Any) -> BrokerView:
    base: dict[str, Any] = {
        "protected_request_ids": frozenset(),
        "disaster_stops": {_RID: DisasterStop(uic=307, side="SELL", stop_price=79.0)},
        "working_children": {},
    }
    base.update(over)
    return BrokerView(**base)


class TestAdvanceFilledSizesRealizedQty(unittest.TestCase):
    def test_filled_open_places_stop_at_realized_fill_not_planned(self) -> None:
        verdicts = reconcile_brackets([_record()], _FilledOpenBroker())
        self.assertEqual(len(verdicts), 1)
        verdict = verdicts[0]
        self.assertEqual(verdict.status, "FILLED")
        self.assertEqual(verdict.details["filled_quantity"], 2.0)
        action = advance(verdict, _view())
        self.assertIsInstance(action, PlaceStandaloneStop)
        assert isinstance(action, PlaceStandaloneStop)
        self.assertEqual(action.qty, 2.0)
        self.assertNotEqual(action.qty, verdict.qty)
        self.assertEqual(action.stop_price, 79.0)


class TestAdvanceDecisionTable(unittest.TestCase):
    def _verdict(self, **over: Any) -> ReconcileVerdict:
        base: dict[str, Any] = {
            "brief_date": "2026-07-20",
            "ticker": "KO",
            "qty": 3,
            "entry_order_id": _ENTRY,
            "status": "WORKING",
            "verdict": "WORKING",
            "details": {"client_request_id": _RID},
        }
        base.update(over)
        return ReconcileVerdict(**base)

    def test_working_is_noop(self) -> None:
        self.assertIsInstance(advance(self._verdict(), _view()), NoOp)

    def test_divergence_alerts_never_cancels(self) -> None:
        v = self._verdict(
            status="WORKING",
            verdict="WORKING(PAST-TTL!)",
            divergence=True,
            reason="entry still working past ttl",
        )
        action = advance(v, _view())
        self.assertIsInstance(action, AlertOnly)
        assert isinstance(action, AlertOnly)
        self.assertIn("past ttl", action.reason)

    def test_unresolved_alerts(self) -> None:
        v = self._verdict(
            status="UNRESOLVED", verdict="UNRESOLVED(audit_error)", reason="audit_error: boom"
        )
        self.assertIsInstance(advance(v, _view()), AlertOnly)

    def test_terminal_cancelled_cancels_remaining(self) -> None:
        self.assertIsInstance(
            advance(self._verdict(status="CANCELLED", verdict="CANCELLED"), _view()),
            CancelRemaining,
        )

    def test_filled_round_trip_closed_cancels_remaining(self) -> None:
        v = self._verdict(
            status="FILLED",
            verdict="FILLED(closed r=+1.00)",
            note="round trip closed (FIFO pair)",
            details={"client_request_id": _RID, "filled_quantity": 2.0},
        )
        self.assertIsInstance(advance(v, _view()), CancelRemaining)

    def test_filled_open_already_protected_is_noop(self) -> None:
        v = self._verdict(
            status="FILLED",
            verdict="FILLED",
            note="position open, exit orders working",
            details={"client_request_id": _RID, "filled_quantity": 2.0},
        )
        self.assertIsInstance(advance(v, _view(protected_request_ids=frozenset({_RID}))), NoOp)

    def test_filled_open_missing_disaster_stop_alerts(self) -> None:
        v = self._verdict(
            status="FILLED",
            verdict="FILLED",
            note="position open, exit orders working",
            details={"client_request_id": _RID, "filled_quantity": 2.0},
        )
        self.assertIsInstance(advance(v, _view(disaster_stops={})), AlertOnly)

    def test_filled_open_unknown_fill_qty_alerts_never_sizes(self) -> None:
        v = self._verdict(
            status="FILLED",
            verdict="FILLED",
            note="position open, exit orders working",
            details={"client_request_id": _RID},
        )
        self.assertIsInstance(advance(v, _view()), AlertOnly)


if __name__ == "__main__":
    unittest.main()
