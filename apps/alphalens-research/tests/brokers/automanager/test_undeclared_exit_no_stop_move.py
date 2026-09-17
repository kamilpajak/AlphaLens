"""A document with ``exit: null`` never has its stop MOVED by the daemon, under any
exit policy in the registry and whatever its ``meta.source`` says (#1325, #1236,
re-stated in #1470 when `broker arm-manual` was removed).

Every document here is armed through the real `broker arm` door and read back
from the journal fold, and the reaction handed to the position manager is the one
the daemon itself derives (``control_loop._declared_reaction``). A hand-built
``reaction=None`` would prove only that None moves nothing.

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
decision used to rest on a SIDE EFFECT: ``arm-manual`` built its intent with
``exit=None``, so no geometry stamp reached the journal, so ``PlannedExit``
carried no ATR, and both post-fill stop-move arms refused on exactly that. The
property was real but incidental, and the note here used to warn against "fixing"
the ATR guard in isolation, because relaxing it would have turned trailing ON for
exactly the picks the decision excludes.

It is now a GUARD. A pick's stop is managed if and only if its document DECLARES
management, and a document that states ``exit: null`` declares nothing. The ATR
guard could therefore be moved into the policies that need an ATR without touching this promise — which is
what #1236 did.

Provenance of the numbers: the AMBA LIVE round trip of 2026-09-04 (entry 8 @
59.00, disaster stop 55.00, so 1R = 4.00 and the 0.5R activation sits at 61.00;
session peak 62.78). Its stored ``planned`` line in
``~/.alphalens/broker_orders/live/standalone_stops.jsonl`` carries no
``geometry`` key.
"""

from __future__ import annotations

import json
import unittest

from alphalens_pipeline.brokers.automanager.control_loop import (
    _build_managed_exits,
    _declared_reaction,
    _fold_trailed_since_latest_plan,
    _placed_geometry_stamp,
)
from alphalens_pipeline.brokers.automanager.control_loop import (
    _build_planned_line as _planned_line,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    AmendStop,
    NoOp,
    PlannedExit,
    ProtectionView,
    _reconcile_long,
)
from broker_contract.contract import InstrumentRef, OrderState, OrderStatus, Position
from broker_contract.exit_geometry.registry import exit_policy_registry
from broker_contract.trade_intent.codec import intent_from_jsonable
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
        peak_by_uic={_UIC: _PEAK},
        last_price_by_uic={_UIC: _LAST_PRICE},
        trailed_stop_by_uic=trailed_stop_by_uic or {},
    )
    return list(_reconcile_long(_UIC, pos, view))


_SOURCES = ("manual", "brief")
_DECLARED_TRAIL = {
    "initial_levels": None,
    "reaction_plan": [{"kind": "trailing_stop", "arm_trigger_r": 0.5, "trail_frac": 0.6}],
}


def _document(*, source: str, exit_spec: dict | None = None, tps: list | None = None) -> dict:
    """The AMBA pick as an author writes it. ``trade_date`` is stated because a
    brief document requires it."""
    return {
        "instrument": {"ticker": "AMBA", "mic": "XNAS"},
        "spec": {
            "entry_tiers": [{"limit_price": _AVG_PRICE, "alloc_pct": 100.0}],
            "disaster_stop": _PLAN_STOP,
            "tp_tranches": [{"price": 62.5, "tranche_pct": 100.0}] if tps is None else tps,
            "size": {"notional_acct": 500.0, "currency": "USD"},
        },
        "meta": {"source": source, "trade_date": "2026-09-04"},
        "exit": exit_spec,
    }


class _ArmsThroughTheDoor(unittest.TestCase):
    """Arms each document into its own empty home and returns the journaled intent."""

    def armed(self, document: dict):
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager.picks import read_pick_fold
        from typer.testing import CliRunner

        from tests.brokers.automanager.cli_isolation import _isolate_home

        home = _isolate_home(self)
        result = CliRunner().invoke(broker_app, ["arm", "-"], input=json.dumps(document))
        self.assertEqual(result.exit_code, 0, result.output)
        inbox = home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        (record,) = read_pick_fold(path=inbox).records
        return intent_from_jsonable(record.record["intent"])


class TestAnUndeclaredDocumentCarriesNoStamp(_ArmsThroughTheDoor):
    """The joint the decision hangs on: ``exit: null`` through the door ->
    no ``exit`` on the journaled intent -> no ``geometry`` stamp and no declared
    reaction. Since #1414 that holds by the shape of the document alone."""

    def test_the_journaled_intent_has_no_exit(self) -> None:
        for source in _SOURCES:
            with self.subTest(source=source):
                self.assertIsNone(self.armed(_document(source=source)).exit)

    def test_no_geometry_stamp_and_no_reaction(self) -> None:
        for source in _SOURCES:
            with self.subTest(source=source):
                armed = self.armed(_document(source=source))
                self.assertIsNone(_placed_geometry_stamp(armed.exit))
                self.assertIsNone(_declared_reaction(armed.exit))

    def test_positive_control_an_exit_spec_does_produce_a_stamp(self) -> None:
        """Guards the check above against rotting to vacuous: the same call on a
        document that DOES carry levels stamps them as placed."""
        exit_spec = ExitGeometrySpec(
            initial_levels=InitialLevels(stop=_PLAN_STOP, tp=62.5),
            reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=_ATR),),
        )
        stamp = _placed_geometry_stamp(exit_spec)
        assert stamp is not None
        self.assertEqual((stamp["geometry_tp"], stamp["applied"]), (62.5, True))


class TestAnUndeclaredDocumentIsPolicyImmune(_ArmsThroughTheDoor):
    """The end-to-end pin: with a peak far above every activation threshold, an
    armed ``exit: null`` document yields no stop move under ANY registry policy."""

    def test_no_stop_move_under_any_policy(self) -> None:
        for source in _SOURCES:
            reaction = _declared_reaction(self.armed(_document(source=source)).exit)
            for name in exit_policy_registry():
                with self.subTest(source=source, policy=name):
                    actions = _actions_for(name, reaction=reaction)
                    self.assertEqual([type(a) for a in actions], [NoOp])

    def test_positive_control_a_declared_trail_through_the_same_path_moves_it(self) -> None:
        """Same door, same fold, same derivation: only the declaration differs.
        Without this the test above would also pass if nothing were ever armed
        with a reaction at all."""
        for source in _SOURCES:
            armed = self.armed(_document(source=source, exit_spec=_DECLARED_TRAIL))
            reaction = _declared_reaction(armed.exit)
            for name in exit_policy_registry():
                with self.subTest(source=source, policy=name):
                    actions = _actions_for(name, reaction=reaction)
                    self.assertEqual([type(a) for a in actions], [AmendStop])

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


class TestInheritedTrailedLevelCannotMoveAnUndeclaredPick(_ArmsThroughTheDoor):
    """The second route to a moved stop, which does NOT go through the guard
    above: ``trailed_stop_by_uic`` is a journal-lifetime fold, so a level earned
    by an EARLIER position on the same uic can outlive it (SIM really carries
    GME twice — once from a brief, once manual). Two consumers, both checked.
    """

    def test_the_ratchet_floor_alone_proposes_nothing(self) -> None:
        """``_maybe_trail`` only GATES a proposal against the floor; it never
        proposes the floor itself, so an inherited level cannot become an
        amend on an undeclared pick."""
        reaction = _declared_reaction(self.armed(_document(source="manual")).exit)
        for name in exit_policy_registry():
            with self.subTest(policy=name):
                actions = _actions_for(
                    name, reaction=reaction, trailed_stop_by_uic={_UIC: _PLAN_STOP + 6.5}
                )
                self.assertEqual([type(a) for a in actions], [NoOp])

    def test_a_later_manual_pick_does_not_inherit_the_level(self) -> None:
        """The other consumer is ``control_loop._build_managed_exits``, which
        takes ``max(plan stop, trailed)`` and PLACES it.

        The MECHANISM changed with #1236 and this test changed with it: the level
        used to be cleared by a generation reset on the later pick's
        ``tranche_plan`` line, which is why a pick with no take-profit — journaling no
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
        # The later manual pick supersedes the earlier plan on this uic. No TP
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
        """The residual case the reset does NOT cover: an empty ``tp_tranches`` journals no
        ``tranche_plan`` at all (``_journal_tranche_plan_core`` returns early on
        an empty ladder), so an inherited level stays in the fold. It still
        cannot be placed — ``_build_managed_exits`` skips a uic with no tranche
        plan. Closed for a DIFFERENT reason than the case above, which is why
        it is pinned separately."""
        # The premise: the door takes a document with no take-profit at all.
        armed = self.armed(_document(source="manual", tps=[]))
        self.assertEqual(armed.spec.tp_tranches, ())
        managed = _build_managed_exits(
            long_positions=[_position()],
            tranche_plans={},  # empty tp_tranches: nothing journaled for this uic
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
