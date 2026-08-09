"""Task 2: the pure ``_maybe_trail`` reconcile arm + its ``ProtectionView`` peak /
ratchet inputs + the ``trailed`` journal fold.

``_maybe_trail`` is the trailing-stop sibling of the shipped ``_maybe_reanchor``:
a covered standalone stop is PATCHed UP to the Chandelier target ``peak - k*atr``
once the ``trailing_atr`` policy is armed, guarded by (1) a coarse ratchet against
the last live trailed level and (2) the never-below-brief-floor clamp. It is a
PURE oracle over a hand-built ``ProtectionView`` snapshot — no broker, no feed,
no network. Non-trailing policies (``setup_static`` / ``atr_bracket_1p5``) route
to ``_maybe_reanchor`` instead, so they stay byte-identical.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import position_manager as pm
from alphalens_pipeline.brokers.automanager.position_manager import (
    AmendStop,
    NoOp,
    PlannedExit,
    ProtectionView,
    ReanchorFacts,
    _maybe_trail,
    _reconcile_long,
)
from broker_contract.contract import (
    InstrumentRef,
    OrderState,
    OrderStatus,
    Position,
)
from broker_contract.exit_geometry import SetupStaticPolicy, resolve_exit_policy
from broker_contract.exit_geometry.policy import TrailingAtrPolicy
from broker_contract.exit_geometry.registry import resolve_policy

_UIC = 43070

# A custom trailing policy whose Chandelier target lands BELOW the min-distance
# floor (anchor = avg_price) for the peaks used here, so the armed target is the
# raw ``peak - k*atr`` and the assertions read cleanly. atr_bracket_1p5 sets
# stop_atr_mult=1.5, so risk = 1.5*atr and activation fires at
# ``peak >= avg_price + 0.5*1.5*atr``.
_TRAIL = TrailingAtrPolicy(resolve_policy("atr_bracket_1p5"), activation_r=0.5, k_atr=2.0)
_ATR_BRACKET = resolve_exit_policy("atr_bracket_1p5")  # trails=False sibling


def _instrument(uic: int = _UIC) -> InstrumentRef:
    return InstrumentRef(
        ticker="BIO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id=str(uic),
        broker_symbol="BIO:xnys",
    )


def _pos(qty: float = 7.0, *, avg_price: float = 100.0, uic: int = _UIC) -> Position:
    return Position(
        instrument=_instrument(uic),
        quantity=qty,
        avg_price=avg_price,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-1",
    )


def _stop_leg(amount: float = 7.0, *, uic: int = _UIC) -> OrderState:
    return OrderState(
        order_id="stop-1",
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=0.0,
        raw_status="Working",
        uic=uic,
        side="SELL",
        order_type="StopIfTraded",
        amount=amount,
        external_reference="stop-1",
    )


def _plan(*, stop_price: float = 90.0, atr: float = 4.0, k_atr: float = 2.0) -> PlannedExit:
    return PlannedExit(
        uic=_UIC,
        entry_crid="crid",
        side="SELL",
        stop_price=stop_price,
        tp_price=None,
        conflicting=False,
        n_plans=1,
        reanchor=ReanchorFacts(k_atr=k_atr, atr=atr),
    )


def _view(
    *,
    pos: Position,
    plan: PlannedExit,
    legs: tuple[OrderState, ...],
    exit_policy: object = _TRAIL,
    peak_by_uic: dict[int, float] | None = None,
    last_price_by_uic: dict[int, float] | None = None,
    trailed_stop_by_uic: dict[int, float] | None = None,
    amend_recently_failed: frozenset[int] = frozenset(),
) -> ProtectionView:
    return ProtectionView(
        long_positions={_UIC: pos},
        all_positions={_UIC: pos},
        sell_legs_by_uic={_UIC: legs},
        planned_by_uic={_UIC: plan},
        oco_unsupported=frozenset(),
        amend_recently_failed=amend_recently_failed,
        exit_policy=exit_policy,
        peak_by_uic=peak_by_uic or {},
        last_price_by_uic=last_price_by_uic or {},
        trailed_stop_by_uic=trailed_stop_by_uic or {},
    )


class TestMaybeTrailArmed(unittest.TestCase):
    """The armed happy path: peak clears the activation threshold, the ratchet is
    open, the clamp allows the tighten -> an ``AmendStop`` at ``peak - k*atr``."""

    def test_armed_emits_amendstop_at_chandelier_target(self) -> None:
        pos = _pos()  # avg_price 100.0, qty 7.0
        plan = _plan(stop_price=90.0)  # brief floor well below the 96.0 target
        legs = (_stop_leg(),)
        # peak 104.0 >= activation 100 + 0.5*1.5*4 = 103.0; target 104 - 2*4 = 96.0
        view = _view(pos=pos, plan=plan, legs=legs, peak_by_uic={_UIC: 104.0})
        action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertAlmostEqual(action.stop_price, 96.0)  # peak 104 - k_atr 2 * atr 4
        self.assertEqual(action.target_qty, 7.0)  # netted owned, never planned
        self.assertEqual(action.order_id, "stop-1")
        self.assertEqual(action.reason, "trail")
        self.assertEqual(action.reanchor_avg_price, 100.0)
        self.assertIn("-amend-", action.request_id)

    def test_reconcile_long_routes_trailing_policy_to_maybe_trail(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(pos=pos, plan=plan, legs=legs, peak_by_uic={_UIC: 104.0})
        actions = _reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], AmendStop)
        assert isinstance(actions[0], AmendStop)
        self.assertEqual(actions[0].reason, "trail")


class TestMaybeTrailDark(unittest.TestCase):
    """The refusal / veto paths -> ``None`` (never a bad stop)."""

    def test_dark_before_activation(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        # peak 102.0 < activation 103.0 -> policy returns None (not yet armed)
        view = _view(pos=pos, plan=plan, legs=legs, peak_by_uic={_UIC: 102.0})
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_no_peak_is_veto(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(pos=pos, plan=plan, legs=legs, peak_by_uic={})  # feed veto
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_non_finite_peak_is_veto(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(pos=pos, plan=plan, legs=legs, peak_by_uic={_UIC: 0.0})
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_ratchet_drops_proposal_at_or_below_floor_plus_eps(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        # proposed 96.0; last trailed level 95.99 -> 96.0 <= 95.99 + eps -> drop
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 104.0},
            trailed_stop_by_uic={_UIC: 96.0 - pm._TRAIL_STEP_EPS / 2.0},
        )
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_ratchet_clears_coarse_step_fires(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        # proposed 96.0 clears 94.0 + eps -> fires
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 104.0},
            trailed_stop_by_uic={_UIC: 94.0},
        )
        action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsInstance(action, AmendStop)

    def test_clamp_refuses_below_brief_floor_and_logs(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=98.0)  # brief floor ABOVE the 96.0 proposal
        legs = (_stop_leg(),)
        view = _view(pos=pos, plan=plan, legs=legs, peak_by_uic={_UIC: 104.0})
        with self.assertLogs(pm.__name__, level="INFO") as cm:
            action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsNone(action)
        self.assertTrue(any("below brief floor" in message for message in cm.output))

    def test_amend_recently_failed_is_veto(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 104.0},
            amend_recently_failed=frozenset({_UIC}),
        )
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_plan_reanchor_none_is_veto(self) -> None:
        pos = _pos()
        plan = PlannedExit(
            uic=_UIC,
            entry_crid="crid",
            side="SELL",
            stop_price=90.0,
            tp_price=None,
            conflicting=False,
            n_plans=1,
            reanchor=None,
        )
        legs = (_stop_leg(),)
        view = _view(pos=pos, plan=plan, legs=legs, peak_by_uic={_UIC: 104.0})
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))


class TestNonTrailingPolicyNeverTrails(unittest.TestCase):
    """A non-trailing policy has ``trails=False``: ``_maybe_trail`` returns None,
    and ``_reconcile_long`` routes it to ``_maybe_reanchor`` (byte-identical)."""

    def test_setup_static_maybe_trail_returns_none(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            exit_policy=SetupStaticPolicy(),
            peak_by_uic={_UIC: 104.0},
        )
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_atr_bracket_maybe_trail_returns_none(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            exit_policy=_ATR_BRACKET,
            peak_by_uic={_UIC: 104.0},
        )
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_reconcile_long_routes_atr_bracket_to_reanchor(self) -> None:
        # avg_price 100.0 realized ABOVE the planned blend -> reanchor target
        # 100 - 1.5*4 = 94.0 sits above the 90.0 brief floor -> _maybe_reanchor
        # fires with its own reason (proves the non-trailing arm is still used).
        pos = _pos(avg_price=100.0)
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            exit_policy=_ATR_BRACKET,
            peak_by_uic={_UIC: 104.0},  # ignored by the reanchor arm
        )
        actions = _reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], AmendStop)
        assert isinstance(actions[0], AmendStop)
        self.assertEqual(actions[0].reason, "reanchor-on-fill")

    def test_reconcile_long_inert_setup_static_is_noop(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            exit_policy=SetupStaticPolicy(),
            peak_by_uic={_UIC: 104.0},
        )
        self.assertEqual(_reconcile_long(_UIC, pos, view), [NoOp()])


class TestFoldTrailedMarkers(unittest.TestCase):
    """``_fold_trailed_markers`` folds append-only ``trailed`` markers into the
    latest-by-ts ``level`` per uic (mirror of ``_fold_reanchored_markers``)."""

    def test_latest_by_ts_per_uic(self) -> None:
        lines = [
            {"kind": "trailed", "uic": _UIC, "level": 95.0, "ts": 1.0},
            {"kind": "trailed", "uic": _UIC, "level": 97.0, "ts": 3.0},
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 2.0},
            {"kind": "trailed", "uic": 999, "level": 50.0, "ts": 1.0},
        ]
        self.assertEqual(cl._fold_trailed_markers(lines), {_UIC: 97.0, 999: 50.0})

    def test_ignores_other_kinds_and_malformed(self) -> None:
        lines = [
            {"kind": "reanchored", "uic": _UIC, "avg_price": 95.0, "ts": 1.0},
            {"kind": "trailed", "uic": _UIC, "ts": 2.0},  # missing level
            {"kind": "trailed", "uic": "oops", "level": 96.0, "ts": 3.0},  # bad uic
            {"kind": "trailed", "uic": _UIC, "level": 96.5, "ts": 4.0},
        ]
        self.assertEqual(cl._fold_trailed_markers(lines), {_UIC: 96.5})

    def test_empty_is_empty(self) -> None:
        self.assertEqual(cl._fold_trailed_markers([]), {})


if __name__ == "__main__":
    unittest.main()
