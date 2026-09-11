"""Unit tests for ``trade_intent/codec.py`` — the JSON round-trip for
:class:`~broker_contract.trade_intent.schema.TradeIntent` (PR-7, memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
section 5). Pure stdlib json/dataclasses; no I/O.
"""

from __future__ import annotations

import json
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
    return IntentMeta(armed_ts="2026-07-31T12:00:00+00:00", trade_date="2026-07-31")


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
                trade_date="2026-09-02",
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


class TestMetaGeneration(unittest.TestCase):
    """#1371: the same-day re-arm counter. Generation 1 is the implicit
    generation of every journal line written before the field existed."""

    def _intent(self, **meta_kwargs) -> TradeIntent:
        return TradeIntent(
            intent_id="ENPH:2026-09-08:manual-g2",
            instrument=InstrumentHint(ticker="ENPH", mic="XNAS"),
            spec=_spec(),
            meta=IntentMeta(
                armed_ts="2026-09-08T14:00:00+00:00",
                trade_date="2026-09-08",
                source="manual",
                **meta_kwargs,
            ),
            exit=None,
        )

    def test_generation_round_trips(self) -> None:
        intent = self._intent(generation=2)
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        self.assertEqual(restored, intent)
        self.assertEqual(restored.meta.generation, 2)

    def test_legacy_payload_without_generation_decodes_to_one(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        del data["meta"]["generation"]
        restored = intent_from_jsonable(data)
        self.assertEqual(restored.meta.generation, 1)

    def test_non_int_or_non_positive_generation_is_a_decode_error(self) -> None:
        # _filtered() checks key names, not value types; a hand-edited journal
        # line must be SKIPPED by the drain (decode error) rather than reach the
        # identity helpers with a str / bool / zero and fail deep inside a tick.
        for bad in ("2", 0, -1, True, 2.0, None):
            with self.subTest(generation=bad):
                data = intent_to_jsonable(self._intent(generation=2))
                data["meta"]["generation"] = bad
                with self.assertRaises(TradeIntentDecodeError):
                    intent_from_jsonable(data)


class TestMetaTradeDateLegacyDecode(unittest.TestCase):
    """The journal date key rename (#1252): the field is ``IntentMeta.trade_date``,
    but data on disk written before the rename carries the old ``brief_date``
    key under ``meta``. Legacy lines decode silently (no unknown-key warning)."""

    def test_legacy_brief_date_key_decodes_silently(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        data["meta"]["brief_date"] = data["meta"].pop("trade_date")
        with self.assertNoLogs("broker_contract.trade_intent.codec", level="WARNING"):
            restored = intent_from_jsonable(data)
        self.assertEqual(restored.meta.trade_date, "2026-07-31")

    def test_round_trip_encodes_trade_date_not_brief_date(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        self.assertIn("trade_date", data["meta"])
        self.assertNotIn("brief_date", data["meta"])

    def test_trade_date_wins_when_both_keys_present(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        data["meta"]["brief_date"] = "1999-01-01"
        restored = intent_from_jsonable(data)
        self.assertEqual(restored.meta.trade_date, "2026-07-31")


class TestEntryTierMode(unittest.TestCase):
    """The immediate-entry tier marker for arm-manual "now" tranches (#1247)."""

    def test_entry_mode_round_trips(self) -> None:
        intent = TradeIntent(
            intent_id="RHI:2026-09-03:manual",
            instrument=InstrumentHint(ticker="RHI", mic="XNYS"),
            spec=TradeSpec(
                entry_tiers=(
                    EntryTierSpec(
                        limit_price=43.0, alloc_pct=40.0, tag="T1", entry_mode="immediate"
                    ),
                    EntryTierSpec(limit_price=41.0, alloc_pct=60.0, tag="T2"),
                ),
                disaster_stop=39.0,
                tp_tranches=(),
                suggested_size_pct=2.0,
            ),
            meta=_meta(),
            exit=None,
        )
        restored = intent_from_jsonable(intent_to_jsonable(intent))
        self.assertEqual(restored, intent)
        self.assertEqual(restored.spec.entry_tiers[0].entry_mode, "immediate")
        self.assertEqual(restored.spec.entry_tiers[1].entry_mode, "pullback")

    def test_legacy_tier_without_entry_mode_decodes_to_pullback(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        del data["spec"]["entry_tiers"][0]["entry_mode"]
        restored = intent_from_jsonable(data)
        self.assertEqual(restored.spec.entry_tiers[0].entry_mode, "pullback")


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


class TestGeometryLessExitSpec(unittest.TestCase):
    """#1236: a document may declare how its stop is managed WITHOUT supplying
    levels to place.

    ``initial_levels`` used to be required, which made "declares ``TrailingStop``,
    supplies no geometry" unrepresentable — and that is exactly the shape a pick
    whose exit is managed by the trail, not by a client-computed bracket, needs.
    The field becoming optional is what lets the reaction plan carry the
    declaration on its own."""

    def _trailing_only(self) -> TradeIntent:
        return TradeIntent(
            intent_id="abc123",
            instrument=InstrumentHint(ticker="NVDA", mic="XNAS"),
            spec=_spec(),
            meta=_meta(),
            exit=ExitGeometrySpec(reaction_plan=(TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),)),
        )

    def test_an_exit_spec_can_carry_a_reaction_plan_and_no_levels(self) -> None:
        self.assertIsNone(self._trailing_only().exit.initial_levels)

    def test_it_round_trips(self) -> None:
        # Compared on the SERIALIZED form: `intent_to_jsonable` yields tuples and
        # `json.loads` yields lists, so comparing the dicts would differ for a
        # reason that has nothing to do with the contract.
        intent = self._trailing_only()
        restored = intent_from_jsonable(json.loads(json.dumps(intent_to_jsonable(intent))))
        self.assertEqual(
            json.dumps(intent_to_jsonable(restored), sort_keys=True),
            json.dumps(intent_to_jsonable(intent), sort_keys=True),
        )
        primitive = restored.exit.reaction_plan[0]
        assert isinstance(primitive, TrailingStop)
        self.assertEqual((primitive.arm_trigger_r, primitive.trail_frac), (0.5, 0.6))

    def test_a_payload_omitting_the_key_entirely_decodes(self) -> None:
        """What an external producer actually sends: no ``initial_levels`` key at
        all, rather than an explicit null."""
        data = intent_to_jsonable(self._trailing_only())
        data["exit"].pop("initial_levels", None)
        self.assertIsNone(intent_from_jsonable(data).exit.initial_levels)

    def test_levels_still_decode_when_present(self) -> None:
        """Positive control: making the key optional must not stop it being read."""
        restored = intent_from_jsonable(intent_to_jsonable(_intent_with_reanchor()))
        assert restored.exit is not None and restored.exit.initial_levels is not None
        self.assertEqual(restored.exit.initial_levels.stop, 90.0)
