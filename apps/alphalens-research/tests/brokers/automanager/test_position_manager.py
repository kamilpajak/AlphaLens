"""Hermetic tests for position_manager.advance.

The flagship case drives the verdict through the shipped reconcile core with a
stub broker returning the REAL SIM FinalFill quantity (FillAmount==2.0, entry
order 5039287596 captured 2026-07-20) so the standalone stop sizes to the
REALIZED fill (2.0), never the planned qty (3). Realized-qty = design memo Risk 2.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from alphalens_pipeline.brokers.automanager import position_manager as pm
from alphalens_pipeline.brokers.automanager.position_manager import (
    AlertOnly,
    BrokerView,
    CancelRemaining,
    CancelSellLegs,
    NoOp,
    PlaceStop,
    PlannedExit,
    ProtectionView,
    _exit_stop_ref,
    advance,
    reconcile_protection,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    _reconcile_long as reconcile_long,
)
from alphalens_pipeline.brokers.contract import (
    InstrumentRef,
    OrderState,
    OrderStatus,
    Position,
)
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

_RID = "87e0ab88-c1f2-4e88-b5b8-8fbbbb6e1a6d"
_ENTRY = "5039287596"


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
        self.assertIsInstance(advance(self._verdict()), NoOp)

    def test_partially_filled_alerts_never_silent(self) -> None:
        # Risk 2: a partial entry fill leaves the position with NO standalone
        # stop yet. Surface it as an alert rather than a silent NoOp.
        v = self._verdict(
            status="PARTIALLY_FILLED",
            verdict="PARTIALLY_FILLED",
            details={"client_request_id": _RID, "filled_quantity": 1.0},
        )
        action = advance(v)
        self.assertIsInstance(action, AlertOnly)
        assert isinstance(action, AlertOnly)
        self.assertIn("KO", action.reason)
        self.assertIn("partial", action.reason.lower())

    def test_divergence_alerts_never_cancels(self) -> None:
        v = self._verdict(
            status="WORKING",
            verdict="WORKING(PAST-TTL!)",
            divergence=True,
            reason="entry still working past ttl",
        )
        action = advance(v)
        self.assertIsInstance(action, AlertOnly)
        assert isinstance(action, AlertOnly)
        self.assertIn("past ttl", action.reason)

    def test_unresolved_alerts(self) -> None:
        v = self._verdict(
            status="UNRESOLVED", verdict="UNRESOLVED(audit_error)", reason="audit_error: boom"
        )
        self.assertIsInstance(advance(v), AlertOnly)

    def test_terminal_cancelled_cancels_remaining(self) -> None:
        self.assertIsInstance(
            advance(self._verdict(status="CANCELLED", verdict="CANCELLED")),
            CancelRemaining,
        )

    def test_filled_round_trip_closed_cancels_remaining(self) -> None:
        v = self._verdict(
            status="FILLED",
            verdict="FILLED(closed r=+1.00)",
            note="round trip closed (FIFO pair)",
            details={"client_request_id": _RID, "filled_quantity": 2.0},
        )
        self.assertIsInstance(advance(v), CancelRemaining)

    def test_filled_open_is_noop_protection_pass_owns_it(self) -> None:
        # A FILLED-open entry is handled entirely by the broker-state protection
        # pass (reconcile_protection); advance no longer places a journal-derived
        # stop here, so it returns NoOp.
        v = self._verdict(
            status="FILLED",
            verdict="FILLED",
            note="position open, exit orders working",
            details={"client_request_id": _RID, "filled_quantity": 2.0},
        )
        self.assertIsInstance(advance(v), NoOp)

    def test_legacy_journal_protection_symbols_removed(self) -> None:
        # Straggler cleanup (saxo-oco memo §10): protection is broker-state truth,
        # so the journal-derived DisasterStop / PlaceStandaloneStop are gone and
        # BrokerView carries only working_children (no protected_request_ids /
        # disaster_stops).
        self.assertFalse(hasattr(pm, "DisasterStop"))
        self.assertFalse(hasattr(pm, "PlaceStandaloneStop"))
        field_names = {f.name for f in __import__("dataclasses").fields(BrokerView)}
        self.assertEqual(field_names, {"working_children"})


# --------------------------------------------------------------------------
# Broker-state-truth protection (saxo-oco memo §6): reconcile_protection /
# _reconcile_long over a hand-built ProtectionView. Rung 0 <-> 1, STOP-ONLY.
# --------------------------------------------------------------------------

_UIC = 43070


def _instrument(uic: int = _UIC) -> InstrumentRef:
    return InstrumentRef(
        ticker="BIO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id=str(uic),
        broker_symbol="BIO:xnys",
    )


def _pos(qty: float, uic: int = _UIC) -> Position:
    return Position(
        instrument=_instrument(uic),
        quantity=qty,
        avg_price=296.0,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-1",
    )


def _leg(
    order_id: str,
    order_type: str,
    amount: float,
    *,
    uic: int = _UIC,
    filled: float = 0.0,
    status: OrderStatus = OrderStatus.WORKING,
) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=status,
        instrument=None,
        filled_quantity=filled,
        raw_status="Working",
        uic=uic,
        side="SELL",
        order_type=order_type,
        amount=amount,
        external_reference=order_id,
    )


def _plan(
    *,
    uic: int = _UIC,
    entry_crid: str = "crid",
    stop_price: float = 216.48,
    tp_price: float | None = None,
    conflicting: bool = False,
    n_plans: int = 1,
) -> PlannedExit:
    return PlannedExit(
        uic=uic,
        entry_crid=entry_crid,
        side="SELL",
        stop_price=stop_price,
        tp_price=tp_price,
        conflicting=conflicting,
        n_plans=n_plans,
    )


def _pview(
    *,
    long_positions: dict[int, Position] | None = None,
    all_positions: dict[int, Position] | None = None,
    sell_legs_by_uic: dict[int, tuple[OrderState, ...]] | None = None,
    planned_by_uic: dict[int, PlannedExit] | None = None,
    oco_unsupported: frozenset[int] = frozenset(),
) -> ProtectionView:
    longs = long_positions if long_positions is not None else {}
    alls = all_positions if all_positions is not None else dict(longs)
    return ProtectionView(
        long_positions=longs,
        all_positions=alls,
        sell_legs_by_uic=sell_legs_by_uic or {},
        planned_by_uic=planned_by_uic or {},
        oco_unsupported=oco_unsupported,
    )


class TestBugARetryAfterFailedPost(unittest.TestCase):
    """A prior stop POST raised -> NO sell leg on the uic. The reconciler must
    RETRY (place the stop), never read the position as protected (Bug A)."""

    def test_naked_long_with_plan_retries_place_stop(self) -> None:
        pos = _pos(46.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={},  # the POST failed -> no leg exists
            planned_by_uic={_UIC: _plan()},
        )
        actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 46.0)
        self.assertEqual(action.stop_price, 216.48)
        self.assertEqual(action.supersede_ids, ())
        self.assertEqual(action.cancel_conflicting, ())
        self.assertEqual(action.request_id, _exit_stop_ref("crid", 0))
        self.assertNotIsInstance(action, NoOp)


class TestBugBLoneTpForcesCancelBeforeStop(unittest.TestCase):
    """A lone SELL Limit (TP) with no stop holds the conflicting sell commitment
    (Saxo SellOrdersAlreadyExist). The stop place must cancel the TP FIRST
    (cancel_conflicting), never leave the downside naked (Bug B)."""

    def test_lone_tp_places_stop_with_cancel_conflicting(self) -> None:
        pos = _pos(46.0)
        tp = _leg("tp-1", "Limit", 46.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (tp,)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 46.0)
        self.assertEqual(action.cancel_conflicting, ("tp-1",))
        self.assertEqual(action.supersede_ids, ())
        self.assertNotIsInstance(action, NoOp)


class TestZeroAmountStopLegCoversNothing(unittest.TestCase):
    """A stop leg whose RESTING ``amount`` is a genuine ``0.0`` must contribute
    exactly 0.0 to ``stop_qty`` (explicit-None guard) — it covers no shares, so a
    long with only a zero-amount stop is read as a deficit, not protected. The
    explicit ``leg.amount if leg.amount is not None else 0.0`` form makes the
    intent unambiguous vs the falsy ``or 0.0`` (identical output today, but a
    zero amount is a real quantity, not an absent one)."""

    def test_zero_amount_stop_leg_is_a_deficit(self) -> None:
        pos = _pos(46.0)
        zero_stop = _leg("stop-0", "Stop", 0.0)  # resting amount 0.0 -> covers nothing
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (zero_stop,)},
            planned_by_uic={_UIC: _plan()},
        )
        actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 46.0, "0.0-amount stop covers nothing -> full-owned deficit")
        self.assertEqual(action.supersede_ids, ("stop-0",), "the empty stale stop is superseded")
        self.assertNotIsInstance(action, NoOp)


class TestReconcileDecisionTable(unittest.TestCase):
    def test_naked_places_stop_sized_to_netted_owned(self) -> None:
        pos = _pos(46.0)
        actions = reconcile_long(
            _UIC, pos, _pview(long_positions={_UIC: pos}, planned_by_uic={_UIC: _plan()})
        )
        self.assertIsInstance(actions[0], PlaceStop)
        assert isinstance(actions[0], PlaceStop)
        self.assertEqual(actions[0].qty, 46.0)

    def test_covered_is_noop(self) -> None:
        pos = _pos(46.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (stop,)},
                planned_by_uic={_UIC: _plan()},
            ),
        )
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_crash_after_place_is_noop(self) -> None:
        # A live stop present after a crash -> covered -> NoOp (no double stop).
        pos = _pos(46.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (stop,)},
                planned_by_uic={_UIC: _plan()},
            ),
        )
        self.assertIsInstance(actions[0], NoOp)

    def test_grow_additive_places_delta_no_supersede(self) -> None:
        # Q5 confirmed live (2026-07-21): a covering stop of 20 already rests and
        # owned grew to 46 -> place a stop for the DELTA (26) ONLY, keeping the
        # existing stop (no supersede, no naked window). 20 + 26 == 46 == owned,
        # so the sell side commits exactly owned (Saxo sums same-uic stops).
        pos = _pos(46.0)
        old_stop = _leg("stop-old", "StopIfTraded", 20.0)  # covers only the first fill
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (old_stop,)},
                planned_by_uic={_UIC: _plan()},
            ),
        )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 26.0)  # DELTA only (owned 46 - already-covered 20)
        self.assertEqual(action.supersede_ids, ())  # existing stop KEPT, never cancelled
        self.assertEqual(action.cancel_conflicting, ())  # no lone TP to clear

    def test_grow_additive_cancels_lone_tp_before_delta(self) -> None:
        # A covering stop (20) + a lone TP (Bug-B shape) and owned grew to 46:
        # the additive delta (26) still cancels the conflicting TP BEFORE the place
        # (the TP holds a sell commitment that would push the sum past owned).
        pos = _pos(46.0)
        old_stop = _leg("stop-old", "StopIfTraded", 20.0)
        lone_tp = _leg("tp-1", "Limit", 20.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (old_stop, lone_tp)},
                planned_by_uic={_UIC: _plan(tp_price=306.72)},
            ),
        )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 26.0)
        self.assertEqual(action.supersede_ids, ())  # keep the covering stop
        self.assertEqual(action.cancel_conflicting, ("tp-1",))  # lone TP cleared BEFORE

    def test_grow_additive_disabled_falls_back_place_first(self) -> None:
        # Kill-switch off (Q5 unconfirmed for a future build/instrument class): the
        # grow arm reverts to cancel-replace, place-full-owned-first, small stop
        # superseded AFTER (the shipped Stage-1 behavior, still available).
        pos = _pos(46.0)
        old_stop = _leg("stop-old", "StopIfTraded", 20.0)
        with patch.object(pm, "ADDITIVE_STOPS_CONFIRMED", False):
            actions = reconcile_long(
                _UIC,
                pos,
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (old_stop,)},
                    planned_by_uic={_UIC: _plan()},
                ),
            )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 46.0)  # full netted owned, place-first
        self.assertEqual(action.supersede_ids, ("stop-old",))  # old stop cancelled AFTER

    def test_grow_additive_skipped_when_oco_unsupported(self) -> None:
        # A uic flagged oco_unsupported opts out of additive too (same broker
        # multi-order capability gate) -> cancel-replace full-owned, never a delta.
        pos = _pos(46.0)
        old_stop = _leg("stop-old", "StopIfTraded", 20.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (old_stop,)},
                planned_by_uic={_UIC: _plan()},
                oco_unsupported=frozenset({_UIC}),
            ),
        )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 46.0)  # full netted owned, place-first
        self.assertEqual(action.supersede_ids, ("stop-old",))

    def test_over_hedge_places_residual_before_cancel(self) -> None:
        # TP leg partially filled (26 of 46), owned dropped to 20, the stop still
        # rests at 46 -> total(46 stop + 20 resting TP) > owned(20) -> over-hedge.
        pos = _pos(20.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)
        tp = _leg("tp-1", "Limit", 20.0, filled=26.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (stop, tp)},
                planned_by_uic={_UIC: _plan(tp_price=306.72)},
            ),
        )
        self.assertEqual(len(actions), 2)
        self.assertIsInstance(actions[0], PlaceStop)  # residual FIRST
        self.assertIsInstance(actions[1], CancelSellLegs)  # cancel the over-committed group AFTER
        assert isinstance(actions[0], PlaceStop)
        assert isinstance(actions[1], CancelSellLegs)
        self.assertEqual(actions[0].qty, 20.0)  # residual == netted owned
        self.assertIn("stop-1", actions[0].supersede_ids)
        self.assertEqual(set(actions[1].order_ids), {"stop-1", "tp-1"})

    def test_sizes_to_netted_owned_not_planned(self) -> None:
        # PlannedExit carries NO qty; the stop is sized to pos.quantity (netted),
        # never to any single planned tier qty.
        pos = _pos(137.0)  # 3 tiers netted; no single planned tier is 137
        actions = reconcile_long(
            _UIC, pos, _pview(long_positions={_UIC: pos}, planned_by_uic={_UIC: _plan()})
        )
        self.assertIsInstance(actions[0], PlaceStop)
        assert isinstance(actions[0], PlaceStop)
        self.assertEqual(actions[0].qty, 137.0)

    def test_float_tolerance_no_flicker(self) -> None:
        pos = _pos(46.0)
        stop = _leg("stop-1", "StopIfTraded", 45.9999999)  # within _QTY_EPS of owned
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (stop,)},
                planned_by_uic={_UIC: _plan()},
            ),
        )
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_covered_with_tp_plan_stays_noop_stop_only(self) -> None:
        # Stage 1 is STOP-ONLY: a covered long WITH a journaled TP price must NOT
        # emit UpgradeToOco (_oco_enabled() is False) — it stays NoOp.
        pos = _pos(46.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (stop,)},
                planned_by_uic={_UIC: _plan(tp_price=306.72)},
            ),
        )
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_no_plan_alerts(self) -> None:
        pos = _pos(46.0)
        actions = reconcile_long(_UIC, pos, _pview(long_positions={_UIC: pos}, planned_by_uic={}))
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], AlertOnly)

    def test_conflicting_plans_refuse_merge(self) -> None:
        pos = _pos(46.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                planned_by_uic={_UIC: _plan(conflicting=True, n_plans=2)},
            ),
        )
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], AlertOnly)
        assert isinstance(actions[0], AlertOnly)
        self.assertIn("refusing to merge", actions[0].reason)


class TestReconcileProtectionArms(unittest.TestCase):
    def test_orphan_exit_on_flat_uic_swept(self) -> None:
        # A working SELL on a uic with NO long -> orphan sweep -> CancelSellLegs.
        orphan = _leg("orphan-1", "StopIfTraded", 46.0)
        view = _pview(
            long_positions={},
            all_positions={},
            sell_legs_by_uic={_UIC: (orphan,)},
        )
        actions = reconcile_protection(view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], CancelSellLegs)
        assert isinstance(actions[0], CancelSellLegs)
        self.assertEqual(actions[0].order_ids, ("orphan-1",))

    def test_negative_position_alerts(self) -> None:
        short = _pos(-5.0)
        view = _pview(long_positions={}, all_positions={_UIC: short})
        actions = reconcile_protection(view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], AlertOnly)
        assert isinstance(actions[0], AlertOnly)
        self.assertIn("SHORT", actions[0].reason)

    def test_covered_long_and_orphan_uic_both_handled(self) -> None:
        # A covered long on one uic (NoOp) + an orphan sell on another uic (sweep).
        pos = _pos(46.0, uic=_UIC)
        stop = _leg("stop-1", "StopIfTraded", 46.0, uic=_UIC)
        orphan = _leg("orphan-2", "StopIfTraded", 10.0, uic=999)
        view = _pview(
            long_positions={_UIC: pos},
            all_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop,), 999: (orphan,)},
            planned_by_uic={_UIC: _plan()},
        )
        actions = reconcile_protection(view)
        kinds = [type(a).__name__ for a in actions]
        self.assertIn("NoOp", kinds)
        self.assertIn("CancelSellLegs", kinds)


if __name__ == "__main__":
    unittest.main()
