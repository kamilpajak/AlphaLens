"""Unit tests for ``broker_contract/trade_intent/validate.py`` — the semantic
validation of a :class:`TradeIntent` (#1404).

Every rule here used to live in the CLI-layer builder behind `alphalens broker
arm-manual`, so nothing that did not invoke our Typer command could reach it.
These tests exercise the rules with NO CLI in the picture — that is the property
#1406's `arm --from-intent` door depends on.
"""

from __future__ import annotations

import dataclasses
import json
import unittest

from broker_contract.failure import CONTRACT_FAILURE_CODES
from broker_contract.trade_intent.codec import intent_from_jsonable, intent_to_jsonable
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
from broker_contract.trade_intent.validate import (
    INTENT_INVALID_REASONS,
    IntentInvalidError,
    validate_intent,
)


def _intent(**spec_overrides) -> TradeIntent:
    """A valid two-tier manual intent; override any ``TradeSpec`` field."""
    spec_kwargs = {
        "entry_tiers": (
            EntryTierSpec(limit_price=72.5, alloc_pct=60.0, tag="T1"),
            EntryTierSpec(limit_price=70.0, alloc_pct=40.0, tag="T2"),
        ),
        "disaster_stop": 66.0,
        "tp_tranches": (
            TpTrancheSpec(price=80.0, tranche_pct=50.0, r_multiple=1.5, tag="TP1"),
            TpTrancheSpec(price=90.0, tranche_pct=50.0, r_multiple=3.0, tag="TP2"),
        ),
        "suggested_size_pct": 10.0,
    }
    spec_kwargs.update(spec_overrides)
    return TradeIntent(
        intent_id="NVO:2026-09-10:manual",
        instrument=InstrumentHint(ticker="NVO", mic="XNYS"),
        spec=TradeSpec(**spec_kwargs),
        meta=IntentMeta(armed_ts="2026-09-10T12:00:00+00:00", trade_date="2026-09-10"),
    )


def _reason_of(exc: IntentInvalidError) -> str:
    return str(exc.failure.details["reason"])


class ValidIntentPassesTest(unittest.TestCase):
    def test_the_baseline_intent_is_accepted(self) -> None:
        self.assertIsNone(validate_intent(_intent()))


class IdentityRulesTest(unittest.TestCase):
    def test_blank_intent_id_refuses(self) -> None:
        intent = _intent()
        blank = TradeIntent(
            intent_id="  ",
            instrument=intent.instrument,
            spec=intent.spec,
            meta=intent.meta,
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(blank)
        self.assertEqual(_reason_of(ctx.exception), "intent_id_empty")

    def test_blank_ticker_refuses(self) -> None:
        intent = _intent()
        blank = TradeIntent(
            intent_id=intent.intent_id,
            instrument=InstrumentHint(ticker="   ", mic="XNYS"),
            spec=intent.spec,
            meta=intent.meta,
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(blank)
        self.assertEqual(_reason_of(ctx.exception), "ticker_empty")


class LiteralIsNotAGateTest(unittest.TestCase):
    """``Literal`` annotates; it does not enforce. Measured, not assumed.

    Each test asserts BOTH halves: that the bad document really does decode (the
    positive control — without it the test would also pass if ``Literal`` were
    enforced and the rule were dead code), and that ``validate_intent`` refuses it.
    """

    def _document(self, **spec_overrides) -> dict:
        doc = intent_to_jsonable(_intent())
        doc["spec"].update(spec_overrides)
        return doc

    def test_side_short_decodes_and_is_then_refused(self) -> None:
        doc = self._document(side="short")
        decoded = intent_from_jsonable(doc)
        self.assertEqual(decoded.spec.side, "short")  # positive control
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(decoded)
        self.assertEqual(_reason_of(ctx.exception), "side_not_long")

    def test_unknown_entry_mode_decodes_and_is_then_refused(self) -> None:
        doc = intent_to_jsonable(_intent())
        doc["spec"]["entry_tiers"][0]["entry_mode"] = "sideways"
        decoded = intent_from_jsonable(doc)
        self.assertEqual(decoded.spec.entry_tiers[0].entry_mode, "sideways")
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(decoded)
        self.assertEqual(_reason_of(ctx.exception), "entry_mode_unknown")


class EntryLadderRulesTest(unittest.TestCase):
    def test_empty_ladder_refuses(self) -> None:
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=()))
        self.assertEqual(_reason_of(ctx.exception), "entry_tiers_empty")

    def test_allocations_off_100_refuse_without_rescaling(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=72.5, alloc_pct=60.0),
            EntryTierSpec(limit_price=70.0, alloc_pct=30.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers))
        self.assertEqual(_reason_of(ctx.exception), "entry_alloc_sum")
        self.assertIn("no silent rescaling", ctx.exception.failure.message)

    def test_float_noise_in_an_equal_split_is_tolerated(self) -> None:
        third = 100.0 / 3
        tiers = tuple(
            EntryTierSpec(limit_price=70.0 + index, alloc_pct=third) for index in range(3)
        )
        self.assertIsNone(validate_intent(_intent(entry_tiers=tiers)))

    def test_non_positive_tier_price_refuses_and_names_the_tier(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=72.5, alloc_pct=50.0),
            EntryTierSpec(limit_price=0.0, alloc_pct=50.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers))
        self.assertEqual(_reason_of(ctx.exception), "entry_price_non_positive")
        self.assertEqual(ctx.exception.failure.details["tier_index"], 1)

    def test_non_positive_allocation_refuses(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=72.5, alloc_pct=100.0),
            EntryTierSpec(limit_price=70.0, alloc_pct=0.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers))
        self.assertEqual(_reason_of(ctx.exception), "entry_alloc_non_positive")

    def test_duplicate_tier_price_refuses(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=70.0, alloc_pct=50.0),
            EntryTierSpec(limit_price=70.0, alloc_pct=50.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers))
        self.assertEqual(_reason_of(ctx.exception), "entry_price_duplicate")

    def test_two_immediate_tiers_refuse(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=75.0, alloc_pct=50.0, entry_mode="immediate"),
            EntryTierSpec(limit_price=74.0, alloc_pct=50.0, entry_mode="immediate"),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers))
        self.assertEqual(_reason_of(ctx.exception), "immediate_tier_count")

    def test_immediate_tier_not_listed_first_refuses(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=70.0, alloc_pct=50.0),
            EntryTierSpec(limit_price=75.0, alloc_pct=50.0, entry_mode="immediate"),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers))
        self.assertEqual(_reason_of(ctx.exception), "immediate_tier_not_first")

    def test_a_leading_immediate_tier_is_accepted(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=75.0, alloc_pct=50.0, entry_mode="immediate"),
            EntryTierSpec(limit_price=70.0, alloc_pct=50.0),
        )
        self.assertIsNone(validate_intent(_intent(entry_tiers=tiers)))


class StopAndSizeRulesTest(unittest.TestCase):
    def test_non_positive_stop_refuses(self) -> None:
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(disaster_stop=0.0))
        self.assertEqual(_reason_of(ctx.exception), "stop_non_positive")

    def test_stop_at_or_above_the_lowest_tier_refuses(self) -> None:
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(disaster_stop=70.0))
        self.assertEqual(_reason_of(ctx.exception), "stop_above_entry")

    def test_a_levered_size_refuses(self) -> None:
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(suggested_size_pct=100.1))
        self.assertEqual(_reason_of(ctx.exception), "size_pct_out_of_range")

    def test_a_full_frame_size_is_accepted(self) -> None:
        self.assertIsNone(validate_intent(_intent(suggested_size_pct=100.0)))

    def test_zero_size_refuses(self) -> None:
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(suggested_size_pct=0.0))
        self.assertEqual(_reason_of(ctx.exception), "size_pct_out_of_range")


class TakeProfitRulesTest(unittest.TestCase):
    def test_tranche_percentages_under_100_are_LEGAL_a_runner_is_left(self) -> None:
        """The TP rule is ONE-SIDED, unlike the entry ladder's.

        Summing to less than 100 means the pick deliberately leaves a runner. If
        this ever becomes a refusal, a real strategy stops being expressible.
        """
        tranches = (TpTrancheSpec(price=80.0, tranche_pct=60.0),)
        self.assertIsNone(validate_intent(_intent(tp_tranches=tranches)))

    def test_tranche_percentages_over_100_refuse(self) -> None:
        tranches = (
            TpTrancheSpec(price=80.0, tranche_pct=60.0),
            TpTrancheSpec(price=90.0, tranche_pct=41.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(tp_tranches=tranches))
        self.assertEqual(_reason_of(ctx.exception), "tp_pct_sum_exceeds_100")

    def test_no_tp_tranches_at_all_is_accepted(self) -> None:
        self.assertIsNone(validate_intent(_intent(tp_tranches=())))

    def test_non_positive_tranche_pct_refuses(self) -> None:
        tranches = (TpTrancheSpec(price=80.0, tranche_pct=0.0),)
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(tp_tranches=tranches))
        self.assertEqual(_reason_of(ctx.exception), "tp_pct_non_positive")

    def test_duplicate_tp_price_refuses(self) -> None:
        tranches = (
            TpTrancheSpec(price=80.0, tranche_pct=50.0),
            TpTrancheSpec(price=80.0, tranche_pct=50.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(tp_tranches=tranches))
        self.assertEqual(_reason_of(ctx.exception), "tp_price_duplicate")

    def test_tp_at_or_below_the_planned_blend_refuses_and_names_the_tranche(self) -> None:
        # Blend over 72.5@60 / 70.0@40 is 71.5.
        tranches = (
            TpTrancheSpec(price=80.0, tranche_pct=50.0),
            TpTrancheSpec(price=71.0, tranche_pct=50.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(tp_tranches=tranches))
        self.assertEqual(_reason_of(ctx.exception), "tp_price_below_blend")
        self.assertEqual(ctx.exception.failure.details["tranche_index"], 1)


class NonFiniteNumbersTest(unittest.TestCase):
    """A NaN passes every comparison silently, so it must be refused first.

    Reachable: `json.loads` accepts a bare `NaN` literal by default and the codec
    carries it through unchanged, so a document arriving at a door really can hold
    one. The CLI path never could — `_parse_float` checks `math.isfinite` — which
    is exactly why the corpus of real intents could not have surfaced this.
    """

    def _refuses(self, **spec_overrides) -> str:
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(**spec_overrides))
        return _reason_of(ctx.exception)

    def test_nan_tier_price_refuses(self) -> None:
        tiers = (EntryTierSpec(limit_price=float("nan"), alloc_pct=100.0),)
        self.assertEqual(self._refuses(entry_tiers=tiers), "numeric_not_finite")

    def test_infinite_tier_price_refuses(self) -> None:
        tiers = (EntryTierSpec(limit_price=float("inf"), alloc_pct=100.0),)
        self.assertEqual(self._refuses(entry_tiers=tiers), "numeric_not_finite")

    def test_nan_allocation_refuses(self) -> None:
        tiers = (EntryTierSpec(limit_price=70.0, alloc_pct=float("nan")),)
        self.assertEqual(self._refuses(entry_tiers=tiers), "numeric_not_finite")

    def test_nan_stop_refuses(self) -> None:
        self.assertEqual(self._refuses(disaster_stop=float("nan")), "numeric_not_finite")

    def test_nan_size_refuses(self) -> None:
        self.assertEqual(self._refuses(suggested_size_pct=float("nan")), "numeric_not_finite")

    def test_nan_tp_price_refuses(self) -> None:
        tranches = (TpTrancheSpec(price=float("nan"), tranche_pct=50.0),)
        self.assertEqual(self._refuses(tp_tranches=tranches), "numeric_not_finite")

    def test_the_offending_field_is_named(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=70.0, alloc_pct=60.0),
            EntryTierSpec(limit_price=float("nan"), alloc_pct=40.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers))
        details = ctx.exception.failure.details
        self.assertEqual(details["field"], "entry_tiers[1].limit_price")
        self.assertEqual(details["tier_index"], 1)

    def test_a_nan_document_really_does_decode_first(self) -> None:
        """Positive control: without this the rule could be unreachable in practice."""
        raw = (
            '{"intent_id":"X:2026-09-10:m","instrument":{"ticker":"X","mic":"XNYS"},'
            '"spec":{"entry_tiers":[{"limit_price":NaN,"alloc_pct":100.0}],'
            '"disaster_stop":5.0,"tp_tranches":[],"suggested_size_pct":10.0},'
            '"meta":{"armed_ts":"t","trade_date":"2026-09-10"}}'
        )
        decoded = intent_from_jsonable(json.loads(raw))
        self.assertNotEqual(
            decoded.spec.entry_tiers[0].limit_price, decoded.spec.entry_tiers[0].limit_price
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(decoded)
        self.assertEqual(_reason_of(ctx.exception), "numeric_not_finite")


class StopIsJudgedAgainstREALTiersOnlyTest(unittest.TestCase):
    """A bogus tier must not manufacture a second, false violation.

    `violations` now carries the complete list, so a spurious entry is not
    cosmetic: it would send a client to "fix" a stop that was never wrong.
    """

    def test_a_zero_tier_does_not_make_a_valid_stop_look_too_high(self) -> None:
        tiers = (
            EntryTierSpec(limit_price=70.0, alloc_pct=50.0),
            EntryTierSpec(limit_price=0.0, alloc_pct=50.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            # 60 sits below the only REAL tier; only the bogus 0 makes min() 0.
            validate_intent(_intent(entry_tiers=tiers, disaster_stop=60.0))
        reasons = [v["reason"] for v in ctx.exception.failure.details["violations"]]
        self.assertIn("entry_price_non_positive", reasons)
        self.assertNotIn("stop_above_entry", reasons)

    def test_a_genuinely_high_stop_is_still_caught_beside_a_bogus_tier(self) -> None:
        """The positive control: the filter must not disable the rule."""
        tiers = (
            EntryTierSpec(limit_price=70.0, alloc_pct=50.0),
            EntryTierSpec(limit_price=0.0, alloc_pct=50.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers, disaster_stop=75.0))
        reasons = [v["reason"] for v in ctx.exception.failure.details["violations"]]
        self.assertIn("stop_above_entry", reasons)

    def test_an_all_bogus_ladder_reports_only_the_price_rule(self) -> None:
        tiers = (EntryTierSpec(limit_price=0.0, alloc_pct=100.0),)
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers, disaster_stop=5.0))
        reasons = [v["reason"] for v in ctx.exception.failure.details["violations"]]
        self.assertNotIn("stop_above_entry", reasons)


class TheFailureShapeTest(unittest.TestCase):
    def test_the_code_is_the_registered_contract_code(self) -> None:
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(disaster_stop=0.0))
        failure = ctx.exception.failure
        self.assertEqual(failure.code, "intent_invalid")
        self.assertIn(failure.code, CONTRACT_FAILURE_CODES)
        self.assertFalse(failure.retryable)

    def test_the_failure_renders_as_json(self) -> None:
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(disaster_stop=0.0))
        rendered = json.loads(json.dumps(ctx.exception.failure.to_jsonable()))
        self.assertEqual(rendered["code"], "intent_invalid")
        self.assertEqual(rendered["details"]["reason"], "stop_non_positive")

    def test_every_violation_is_reported_not_only_the_first(self) -> None:
        """A generated document must be fixable in one pass, not by a submit loop.

        Both halves matter: the complete list is what a machine consumer needs,
        and ``message`` still carrying the FIRST violation is what keeps the
        operator's text unchanged.
        """
        tiers = (
            EntryTierSpec(limit_price=72.5, alloc_pct=60.0),
            EntryTierSpec(limit_price=70.0, alloc_pct=30.0),
        )
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers, disaster_stop=0.0, suggested_size_pct=250.0))
        details = ctx.exception.failure.details
        reasons = [v["reason"] for v in details["violations"]]
        self.assertEqual(reasons, ["entry_alloc_sum", "stop_non_positive", "size_pct_out_of_range"])
        self.assertEqual(details["reason"], "entry_alloc_sum")
        self.assertIn("sum to 100", ctx.exception.failure.message)

    def test_every_reason_raised_is_in_the_published_registry(self) -> None:
        """Positive control: the registry is not allowed to rot into a tautology."""
        self.assertIn("stop_non_positive", INTENT_INVALID_REASONS)
        self.assertNotIn("a_reason_nobody_registered", INTENT_INVALID_REASONS)


class BlendDependentRulesAreSkippedWhenTheBlendIsUncomputableTest(unittest.TestCase):
    def test_an_all_zero_ladder_reports_the_price_rule_not_a_blend_crash(self) -> None:
        tiers = (EntryTierSpec(limit_price=0.0, alloc_pct=100.0),)
        with self.assertRaises(IntentInvalidError) as ctx:
            validate_intent(_intent(entry_tiers=tiers))
        self.assertEqual(_reason_of(ctx.exception), "entry_price_non_positive")
        reasons = [v["reason"] for v in ctx.exception.failure.details["violations"]]
        self.assertNotIn("tp_price_below_blend", reasons)


class LegalDocumentShapesTest(unittest.TestCase):
    def test_a_zero_order_ttl_is_LEGAL_it_is_the_planner_field_absent_sentinel(self) -> None:
        """``order_ttl_days == 0`` is a rule about the CLI flag, not the document.

        ``brokers/execution.py`` resolves the 0 sentinel to a default on purpose.
        Refusing it here would make a document the brief path legitimately emits
        un-submittable.
        """
        self.assertIsNone(validate_intent(_intent(order_ttl_days=0)))


class RoundTripTest(unittest.TestCase):
    def test_a_validated_intent_survives_encode_decode_and_validates_again(self) -> None:
        intent = _intent()
        validate_intent(intent)
        emitted = json.loads(json.dumps(intent_to_jsonable(intent)))
        decoded = intent_from_jsonable(emitted)
        validate_intent(decoded)
        # Compare the SERIALISED documents: intent_to_jsonable emits tuples where
        # json.loads gives lists, so comparing the decoded objects is False for a
        # reason that has nothing to do with the contract.
        self.assertEqual(
            json.dumps(emitted, sort_keys=True),
            json.dumps(intent_to_jsonable(decoded), sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()


class TheDeclarationRulesTest(unittest.TestCase):
    """#1236: the exit's reaction plan is a DECLARATION the daemon honours, so the
    door has to refuse anything it cannot honour — loudly, at arm time, rather
    than by quietly doing something else.

    Three refusals, each for a different reason:

    * **More than one stop-management primitive.** The daemon manages one stop;
      two declarations would need a precedence rule, and a precedence rule
      invented here is a rule no client can read off the document.
    * **``ReanchorOnFill`` without ``initial_levels``.** That primitive is the
      brief-geometry shape: 936 of 936 plannable brief rows carry both. A
      re-anchor declared with no levels beside it is a document whose two halves
      disagree.
    * **``ceiling_price``**, which reads like a stop-side cap and is not. In
      ``atr_bracket_levels`` it applies as ``tp = min(tp, ceiling_price)`` and
      never touches the stop — it is a take-profit, that is, a PLACEMENT
      parameter. This work has nothing to honour it with, and a door must not
      accept a field it discards. The placement issue lifts this refusal.
    """

    def _with(self, exit_spec):
        return dataclasses.replace(_intent(), exit=exit_spec)

    def _reason(self, exit_spec) -> str:
        with self.assertRaises(IntentInvalidError) as caught:
            validate_intent(self._with(exit_spec))
        return caught.exception.failure.details["reason"]

    def test_a_trailing_declaration_with_no_levels_is_accepted(self):
        """The shape the whole change exists for — asserted first, so the
        refusals below cannot be passing by refusing everything."""
        validate_intent(self._with(ExitGeometrySpec(reaction_plan=(TrailingStop(0.5, 0.6),))))

    def test_a_reanchor_beside_its_levels_is_accepted(self):
        validate_intent(
            self._with(
                ExitGeometrySpec(
                    initial_levels=InitialLevels(stop=90.0, tp=130.0),
                    reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0),),
                )
            )
        )

    def test_two_stop_management_primitives_are_refused(self):
        self.assertEqual(
            self._reason(
                ExitGeometrySpec(
                    initial_levels=InitialLevels(stop=90.0, tp=130.0),
                    reaction_plan=(TrailingStop(0.5, 0.6), ReanchorOnFill(k_atr=1.5, atr=2.0)),
                )
            ),
            "reaction_plan_ambiguous",
        )

    def test_a_reanchor_without_levels_is_refused(self):
        self.assertEqual(
            self._reason(ExitGeometrySpec(reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0),))),
            "reanchor_without_levels",
        )

    def test_a_ceiling_price_is_refused(self):
        self.assertEqual(
            self._reason(
                ExitGeometrySpec(
                    initial_levels=InitialLevels(stop=90.0, tp=130.0),
                    reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0, ceiling_price=140.0),),
                )
            ),
            "ceiling_price_unsupported",
        )

    def test_an_unhonourable_primitive_is_refused(self):
        self.assertEqual(
            self._reason(ExitGeometrySpec(reaction_plan=(ModelPush(),))),
            "reaction_kind_unsupported",
        )

    def test_the_declared_parameters_are_bounded(self):
        for exit_spec, reason in (
            (
                ExitGeometrySpec(reaction_plan=(TrailingStop(0.0, 0.6),)),
                "arm_trigger_r_non_positive",
            ),
            (
                ExitGeometrySpec(reaction_plan=(TrailingStop(-1.0, 0.6),)),
                "arm_trigger_r_non_positive",
            ),
            (ExitGeometrySpec(reaction_plan=(TrailingStop(0.5, 0.0),)), "trail_frac_out_of_range"),
            (ExitGeometrySpec(reaction_plan=(TrailingStop(0.5, 1.5),)), "trail_frac_out_of_range"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(self._reason(exit_spec), reason)

    def test_a_trail_frac_of_exactly_one_is_legal(self):
        """The boundary is inclusive on purpose: giving back 100% of the
        excursion is "trail at the peak", a coherent instruction."""
        validate_intent(self._with(ExitGeometrySpec(reaction_plan=(TrailingStop(0.5, 1.0),))))

    def test_a_reanchor_multiple_must_be_positive(self):
        self.assertEqual(
            self._reason(
                ExitGeometrySpec(
                    initial_levels=InitialLevels(stop=90.0, tp=130.0),
                    reaction_plan=(ReanchorOnFill(k_atr=0.0, atr=2.0),),
                )
            ),
            "k_atr_non_positive",
        )

    def test_a_non_finite_declared_parameter_is_caught_before_any_comparison(self):
        """NaN answers False to every ordering comparison, so a bounds rule built
        from `<= 0` would pass it in full. Finiteness is checked first — the same
        discipline the spec's own numbers get."""
        self.assertEqual(
            self._reason(ExitGeometrySpec(reaction_plan=(TrailingStop(float("nan"), 0.6),))),
            "numeric_not_finite",
        )
