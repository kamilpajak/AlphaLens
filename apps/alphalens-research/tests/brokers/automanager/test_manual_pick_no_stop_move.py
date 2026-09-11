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

MECHANISM — and it changed with #1236, which is the point of this rewrite. The
decision used to rest on a SIDE EFFECT: ``arm-manual`` builds its intent with
``exit=None``, so no geometry stamp reached the journal, so ``PlannedExit``
carried no ATR, and both post-fill stop-move arms refused on exactly that. The
property was real but incidental, and the note here used to warn against "fixing"
the ATR guard in isolation, because relaxing it would have turned trailing ON for
exactly the picks the decision excludes.

It is now a GUARD. A pick's stop is managed if and only if its document DECLARES
management, and a manual pick declares nothing. The ATR guard could therefore be
moved into the policies that need an ATR without touching this promise — which is
what #1236 did.

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
from alphalens_pipeline.brokers.automanager.control_loop import (
    _build_planned_line as _planned_line,
)
from alphalens_pipeline.brokers.automanager.manual_intent import build_manual_intent
from alphalens_pipeline.brokers.automanager.position_manager import (
    AmendStop,
    NoOp,
    PlannedExit,
    ProtectionView,
    _reconcile_long,
)
from broker_contract.contract import InstrumentRef, OrderState, OrderStatus, Position
from broker_contract.exit_geometry.registry import exit_policy_registry
from broker_contract.trade_intent.schema import (
    ExitGeometrySpec,
    InitialLevels,
    ReanchorOnFill,
    TrailingStop,
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


def _plan(*, reaction: object) -> PlannedExit:
    return PlannedExit(
        uic=_UIC,
        entry_crid="AMBA-2026-09-04-entry-t0",
        side="SELL",
        stop_price=_PLAN_STOP,
        tp_price=None,
        conflicting=False,
        n_plans=1,
        reaction=reaction,  # type: ignore[arg-type]
    )


def _actions_for(
    policy_name: str,
    *,
    reaction: object,
    trailed_stop_by_uic: dict[int, float] | None = None,
) -> list:
    pos = _position()
    view = ProtectionView(
        long_positions={_UIC: pos},
        all_positions={_UIC: pos},
        sell_legs_by_uic={_UIC: (_resting_stop(),)},
        planned_by_uic={_UIC: _plan(reaction=reaction)},
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
                actions = _actions_for(name, reaction=None)
                self.assertEqual([type(a) for a in actions], [NoOp])

    def test_positive_control_the_same_numbers_move_the_stop_once_stamped(self) -> None:
        """The discriminator. If this ever goes green-by-vacuity (nothing fires
        even WITH the stamp) the test above proves nothing."""
        for name in _STOP_MOVING_POLICIES:
            with self.subTest(policy=name):
                actions = _actions_for(name, reaction=ReanchorOnFill(k_atr=1.5, atr=_ATR))
                self.assertEqual([type(a) for a in actions], [AmendStop])

    def test_the_daemon_policy_no_longer_silences_a_declared_pick(self) -> None:
        """The inversion, asserted in the direction that catches a regression.

        This used to read "the inert daemon policy stays a NoOp even when the
        plan is stamped" — the env could silence a pick that had asked for
        management. Since #1236 the declaration decides, so the same shape under
        ``setup_static`` fires. The manual pick is protected by declaring
        NOTHING, which is the test above, not by the daemon being inert."""
        actions = _actions_for("setup_static", reaction=ReanchorOnFill(k_atr=1.5, atr=_ATR))
        self.assertEqual([type(a) for a in actions], [AmendStop])

    def test_a_declared_trail_ignores_a_geometry_stamp_entirely(self) -> None:
        """The successor to a test whose subject #1236 removed.

        It used to read "the trail target does not depend on the ATR it is
        vetoed for" — the point being that the guard refused a policy for a
        number it discards. There is no such guard now. What remains worth
        pinning is the other half: a declared trail's target is a function of
        the declaration and the price path only, so whatever telemetry the plan
        also carries cannot change where the stop goes."""
        declared = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)
        targets = []
        for policy_name in ("setup_static", "atr_bracket_1p5", "breakeven_trail"):
            actions = _actions_for(policy_name, reaction=declared)
            self.assertEqual([type(a) for a in actions], [AmendStop])
            amend = actions[0]
            assert isinstance(amend, AmendStop)
            targets.append(amend.stop_price)
        self.assertEqual(len(set(targets)), 1)


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
                    name, reaction=None, trailed_stop_by_uic={_UIC: _PLAN_STOP + 6.5}
                )
                self.assertEqual([type(a) for a in actions], [NoOp])

    def test_a_later_manual_pick_does_not_inherit_the_level(self) -> None:
        """The other consumer is ``control_loop._build_managed_exits``, which
        takes ``max(plan stop, trailed)`` and PLACES it.

        The MECHANISM changed with #1236 and this test changed with it: the level
        used to be cleared by a generation reset on the later pick's
        ``tranche_plan`` line, which is why a ``--no-tp`` pick — journaling no
        such line — inherited it anyway. Now the marker carries the identity of
        the plan it was trailed under, and the fold keeps it only while that plan
        still governs the uic. The promise is unchanged and strictly wider: it
        now holds for the trail-only shape too, which is the one this file is
        about."""
        earlier = [
            _planned_line(
                entry_crid="AMBA-2026-08-27-entry-t0",
                uic=_UIC,
                side="SELL",
                stop_price=_PLAN_STOP,
                take_profit=None,
                tier_index=0,
                pick_key="AMBA:2026-08-27",
            ),
            {
                "kind": "trailed",
                "uic": _UIC,
                "ts": 110.0,
                "level": _PLAN_STOP + 6.5,
                "pick_key": "AMBA:2026-08-27",
            },
        ]
        # Positive control: while its own pick still governs, the level survives.
        self.assertEqual(_fold_trailed_since_latest_plan(earlier), {_UIC: _PLAN_STOP + 6.5})
        # The later manual pick supersedes the earlier plan on this uic. `--no-tp`
        # on purpose: no tranche_plan line anywhere, which is exactly the case the
        # old generation reset could not see.
        retraction = {
            "kind": "planned_retracted",
            "client_request_id": "AMBA-2026-08-27-entry-t0",
            "uic": _UIC,
            "note": "superseded",
        }
        manual_plan = _planned_line(
            entry_crid="AMBA-2026-09-04-entry-t0",
            uic=_UIC,
            side="SELL",
            stop_price=_PLAN_STOP,
            take_profit=None,
            tier_index=0,
            pick_key="AMBA:2026-09-04",
        )
        self.assertEqual(_fold_trailed_since_latest_plan([*earlier, retraction, manual_plan]), {})

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
