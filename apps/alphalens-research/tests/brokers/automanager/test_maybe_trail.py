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
from typing import Any

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
_TRAIL = TrailingAtrPolicy(
    resolve_policy("atr_bracket_1p5"), name="trailing_atr", activation_r=0.5, k_atr=2.0
)
_ATR_BRACKET = resolve_exit_policy("atr_bracket_1p5")  # trails=False sibling
_BE_TRAIL = resolve_exit_policy("breakeven_trail")  # lens-faithful, plan_stop-risk


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
        # peak 104.0 >= activation 100 + 0.5*1.5*4 = 103.0; target 104 - 2*4 = 96.0.
        # last_price 104 -> the live-price clamp floor (103.79) is above the 96.0
        # target, so the raw Chandelier level is emitted unclamped.
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 104.0},
            last_price_by_uic={_UIC: 104.0},
        )
        action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertAlmostEqual(action.stop_price, 96.0)  # peak 104 - k_atr 2 * atr 4
        self.assertEqual(action.target_qty, 7.0)  # netted owned, never planned
        self.assertEqual(action.order_id, "stop-1")
        self.assertEqual(action.reason, "trail")
        self.assertEqual(action.reanchor_avg_price, 100.0)
        self.assertIn("-amend-", action.request_id)

    def test_armed_ratchets_stop_above_entry_to_lock_profit(self) -> None:
        # THE FIX: with the min-distance floor anchored on the LIVE PRICE (not
        # avg_price), an armed trail CAN ratchet the stop ABOVE entry to lock in
        # profit. peak 112 -> target 112 - 2*4 = 104.0 > avg_price 100.0; live
        # price 112 -> clamp floor 111.78 does not bind, so 104.0 is emitted.
        # Under the old avg_price anchor the clamp capped at ~99.8 (< entry).
        pos = _pos(avg_price=100.0)  # qty 7.0
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 112.0},
            last_price_by_uic={_UIC: 112.0},
        )
        action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertGreater(action.stop_price, pos.avg_price)  # profit locked
        self.assertAlmostEqual(action.stop_price, 104.0)

    def test_clamp_caps_target_just_below_live_price(self) -> None:
        # When the raw Chandelier target sits ABOVE the current market (price
        # pulled back from the peak), the live-price clamp pulls it down to just
        # below the market (never at/above -> OnWrongSideOfMarket). peak 120 ->
        # raw target 120 - 2*4 = 112.0, but last_price 108 -> floor 108*0.998 =
        # 107.784, so the emitted stop is 107.784 (< market 108, still > entry).
        pos = _pos(avg_price=100.0)
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        last_price = 108.0
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 120.0},
            last_price_by_uic={_UIC: last_price},
        )
        action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertLess(action.stop_price, last_price)  # never at/above market
        self.assertAlmostEqual(action.stop_price, last_price * (1.0 - 0.002))

    def test_reconcile_long_routes_trailing_policy_to_maybe_trail(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 104.0},
            last_price_by_uic={_UIC: 104.0},
        )
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
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 102.0},
            last_price_by_uic={_UIC: 102.0},
        )
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

    def test_no_last_price_is_veto(self) -> None:
        # peak present but NO live price -> feed veto (the clamp anchor is missing)
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(pos=pos, plan=plan, legs=legs, peak_by_uic={_UIC: 104.0}, last_price_by_uic={})
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_non_finite_last_price_is_veto(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 104.0},
            last_price_by_uic={_UIC: 0.0},
        )
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
            last_price_by_uic={_UIC: 104.0},
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
            last_price_by_uic={_UIC: 104.0},
            trailed_stop_by_uic={_UIC: 94.0},
        )
        action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsInstance(action, AmendStop)

    def test_clamp_refuses_below_brief_floor_and_logs(self) -> None:
        pos = _pos()
        plan = _plan(stop_price=98.0)  # brief floor ABOVE the 96.0 proposal
        legs = (_stop_leg(),)
        # live price 104 -> clamp target is the raw 96.0, which sits below the
        # 98.0 brief floor -> never-below-brief-floor refusal (returns None + logs)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            peak_by_uic={_UIC: 104.0},
            last_price_by_uic={_UIC: 104.0},
        )
        with self.assertLogs(pm.__name__, level="INFO") as cm:
            action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsNone(action)
        self.assertTrue(any("below brief floor" in message for message in cm.output))
        # ...and it must NAME the policy that refused (#1138/#1139). This is the
        # ONE log line in the file reachable only from the trailing pass, so it
        # is the one that printed "policy=atr_bracket_1p5" — the name of the
        # non-trailing policy — while LIVE ran trailing_atr. The sibling lines in
        # _maybe_reanchor cannot be reached under a trailing policy at all: it is
        # called without `peak`, and TrailingAtrPolicy.decide_reanchor returns
        # None without one. Substring-only assertions are what let the mislabel
        # survive, so assert the value.
        self.assertTrue(
            any("policy=trailing_atr" in message for message in cm.output),
            f"the refusal must name the trailing policy, got: {cm.output}",
        )

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


class TestMaybeTrailBreakevenTrail(unittest.TestCase):
    """The ``breakeven_trail`` policy through the SAME arm: risk is the brief
    geometry (``plan.stop_price``), which ``_maybe_trail`` must pass into
    ``decide_reanchor`` as ``plan_stop`` — without it the policy is
    unconditionally dark and the lens-faithful trail never fires."""

    def test_armed_emits_amendstop_at_fractional_giveback_target(self) -> None:
        pos = _pos(avg_price=100.0)  # qty 7.0
        plan = _plan(stop_price=90.0)  # 1R = 100 - 90 = 10
        legs = (_stop_leg(),)
        # peak 106 >= activation 100 + 0.5*10 = 105 -> armed; target
        # 100 + 0.6*(106-100) = 103.6; live price 106 -> clamp floor 105.788
        # does not bind -> the raw fractional-giveback level is emitted.
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            exit_policy=_BE_TRAIL,
            peak_by_uic={_UIC: 106.0},
            last_price_by_uic={_UIC: 106.0},
        )
        action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertAlmostEqual(action.stop_price, 103.6)
        self.assertEqual(action.reason, "trail")

    def test_dark_before_half_r(self) -> None:
        pos = _pos(avg_price=100.0)
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        # peak 104.9 < 105 -> not armed; ATR-based arming (103.0 for the
        # trailing_atr fixture above) must NOT apply here.
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            exit_policy=_BE_TRAIL,
            peak_by_uic={_UIC: 104.9},
            last_price_by_uic={_UIC: 104.9},
        )
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_reconcile_long_routes_breakeven_trail_to_maybe_trail(self) -> None:
        pos = _pos(avg_price=100.0)
        plan = _plan(stop_price=90.0)
        legs = (_stop_leg(),)
        view = _view(
            pos=pos,
            plan=plan,
            legs=legs,
            exit_policy=_BE_TRAIL,
            peak_by_uic={_UIC: 106.0},
            last_price_by_uic={_UIC: 106.0},
        )
        actions = _reconcile_long(_UIC, pos, view)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], AmendStop)
        assert isinstance(actions[0], AmendStop)
        self.assertEqual(actions[0].reason, "trail")


class TestFoldTrailedMarkers(unittest.TestCase):
    """``_fold_trailed_since_latest_plan`` folds append-only ``trailed`` markers into the
    latest-by-ts ``level`` per uic (mirror of ``_fold_reanchored_markers``) —
    RESET on each new-generation ``tranche_plan`` line, with the SAME identity
    rules as ``_fold_fired_since_latest_plan``: a keyless plan or a DIFFERENT
    ``pick_key`` starts a new position generation (drop the old trailed level —
    a new position in a previously-traded uic must never inherit the prior
    trade's trail, which could sit absurdly above its entry); a re-appended plan
    with the SAME ``pick_key`` is the idempotent crash-recovery re-drive and
    must NOT reset; ``tranche_plan_retracted`` clears."""

    def test_latest_by_ts_per_uic(self) -> None:
        lines = [
            {"kind": "trailed", "uic": _UIC, "level": 95.0, "ts": 1.0},
            {"kind": "trailed", "uic": _UIC, "level": 97.0, "ts": 3.0},
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 2.0},
            {"kind": "trailed", "uic": 999, "level": 50.0, "ts": 1.0},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {_UIC: 97.0, 999: 50.0})

    def test_ignores_other_kinds_and_malformed(self) -> None:
        lines = [
            {"kind": "reanchored", "uic": _UIC, "avg_price": 95.0, "ts": 1.0},
            {"kind": "trailed", "uic": _UIC, "ts": 2.0},  # missing level
            {"kind": "trailed", "uic": "oops", "level": 96.0, "ts": 3.0},  # bad uic
            {"kind": "trailed", "uic": _UIC, "level": 96.5, "ts": 4.0},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {_UIC: 96.5})

    def test_empty_is_empty(self) -> None:
        self.assertEqual(cl._fold_trailed_since_latest_plan([]), {})

    def test_new_keyless_plan_drops_the_prior_trailed_level(self) -> None:
        # The stale-generation poison: position 1 trailed to 115, closed; a NEW
        # placement (keyless bracket-path plan) opens at a lower price. The old
        # 115 must not survive into the new generation.
        lines = [
            {"kind": "tranche_plan", "uic": _UIC},
            {"kind": "trailed", "uic": _UIC, "level": 115.0, "ts": 1.0},
            {"kind": "tranche_plan", "uic": _UIC},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {})

    def test_new_pick_key_drops_the_prior_trailed_level(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "trade-1"},
            {"kind": "trailed", "uic": _UIC, "level": 115.0, "ts": 1.0},
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "trade-2"},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {})

    def test_same_pick_key_reappend_keeps_the_trailed_level(self) -> None:
        # The already_watching crash-recovery re-drive re-journals the SAME
        # plan every tick; that identity re-append is not a new generation.
        lines = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "trade-1"},
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 1.0},
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "trade-1"},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {_UIC: 96.0})

    def test_retracted_plan_drops_the_trailed_level(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "trade-1"},
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 1.0},
            {"kind": "tranche_plan_retracted", "uic": _UIC},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {})

    def test_reset_is_per_uic(self) -> None:
        lines = [
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 1.0},
            {"kind": "trailed", "uic": 999, "level": 50.0, "ts": 1.0},
            {"kind": "tranche_plan", "uic": 999},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {_UIC: 96.0})

    def test_a_marker_before_the_first_plan_line_is_dropped(self) -> None:
        # A trailed marker that precedes the uic's FIRST tranche_plan belongs
        # to an earlier (pre-plan-journal) generation: the first plan line is a
        # new placement and resets, exactly like any other generation boundary.
        # (Markers for uics that never get a plan line at all are kept — see
        # test_latest_by_ts_per_uic, which folds without any plan lines.)
        lines = [
            {"kind": "trailed", "uic": _UIC, "level": 115.0, "ts": 1.0},
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "trade-1"},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {})


class TestCompactorKeepsTrailedRatchet(unittest.TestCase):
    """#1324: the boot journal compactor must keep the ``trailed`` markers the
    ratchet floor is folded from. It used to drop them wholesale, so every
    daemon restart (23 LIVE / 34 SIM in 14 days) erased
    ``ProtectionView.trailed_stop_by_uic`` and re-seeded ``deps.peak_tracker``
    from the live price — leaving ``_maybe_trail`` free to PATCH the stop BELOW
    the level it had already been trailed to.

    The property under test is the compactor's own stated contract: the
    compacted set must fold IDENTICALLY to the full journal. Kind-presence is
    NOT enough — ``_fold_trailed_since_latest_plan`` is generation-reset by
    ``tranche_plan``, and the compactor emits tranche lines late, so a kept
    ``trailed`` line written BEFORE its uic's plan line still folds away."""

    @staticmethod
    def _trailed_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [line for line in lines if line.get("kind") == "trailed"]

    def _assert_fold_identical(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compacted = cl._compact_standalone_stop_journal_lines(lines)
        self.assertEqual(
            cl._fold_trailed_since_latest_plan(lines),
            cl._fold_trailed_since_latest_plan(compacted),
        )
        return compacted

    def test_a_trailed_marker_survives_compaction(self) -> None:
        lines: list[dict[str, Any]] = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "OLN:2026-08-14"},
            {"kind": "trailed", "uic": _UIC, "level": 97.0, "ts": 30.0},
        ]
        compacted = cl._compact_standalone_stop_journal_lines(lines)
        self.assertEqual(len(self._trailed_lines(compacted)), 1)

    def test_the_compacted_set_folds_identically_with_a_governing_plan(self) -> None:
        lines: list[dict[str, Any]] = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "OLN:2026-08-14"},
            {"kind": "trailed", "uic": _UIC, "level": 97.0, "ts": 30.0},
        ]
        # Power check: the fold is non-empty on the full journal, so an empty
        # fold on the compacted set is an observation this test CAN produce.
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {_UIC: 97.0})
        self._assert_fold_identical(lines)

    def test_a_trailed_marker_with_no_plan_line_survives(self) -> None:
        # NOT the bracket path — that one DOES journal a plan line, keyless
        # (``_journal_tranche_plan`` passes ``pick_key=None`` unless the #1247
        # split override supplies one), and a keyless plan is an unconditional
        # generation reset. This pins the pre-plan / adopted-position shape: a
        # uic whose journal carries no ``tranche_plan`` at all. Gating the keep
        # on "has a plan" would silently drop its ratchet floor.
        lines: list[dict[str, Any]] = [{"kind": "trailed", "uic": 222, "level": 42.0, "ts": 5.0}]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {222: 42.0})
        self._assert_fold_identical(lines)

    def test_a_new_pick_key_generation_is_not_resurrected_by_compaction(self) -> None:
        # A generation-BLIND newest-per-uic keep would carry the dead trade's
        # 115.0 behind the kept plan-B line and hand the fresh position an
        # absurdly high ratchet floor (and, via _build_managed_exits, SL).
        lines: list[dict[str, Any]] = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "A"},
            {"kind": "trailed", "uic": _UIC, "level": 115.0, "ts": 1.0},
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "B"},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {})
        compacted = self._assert_fold_identical(lines)
        self.assertEqual(cl._fold_trailed_since_latest_plan(compacted), {})

    def test_a_retracted_plan_leaves_no_trailed_line(self) -> None:
        lines: list[dict[str, Any]] = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "A"},
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 1.0},
            {"kind": "tranche_plan_retracted", "uic": _UIC},
        ]
        compacted = self._assert_fold_identical(lines)
        self.assertEqual(cl._fold_trailed_since_latest_plan(compacted), {})
        self.assertEqual(self._trailed_lines(compacted), [])

    def test_only_the_newest_trailed_per_uic_is_kept(self) -> None:
        lines: list[dict[str, Any]] = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "A"},
            {"kind": "tranche_plan", "uic": 999, "pick_key": "B"},
            {"kind": "trailed", "uic": _UIC, "level": 95.0, "ts": 1.0},
            {"kind": "trailed", "uic": _UIC, "level": 97.0, "ts": 3.0},
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 2.0},
            {"kind": "trailed", "uic": 999, "level": 50.0, "ts": 1.0},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {_UIC: 97.0, 999: 50.0})
        compacted = self._assert_fold_identical(lines)
        self.assertEqual(len(self._trailed_lines(compacted)), 2, "one kept marker per uic")

    def test_an_equal_timestamp_tie_is_broken_by_the_later_line(self) -> None:
        # The fold elects with ``ts >= latest_ts[uic]``; an election using a
        # strict ``>`` would keep 95.0 and diverge on this input alone.
        lines: list[dict[str, Any]] = [
            {"kind": "trailed", "uic": _UIC, "level": 95.0, "ts": 7.0},
            {"kind": "trailed", "uic": _UIC, "level": 98.0, "ts": 7.0},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {_UIC: 98.0})
        self._assert_fold_identical(lines)

    def test_a_malformed_newer_marker_does_not_evict_the_good_older_one(self) -> None:
        # The fold SKIPS an unparsable level without touching its latest_ts, so
        # an election keyed on (uic, ts) alone would let the junk line win and
        # the uic would vanish from the compacted fold.
        lines: list[dict[str, Any]] = [
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 1.0},
            {"kind": "trailed", "uic": _UIC, "level": "oops", "ts": 9.0},
        ]
        self.assertEqual(cl._fold_trailed_since_latest_plan(lines), {_UIC: 96.0})
        self._assert_fold_identical(lines)

    def test_compaction_is_idempotent_with_trailed_lines(self) -> None:
        lines: list[dict[str, Any]] = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "A"},
            {"kind": "stop_placed", "uic": _UIC, "qty": 7.0, "ts": 10.0},
            {"kind": "trailed", "uic": _UIC, "level": 96.0, "ts": 20.0},
            {"kind": "trailed", "uic": _UIC, "level": 97.0, "ts": 30.0},
        ]
        once = cl._compact_standalone_stop_journal_lines(lines)
        self.assertEqual(once, cl._compact_standalone_stop_journal_lines(once))
        self.assertEqual(
            cl._fold_trailed_since_latest_plan(lines),
            cl._fold_trailed_since_latest_plan(once),
        )

    def test_the_kept_marker_is_written_after_its_uics_plan_lines(self) -> None:
        # Ordering is the silent failure mode: the folds walk lines in write
        # order and the FIRST kept tranche_plan for a uic always resets, so a
        # marker emitted before the plan block folds to {} even though it is
        # present in the file. Pin the index, not just the presence.
        lines: list[dict[str, Any]] = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "A"},
            {"kind": "trailed", "uic": _UIC, "level": 97.0, "ts": 30.0},
        ]
        compacted = cl._compact_standalone_stop_journal_lines(lines)
        plan_idx = [
            i
            for i, line in enumerate(compacted)
            if line.get("kind") == "tranche_plan" and line.get("uic") == _UIC
        ]
        trailed_idx = [
            i
            for i, line in enumerate(compacted)
            if line.get("kind") == "trailed" and line.get("uic") == _UIC
        ]
        self.assertTrue(plan_idx and trailed_idx)
        self.assertGreater(min(trailed_idx), max(plan_idx))

    def test_the_kept_marker_carries_its_telemetry_fields(self) -> None:
        # The kept line must be the ORIGINAL record, not a synthesised
        # {kind,uic,level,ts} stub: peak / last_price are the substrate for the
        # future /edge trailing lens and a stub would erase them every boot.
        lines: list[dict[str, Any]] = [
            {
                "kind": "trailed",
                "uic": _UIC,
                "level": 96.0,
                "ts": 20.0,
                "peak": 104.0,
                "last_price": 103.5,
            }
        ]
        kept = self._trailed_lines(cl._compact_standalone_stop_journal_lines(lines))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].get("peak"), 104.0)
        self.assertEqual(kept[0].get("last_price"), 103.5)

    def test_the_retention_check_can_actually_fail(self) -> None:
        # Positive control: the fold-identity assertion above must be able to
        # produce the disproving observation. Run it against a deliberately
        # hobbled result and require it to raise.
        lines: list[dict[str, Any]] = [
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "OLN:2026-08-14"},
            {"kind": "trailed", "uic": _UIC, "level": 97.0, "ts": 30.0},
        ]
        compacted = self._assert_fold_identical(lines)
        hobbled = [line for line in compacted if line.get("kind") != "trailed"]
        with self.assertRaises(AssertionError):
            self.assertEqual(
                cl._fold_trailed_since_latest_plan(lines),
                cl._fold_trailed_since_latest_plan(hobbled),
            )


if __name__ == "__main__":
    unittest.main()
