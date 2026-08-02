"""Unit tests for the Boundary-2 wire schema (pure, unconsumed leaf).

Covers round-trip field access, frozen-dataclass immutability, documented
defaults, and the discriminated-union dispatch over the reaction-plan
primitives (memo revision R3).
"""

from __future__ import annotations

import dataclasses
import unittest

from broker_contract.constants import DEFAULT_ORDER_TTL_DAYS
from broker_contract.trade_intent.schema import (
    DEFAULT_ACCOUNT_ID,
    SCHEMA_VERSION,
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    InstrumentHint,
    IntentMeta,
    ModelPush,
    ReanchorOnFill,
    TpTrancheSpec,
    TradeIntent,
    TradeSpec,
    TrailingStop,
)


def _build_trade_intent() -> TradeIntent:
    spec = TradeSpec(
        entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=50.0, tag="T1"),),
        disaster_stop=90.0,
        tp_tranches=(TpTrancheSpec(price=110.0, tranche_pct=100.0, r_multiple=2.0, tag="TP1"),),
        suggested_size_pct=2.0,
    )
    exit_spec = ExitGeometrySpec(
        initial_levels=InitialLevels(stop=90.0, tp=110.0),
        reaction_plan=(
            ReanchorOnFill(k_atr=1.5, atr=2.0),
            TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),
        ),
    )
    meta = IntentMeta(armed_ts="2026-07-31T12:00:00Z", brief_date="2026-07-31")
    return TradeIntent(
        intent_id="abc123",
        instrument=InstrumentHint(ticker="NVDA", mic="XNAS"),
        spec=spec,
        exit=exit_spec,
        meta=meta,
    )


class TestTradeIntentRoundTrip(unittest.TestCase):
    def test_full_construction_round_trips_all_fields(self):
        intent = _build_trade_intent()

        self.assertEqual(intent.intent_id, "abc123")
        self.assertEqual(intent.instrument.ticker, "NVDA")
        self.assertEqual(intent.instrument.mic, "XNAS")
        self.assertEqual(intent.spec.entry_tiers[0].limit_price, 100.0)
        self.assertEqual(intent.spec.entry_tiers[0].alloc_pct, 50.0)
        self.assertEqual(intent.spec.entry_tiers[0].tag, "T1")
        self.assertEqual(intent.spec.disaster_stop, 90.0)
        self.assertEqual(intent.spec.tp_tranches[0].price, 110.0)
        self.assertEqual(intent.spec.tp_tranches[0].tranche_pct, 100.0)
        self.assertEqual(intent.spec.tp_tranches[0].r_multiple, 2.0)
        self.assertEqual(intent.spec.tp_tranches[0].tag, "TP1")
        self.assertEqual(intent.spec.suggested_size_pct, 2.0)
        self.assertEqual(intent.exit.initial_levels.stop, 90.0)
        self.assertEqual(intent.exit.initial_levels.tp, 110.0)
        self.assertEqual(intent.meta.armed_ts, "2026-07-31T12:00:00Z")
        self.assertEqual(intent.meta.brief_date, "2026-07-31")


class TestFrozenImmutability(unittest.TestCase):
    def test_trade_spec_field_assignment_raises(self):
        spec = TradeSpec(
            entry_tiers=(),
            disaster_stop=90.0,
            tp_tranches=(),
            suggested_size_pct=2.0,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.disaster_stop = 80.0  # type: ignore[misc]

    def test_trade_intent_field_assignment_raises(self):
        intent = _build_trade_intent()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            intent.intent_id = "other"  # type: ignore[misc]


class TestDefaults(unittest.TestCase):
    def test_trade_spec_defaults(self):
        spec = TradeSpec(
            entry_tiers=(),
            disaster_stop=90.0,
            tp_tranches=(),
            suggested_size_pct=2.0,
        )
        self.assertEqual(spec.order_ttl_days, DEFAULT_ORDER_TTL_DAYS)
        self.assertEqual(spec.side, "long")
        self.assertEqual(spec.schema_version, SCHEMA_VERSION)

    def test_exit_geometry_spec_reaction_plan_defaults_empty(self):
        exit_spec = ExitGeometrySpec(initial_levels=InitialLevels(stop=90.0, tp=110.0))
        self.assertEqual(exit_spec.reaction_plan, ())

    def test_trade_intent_account_id_defaults(self):
        intent = _build_trade_intent()
        self.assertEqual(intent.account_id, DEFAULT_ACCOUNT_ID)

    def test_entry_tier_spec_tag_defaults_empty(self):
        tier = EntryTierSpec(limit_price=100.0, alloc_pct=50.0)
        self.assertEqual(tier.tag, "")

    def test_tp_tranche_spec_r_multiple_and_tag_default(self):
        tranche = TpTrancheSpec(price=110.0, tranche_pct=100.0)
        self.assertEqual(tranche.r_multiple, 0.0)
        self.assertEqual(tranche.tag, "")


class TestReactionPrimitiveDiscriminatedUnion(unittest.TestCase):
    def test_each_primitive_carries_its_distinct_kind_tag(self):
        self.assertEqual(ReanchorOnFill(k_atr=1.5, atr=2.0).kind, "reanchor_on_fill")
        self.assertEqual(TrailingStop(arm_trigger_r=0.5, trail_frac=0.6).kind, "trailing_stop")
        self.assertEqual(ModelPush().kind, "model")

    def test_mixed_reaction_plan_keeps_element_types_in_order(self):
        plan = (
            ReanchorOnFill(k_atr=1.5, atr=2.0),
            TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),
            ModelPush(),
        )
        exit_spec = ExitGeometrySpec(
            initial_levels=InitialLevels(stop=90.0, tp=110.0), reaction_plan=plan
        )

        self.assertIsInstance(exit_spec.reaction_plan[0], ReanchorOnFill)
        self.assertIsInstance(exit_spec.reaction_plan[1], TrailingStop)
        self.assertIsInstance(exit_spec.reaction_plan[2], ModelPush)

    def test_isinstance_dispatch_selects_the_right_branch_per_element(self):
        plan = (
            ReanchorOnFill(k_atr=1.5, atr=2.0),
            TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),
            ModelPush(),
        )

        dispatched_kinds = []
        for primitive in plan:
            if isinstance(primitive, ReanchorOnFill):
                dispatched_kinds.append("reanchor_on_fill")
            elif isinstance(primitive, TrailingStop):
                dispatched_kinds.append("trailing_stop")
            elif isinstance(primitive, ModelPush):
                dispatched_kinds.append("model")
            else:
                self.fail(f"unhandled reaction primitive type: {type(primitive)!r}")

        self.assertEqual(dispatched_kinds, ["reanchor_on_fill", "trailing_stop", "model"])


class TestReanchorOnFillAndModelPushConstruction(unittest.TestCase):
    def test_reanchor_on_fill_ceiling_price_defaults_none(self):
        primitive = ReanchorOnFill(k_atr=1.5, atr=2.0)
        self.assertIsNone(primitive.ceiling_price)

    def test_model_push_constructs_with_no_args(self):
        primitive = ModelPush()
        self.assertEqual(primitive.kind, "model")


if __name__ == "__main__":
    unittest.main()
