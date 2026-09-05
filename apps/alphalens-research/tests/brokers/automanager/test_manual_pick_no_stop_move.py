"""#1325: a manual (`alphalens broker arm-manual`) pick's stop is never MOVED by
the daemon — under any exit policy in the registry.

DECISION (2026-09-05, issue #1325). The daemon holds a manual pick's disaster
stop and never tightens it; the exit is managed by hand. Two reasons, neither of
them the counterfactual replay (which is n=2 and contained no reversal, the one
scenario a trail exists for):

  * the trail's risk unit is ``avg_price - plan_stop``, and for a manual pick
    ``plan_stop`` is a hand-set number, so the 0.5R activation threshold means a
    different thing on every pick — 6.8% of entry on AMBA (inside one session's
    range), 29% on RHI (unreachable);
  * the exit is a human decision on these picks, which is what issue #1236
    exists to make explicit per pick.

MECHANISM, and why this file exists. The decision is implemented today by a
STRUCTURAL property rather than by a switch: ``arm-manual`` builds its intent
with ``exit=None``, so ``control_loop._geometry_shadow_stamp`` returns ``None``,
so the ``planned`` journal line carries no ``geometry`` key, so
``PlannedExit.reanchor`` folds to ``None`` — and BOTH post-fill stop-move arms
(``_maybe_reanchor`` and ``_maybe_trail``) refuse on exactly that. The property
is real but it is a side effect, so these tests pin it: they turn red the moment
``arm-manual`` starts producing an exit spec, or a guard is relaxed, which is
the change that would silently start trailing real money.

Do NOT "fix" the ATR guard in ``_maybe_trail`` on its own to make
``breakeven_trail`` (which ignores ATR) arm here. That guard is what implements
this decision; relaxing it turns trailing ON for exactly the picks the decision
excludes. The supported way to trail a manual pick is the per-pick policy
override, issue #1236.

Provenance of the numbers: the AMBA LIVE round trip of 2026-09-04 (entry 8 @
59.00, disaster stop 55.00, so 1R = 4.00 and the 0.5R activation sits at 61.00;
session peak 62.78). Its stored ``planned`` line in
``~/.alphalens/broker_orders/live/standalone_stops.jsonl`` carries no
``geometry`` key.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager.control_loop import (
    _build_managed_exits,
    _fold_trailed_since_latest_plan,
    _geometry_shadow_stamp,
)
from alphalens_pipeline.brokers.automanager.manual_intent import build_manual_intent
from alphalens_pipeline.brokers.automanager.position_manager import (
    AmendStop,
    NoOp,
    PlannedExit,
    ProtectionView,
    ReanchorFacts,
    _reconcile_long,
)
from broker_contract.contract import InstrumentRef, OrderState, OrderStatus, Position
from broker_contract.exit_geometry.registry import exit_policy_registry
from broker_contract.trade_intent.schema import (
    ExitGeometrySpec,
    InitialLevels,
    ReanchorOnFill,
)

_UIC = 267154
_AVG_PRICE = 59.00
_PLAN_STOP = 55.00  # 1R = 4.00 -> breakeven_trail arms at 61.00
_QTY = 8.0
_PEAK = 62.78  # the session high, far above the activation threshold
_LAST_PRICE = 62.40
_ATR = 0.4674

# The policies that move a resting stop once the plan carries geometry facts.
# ``setup_static`` is the inert one and is expected to stay a NoOp in BOTH arms —
# it is what makes the positive control below a real discriminator rather than a
# blanket "everything fires".
_STOP_MOVING_POLICIES = ("atr_bracket_1p5", "trailing_atr", "breakeven_trail")


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        ticker="AMBA",
        exchange_mic="XNAS",
        asset_type="Stock",
        broker_instrument_id=str(_UIC),
        broker_symbol="AMBA:xnas",
    )


def _position() -> Position:
    return Position(
        instrument=_instrument(),
        quantity=_QTY,
        avg_price=_AVG_PRICE,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-amba",
    )


def _resting_stop() -> OrderState:
    """The one clean standalone stop the manual pick rests behind."""
    return OrderState(
        order_id="stop-1",
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=0.0,
        raw_status="Working",
        uic=_UIC,
        side="SELL",
        order_type="StopIfTraded",
        amount=_QTY,
        external_reference="stop-1",
    )


def _plan(*, reanchor: ReanchorFacts | None) -> PlannedExit:
    return PlannedExit(
        uic=_UIC,
        entry_crid="AMBA-2026-09-04-entry-t0",
        side="SELL",
        stop_price=_PLAN_STOP,
        tp_price=None,
        conflicting=False,
        n_plans=1,
        reanchor=reanchor,
    )


def _actions_for(
    policy_name: str,
    *,
    reanchor: ReanchorFacts | None,
    trailed_stop_by_uic: dict[int, float] | None = None,
) -> list:
    pos = _position()
    view = ProtectionView(
        long_positions={_UIC: pos},
        all_positions={_UIC: pos},
        sell_legs_by_uic={_UIC: (_resting_stop(),)},
        planned_by_uic={_UIC: _plan(reanchor=reanchor)},
        oco_unsupported=frozenset(),
        exit_policy=exit_policy_registry()[policy_name],
        peak_by_uic={_UIC: _PEAK},
        last_price_by_uic={_UIC: _LAST_PRICE},
        trailed_stop_by_uic=trailed_stop_by_uic or {},
    )
    return list(_reconcile_long(_UIC, pos, view))


def _manual_intent():
    return build_manual_intent(
        ticker="amba",
        mic="XNAS",
        tiers_raw=["59.0"],
        stop=_PLAN_STOP,
        tps_raw=["62.5:100"],
        no_tp=False,
        size_pct=None,
        notional=500.0,
        frame=15000.0,
        ttl_days=None,
        arm_date=dt.date(2026, 9, 4),
        armed_ts="2026-09-04T12:00:00+00:00",
    )


class TestManualPickCarriesNoGeometryStamp(unittest.TestCase):
    """The joint the decision hangs on: ``arm-manual`` -> ``exit=None`` -> no
    ``geometry`` stamp on the journal line, whatever policy the daemon resolved."""

    def test_arm_manual_builds_an_intent_without_an_exit_spec(self) -> None:
        self.assertIsNone(_manual_intent().exit)

    def test_no_geometry_stamp_is_journaled_for_a_manual_pick(self) -> None:
        intent = _manual_intent()
        for name, policy in exit_policy_registry().items():
            with self.subTest(policy=name):
                stamp = _geometry_shadow_stamp(
                    intent.exit,
                    intent.spec,
                    use_geometry=policy.applies_geometry,
                    exit_policy=policy,
                )
                self.assertIsNone(stamp)

    def test_positive_control_an_exit_spec_does_produce_a_stamped_atr(self) -> None:
        """Guards the check above against rotting to vacuous: the same call on an
        intent that DOES carry an exit spec stamps a usable ``atr``."""
        intent = _manual_intent()
        exit_spec = ExitGeometrySpec(
            initial_levels=InitialLevels(stop=_PLAN_STOP, tp=62.5),
            reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=_ATR),),
        )
        policy = exit_policy_registry()["breakeven_trail"]
        stamp = _geometry_shadow_stamp(
            exit_spec, intent.spec, use_geometry=policy.applies_geometry, exit_policy=policy
        )
        assert stamp is not None
        self.assertEqual(stamp["atr"], _ATR)


class TestManualPickIsPolicyImmune(unittest.TestCase):
    """The end-to-end pin: with a peak far above every activation threshold, a
    manual-shaped plan yields no stop move under ANY registry policy."""

    def test_no_stop_move_under_any_policy(self) -> None:
        for name in exit_policy_registry():
            with self.subTest(policy=name):
                actions = _actions_for(name, reanchor=None)
                self.assertEqual([type(a) for a in actions], [NoOp])

    def test_positive_control_the_same_numbers_move_the_stop_once_stamped(self) -> None:
        """The discriminator. If this ever goes green-by-vacuity (nothing fires
        even WITH the stamp) the test above proves nothing."""
        for name in _STOP_MOVING_POLICIES:
            with self.subTest(policy=name):
                actions = _actions_for(name, reanchor=ReanchorFacts(k_atr=1.5, atr=_ATR))
                self.assertEqual([type(a) for a in actions], [AmendStop])

    def test_the_inert_policy_stays_a_noop_even_when_stamped(self) -> None:
        actions = _actions_for("setup_static", reanchor=ReanchorFacts(k_atr=1.5, atr=_ATR))
        self.assertEqual([type(a) for a in actions], [NoOp])

    def test_the_trail_target_does_not_depend_on_the_atr_it_is_vetoed_for(self) -> None:
        """Why the ATR guard must not be relaxed in isolation: ``breakeven_trail``
        discards ``atr`` entirely, so the veto is for a value the policy never
        reads. Two ATRs three orders of magnitude apart give the SAME stop."""
        targets = []
        for atr in (_ATR, 99.0):
            actions = _actions_for("breakeven_trail", reanchor=ReanchorFacts(k_atr=1.5, atr=atr))
            self.assertEqual([type(a) for a in actions], [AmendStop])
            amend = actions[0]
            self.assertIsInstance(amend, AmendStop)
            targets.append(amend.stop_price)
        self.assertEqual(targets[0], targets[1])


class TestInheritedTrailedLevelCannotMoveAManualPick(unittest.TestCase):
    """The second route to a moved stop, which does NOT go through the guard
    above: ``trailed_stop_by_uic`` is a journal-lifetime fold, so a level earned
    by an EARLIER position on the same uic can outlive it (SIM really carries
    GME twice — once from a brief, once manual). Two consumers, both checked.
    """

    def test_the_ratchet_floor_alone_proposes_nothing(self) -> None:
        """``_maybe_trail`` only GATES a proposal against the floor; it never
        proposes the floor itself, so an inherited level cannot become an
        amend on a manual shape."""
        for name in exit_policy_registry():
            with self.subTest(policy=name):
                actions = _actions_for(
                    name, reanchor=None, trailed_stop_by_uic={_UIC: _PLAN_STOP + 6.5}
                )
                self.assertEqual([type(a) for a in actions], [NoOp])

    def test_a_manual_tranche_plan_resets_the_inherited_level(self) -> None:
        """The other consumer is ``control_loop._build_managed_exits``, which
        takes ``max(plan stop, trailed)`` and PLACES it. A manual pick that
        journals its own ``tranche_plan`` carries a new ``pick_key``, so the
        generation reset clears the inherited marker before the fold is read."""
        earlier = [
            {
                "kind": "tranche_plan",
                "uic": _UIC,
                "ts": 100.0,
                "pick_key": "AMBA:2026-08-27",
                "tiers": [],
                "entry_crid": "AMBA-2026-08-27-entry-t0",
            },
            {"kind": "trailed", "uic": _UIC, "ts": 110.0, "level": _PLAN_STOP + 6.5},
        ]
        # Positive control: without the manual pick's own plan line the level survives.
        self.assertEqual(_fold_trailed_since_latest_plan(earlier), {_UIC: _PLAN_STOP + 6.5})
        manual_plan = {
            "kind": "tranche_plan",
            "uic": _UIC,
            "ts": 200.0,
            "pick_key": "AMBA:2026-09-04",
            "tiers": [],
            "entry_crid": "AMBA-2026-09-04-entry-t0",
        }
        self.assertEqual(_fold_trailed_since_latest_plan([*earlier, manual_plan]), {})

    def test_a_no_tp_manual_pick_is_skipped_by_the_managed_exit_builder(self) -> None:
        """The residual case the reset does NOT cover: ``--no-tp`` journals no
        ``tranche_plan`` at all (``_journal_tranche_plan_core`` returns early on
        an empty ladder), so an inherited level stays in the fold. It still
        cannot be placed — ``_build_managed_exits`` skips a uic with no tranche
        plan. Closed for a DIFFERENT reason than the case above, which is why
        it is pinned separately."""
        managed = _build_managed_exits(
            long_positions=[_position()],
            tranche_plans={},  # --no-tp: nothing journaled for this uic
            fired={},
            trailed={_UIC: _PLAN_STOP + 6.5},
        )
        self.assertEqual(managed, [])

    def test_positive_control_a_tranche_plan_does_let_the_level_through(self) -> None:
        """Without this the test above would pass even if the builder ignored
        ``trailed`` entirely."""
        managed = _build_managed_exits(
            long_positions=[_position()],
            tranche_plans={_UIC: ((), _QTY, _PLAN_STOP)},
            fired={},
            trailed={_UIC: _PLAN_STOP + 6.5},
        )
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0].stop_price, _PLAN_STOP + 6.5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
