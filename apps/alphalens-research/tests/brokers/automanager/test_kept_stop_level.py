"""#1514: the protection pass and the take-profit amend never lower a resting stop.

Every protection branch that resized or re-placed a stop used to price it at
``plan.stop_price``. An amend sends that price as the order price, so a resize
meant only to change the quantity also moved a trailed stop back down to the
disaster level, and ``_maybe_trail``'s ratchet then refused to raise it again
until price made a new high.

The level to keep is where the stop RESTS now (``OrderState.resting_price``):
- an in-place amend keeps it unconditionally (a refused amend leaves the old
  stop resting, so nothing is ever naked);
- a NEW stop next to resting ones takes it only when a live price exists and
  the level sits outside the market band, because a refused place leaves the
  new shares naked;
- a fully naked re-place stays at the plan stop, and says so when a trailed
  level was not restored.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    TrancheExit,
    execute_tranche_exit,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    AlertOnly,
    AmendStop,
    PlaceStop,
    PlannedExit,
    ProtectionView,
    _maybe_trail,
    reconcile_protection,
)
from alphalens_pipeline.brokers.execution import RAIL_LATTICE
from broker_contract.contract import InstrumentRef, OrderState, OrderStatus, Position
from broker_contract.trade_intent.schema import ReanchorOnFill, TrailingStop

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker

_UIC = 1514
_PLAN_STOP = 44.0
_TRAILED = 55.99
_DECLARED_TRAIL = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)
_AMEND_ON = {"ALPHALENS_BROKER_AMEND_ENABLED": "1"}


def _pos(qty: float, *, avg_price: float = 50.0) -> Position:
    return Position(
        instrument=InstrumentRef(
            ticker="TRL",
            exchange_mic="XNYS",
            asset_type="Stock",
            broker_instrument_id=str(_UIC),
            broker_symbol="TRL:xnys",
        ),
        quantity=qty,
        avg_price=avg_price,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-1514",
    )


def _leg(
    order_id: str,
    amount: float,
    price: float | None,
    *,
    order_type: str = "StopIfTraded",
    relation: str | None = None,
    filled: float = 0.0,
) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=filled,
        raw_status="Working",
        uic=_UIC,
        side="SELL",
        order_type=order_type,
        amount=amount,
        external_reference=order_id,
        order_relation=relation,
        resting_price=price,
    )


def _plan(*, reaction: object = _DECLARED_TRAIL, conflicting: bool = False) -> PlannedExit:
    return PlannedExit(
        uic=_UIC,
        entry_crid="crid-1514",
        side="SELL",
        stop_price=_PLAN_STOP,
        tp_price=None,
        conflicting=conflicting,
        n_plans=2 if conflicting else 1,
        reaction=reaction,  # type: ignore[arg-type]
    )


def _view(
    pos: Position,
    legs: tuple[OrderState, ...],
    *,
    plan: PlannedExit | None = None,
    last_price: float | None = None,
    trailed: float | None = None,
) -> ProtectionView:
    return ProtectionView(
        long_positions={_UIC: pos},
        all_positions={_UIC: pos},
        sell_legs_by_uic={_UIC: legs} if legs else {},
        planned_by_uic={_UIC: plan or _plan()},
        oco_unsupported=frozenset(),
        last_price_by_uic={} if last_price is None else {_UIC: last_price},
        peak_by_uic={} if last_price is None else {_UIC: last_price},
        trailed_stop_by_uic={} if trailed is None else {_UIC: trailed},
    )


def _only(actions: list[object], kind: type) -> object:
    matching = [a for a in actions if isinstance(a, kind)]
    assert len(matching) == 1, actions
    return matching[0]


class TestAnAmendKeepsTheRestingLevel(unittest.TestCase):
    """Paths C, F and G: a resize amend moves the quantity, never the price down."""

    def test_downsize_amend_keeps_a_trailed_stop(self) -> None:
        view = _view(_pos(50), (_leg("sl", 100, _TRAILED),))
        with patch.dict(os.environ, _AMEND_ON):
            amend = _only(reconcile_protection(view), AmendStop)
        self.assertEqual(amend.target_qty, 50)  # type: ignore[attr-defined]
        self.assertEqual(amend.stop_price, _TRAILED)  # type: ignore[attr-defined]

    def test_grow_amend_keeps_a_trailed_stop(self) -> None:
        view = _view(_pos(150), (_leg("sl", 100, _TRAILED),))
        with patch.dict(os.environ, _AMEND_ON):
            amend = _only(reconcile_protection(view), AmendStop)
        self.assertEqual(amend.target_qty, 150)  # type: ignore[attr-defined]
        self.assertEqual(amend.stop_price, _TRAILED)  # type: ignore[attr-defined]

    def test_oco_downsize_keeps_the_oco_stop_leg_price(self) -> None:
        legs = (
            _leg("crid-1514-oco-0-stop", 100, _TRAILED, relation="Oco"),
            _leg("crid-1514-oco-0-tp", 100, 70.0, order_type="Limit", relation="Oco"),
        )
        with patch.dict(os.environ, _AMEND_ON):
            amend = _only(reconcile_protection(_view(_pos(50), legs)), AmendStop)
        self.assertEqual(amend.stop_price, _TRAILED)  # type: ignore[attr-defined]

    def test_oco_grow_keeps_the_oco_stop_leg_price(self) -> None:
        legs = (
            _leg("crid-1514-oco-0-stop", 100, _TRAILED, relation="Oco"),
            _leg("crid-1514-oco-0-tp", 100, 70.0, order_type="Limit", relation="Oco"),
        )
        with patch.dict(os.environ, _AMEND_ON):
            amend = _only(reconcile_protection(_view(_pos(150), legs)), AmendStop)
        self.assertEqual(amend.stop_price, _TRAILED)  # type: ignore[attr-defined]

    def test_an_unknown_resting_price_keeps_the_plan_stop(self) -> None:
        view = _view(_pos(50), (_leg("sl", 100, None),))
        with patch.dict(os.environ, _AMEND_ON):
            amend = _only(reconcile_protection(view), AmendStop)
        self.assertEqual(amend.stop_price, _PLAN_STOP)  # type: ignore[attr-defined]

    def test_a_stop_resting_below_the_plan_is_raised_to_the_plan(self) -> None:
        view = _view(_pos(50), (_leg("sl", 100, 40.0),))
        with patch.dict(os.environ, _AMEND_ON):
            amend = _only(reconcile_protection(view), AmendStop)
        self.assertEqual(amend.stop_price, _PLAN_STOP)  # type: ignore[attr-defined]


class TestANewStopBesideRestingOnes(unittest.TestCase):
    """The take-profit's market sell is still listed when protection reads orders:
    the stop was already amended down to the remainder, so the positions read
    (not yet settled) shows a deficit and B1 places a delta stop."""

    def _deficit_view(self, *, last_price: float | None) -> ProtectionView:
        legs = (
            _leg("sl", 50, _TRAILED),
            _leg("mkt", 50, None, order_type="Market"),
        )
        return _view(_pos(100), legs, last_price=last_price)

    def test_the_delta_takes_the_resting_level_when_it_is_clear_of_the_market(self) -> None:
        with patch.dict(os.environ, _AMEND_ON):
            place = _only(reconcile_protection(self._deficit_view(last_price=60.0)), PlaceStop)
        self.assertEqual(place.qty, 50)  # type: ignore[attr-defined]
        self.assertEqual(place.stop_price, _TRAILED)  # type: ignore[attr-defined]

    def test_the_delta_falls_back_to_the_plan_inside_the_market_band(self) -> None:
        # 56.00 * (1 - 0.002) = 55.888 < 55.99: Saxo could refuse a stop that
        # close to the bid, and a refused place leaves the delta naked.
        with patch.dict(os.environ, _AMEND_ON):
            place = _only(reconcile_protection(self._deficit_view(last_price=56.0)), PlaceStop)
        self.assertEqual(place.stop_price, _PLAN_STOP)  # type: ignore[attr-defined]

    def test_the_delta_falls_back_to_the_plan_without_a_live_price(self) -> None:
        with patch.dict(os.environ, _AMEND_ON):
            place = _only(reconcile_protection(self._deficit_view(last_price=None)), PlaceStop)
        self.assertEqual(place.stop_price, _PLAN_STOP)  # type: ignore[attr-defined]

    def test_the_residual_takes_the_highest_resting_level(self) -> None:
        legs = (_leg("sl-a", 50, _TRAILED), _leg("sl-b", 50, _PLAN_STOP))
        with patch.dict(os.environ, _AMEND_ON):
            place = _only(reconcile_protection(_view(_pos(50), legs, last_price=60.0)), PlaceStop)
        self.assertEqual(place.qty, 50)  # type: ignore[attr-defined]
        self.assertEqual(place.stop_price, _TRAILED)  # type: ignore[attr-defined]

    def test_a_partially_triggered_stop_does_not_set_the_level(self) -> None:
        # The market traded through 55.99, so a new stop there is on the wrong side.
        legs = (_leg("sl-a", 50, _TRAILED, filled=10), _leg("sl-b", 50, _PLAN_STOP))
        with patch.dict(os.environ, _AMEND_ON):
            place = _only(reconcile_protection(_view(_pos(50), legs, last_price=60.0)), PlaceStop)
        self.assertEqual(place.stop_price, _PLAN_STOP)  # type: ignore[attr-defined]

    def test_the_conservative_branch_keeps_the_plan_stop(self) -> None:
        legs = (_leg("sl", 50, _TRAILED),)
        view = _view(_pos(100), legs, plan=_plan(conflicting=True), last_price=60.0)
        place = _only(reconcile_protection(view), PlaceStop)
        self.assertEqual(place.stop_price, _PLAN_STOP)  # type: ignore[attr-defined]


class TestANakedReplaceSaysWhatItDidNotRestore(unittest.TestCase):
    """Path I: the stop was cancelled. It comes back at the plan stop, because a
    journaled level can belong to an earlier fill of the same pick, and the owner
    is told which trailed level was not restored."""

    def test_a_trailed_uic_is_re_placed_at_the_plan_with_one_alert(self) -> None:
        actions = reconcile_protection(_view(_pos(100), (), last_price=60.0, trailed=_TRAILED))
        place = _only(actions, PlaceStop)
        self.assertEqual(place.stop_price, _PLAN_STOP)  # type: ignore[attr-defined]
        alert = _only(actions, AlertOnly)
        self.assertIn("55.99", alert.reason)  # type: ignore[attr-defined]
        self.assertIn("not restored", alert.reason)  # type: ignore[attr-defined]

    def test_a_fresh_fill_without_a_trailed_level_raises_no_alert(self) -> None:
        actions = reconcile_protection(_view(_pos(100), (), last_price=60.0))
        self.assertEqual([type(a) for a in actions], [PlaceStop])


class TestTheTrailNeverPatchesAHigherRestingStopDown(unittest.TestCase):
    def test_a_proposal_below_the_resting_stop_is_refused(self) -> None:
        # avg 50, plan 45 -> 1R = 5. peak 59.17 arms; target = 50 + 0.6 * 9.17
        # = 55.502, which clears the journaled trailed level 55.00 but sits
        # under the stop that actually rests at 56.00.
        plan = PlannedExit(
            uic=_UIC,
            entry_crid="crid-1514",
            side="SELL",
            stop_price=45.0,
            tp_price=None,
            conflicting=False,
            n_plans=1,
            reaction=_DECLARED_TRAIL,
        )
        legs = (_leg("sl", 100, 56.0),)
        view = ProtectionView(
            long_positions={_UIC: _pos(100)},
            all_positions={_UIC: _pos(100)},
            sell_legs_by_uic={_UIC: legs},
            planned_by_uic={_UIC: plan},
            oco_unsupported=frozenset(),
            peak_by_uic={_UIC: 59.17},
            last_price_by_uic={_UIC: 59.0},
            trailed_stop_by_uic={_UIC: 55.0},
        )
        self.assertIsNone(_maybe_trail(_UIC, _pos(100), plan, legs, view))

    def _trail(
        self,
        *,
        avg_price: float,
        plan_stop: float,
        peak: float,
        last_price: float,
        resting: float | None,
        journaled: float | None,
    ) -> AmendStop | None:
        plan = PlannedExit(
            uic=_UIC,
            entry_crid="crid-1581",
            side="SELL",
            stop_price=plan_stop,
            tp_price=None,
            conflicting=False,
            n_plans=1,
            reaction=_DECLARED_TRAIL,
        )
        pos = _pos(100, avg_price=avg_price)
        legs = (_leg("sl", 100, resting),)
        view = ProtectionView(
            long_positions={_UIC: pos},
            all_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: legs},
            planned_by_uic={_UIC: plan},
            oco_unsupported=frozenset(),
            peak_by_uic={_UIC: peak},
            last_price_by_uic={_UIC: last_price},
            trailed_stop_by_uic={} if journaled is None else {_UIC: journaled},
        )
        return _maybe_trail(_UIC, pos, plan, legs, view)

    def test_a_journaled_level_of_exactly_zero_is_a_floor_not_an_absence(self) -> None:
        """The floor list must be filtered on ``is not None``, never on
        truthiness: ``0.0`` is falsy and is a real level.

        A zero floor is only OBSERVABLE where it can veto, which needs a clamped
        level within ``_TRAIL_STEP_EPS`` (0.02) of zero -- so the prices here are
        pennies. avg 0.0100, brief floor 0.0090, peak 0.0120, live 0.0115 put the
        clamped level at 0.0112, and 0.0112 <= 0.0 + 0.02 refuses. At ordinary
        prices the same mutation changes nothing, which is why no existing test
        caught it: measured, a truthiness filter killed 0 of the 3126 tests in
        ``tests/brokers``."""
        penny = {
            "avg_price": 0.01,
            "plan_stop": 0.009,
            "peak": 0.012,
            "last_price": 0.0115,
            "resting": None,
        }
        self.assertIsNone(self._trail(journaled=0.0, **penny))
        # Existence control: a property can be true and empty. Without the zero
        # floor these very inputs DO move the stop, so the assertion above is
        # about the floor and not about an arm that never fires at this scale.
        moved = self._trail(journaled=None, **penny)
        self.assertIsInstance(moved, AmendStop)
        self.assertAlmostEqual(moved.stop_price, 0.0112, places=6)  # type: ignore[union-attr]

    def test_a_non_finite_resting_price_is_not_a_floor(self) -> None:
        """The resting price enters the ratchet through
        ``_resting_stop_price``, which drops a non-finite or non-positive one.
        Read straight off ``leg.resting_price`` instead and an infinite price
        becomes an infinite floor that vetoes every move for ever.

        Measured: composing from the raw field killed 0 of the 3126 tests in
        ``tests/brokers``."""
        moved = self._trail(
            avg_price=50.0,
            plan_stop=45.0,
            peak=59.17,
            last_price=59.0,
            resting=float("inf"),
            journaled=None,
        )
        self.assertIsInstance(moved, AmendStop)
        self.assertAlmostEqual(moved.stop_price, 55.502, places=6)  # type: ignore[union-attr]
        # Existence control: a FINITE resting price above the proposal still
        # refuses, so this test is about the filter and not about a dead arm.
        self.assertIsNone(
            self._trail(
                avg_price=50.0,
                plan_stop=45.0,
                peak=59.17,
                last_price=59.0,
                resting=56.0,
                journaled=None,
            )
        )


class TestTheTakeProfitAmendKeepsTheRestingLevel(unittest.TestCase):
    def test_the_tranche_amend_never_lowers_the_stop(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=50.0)
        sl_id = broker.add_resting_sell("KO", 100, 56.0, order_type="StopIfTraded")
        sl = next(o for o in broker.list_working_sell_orders() if o.order_id == sl_id)
        execute_tranche_exit(
            broker,
            uic=uic,
            exit=TrancheExit("tp1", 40, 60.0),
            sl_leg=sl,
            stop_price=55.0,
            request_ref="KO-g0",
            lattice=RAIL_LATTICE,
        )
        resting = next(o for o in broker.list_working_sell_orders() if o.order_id == sl_id)
        self.assertEqual(resting.amount, 60.0)
        self.assertEqual(resting.resting_price, 56.0)


class TestALiveTradeIsFetchedForEveryStopMovingPick(unittest.TestCase):
    """The placement rule needs a live price. It used to be fetched only for picks
    that declare a trail, so a re-anchor pick always fell back to the plan stop
    and its raised stop could be superseded by a lower one."""

    def _journal_with(self, reactions: dict[int, object]) -> list[dict[str, object]]:
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "standalone_stops.jsonl"
            with patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                for uic, reaction in reactions.items():
                    cl._append_standalone_stop_journal(
                        cl._build_planned_line(
                            entry_crid=f"crid-{uic}",
                            uic=uic,
                            side="SELL",
                            stop_price=90.0,
                            take_profit=None,
                            tier_index=0,
                            reaction=reaction,  # type: ignore[arg-type]
                        )
                    )
                return list(cl._iter_standalone_stop_journal())

    def test_trail_and_reanchor_picks_are_fetched_and_an_undeclared_one_is_not(self) -> None:
        lines = self._journal_with(
            {1: _DECLARED_TRAIL, 2: ReanchorOnFill(k_atr=2.0, atr=3.0), 3: None}
        )
        self.assertEqual(cl._uics_moving_their_stop(lines), frozenset({1, 2}))


if __name__ == "__main__":
    unittest.main()
