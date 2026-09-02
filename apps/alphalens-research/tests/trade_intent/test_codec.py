"""Unit tests for ``trade_intent/codec.py`` — the JSON round-trip for
:class:`~broker_contract.trade_intent.schema.TradeIntent` (PR-7, memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
section 5). Pure stdlib json/dataclasses; no I/O.
"""

from __future__ import annotations

import unittest

from broker_contract.trade_intent.codec import (
    TradeIntentDecodeError,
    intent_from_jsonable,
    intent_to_jsonable,
)
from broker_contract.trade_intent.schema import (
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


def _spec() -> TradeSpec:
    return TradeSpec(
        entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=50.0, tag="T1"),),
        disaster_stop=90.0,
        tp_tranches=(TpTrancheSpec(price=110.0, tranche_pct=100.0, r_multiple=2.0, tag="TP1"),),
        suggested_size_pct=2.0,
    )


def _meta() -> IntentMeta:
    return IntentMeta(armed_ts="2026-07-31T12:00:00+00:00", brief_date="2026-07-31")


def _intent_with_reanchor() -> TradeIntent:
    exit_spec = ExitGeometrySpec(
        initial_levels=InitialLevels(stop=90.0, tp=110.0),
        reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0, ceiling_price=120.0),),
    )
    return TradeIntent(
        intent_id="abc123",
        instrument=InstrumentHint(ticker="NVDA", mic="XNAS"),
        spec=_spec(),
        meta=_meta(),
        exit=exit_spec,
    )


class TestRoundTrip(unittest.TestCase):
    def test_full_round_trip_with_reanchor_on_fill(self) -> None:
        intent = _intent_with_reanchor()
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        self.assertEqual(restored, intent)

    def test_round_trip_exit_none(self) -> None:
        intent = TradeIntent(
            intent_id="no-exit",
            instrument=InstrumentHint(ticker="KO", mic="XNYS"),
            spec=_spec(),
            meta=_meta(),
            exit=None,
        )
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        self.assertEqual(restored, intent)
        self.assertIsNone(restored.exit)

    def test_round_trip_empty_reaction_plan(self) -> None:
        exit_spec = ExitGeometrySpec(initial_levels=InitialLevels(stop=90.0, tp=110.0))
        intent = TradeIntent(
            intent_id="empty-plan",
            instrument=InstrumentHint(ticker="KO", mic="XNYS"),
            spec=_spec(),
            meta=_meta(),
            exit=exit_spec,
        )
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        self.assertEqual(restored, intent)
        self.assertEqual(restored.exit.reaction_plan, ())

    def test_trailing_stop_round_trips_and_preserves_fields(self) -> None:
        exit_spec = ExitGeometrySpec(
            initial_levels=InitialLevels(stop=90.0, tp=110.0),
            reaction_plan=(TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),),
        )
        intent = TradeIntent(
            intent_id="trail",
            instrument=InstrumentHint(ticker="KO", mic="XNYS"),
            spec=_spec(),
            meta=_meta(),
            exit=exit_spec,
        )
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        self.assertEqual(restored, intent)
        primitive = restored.exit.reaction_plan[0]
        self.assertIsInstance(primitive, TrailingStop)
        self.assertEqual(primitive.arm_trigger_r, 0.5)
        self.assertEqual(primitive.trail_frac, 0.6)

    def test_model_push_round_trips_and_preserves_kind(self) -> None:
        exit_spec = ExitGeometrySpec(
            initial_levels=InitialLevels(stop=90.0, tp=110.0),
            reaction_plan=(ModelPush(),),
        )
        intent = TradeIntent(
            intent_id="model",
            instrument=InstrumentHint(ticker="KO", mic="XNYS"),
            spec=_spec(),
            meta=_meta(),
            exit=exit_spec,
        )
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        self.assertEqual(restored, intent)
        self.assertIsInstance(restored.exit.reaction_plan[0], ModelPush)

    def test_reanchor_on_fill_round_trips_and_preserves_fields(self) -> None:
        intent = _intent_with_reanchor()
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        primitive = restored.exit.reaction_plan[0]
        self.assertIsInstance(primitive, ReanchorOnFill)
        self.assertEqual(primitive.k_atr, 1.5)
        self.assertEqual(primitive.atr, 2.0)
        self.assertEqual(primitive.ceiling_price, 120.0)


class TestMetaSource(unittest.TestCase):
    """The provenance marker separating manual picks from brief picks (#1235)."""

    def test_meta_source_manual_round_trips(self) -> None:
        intent = TradeIntent(
            intent_id="NVO:2026-09-02:manual",
            instrument=InstrumentHint(ticker="NVO", mic="XNYS"),
            spec=_spec(),
            meta=IntentMeta(
                armed_ts="2026-09-02T12:00:00+00:00",
                brief_date="2026-09-02",
                source="manual",
            ),
            exit=None,
        )
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        self.assertEqual(restored, intent)
        self.assertEqual(restored.meta.source, "manual")

    def test_legacy_payload_without_source_decodes_to_brief(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        del data["meta"]["source"]
        restored = intent_from_jsonable(data)
        self.assertEqual(restored.meta.source, "brief")


class TestDecodeErrors(unittest.TestCase):
    def test_unknown_reaction_kind_raises(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        data["exit"]["reaction_plan"][0]["kind"] = "some_future_primitive"
        with self.assertRaises(TradeIntentDecodeError):
            intent_from_jsonable(data)

    def test_missing_required_key_raises(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        del data["intent_id"]
        with self.assertRaises(TradeIntentDecodeError):
            intent_from_jsonable(data)

    def test_non_mapping_raises(self) -> None:
        with self.assertRaises(TradeIntentDecodeError):
            intent_from_jsonable(["not", "a", "mapping"])  # type: ignore[arg-type]


class TestUnknownKeyObservability(unittest.TestCase):
    """zen review (PR-7): an unknown key is dropped (forward-compat) but WARNED so
    a typo / schema drift whose value silently vanishes surfaces early. The
    decoded intent stays byte-identical to the same payload without the extra key."""

    def test_unknown_nested_key_is_dropped_and_warned(self) -> None:
        intent = _intent_with_reanchor()
        data = intent_to_jsonable(intent)
        data["spec"]["limit_pirce"] = 999  # typo of a would-be field, on the spec leaf
        with self.assertLogs("broker_contract.trade_intent.codec", level="WARNING") as cm:
            restored = intent_from_jsonable(data)
        self.assertEqual(restored, intent)  # value dropped -> decoded identically
        self.assertTrue(any("limit_pirce" in line for line in cm.output))

    def test_clean_payload_emits_no_warning(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        with self.assertNoLogs("broker_contract.trade_intent.codec", level="WARNING"):
            intent_from_jsonable(data)


if __name__ == "__main__":
    unittest.main()
