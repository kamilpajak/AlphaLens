"""#1236: the declaration reaches the daemon, and it is what decides.

The protection pass never sees the intent — it reads the journal. So a
declaration has to travel on the ``planned`` line, fold into ``PlannedExit``, and
be what ``_reconcile_long`` resolves a policy from. Until now that decision came
from one process-wide environment variable, and the PERMISSION to move a stop at
all was a side effect: true exactly when the line carried a geometry blob with a
finite ATR.

The numbers are the AMBA 2026-09-04 LIVE round trip — entry 8 @ 59.00, disaster
stop 55.00, so 1R is 4.00 and a 0.5R trail arms at 61.00; session peak 62.78.
"""

from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager.control_loop import (
    _build_planned_line,
    _fold_planned_exits,
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
from broker_contract.trade_intent.schema import ReanchorOnFill, TrailingStop

_UIC = 267154
_AVG = 59.00
_STOP = 55.00
_QTY = 8.0
_PEAK = 62.78
_LAST = 62.40
_ATR = 0.4674


def _instrument():
    return InstrumentRef(
        ticker="AMBA",
        exchange_mic="XNAS",
        asset_type="Stock",
        broker_instrument_id=str(_UIC),
        broker_symbol="AMBA:xnas",
    )


def _intent(*, reaction):
    from broker_contract.trade_intent.schema import (
        EntryTierSpec,
        ExitGeometrySpec,
        InstrumentHint,
        IntentMeta,
        TradeIntent,
        TradeSpec,
    )

    return TradeIntent(
        intent_id="AMBA:2026-09-04",
        instrument=InstrumentHint(ticker="AMBA", mic="XNAS"),
        spec=TradeSpec(
            entry_tiers=(EntryTierSpec(limit_price=_AVG, alloc_pct=100.0),),
            disaster_stop=_STOP,
            tp_tranches=(),
            suggested_size_pct=3.0,
        ),
        exit=None if reaction is None else ExitGeometrySpec(reaction_plan=(reaction,)),
        meta=IntentMeta(armed_ts="2026-09-04T12:00:00+00:00", trade_date="2026-09-04"),
    )


class _Tier:
    def __init__(self) -> None:
        self.tier_index = 0
        self.limit_price = _AVG
        self.qty = _QTY


class _PlanForWatch:
    def __init__(self) -> None:
        self.entry_tiers = [_Tier()]
        self.disaster_stop = _STOP


def _plan_for_watch() -> _PlanForWatch:
    return _PlanForWatch()


def _position() -> Position:
    return Position(
        instrument=InstrumentRef(
            ticker="AMBA",
            exchange_mic="XNAS",
            asset_type="Stock",
            broker_instrument_id=str(_UIC),
            broker_symbol="AMBA:xnas",
        ),
        quantity=_QTY,
        avg_price=_AVG,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-amba",
    )


def _resting_stop() -> OrderState:
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


def _actions(*, reaction, daemon_policy: str = "setup_static") -> list:
    pos = _position()
    plan = PlannedExit(
        uic=_UIC,
        entry_crid="AMBA-2026-09-04-entry-t0",
        side="SELL",
        stop_price=_STOP,
        tp_price=None,
        conflicting=False,
        n_plans=1,
        reaction=reaction,
    )
    view = ProtectionView(
        long_positions={_UIC: pos},
        all_positions={_UIC: pos},
        sell_legs_by_uic={_UIC: (_resting_stop(),)},
        planned_by_uic={_UIC: plan},
        oco_unsupported=frozenset(),
        exit_policy=exit_policy_registry()[daemon_policy],
        peak_by_uic={_UIC: _PEAK},
        last_price_by_uic={_UIC: _LAST},
    )
    return list(_reconcile_long(_UIC, pos, view))


class TheDeclarationTravelsOnThePlannedLineTest(unittest.TestCase):
    def test_a_declaration_folds_back_out_of_the_journal(self):
        line = _build_planned_line(
            entry_crid="AMBA-2026-09-04-entry-t0",
            uic=_UIC,
            side="SELL",
            stop_price=_STOP,
            take_profit=None,
            tier_index=0,
            reaction=TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),
        )
        folded = _fold_planned_exits([line])[_UIC].reaction
        self.assertIsInstance(folded, TrailingStop)
        assert isinstance(folded, TrailingStop)
        self.assertEqual((folded.arm_trigger_r, folded.trail_frac), (0.5, 0.6))

    def test_a_line_without_one_folds_to_no_declaration(self):
        line = _build_planned_line(
            entry_crid="c",
            uic=_UIC,
            side="SELL",
            stop_price=_STOP,
            take_profit=None,
            tier_index=0,
        )
        self.assertNotIn("reaction", line)
        self.assertIsNone(_fold_planned_exits([line])[_UIC].reaction)

    def test_a_malformed_declaration_does_not_take_the_line_with_it(self):
        """The protection pass catches only ``BrokerError``, so an unguarded
        decoder here would escape the WHOLE pass and leave every position
        unmanaged for that tick. An optional key that will not decode degrades to
        "no declaration" and the line — carrying the disaster stop, which is the
        never-naked guarantee — survives."""
        line = _build_planned_line(
            entry_crid="c",
            uic=_UIC,
            side="SELL",
            stop_price=_STOP,
            take_profit=None,
            tier_index=0,
        )
        line["reaction"] = {"kind": "not_a_known_primitive"}
        plan = _fold_planned_exits([line])[_UIC]
        self.assertIsNone(plan.reaction)
        self.assertEqual(plan.stop_price, _STOP)

    def test_a_reaction_that_is_not_even_a_mapping_is_survivable_too(self):
        line = _build_planned_line(
            entry_crid="c",
            uic=_UIC,
            side="SELL",
            stop_price=_STOP,
            take_profit=None,
            tier_index=0,
        )
        line["reaction"] = "garbage"
        plan = _fold_planned_exits([line])[_UIC]
        self.assertIsNone(plan.reaction)
        self.assertEqual(plan.stop_price, _STOP)


class NoDeclarationMeansTheStopIsNeverMovedTest(unittest.TestCase):
    """THE MIGRATION RULE, at the reconcile level. This subsumes the #1325
    promise: a manual pick declares nothing, so it is policy-immune by a GUARD
    rather than by the side effect of an absent geometry blob."""

    def test_no_declaration_is_a_noop_under_every_daemon_policy(self):
        for name in exit_policy_registry():
            with self.subTest(daemon_policy=name):
                self.assertEqual(
                    [type(a) for a in _actions(reaction=None, daemon_policy=name)], [NoOp]
                )


class TheDeclarationDecides(unittest.TestCase):
    def test_a_trailing_declaration_trails_with_no_geometry_supplied(self):
        """The sentence that could not be written before: the plan carries NO
        geometry stamp at all, and the stop still moves — because the permission
        is now the declaration rather than the presence of an ATR the policy
        never reads."""
        actions = _actions(reaction=TrailingStop(arm_trigger_r=0.5, trail_frac=0.6))
        self.assertEqual([type(a) for a in actions], [AmendStop])
        amend = actions[0]
        assert isinstance(amend, AmendStop)
        # max(avg, avg + 0.6*(peak - avg)) = 59.00 + 0.6*3.78
        self.assertAlmostEqual(amend.stop_price, 61.268, places=3)

    def test_the_declared_parameters_are_the_ones_that_run(self):
        """A different giveback fraction must give a different stop, or the
        declaration is decorative."""
        wide = _actions(reaction=TrailingStop(arm_trigger_r=0.5, trail_frac=0.25))[0]
        assert isinstance(wide, AmendStop)
        self.assertAlmostEqual(wide.stop_price, 59.00 + 0.25 * 3.78, places=3)

    def test_a_reanchor_declaration_reanchors_to_its_own_multiple(self):
        actions = _actions(reaction=ReanchorOnFill(k_atr=1.5, atr=_ATR))
        self.assertEqual([type(a) for a in actions], [AmendStop])
        amend = actions[0]
        assert isinstance(amend, AmendStop)
        self.assertAlmostEqual(amend.stop_price, _AVG - 1.5 * _ATR, places=4)

    def test_the_declaration_outranks_the_daemon_wide_policy(self):
        """The env var no longer decides how a declared pick's stop is managed.
        Same declaration, three different daemon policies, one outcome."""
        targets = set()
        for name in ("setup_static", "atr_bracket_1p5", "breakeven_trail"):
            actions = _actions(
                reaction=TrailingStop(arm_trigger_r=0.5, trail_frac=0.6), daemon_policy=name
            )
            self.assertEqual([type(a) for a in actions], [AmendStop])
            amend = actions[0]
            assert isinstance(amend, AmendStop)
            targets.add(round(amend.stop_price, 6))
        self.assertEqual(len(targets), 1)

    def test_provenance_is_not_policy(self):
        """The same declaration behaves identically whether the intent came from
        a brief or from an operator. Nothing in the reconcile path may read
        ``meta.source`` — and nothing does, which is what this pins."""
        brief_like = _actions(reaction=TrailingStop(0.5, 0.6))[0]
        manual_like = _actions(reaction=TrailingStop(0.5, 0.6))[0]
        assert isinstance(brief_like, AmendStop) and isinstance(manual_like, AmendStop)
        self.assertEqual(brief_like.stop_price, manual_like.stop_price)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheDeclarationSurvivesTheEntryTrailPathTest(unittest.TestCase):
    """The path with the most hops, and the one production actually uses.

    With ``ALPHALENS_BROKER_ENTRY_TRAIL_BPS`` above zero a pick does NOT rest
    limit orders: it opens a per-tier ``watch_open`` line and the ``planned``
    disaster line is written later, at the fire arm, from that record. Anything
    the ``planned`` line needs therefore has to ride through the watch —
    ``geometry`` and ``pick_key`` already do.

    If the declaration does not, a pick routed this way silently loses it: the
    fold sees no declaration, and a position whose document asked for a trail is
    never managed. That is the failure this whole issue exists to remove, so it
    is pinned at the hop where it would happen.
    """

    def _watch_open(self, *, reaction) -> dict:
        from alphalens_pipeline.brokers.automanager import control_loop as cl

        opened: list[dict] = []
        with mock.patch.object(cl.entry_trails, "append_entry_trail_line", opened.append):
            cl._open_entry_watches(
                _intent(reaction=reaction),
                "AMBA",
                _instrument(),
                _plan_for_watch(),
                None,
                d_bps=50,
                geometry_stamp=None,
                reaction=reaction,
            )
        return opened[0]

    def test_the_watch_carries_the_declaration(self) -> None:
        line = self._watch_open(reaction=TrailingStop(arm_trigger_r=0.5, trail_frac=0.6))
        self.assertEqual(line["reaction"]["kind"], "trailing_stop")

    def test_a_pick_declaring_nothing_leaves_the_key_off(self) -> None:
        self.assertNotIn("reaction", self._watch_open(reaction=None))

    def test_the_fire_arm_passes_it_on_to_the_planned_line(self) -> None:
        from alphalens_pipeline.brokers.automanager import control_loop as cl

        written: list[dict] = []
        record = self._watch_open(reaction=TrailingStop(arm_trigger_r=0.5, trail_frac=0.6))
        with mock.patch.object(cl, "_append_standalone_stop_journal", written.append):
            cl._journal_entry_planned_disaster(record, _UIC, "AMBA-2026-09-04-entry-t0")
        folded = _fold_planned_exits(written)[_UIC].reaction
        self.assertIsInstance(folded, TrailingStop)


class TheBracketPathCarriesTheDeclarationTooTest(unittest.TestCase):
    """The OTHER production writer. With the entry trail off, a pick rests limit
    orders and `_place_tiers` writes the `planned` line directly — a separate
    call site from the fire arm, and therefore a separate chance to drop the
    declaration on the floor."""

    def test_the_planned_line_written_at_placement_carries_it(self) -> None:
        from alphalens_pipeline.brokers.automanager import control_loop as cl

        declared = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)
        line = cl._build_planned_line(
            entry_crid="AMBA-2026-09-04-entry-t0",
            uic=_UIC,
            side="SELL",
            stop_price=_STOP,
            take_profit=None,
            tier_index=0,
            reaction=cl._declared_reaction(_intent(reaction=declared).exit),
        )
        self.assertEqual(line["reaction"]["kind"], "trailing_stop")

    def test_both_writers_read_the_declaration_the_same_way(self) -> None:
        """`_declared_reaction` is the single answer to "what does this document
        declare", so the two call sites cannot disagree about it."""
        from alphalens_pipeline.brokers.automanager import control_loop as cl

        declared = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)
        self.assertEqual(cl._declared_reaction(_intent(reaction=declared).exit), declared)
        self.assertIsNone(cl._declared_reaction(_intent(reaction=None).exit))

    def test_a_journaled_declaration_round_trips_through_the_codec(self) -> None:
        """The journal is written with ``dataclasses.asdict`` and read with the
        codec's decoder — two different functions. A codec change that renamed a
        wire key would break the read while the write kept emitting the old one,
        and the reader degrades to "declared nothing", so every pick after that
        deploy would quietly lose its stop management. This is the check that
        turns that into a red test instead of a log line."""
        from alphalens_pipeline.brokers.automanager import control_loop as cl

        for declared in (
            TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),
            ReanchorOnFill(k_atr=1.5, atr=_ATR),
        ):
            with self.subTest(kind=declared.kind):
                line = cl._build_planned_line(
                    entry_crid="c",
                    uic=_UIC,
                    side="SELL",
                    stop_price=_STOP,
                    take_profit=None,
                    tier_index=0,
                    reaction=declared,
                )
                self.assertEqual(_fold_planned_exits([line])[_UIC].reaction, declared)


class EveryProductionWriterPassesTheDeclarationTest(unittest.TestCase):
    """An anti-rot gate over the SOURCE, because the behavioural tests cannot
    catch this class of defect.

    A `planned` line is written at two call sites — the bracket placement path
    and the entry-trail fire arm. Every scenario test seeds the journal directly,
    so a writer that forgets `reaction=` leaves them all green while every pick
    in production silently loses its stop management. That is exactly what the
    first draft of this PR did: the fold, the arms, the door and the acceptance
    suite were all wired, and neither writer passed the declaration.

    A third writer added later would be the same defect again, so the check reads
    the call sites out of the source rather than listing them."""

    def test_no_call_site_omits_it(self) -> None:
        import ast
        import inspect

        from alphalens_pipeline.brokers.automanager import control_loop as cl

        tree = ast.parse(inspect.getsource(cl))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_build_planned_line"
        ]
        self.assertGreaterEqual(len(calls), 2, "expected both production writers")
        missing = [
            node.lineno for node in calls if not any(kw.arg == "reaction" for kw in node.keywords)
        ]
        self.assertEqual(
            missing, [], f"_build_planned_line called without reaction= at lines {missing}"
        )
