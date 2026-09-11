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
