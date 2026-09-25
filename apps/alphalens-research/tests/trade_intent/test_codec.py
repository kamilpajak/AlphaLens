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
    author_jsonable,
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
    PickSize,
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
        size=PickSize(notional_acct=1500.0, currency="USD"),
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
    """The immediate-entry tier marker for "now" tranches the daemon places at drain (#1247)."""

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
                size=PickSize(notional_acct=1500.0, currency="USD"),
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


class TestPickSizeCodec(unittest.TestCase):
    def test_size_round_trips_as_a_nested_object(self) -> None:
        intent = _intent_with_reanchor()
        data = intent_to_jsonable(intent)

        self.assertEqual(data["spec"]["size"], {"notional_acct": 1500.0, "currency": "USD"})
        self.assertEqual(intent_from_jsonable(data), intent)

    def test_a_v2_document_carrying_a_percent_does_not_decode(self) -> None:
        # #1467: a percent needs a frame the daemon no longer has, so there is no
        # silent mapping. The journal drain recognises these lines before decoding.
        data = intent_to_jsonable(_intent_with_reanchor())
        del data["spec"]["size"]
        data["spec"]["suggested_size_pct"] = 2.0

        with self.assertRaises(TradeIntentDecodeError):
            intent_from_jsonable(data)

    def test_a_size_that_is_not_an_object_is_a_decode_error(self) -> None:
        data = intent_to_jsonable(_intent_with_reanchor())
        data["spec"]["size"] = 1500.0

        with self.assertRaises(TradeIntentDecodeError):
            intent_from_jsonable(data)


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


def _trailing_intent() -> TradeIntent:
    return TradeIntent(
        intent_id="KBH:2026-08-21",
        instrument=InstrumentHint(ticker="KBH", mic="XNYS"),
        spec=TradeSpec(
            entry_tiers=(
                EntryTierSpec(limit_price=60.0, alloc_pct=60.0, tag="swing-low"),
                EntryTierSpec(limit_price=58.0, alloc_pct=40.0, tag="50-day MA"),
            ),
            disaster_stop=54.0,
            tp_tranches=(
                TpTrancheSpec(price=66.0, tranche_pct=50.0, r_multiple=1.2, tag="TP1"),
                TpTrancheSpec(price=70.0, tranche_pct=50.0, r_multiple=2.0, tag="TP2"),
            ),
            size=PickSize(notional_acct=1500.0, currency="EUR"),
        ),
        meta=IntentMeta(armed_ts="2026-09-16T15:00:00+00:00", trade_date="2026-08-21"),
        exit=ExitGeometrySpec(reaction_plan=(TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),)),
    )


def _without(document: dict, paths: list[str]) -> dict:
    """``document`` with each dotted path removed; ``a[1].b`` indexes a list."""
    import copy
    import re

    trimmed = copy.deepcopy(document)
    for path in paths:
        node = trimmed
        parts = path.split(".")
        for part in parts[:-1]:
            match = re.fullmatch(r"(\w+)\[(\d+)\]", part)
            node = node[match.group(1)][int(match.group(2))] if match else node[part]
        del node[parts[-1]]
    return trimmed


class TestAuthorJsonable(unittest.TestCase):
    """What an author sends: the stored rendering minus the fields the door derives (#1469)."""

    def test_it_is_the_stored_rendering_without_the_derived_fields(self) -> None:
        from alphalens_pipeline.brokers.automanager.intent_door import supplied_derived_paths

        intent = _trailing_intent()
        stored = intent_to_jsonable(intent)
        derived = supplied_derived_paths(stored)

        # Positive control: the stored rendering does carry every derived field,
        # including the one nested under a tuple of dataclasses.
        self.assertEqual(
            derived,
            [
                "intent_id",
                "meta.armed_ts",
                "spec.tp_tranches[0].r_multiple",
                "spec.tp_tranches[1].r_multiple",
            ],
        )
        self.assertEqual(author_jsonable(intent), _without(stored, derived))

    def test_it_is_part_of_the_published_codec_api(self) -> None:
        from broker_contract.trade_intent import codec

        self.assertIn("author_jsonable", codec.__all__)

    def test_it_carries_no_derived_field(self) -> None:
        from alphalens_pipeline.brokers.automanager.intent_door import supplied_derived_paths

        self.assertEqual(supplied_derived_paths(author_jsonable(_trailing_intent())), [])

    def test_a_filled_field_is_rendered_as_it_is(self) -> None:
        # The door treats a stated generation as a replace, so a caller that
        # wants a NEW pick drops it; the renderer does not guess that for it.
        self.assertEqual(author_jsonable(_trailing_intent())["meta"]["generation"], 1)

    def test_the_input_schema_accepts_it(self) -> None:
        import jsonschema
        from broker_contract.trade_intent.json_schema import generate_schema

        validator = jsonschema.Draft202012Validator(generate_schema("input"))
        errors = [
            error.message for error in validator.iter_errors(author_jsonable(_trailing_intent()))
        ]
        self.assertEqual(errors, [])

    def test_a_part_of_the_intent_renders_on_its_own(self) -> None:
        spec = _trailing_intent().spec
        rendered = author_jsonable(spec)
        self.assertEqual(
            rendered["tp_tranches"][1], {"price": 70.0, "tranche_pct": 50.0, "tag": "TP2"}
        )
        self.assertIsInstance(rendered["entry_tiers"], list)

    def test_an_absent_exit_level_is_null(self) -> None:
        exit_spec = author_jsonable(_trailing_intent())["exit"]
        self.assertIsNone(exit_spec["initial_levels"])
        self.assertEqual(exit_spec["reaction_plan"][0]["kind"], "trailing_stop")


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
        # Through a real serialise/parse, because that is the trip the document
        # makes; the documents themselves compare directly (#1405).
        intent = self._trailing_only()
        restored = intent_from_jsonable(json.loads(json.dumps(intent_to_jsonable(intent))))
        self.assertEqual(intent_to_jsonable(restored), intent_to_jsonable(intent))
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


class TestTheOutputIsActuallyJsonShaped(unittest.TestCase):
    """``intent_to_jsonable`` promises a jsonable dict, and the promise is used.

    ``dataclasses.asdict`` recurses tuples into tuples, which ``json.dumps``
    renders as arrays but which every OTHER json tool reads as "not an array" —
    a JSON Schema validator among them (#1405). The name is the contract: what
    comes out here must equal what a consumer reads back off the wire, without a
    ``json.loads(json.dumps(...))`` dance in between.
    """

    def test_the_document_equals_its_own_serialised_form(self) -> None:
        document = intent_to_jsonable(_intent_with_reanchor())
        self.assertEqual(document, json.loads(json.dumps(document)))

    def test_every_sequence_is_a_list(self) -> None:
        document = intent_to_jsonable(_intent_with_reanchor())
        self.assertIsInstance(document["spec"]["entry_tiers"], list)
        self.assertIsInstance(document["spec"]["tp_tranches"], list)
        assert document["exit"] is not None
        self.assertIsInstance(document["exit"]["reaction_plan"], list)

    def test_an_empty_reaction_plan_is_an_empty_list(self) -> None:
        intent = TradeIntent(
            intent_id="abc123",
            instrument=InstrumentHint(ticker="NVDA", mic="XNAS"),
            spec=_spec(),
            meta=_meta(),
            exit=ExitGeometrySpec(initial_levels=InitialLevels(stop=90.0, tp=110.0)),
        )
        document = intent_to_jsonable(intent)
        assert document["exit"] is not None
        self.assertEqual(document["exit"]["reaction_plan"], [])

    def test_the_bytes_on_the_journal_are_unchanged(self) -> None:
        """The reason this change is safe: ``picks.jsonl`` is written through
        ``json.dumps``, which renders a tuple and a list identically."""
        intent = _intent_with_reanchor()
        import dataclasses

        self.assertEqual(
            json.dumps(dataclasses.asdict(intent), sort_keys=True),
            json.dumps(intent_to_jsonable(intent), sort_keys=True),
        )


class TestTheDoorHelpersLiveInTheCodec(unittest.TestCase):
    """The two door gates that are properties of the codec, published by it (#1575).

    ``supplied_derived_paths`` is the other half of :func:`author_jsonable`: one
    strips the derived fields, the other reports them. ``discarded_paths`` is the
    codec's fixed point: what decode + re-render gives back unchanged. Both used
    to live pipeline-side; the replay door (a second user that may not import
    the pipeline) is why they moved. The arming door keeps calling them through
    the same names, pinned here by identity so the two doors cannot drift apart.
    """

    def test_the_derived_role_names_exactly_the_three_fields_the_helper_walks(self) -> None:
        import dataclasses

        from broker_contract.trade_intent import schema as contract_schema

        derived = {
            field.name
            for value in vars(contract_schema).values()
            if isinstance(value, type) and dataclasses.is_dataclass(value)
            for field in dataclasses.fields(value)
            if field.metadata.get("door") == "derived"
        }
        self.assertEqual(derived, {"intent_id", "armed_ts", "r_multiple"})

    def test_supplied_derived_paths_reports_every_derived_field_as_a_path(self) -> None:
        from broker_contract.trade_intent.codec import supplied_derived_paths

        stored = intent_to_jsonable(_trailing_intent())
        self.assertEqual(
            supplied_derived_paths(stored),
            [
                "intent_id",
                "meta.armed_ts",
                "spec.tp_tranches[0].r_multiple",
                "spec.tp_tranches[1].r_multiple",
            ],
        )
        self.assertEqual(supplied_derived_paths(author_jsonable(_trailing_intent())), [])

    def test_supplied_derived_paths_is_total_over_wrong_containers(self) -> None:
        from broker_contract.trade_intent.codec import supplied_derived_paths

        for document in ([], "abc", None, 1.5, {"meta": 3, "spec": {"tp_tranches": {"x": 1}}}):
            with self.subTest(document=document):
                self.assertEqual(supplied_derived_paths(document), [])

    def test_discarded_paths_names_what_was_sent_and_not_given_back(self) -> None:
        from broker_contract.trade_intent.codec import discarded_paths

        sent = {"a": 1, "b": {"c": 2, "typo": 3}, "items": [{"x": 1}, {"y": 2}]}
        rendered = {"a": 1, "b": {"c": 2}, "items": [{"x": 1}, {}], "added": True}
        self.assertEqual(discarded_paths(sent, rendered), ["b.typo", "items[1].y"])

    def test_discarded_paths_reports_a_container_of_another_kind_or_length_whole(self) -> None:
        from broker_contract.trade_intent.codec import discarded_paths

        self.assertEqual(discarded_paths({"a": [1, 2]}, {"a": [1]}), ["a"])
        self.assertEqual(discarded_paths({"a": {"b": 1}}, {"a": 1}), ["a"])
        self.assertEqual(discarded_paths([1], {"a": 1}), ["$"])
        self.assertEqual(discarded_paths(1, 2), ["$"])

    def test_discarded_paths_treats_a_nan_leaf_as_unchanged(self) -> None:
        from broker_contract.trade_intent.codec import discarded_paths

        self.assertEqual(discarded_paths({"p": float("nan")}, {"p": float("nan")}), [])
        self.assertEqual(discarded_paths({"p": float("nan")}, {"p": 1.0}), ["p"])

    def test_the_arming_door_calls_the_codec_functions(self) -> None:
        from alphalens_cli.commands import broker
        from alphalens_pipeline.brokers.automanager import intent_door
        from broker_contract.trade_intent import codec

        self.assertIs(broker._discarded_paths, codec.discarded_paths)
        self.assertIs(intent_door.supplied_derived_paths, codec.supplied_derived_paths)

    def test_both_are_part_of_the_published_codec_api(self) -> None:
        from broker_contract.trade_intent import codec

        self.assertIn("discarded_paths", codec.__all__)
        self.assertIn("supplied_derived_paths", codec.__all__)
