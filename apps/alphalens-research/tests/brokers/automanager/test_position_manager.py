"""Hermetic tests for position_manager.advance.

The flagship case drives the verdict through the shipped reconcile core with a
stub broker returning the REAL SIM FinalFill quantity (FillAmount==2.0, entry
order 5039287596 captured 2026-07-20) so the standalone stop sizes to the
REALIZED fill (2.0), never the planned qty (3). Realized-qty = design memo Risk 2.
"""

from __future__ import annotations

import itertools
import os
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from alphalens_pipeline.brokers.automanager import position_manager as pm
from alphalens_pipeline.brokers.automanager.position_manager import (
    AlertOnly,
    AmendStop,
    BrokerView,
    CancelRemaining,
    CancelSellLegs,
    NoOp,
    PlaceStop,
    PlannedExit,
    ProtectionView,
    UpgradeToOco,
    _exit_stop_ref,
    advance,
    reconcile_protection,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    _oco_stop_leg as oco_stop_leg,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    _reconcile_long as reconcile_long,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    _tp_only_leg_ids as tp_only_leg_ids,
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
    next_amend_seq: Callable[[], int] | None = None,
) -> PlannedExit:
    kwargs: dict[str, Any] = {}
    if next_amend_seq is not None:
        kwargs["next_amend_seq"] = next_amend_seq
    return PlannedExit(
        uic=uic,
        entry_crid=entry_crid,
        side="SELL",
        stop_price=stop_price,
        tp_price=tp_price,
        conflicting=conflicting,
        n_plans=n_plans,
        **kwargs,
    )


def _pview(
    *,
    long_positions: dict[int, Position] | None = None,
    all_positions: dict[int, Position] | None = None,
    sell_legs_by_uic: dict[int, tuple[OrderState, ...]] | None = None,
    planned_by_uic: dict[int, PlannedExit] | None = None,
    oco_unsupported: frozenset[int] = frozenset(),
    oco_recently_placed: frozenset[int] = frozenset(),
    amend_recently_failed: frozenset[int] = frozenset(),
) -> ProtectionView:
    longs = long_positions if long_positions is not None else {}
    alls = all_positions if all_positions is not None else dict(longs)
    return ProtectionView(
        long_positions=longs,
        all_positions=alls,
        sell_legs_by_uic=sell_legs_by_uic or {},
        planned_by_uic=planned_by_uic or {},
        oco_unsupported=oco_unsupported,
        oco_recently_placed=oco_recently_placed,
        amend_recently_failed=amend_recently_failed,
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
        self.assertIsInstance(actions[1], CancelSellLegs)  # cancel only the non-stop legs
        assert isinstance(actions[0], PlaceStop)
        assert isinstance(actions[1], CancelSellLegs)
        self.assertEqual(actions[0].qty, 20.0)  # residual == netted owned
        # The STOP leg leaves ONLY via supersede-after-a-successful-place (never a
        # naked window). The unconditional CancelSellLegs names the NON-stop leg only.
        self.assertEqual(actions[0].supersede_ids, ("stop-1",))
        self.assertEqual(set(actions[1].order_ids), {"tp-1"})

    def test_over_hedge_stop_only_places_residual_no_cancel(self) -> None:
        # Stage-1 STOP-ONLY over-hedge (no TP, no noise): the position shrank so a
        # lone resting stop over-covers (stop 46 > owned 20). The arm must place a
        # residual-sized stop and supersede the old stop ONLY — emitting NO
        # unconditional CancelSellLegs. If the place DEFERS (SellOrdersAlreadyExist)
        # the executor skips supersede, the old over-sized stop keeps resting, and
        # the position stays OVER-covered, never naked (the Bug-A cardinal sin).
        pos = _pos(20.0)
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
        self.assertEqual(len(actions), 1)  # PlaceStop only — NO CancelSellLegs
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 20.0)  # residual == netted owned
        self.assertEqual(action.supersede_ids, ("stop-1",))  # old stop cancelled AFTER place

    def test_over_hedge_multi_stop_supersedes_every_stop(self) -> None:
        # Two resting stops (additive-on-growth placed a 2nd delta stop) then the
        # position shrank -> arm A over-hedge with TWO stop legs (4 + 3 = 7 > owned
        # 5). EVERY stop must be in supersede_ids — a partial supersede would leave
        # an old over-covering stop resting (intended final sell > owned). Fences
        # the `supersede_ids=bad.stop_leg_ids[:1]` regression.
        pos = _pos(5.0)
        stop_a = _leg("stop-a", "StopIfTraded", 4.0)
        stop_b = _leg("stop-b", "StopIfTraded", 3.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (stop_a, stop_b)},
                planned_by_uic={_UIC: _plan()},
            ),
        )
        self.assertEqual(len(actions), 1)  # PlaceStop only — both legs are stops, nothing to cancel
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 5.0)  # residual == netted owned
        self.assertEqual(set(action.supersede_ids), {"stop-a", "stop-b"})  # ALL stops superseded
        self.assertEqual([a for a in actions if isinstance(a, CancelSellLegs)], [])

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


class TestOcoEnablementGate(unittest.TestCase):
    """Stage 3 rung-1 REFUSE (saxo Stage-3 memo): a covered long that ALREADY has
    a resting standalone stop stays STOP-ONLY for its whole life — arm C never
    upgrades a resting stop to OCO (PATCH cannot add a TP leg, cancel-then-OCO is
    naked, OCO-then-cancel is 2x-owned rejected live). OCO is reached only via B0
    on a fresh naked fill. Arm C is therefore always NoOp on a covered long."""

    def _covered_view(
        self, *, oco_unsupported: frozenset[int] = frozenset()
    ) -> tuple[Position, ProtectionView]:
        pos = _pos(46.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop,)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
            oco_unsupported=oco_unsupported,
        )
        return pos, view

    def test_covered_with_tp_stays_noop_when_flag_unset(self) -> None:
        pos, view = self._covered_view()
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_OCO_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_covered_with_resting_stop_stays_noop_rung1_refuse(self) -> None:
        # Stage 3 rung-1 REFUSE: a covered long with a resting standalone stop is
        # NEVER upgraded to OCO, even with the flag on — it stays stop-only NoOp.
        pos, view = self._covered_view()
        with patch.dict(os.environ, {"ALPHALENS_BROKER_OCO_ENABLED": "1"}):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()])
        self.assertNotIsInstance(actions[0], UpgradeToOco)

    def test_covered_with_tp_stays_noop_when_oco_unsupported_even_if_flag_on(self) -> None:
        pos, view = self._covered_view(oco_unsupported=frozenset({_UIC}))
        with patch.dict(os.environ, {"ALPHALENS_BROKER_OCO_ENABLED": "1"}):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_covered_without_tp_stays_noop_even_when_flag_on(self) -> None:
        # No journaled TP price -> nothing to capture -> stop-only NoOp regardless.
        pos = _pos(46.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop,)},
            planned_by_uic={_UIC: _plan(tp_price=None)},
        )
        with patch.dict(os.environ, {"ALPHALENS_BROKER_OCO_ENABLED": "1"}):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)


def _oco_leg(
    order_id: str,
    order_type: str,
    amount: float,
    *,
    uic: int = _UIC,
    base: str = "crid-oco-0",
    external_reference: str | None = "",
    order_relation: str | None = "Oco",
    filled: float = 0.0,
) -> OrderState:
    """A resting OCO exit leg: ``OrderRelation="Oco"`` + a shared base ref with a
    ``-stop`` / ``-tp`` suffix (what ``_build_oco_exit_body`` stamps). Pass
    ``external_reference=None`` to model the Q7 case where Saxo does NOT echo the
    per-leg ref (detection then falls back to ``OrderRelation``)."""
    if external_reference == "":
        suffix = "-stop" if order_type in ("StopIfTraded", "Stop") else "-tp"
        external_reference = f"{base}{suffix}"
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=filled,
        raw_status="Working",
        uic=uic,
        side="SELL",
        order_type=order_type,
        amount=amount,
        external_reference=external_reference,
        order_relation=order_relation,
    )


class TestOcoSteadyStateNotOverHedge(unittest.TestCase):
    """After a successful rung 1 -> 2 upgrade the resting pair is
    {StopIfTraded=owned, Limit=owned}. Saxo counts a mutually-exclusive OCO pair
    as a SINGLE sell commitment, so summing every leg (2*owned) would falsely trip
    the over-hedge arm and tear the pair down — cancelling one OCO leg cascades its
    sibling while the replacement standalone stop can be rejected
    (SellOrdersAlreadyExist), opening a naked window and recurring churn. A healthy
    pair must be the terminal NoOp instead."""

    def test_healthy_oco_pair_is_noop_not_over_hedge(self) -> None:
        pos = _pos(46.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 46.0)
        tp = _oco_leg("oco-tp", "Limit", 46.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        # Flag ON: still terminal NoOp (the OCO already covers both rungs) —
        # never an over-hedge tear-down, never a re-upgrade.
        with patch.dict(os.environ, {"ALPHALENS_BROKER_OCO_ENABLED": "1"}):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_healthy_oco_pair_noop_via_order_relation_only(self) -> None:
        # Q7 unverified: Saxo may echo only a top-level ref, so per-leg refs are
        # absent. Detection then relies on OrderRelation alone — still one group.
        pos = _pos(46.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 46.0, external_reference=None)
        tp = _oco_leg("oco-tp", "Limit", 46.0, external_reference=None)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, {"ALPHALENS_BROKER_OCO_ENABLED": "1"}):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_healthy_oco_pair_noop_via_ref_infix_only(self) -> None:
        # Belt: if Saxo does NOT echo OrderRelation but DOES echo the per-leg ref,
        # the "-oco-" infix still identifies the pair as one commitment.
        pos = _pos(46.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 46.0, order_relation=None)
        tp = _oco_leg("oco-tp", "Limit", 46.0, order_relation=None)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, {"ALPHALENS_BROKER_OCO_ENABLED": "1"}):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_grow_after_oco_stays_noop_not_reupgrade(self) -> None:
        # Grow-after-OCO: owned grew (56) past the resting OCO's TP (46); the
        # additive delta stop (10) covers the deficit so stop_qty == owned. Arm C
        # sees tp_qty(46) < owned(56) but MUST NOT re-emit UpgradeToOco — a 2nd OCO
        # on top of the resting pair would be rejected SellOrdersAlreadyExist and
        # waste-degrade the uic to oco_unsupported. Keep what rests -> NoOp.
        pos = _pos(56.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 46.0)
        tp = _oco_leg("oco-tp", "Limit", 46.0)
        delta = _leg("delta-stop", "StopIfTraded", 10.0)  # additive, order_relation=None
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp, delta)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, {"ALPHALENS_BROKER_OCO_ENABLED": "1"}):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], NoOp)

    def test_resting_rung1_stop_no_longer_upgrades_stays_noop(self) -> None:
        # Stage 3 rung-1 REFUSE: with ONLY a rung-1 standalone stop (no OCO leg)
        # covering owned, arm C is now a NoOp — the Stage-2 first-upgrade emission
        # was deleted (a resting stop cannot be safely converted to an OCO pair).
        pos = _pos(46.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)  # plain, order_relation=None
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop,)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, {"ALPHALENS_BROKER_OCO_ENABLED": "1"}):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()])

    def test_shrunk_oco_over_hedge_never_cancels_oco_leg(self) -> None:
        # A genuine over-hedge WITH an OCO pair: the OCO limit leg partially filled
        # (owned dropped 46 -> 20) so the OCO group (counted once = 46) over-covers
        # owned 20. The arm must place a residual stop and supersede the OCO stop
        # AFTER a successful place, but must NEVER name the OCO tp leg in an
        # unconditional CancelSellLegs — that would cascade-cancel the OCO stop
        # while the replacement stop is still rejected (SellOrdersAlreadyExist).
        pos = _pos(20.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 46.0)
        tp = _oco_leg("oco-tp", "Limit", 46.0, filled=26.0)
        actions = reconcile_long(
            _UIC,
            pos,
            _pview(
                long_positions={_UIC: pos},
                sell_legs_by_uic={_UIC: (stop, tp)},
                planned_by_uic={_UIC: _plan(tp_price=306.72)},
            ),
        )
        # PlaceStop only — no CancelSellLegs naming an OCO leg.
        self.assertEqual([type(a).__name__ for a in actions], ["PlaceStop"])
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 20.0)  # residual == netted owned
        self.assertEqual(action.supersede_ids, ("oco-stop",))  # OCO stop only via supersede

    def test_non_oco_stop_plus_tp_still_over_hedges(self) -> None:
        # Regression guard: the OCO single-commitment accounting must NOT leak into
        # two INDEPENDENT (non-OCO) sell legs — a plain stop + a plain resting TP
        # still sum in full and trip the over-hedge arm.
        pos = _pos(20.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)  # order_relation=None (plain)
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
        self.assertEqual([type(a).__name__ for a in actions], ["PlaceStop", "CancelSellLegs"])
        assert isinstance(actions[1], CancelSellLegs)
        self.assertEqual(set(actions[1].order_ids), {"tp-1"})


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


_OCO_ON = {"ALPHALENS_BROKER_OCO_ENABLED": "1"}
_AMEND_ON = {"ALPHALENS_BROKER_AMEND_ENABLED": "1"}


class TestB0OcoDirectOnFill(unittest.TestCase):
    """Stage 3 arm B0 (saxo Stage-3 memo): a truly naked fresh fill (no resting
    legs) with OCO wanted goes STRAIGHT to a resting OCO pair via
    ``UpgradeToOco(supersede_ids=())`` — never a stop-only rung 1 first. Suppressed
    while an OCO was just placed but list-orders lags (oco_recently_placed)."""

    def _naked_view(
        self,
        *,
        oco_recently_placed: frozenset[int] = frozenset(),
        oco_unsupported: frozenset[int] = frozenset(),
        tp_price: float | None = 306.72,
    ) -> tuple[Position, ProtectionView]:
        pos = _pos(46.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={},  # truly naked
            planned_by_uic={_UIC: _plan(tp_price=tp_price)},
            oco_unsupported=oco_unsupported,
            oco_recently_placed=oco_recently_placed,
        )
        return pos, view

    def test_b0_naked_fill_oco_enabled_emits_upgradetooco_empty_supersede(self) -> None:
        pos, view = self._naked_view()
        with patch.dict(os.environ, _OCO_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, UpgradeToOco)
        assert isinstance(action, UpgradeToOco)
        self.assertEqual(action.qty, 46.0)
        self.assertEqual(action.stop_price, 216.48)
        self.assertEqual(action.tp_price, 306.72)
        self.assertEqual(action.supersede_ids, ())  # nothing to supersede on a naked fill

    def test_b0_suppressed_when_uic_in_oco_recently_placed(self) -> None:
        # A just-placed OCO rests but list-orders lags (empty legs). The marker
        # suppresses B0 AND must NoOp — a PlaceStop(owned) here would commit a
        # second owned SELL on top of the invisible resting OCO pair (2x owned,
        # the exact double-commit the marker exists to prevent). The OCO stop
        # leg already covers the downside; NoOp until the pair becomes visible
        # or the TTL expires and B0 re-evaluates against live broker state.
        pos, view = self._naked_view(oco_recently_placed=frozenset({_UIC}))
        with patch.dict(os.environ, _OCO_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()])

    def test_b0_not_emitted_when_oco_disabled_falls_to_b2_stop_only(self) -> None:
        pos, view = self._naked_view()
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_OCO_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 46.0)

    def test_b0_not_emitted_when_uic_oco_unsupported(self) -> None:
        pos, view = self._naked_view(oco_unsupported=frozenset({_UIC}))
        with patch.dict(os.environ, _OCO_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], PlaceStop)

    def test_b0_not_emitted_without_tp_price_falls_to_b2(self) -> None:
        pos, view = self._naked_view(tp_price=None)
        with patch.dict(os.environ, _OCO_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], PlaceStop)


class TestGrowAmendStop(unittest.TestCase):
    """Stage 3 GROW amend arm: a SINGLE clean standalone stop under-covers (owned
    grew) -> PATCH amend it UP to live owned in place. Falls through to B1 additive
    when amend is off, >1 stop rests, a TP leg is present, the stop partially
    filled, or the uic recently failed an amend."""

    def test_grow_emits_amendstop_when_sole_clean_stop_amend_enabled(self) -> None:
        pos = _pos(7.0)
        stop = _leg("stop-1", "StopIfTraded", 4.0)  # filled 0, no TP leg
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop,)},
            planned_by_uic={_UIC: _plan()},
        )
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertEqual(action.target_qty, 7.0)  # absolute target = live owned
        self.assertEqual(action.order_id, "stop-1")
        self.assertEqual(action.order_type, "StopIfTraded")
        self.assertEqual(action.stop_price, 216.48)
        self.assertIn("-amend-", action.request_id)

    def test_grow_no_amend_when_amend_disabled_falls_to_b1(self) -> None:
        pos = _pos(7.0)
        stop = _leg("stop-1", "StopIfTraded", 4.0)
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_AMEND_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
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
        action = actions[0]
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 3.0)  # B1 delta (owned 7 - covered 4)
        self.assertEqual(action.supersede_ids, ())

    def test_grow_no_amend_when_sole_stop_partially_filled_falls_to_b1(self) -> None:
        pos = _pos(7.0)
        stop = _leg("stop-1", "StopIfTraded", 4.0, filled=3.0)  # partially triggered
        with patch.dict(os.environ, _AMEND_ON):
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
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 3.0)  # B1 additive delta

    def test_grow_no_amend_when_tp_leg_present_falls_to_b1(self) -> None:
        pos = _pos(7.0)
        stop = _leg("stop-1", "StopIfTraded", 4.0)
        tp = _leg("tp-1", "Limit", 2.0)  # total 6 <= owned 7 -> stays in deficit block
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(
                _UIC,
                pos,
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (stop, tp)},
                    planned_by_uic={_UIC: _plan(tp_price=306.72)},
                ),
            )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 3.0)  # B1 delta
        self.assertEqual(action.cancel_conflicting, ("tp-1",))

    def test_grow_falls_to_b1_additive_when_two_stop_legs(self) -> None:
        pos = _pos(8.0)
        stop_a = _leg("stop-a", "StopIfTraded", 4.0)
        stop_b = _leg("stop-b", "StopIfTraded", 3.0)  # two stops -> not sole
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(
                _UIC,
                pos,
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (stop_a, stop_b)},
                    planned_by_uic={_UIC: _plan()},
                ),
            )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 1.0)  # B1 delta (owned 8 - covered 7)
        self.assertEqual(action.supersede_ids, ())

    def test_grow_falls_to_b1_when_amend_recently_failed(self) -> None:
        pos = _pos(7.0)
        stop = _leg("stop-1", "StopIfTraded", 4.0)
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(
                _UIC,
                pos,
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (stop,)},
                    planned_by_uic={_UIC: _plan()},
                    amend_recently_failed=frozenset({_UIC}),
                ),
            )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 3.0)  # B1 additive covers the delta


class TestDownsizeAmendStop(unittest.TestCase):
    """Stage 3 DOWNSIZE amend arm (#884 gap): a SINGLE clean standalone stop
    over-covers (owned shrank) -> PATCH amend it DOWN to live owned in place.
    Multi-stop / OCO over-hedge keeps the unchanged place-residual-first arm."""

    def test_downsize_emits_amendstop_when_sole_oversized_stop(self) -> None:
        pos = _pos(4.0)
        stop = _leg("stop-1", "StopIfTraded", 7.0)  # over-covers owned 4
        with patch.dict(os.environ, _AMEND_ON):
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
        action = actions[0]
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertEqual(action.target_qty, 4.0)  # absolute target = live owned
        self.assertEqual(action.order_id, "stop-1")
        self.assertIn("-amend-", action.request_id)

    def test_downsize_falls_to_place_first_when_two_stops(self) -> None:
        pos = _pos(5.0)
        stop_a = _leg("stop-a", "StopIfTraded", 4.0)
        stop_b = _leg("stop-b", "StopIfTraded", 3.0)  # sum 7 > owned 5, two stops
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(
                _UIC,
                pos,
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (stop_a, stop_b)},
                    planned_by_uic={_UIC: _plan()},
                ),
            )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 5.0)  # residual == netted owned
        self.assertEqual(set(action.supersede_ids), {"stop-a", "stop-b"})

    def test_downsize_falls_to_place_first_when_oco_leg(self) -> None:
        pos = _pos(20.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 46.0)
        tp = _oco_leg("oco-tp", "Limit", 46.0, filled=26.0)
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(
                _UIC,
                pos,
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (stop, tp)},
                    planned_by_uic={_UIC: _plan(tp_price=306.72)},
                ),
            )
        self.assertEqual([type(a).__name__ for a in actions], ["PlaceStop"])
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 20.0)
        self.assertEqual(action.supersede_ids, ("oco-stop",))

    def test_downsize_falls_to_place_first_when_stray_non_stop_sell_leg(self) -> None:
        # A lone clean stop over-covers, but a stray resting SELL leg of a type
        # OUTSIDE STOP_TYPES/TP_TYPES (e.g. a Market sell) also rests. Amending
        # only the stop and returning would leave that stray leg resting -> a
        # residual over-commit (stop=owned + stray > owned). _sole_standalone_stop
        # must reject any shape that is not the SOLE resting sell leg, so this
        # falls to the always-correct place-residual-first arm (over-covered,
        # never naked) which supersedes the stop and cancels the stray leg.
        pos = _pos(4.0)
        stop = _leg("stop-1", "StopIfTraded", 7.0)  # over-covers owned 4
        stray = _leg("mkt-1", "Market", 2.0)  # not a stop, not a TP
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(
                _UIC,
                pos,
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (stop, stray)},
                    planned_by_uic={_UIC: _plan()},
                ),
            )
        self.assertFalse(
            any(isinstance(a, AmendStop) for a in actions),
            "a stray non-stop sell leg must block the sole-standalone-stop amend",
        )
        place = next(a for a in actions if isinstance(a, PlaceStop))
        self.assertEqual(place.qty, 4.0)  # residual == netted owned
        self.assertIn("stop-1", place.supersede_ids)  # old stop superseded AFTER the place

    def test_downsize_no_amend_when_amend_disabled_falls_to_place_first(self) -> None:
        pos = _pos(4.0)
        stop = _leg("stop-1", "StopIfTraded", 7.0)
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_AMEND_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
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
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 4.0)
        self.assertEqual(action.supersede_ids, ("stop-1",))


class TestAmendRefAndSeq(unittest.TestCase):
    def test_amend_ref_distinct_amend_namespace(self) -> None:
        pos = _pos(7.0)
        stop = _leg("stop-1", "StopIfTraded", 4.0)
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(
                _UIC,
                pos,
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (stop,)},
                    planned_by_uic={_UIC: _plan(entry_crid="crid")},
                ),
            )
        action = actions[0]
        assert isinstance(action, AmendStop)
        self.assertIn("-amend-", action.request_id)
        self.assertNotIn("-stop-", action.request_id)
        self.assertNotIn("-oco-", action.request_id)

    def test_amend_seq_monotonic_never_repeats_same_target(self) -> None:
        # Two amend emissions to the SAME target qty across ticks must carry
        # DIFFERENT seqs (mitigation A3: a monotonic ref never dedup-swallows a
        # genuine re-resize to a previously-seen qty).
        counter = itertools.count(1)
        plan = _plan(next_amend_seq=lambda: next(counter))
        pos = _pos(7.0)
        stop = _leg("stop-1", "StopIfTraded", 4.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop,)},
            planned_by_uic={_UIC: plan},
        )
        with patch.dict(os.environ, _AMEND_ON):
            first = reconcile_long(_UIC, pos, view)[0]
            second = reconcile_long(_UIC, pos, view)[0]
        assert isinstance(first, AmendStop)
        assert isinstance(second, AmendStop)
        self.assertEqual(first.target_qty, second.target_qty)  # SAME target
        self.assertNotEqual(first.request_id, second.request_id)  # different seq


class TestRung1Refuse(unittest.TestCase):
    def test_rung1_resting_stop_oco_enabled_returns_noop(self) -> None:
        pos = _pos(46.0)
        stop = _leg("stop-1", "StopIfTraded", 46.0)  # lone resting rung-1 stop
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop,)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, _OCO_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()])
        self.assertNotIsInstance(actions[0], UpgradeToOco)

    def test_arm_c_healthy_oco_pair_still_noop(self) -> None:
        pos = _pos(46.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 46.0)
        tp = _oco_leg("oco-tp", "Limit", 46.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, _OCO_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()])


class TestOcoStopLegSelector(unittest.TestCase):
    """Stage 3.5 selector ``_oco_stop_leg``: the OCO-REQUIRING inverse of
    ``_sole_standalone_stop`` — returns the child StopIfTraded leg of a CLEAN
    unfilled two-leg OCO pair, else ``None``."""

    def test_returns_child_stop_for_clean_unfilled_oco_pair(self) -> None:
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0)
        tp = _oco_leg("oco-tp", "Limit", 5.0)
        self.assertIs(oco_stop_leg((stop, tp)), stop)

    def test_none_when_oco_stop_leg_partially_filled(self) -> None:
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0, filled=2.0)
        tp = _oco_leg("oco-tp", "Limit", 5.0)
        self.assertIsNone(oco_stop_leg((stop, tp)))

    def test_none_when_oco_tp_leg_partially_filled(self) -> None:
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0)
        tp = _oco_leg("oco-tp", "Limit", 5.0, filled=2.0)
        self.assertIsNone(oco_stop_leg((stop, tp)))

    def test_none_when_stray_standalone_stop_alongside_pair(self) -> None:
        # 3 legs (OCO stop + OCO tp + a standalone stop) -> len != 2 -> None.
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0)
        tp = _oco_leg("oco-tp", "Limit", 5.0)
        stray = _leg("stray-stop", "StopIfTraded", 5.0)  # order_relation=None, plain ref
        self.assertIsNone(oco_stop_leg((stop, tp, stray)))

    def test_none_for_lone_standalone_stop_no_tp(self) -> None:
        stop = _leg("stop-1", "StopIfTraded", 5.0)  # single non-OCO stop
        self.assertIsNone(oco_stop_leg((stop,)))

    def test_recognises_pair_via_orderrelation_only(self) -> None:
        # Q7: both legs OrderRelation=='Oco' but neither carries the -oco- ref infix.
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0, external_reference=None)
        tp = _oco_leg("oco-tp", "Limit", 5.0, external_reference=None)
        self.assertIs(oco_stop_leg((stop, tp)), stop)

    def test_recognises_pair_via_ref_infix_only(self) -> None:
        # Both legs carry the -oco- ref infix but OrderRelation is absent.
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0, order_relation=None)
        tp = _oco_leg("oco-tp", "Limit", 5.0, order_relation=None)
        self.assertIs(oco_stop_leg((stop, tp)), stop)


class TestTpOnlyLegIdsExcludesOco(unittest.TestCase):
    """``_tp_only_leg_ids`` feeds ``cancel_conflicting`` (cancelled BEFORE a place)
    for the Bug-B lone-TP shape. It must name STANDALONE TP legs only — an OCO
    Limit is mutually exclusive with its sibling stop and cancelling it cascade-
    cancels the covering stop, so an OCO leg here would open a naked window."""

    def test_standalone_lone_tp_is_named(self) -> None:
        legs = (_leg("stop-1", "StopIfTraded", 20.0), _leg("tp-1", "Limit", 20.0))
        self.assertEqual(tp_only_leg_ids(legs), ("tp-1",))

    def test_oco_pair_yields_no_cancel_ids(self) -> None:
        legs = (_oco_leg("oco-stop", "StopIfTraded", 4.0), _oco_leg("oco-tp", "Limit", 4.0))
        self.assertEqual(tp_only_leg_ids(legs), ())  # OCO Limit excluded — never pre-cancel

    def test_oco_tp_excluded_but_standalone_tp_kept(self) -> None:
        # Mixed shape: an OCO pair plus a stray STANDALONE TP. Only the standalone
        # TP (which holds an independent commitment) may be pre-cancelled.
        legs = (
            _oco_leg("oco-stop", "StopIfTraded", 4.0),
            _oco_leg("oco-tp", "Limit", 4.0),
            _leg("tp-standalone", "Limit", 3.0),
        )
        self.assertEqual(tp_only_leg_ids(legs), ("tp-standalone",))


class TestOcoGrowAmendArm(unittest.TestCase):
    """Stage 3.5 GROW-after-OCO amend arm (arm B): a CLEAN unfilled resting OCO
    pair under-covers (owned grew) -> PATCH the OCO stop leg UP to live owned in
    place; Q9 propagates symmetrically to the Limit sibling. Falls to B1 additive
    when amend is off / the uic is degraded (DARK parity with #894)."""

    def _grow_view(
        self,
        *,
        owned: float = 7.0,
        stop_amount: float = 4.0,
        oco_unsupported: frozenset[int] = frozenset(),
        amend_recently_failed: frozenset[int] = frozenset(),
        stop_order_type: str = "StopIfTraded",
    ) -> tuple[Position, ProtectionView]:
        pos = _pos(owned)
        stop = _oco_leg("oco-stop", stop_order_type, stop_amount)
        tp = _oco_leg("oco-tp", "Limit", stop_amount)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
            oco_unsupported=oco_unsupported,
            amend_recently_failed=amend_recently_failed,
        )
        return pos, view

    def test_grow_after_oco_emits_amend_up_to_owned(self) -> None:
        pos, view = self._grow_view(owned=7.0, stop_amount=4.0)
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertEqual(action.order_id, "oco-stop")
        self.assertEqual(action.target_qty, 7.0)  # absolute target = live owned
        self.assertTrue(action.reason.startswith("grow-after-OCO"))
        self.assertIn("-amend-", action.request_id)

    def test_grow_after_oco_amend_disabled_falls_to_b1_additive(self) -> None:
        pos, view = self._grow_view(owned=7.0, stop_amount=4.0)
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_AMEND_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 3.0)  # B1 delta (owned 7 - covered 4)
        # NEVER-NAKED: the B1 additive fallback KEEPS the resting OCO pair and adds
        # a delta stop on top; it must NOT pre-cancel either OCO leg (cancel-before
        # cascades the pair -> a fully-naked window before the delta lands).
        self.assertEqual(action.cancel_conflicting, ())

    def test_grow_after_oco_recently_failed_falls_to_b1_additive(self) -> None:
        pos, view = self._grow_view(amend_recently_failed=frozenset({_UIC}))
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 3.0)  # B1 additive covers the delta
        # NEVER-NAKED: the OCO Limit (TP) leg must not be pre-cancelled — an OCO leg
        # in cancel_conflicting cascade-cancels its sibling stop BEFORE the delta
        # place, tearing the whole pair down (the exact bug this pins).
        self.assertEqual(action.cancel_conflicting, ())
        self.assertNotIn("oco-tp", action.cancel_conflicting)
        self.assertNotIn("oco-stop", action.cancel_conflicting)

    def test_grow_after_oco_uic_unsupported_falls_to_b2(self) -> None:
        # oco_unsupported gates BOTH the OCO amend and the B1 additive block, so the
        # uic falls to the place-first B2 path (full owned, stale stop superseded).
        pos, view = self._grow_view(oco_unsupported=frozenset({_UIC}))
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertNotIsInstance(action, AmendStop)
        self.assertIsInstance(action, PlaceStop)
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 7.0)  # full owned, place-first B2
        # NEVER-NAKED: the OCO stop leaves via supersede_ids (cancel AFTER the
        # full-owned place); the OCO Limit (TP) leg must NEVER sit in
        # cancel_conflicting (pre-cancel cascade-cancels the covering stop).
        self.assertEqual(action.cancel_conflicting, ())
        self.assertEqual(action.supersede_ids, ("oco-stop",))

    def test_grow_amend_order_type_defaults_stopiftraded_when_none(self) -> None:
        # The AmendStop preserves the OCO stop leg's order_type, defaulting to
        # 'StopIfTraded' (not '') via ``order_type or "StopIfTraded"``. The selector
        # requires STOP_TYPES membership, so a reachable OCO stop always carries a
        # concrete stop type; the default is the naive-read (OrderType:None) belt.
        pos, view = self._grow_view(stop_order_type="StopIfTraded")
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        action = actions[0]
        assert isinstance(action, AmendStop)
        self.assertEqual(action.order_type, "StopIfTraded")
        self.assertNotEqual(action.order_type, "")


class TestOcoDownsizeAmendArm(unittest.TestCase):
    """Stage 3.5 OCO-DOWNSIZE amend arm (arm A) + M1 propagation-lag NoOp guard: a
    CLEAN unfilled resting OCO pair over-covers -> PATCH the OCO stop leg DOWN to
    live owned in place; a stop-already-<=owned/tp-lags read NoOps one tick rather
    than tearing the pair down."""

    def test_downsize_oco_emits_amend_down_to_owned(self) -> None:
        pos = _pos(3.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0)
        tp = _oco_leg("oco-tp", "Limit", 5.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertEqual(action.order_id, "oco-stop")
        self.assertEqual(action.target_qty, 3.0)  # absolute target = live owned
        self.assertTrue(action.reason.startswith("OCO downsize"))
        self.assertIn("-amend-", action.request_id)

    def test_downsize_oco_amend_disabled_falls_to_place_residual_first(self) -> None:
        pos = _pos(3.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0)
        tp = _oco_leg("oco-tp", "Limit", 5.0)
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_AMEND_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            actions = reconcile_long(
                _UIC,
                _pos(3.0),
                _pview(
                    long_positions={_UIC: pos},
                    sell_legs_by_uic={_UIC: (stop, tp)},
                    planned_by_uic={_UIC: _plan(tp_price=306.72)},
                ),
            )
        # DARK: place-residual-first, OCO stop superseded AFTER, never a NoOp/amend.
        self.assertEqual([type(a).__name__ for a in actions], ["PlaceStop"])
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 3.0)  # residual == netted owned
        self.assertEqual(action.supersede_ids, ("oco-stop",))

    def test_downsize_lag_guard_noops_when_stop_equals_owned_tp_lags(self) -> None:
        # Clean unfilled OCO stop=3 (already == owned) / tp=5 (read lags Q9's
        # symmetric propagation), owned=3 -> M1 NoOps one tick, NOT a teardown.
        pos = _pos(3.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 3.0)
        tp = _oco_leg("oco-tp", "Limit", 5.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()])

    def test_downsize_recently_failed_over_hedge_noops_not_teardown(self) -> None:
        # M1 also holds an OVER-hedged clean OCO pair (stop=5 > owned=3) when the
        # downsize amend was skipped because the uic recently failed an amend. NoOp
        # is the deliberate SAFE hold: the OCO stop keeps covering the downside
        # (excess sell NotOwned-capped on a cash account), the amend_recently_failed
        # TTL self-clears, and the next tick's downsize amend resizes to owned.
        # Tearing down via place-residual-first would POST a doomed 3-stop (Saxo
        # SellOrdersAlreadyExist while the OCO rests) -> alert spam, ending equally
        # over-covered. Never naked either way.
        pos = _pos(3.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0)
        tp = _oco_leg("oco-tp", "Limit", 5.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
            amend_recently_failed=frozenset({_UIC}),
        )
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()])  # safe hold, never a teardown/naked window

    def test_downsize_partial_fill_defers_to_place_residual_first(self) -> None:
        # OCO tp partially filled (2 of the old size), owned=3 -> _oco_stop_leg None
        # (guard 3) -> place-residual-first via _group_with_partial_fill, never NoOp.
        pos = _pos(3.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 5.0)
        tp = _oco_leg("oco-tp", "Limit", 3.0, filled=2.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertFalse(any(isinstance(a, (AmendStop, NoOp)) for a in actions))
        self.assertEqual([type(a).__name__ for a in actions], ["PlaceStop"])
        action = actions[0]
        assert isinstance(action, PlaceStop)
        self.assertEqual(action.qty, 3.0)
        self.assertEqual(action.supersede_ids, ("oco-stop",))


class TestOcoAmendSteadyState(unittest.TestCase):
    """Stage 3.5 steady state: a healthy OCO pair {stop=owned, tp=owned} trips
    neither amend arm nor the M1 lag guard; a grow-then-downsize across ticks
    yields distinct monotonic -amend- refs with absolute targets."""

    def test_healthy_oco_pair_owned_equal_noops(self) -> None:
        pos = _pos(46.0)
        stop = _oco_leg("oco-stop", "StopIfTraded", 46.0)
        tp = _oco_leg("oco-tp", "Limit", 46.0)
        view = _pview(
            long_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop, tp)},
            planned_by_uic={_UIC: _plan(tp_price=306.72)},
        )
        with patch.dict(os.environ, _AMEND_ON):
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()])

    def test_grow_then_downsize_across_ticks_distinct_refs(self) -> None:
        counter = itertools.count(1)
        plan = _plan(tp_price=306.72, next_amend_seq=lambda: next(counter))
        # tick 1: owned grew to 7 over a resting OCO {4, 4} -> grow amend up.
        grow_pos = _pos(7.0)
        grow_view = _pview(
            long_positions={_UIC: grow_pos},
            sell_legs_by_uic={
                _UIC: (_oco_leg("oco-stop", "StopIfTraded", 4.0), _oco_leg("oco-tp", "Limit", 4.0))
            },
            planned_by_uic={_UIC: plan},
        )
        # tick 2: owned shrank to 3 over the propagated OCO {7, 7} -> downsize amend.
        shrink_pos = _pos(3.0)
        shrink_view = _pview(
            long_positions={_UIC: shrink_pos},
            sell_legs_by_uic={
                _UIC: (_oco_leg("oco-stop", "StopIfTraded", 7.0), _oco_leg("oco-tp", "Limit", 7.0))
            },
            planned_by_uic={_UIC: plan},
        )
        with patch.dict(os.environ, _AMEND_ON):
            first = reconcile_long(_UIC, grow_pos, grow_view)[0]
            second = reconcile_long(_UIC, shrink_pos, shrink_view)[0]
        assert isinstance(first, AmendStop)
        assert isinstance(second, AmendStop)
        self.assertEqual(first.target_qty, 7.0)  # absolute grow target
        self.assertEqual(second.target_qty, 3.0)  # absolute downsize target
        self.assertNotEqual(first.request_id, second.request_id)  # distinct -amend- seqs


if __name__ == "__main__":
    unittest.main()
