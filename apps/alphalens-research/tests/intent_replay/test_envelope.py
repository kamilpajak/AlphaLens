"""The result envelope (spec sections 5, 5.1, 5.2 and 5.3).

Documents come from the PUBLISHED templates and go through the real door and
the real interpreter, so nothing here asserts against a shape hand-typed to
match the code. The `divergences` registry is checked against the section 5.2
table BOTH WAYS: a name the code emits and the table does not have is as much
a failure as a table row nothing emits.
"""

from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from typing import Any

from intent_replay import envelope
from intent_replay.bars import Bar
from intent_replay.door import admit
from intent_replay.interpreter import PendingEntry, interpret
from intent_replay.measures import summarise
from intent_replay.walk import walk

from tests.intent_replay.test_config import CANONICAL
from tests.intent_replay.test_walk import MINUTE, WALK_START, _bar, _config, _plan

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SPEC = WORKSPACE_ROOT / "docs" / "superpowers" / "specs" / "2026-09-23-intent-replay-design.md"
EXAMPLES = WORKSPACE_ROOT / "apps" / "alphalens-broker-contract" / "examples" / "manual-pick"

STATED_TRADE_DATE = "2026-09-23"


def _document(name: str, **patch: Any) -> dict[str, Any]:
    data = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    data.setdefault("meta", {})["trade_date"] = STATED_TRADE_DATE
    for key, value in patch.items():
        data[key] = value
    return data


def _built(name: str, bars: tuple[Bar, ...], **patch: Any) -> dict[str, Any]:
    """The whole pipeline: door, interpreter, walk, measures, envelope."""
    admitted = admit(_document(name, **patch))
    plan = interpret(admitted.intent, admitted.document)
    config = _config()
    result = walk(plan, config, bars)
    measures = summarise(result, declared_floor=plan.declared_floor)
    return envelope.build(
        intent=admitted.intent,
        plan=plan,
        config=config,
        result=result,
        measures=measures,
        bars=bars,
    )


TWO_BARS = (_bar(WALK_START, 68.0, 68.6, 67.9), _bar(WALK_START + MINUTE, 70.0, 75.0, 69.0))


class TopLevelShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.built = _built("pullback-trailing-stop", TWO_BARS)

    def test_the_ten_keys_are_the_spec_keys_in_the_spec_order(self) -> None:
        self.assertEqual(
            list(self.built),
            [
                "schema",
                "intent_id",
                "instrument",
                "window",
                "config",
                "divergences",
                "intrabar_rule",
                "outcome",
                "summary",
                "trace",
            ],
        )

    def test_the_schema_is_the_published_identifier(self) -> None:
        self.assertEqual(self.built["schema"], "intent_replay.result/v1")
        self.assertEqual(envelope.SCHEMA, "intent_replay.result/v1")

    def test_the_intent_id_is_the_doors_sentinel_carried_off_the_intent(self) -> None:
        self.assertEqual(self.built["intent_id"], "REPLAY")

    def test_the_instrument_echoes_the_documents_ticker_and_mic(self) -> None:
        self.assertEqual(self.built["instrument"], {"ticker": "KO", "mic": "XNYS"})

    def test_the_intrabar_rule_is_the_literal_the_spec_prints(self) -> None:
        # Section 5: the key NAMES the decision rule; it does not bound the
        # cash. A rename here is a wire change, so the literal is asserted.
        self.assertEqual(self.built["intrabar_rule"], "entries_then_stop_then_ladder")
        self.assertEqual(envelope.INTRABAR_RULE, "entries_then_stop_then_ladder")

    def test_the_trace_is_present_and_is_the_walks_events(self) -> None:
        self.assertEqual(len(self.built["trace"]), 4)
        self.assertEqual(self.built["trace"][0]["kind"], "entry_filled")

    def test_the_whole_envelope_is_strict_json(self) -> None:
        # ``allow_nan=False`` is what keeps a NaN or an infinity off stdout.
        text = json.dumps(self.built, allow_nan=False)
        self.assertEqual(json.loads(text), self.built)


class WindowTest(unittest.TestCase):
    """`window` describes the INPUT series, which is a choice the spec does not
    make for us: in the section 5 example `from_t` equals `config.walk_start`
    and `to_t` equals `config.entry_deadline`, so that block reads equally well
    as "the configuration's boundaries"."""

    def test_the_window_is_the_first_and_last_bar_handed_in(self) -> None:
        built = _built("pullback-trailing-stop", TWO_BARS)
        self.assertEqual(
            built["window"],
            {"from_t": WALK_START, "to_t": WALK_START + MINUTE, "bars": 2},
        )

    def test_the_window_can_start_before_walk_start(self) -> None:
        # A bar before ``walk_start`` is skipped by the walk but is still part
        # of the series the caller handed in, so the window is WIDER than what
        # the walk looked at. Stated in the README rather than left to a reader.
        bars = (_bar(WALK_START - MINUTE, 50.0, 50.0, 50.0), *TWO_BARS)
        built = _built("pullback-trailing-stop", bars)
        self.assertEqual(built["window"]["from_t"], WALK_START - MINUTE)
        self.assertEqual(built["window"]["bars"], 3)

    def test_the_window_can_end_after_the_last_bar_the_walk_read(self) -> None:
        # The loop breaks when the position closes, so trailing bars are never
        # walked and still count in the window.
        bars = (*TWO_BARS, _bar(WALK_START + 2 * MINUTE, 80.0, 81.0, 79.0))
        built = _built("pullback-trailing-stop", bars)
        self.assertEqual(built["outcome"], "closed_tp")
        self.assertEqual(built["window"]["to_t"], WALK_START + 2 * MINUTE)
        self.assertNotEqual(built["trace"][-1]["t"], WALK_START + 2 * MINUTE)


class ConfigEchoTest(unittest.TestCase):
    def test_the_config_block_is_echoed_key_for_key_in_the_spec_order(self) -> None:
        built = _built("pullback-trailing-stop", TWO_BARS)
        self.assertEqual(built["config"], dict(CANONICAL))
        self.assertEqual(list(built["config"]), list(CANONICAL))

    def test_the_echo_departs_from_the_printed_block_in_exactly_one_key(self) -> None:
        # Section 5.4: the printed block states ``entry_trail_bps: 50`` and this
        # version refuses a stated distance, so the fixture -- and therefore the
        # echo -- states null. Asserted so the departure cannot grow silently.
        printed = _spec_config_block()
        echoed = _built("pullback-trailing-stop", TWO_BARS)["config"]
        differing = [key for key in printed if printed[key] != echoed[key]]
        self.assertEqual(differing, ["entry_trail_bps"])
        self.assertEqual(printed["entry_trail_bps"], 50)
        self.assertIsNone(echoed["entry_trail_bps"])


def _spec_config_block() -> dict[str, Any]:
    """The `config` block of the section 5 example, read out of the spec."""
    text = SPEC.read_text(encoding="utf-8")
    start = text.index('  "config": {')
    end = text.index('\n  "divergences"', start)
    return json.loads("{" + text[start:end].rstrip().rstrip(",") + "}")["config"]


class SummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.summary = _built("pullback-trailing-stop", TWO_BARS)["summary"]

    def test_the_nine_summary_keys_are_the_spec_keys_in_order(self) -> None:
        self.assertEqual(
            list(self.summary),
            [
                "filled_fraction",
                "snu_bars",
                "notional_spent",
                "avg_entry_price",
                "pnl_cash",
                "pnl_pct_of_spent",
                "r_multiple",
                "mfe",
                "mae",
            ],
        )

    def test_the_two_counters_are_bare_scalars(self) -> None:
        # Neither carries a unit: a fraction and a COUNT of bars.
        self.assertIsInstance(self.summary["filled_fraction"], float)
        self.assertIsInstance(self.summary["snu_bars"], int)
        self.assertNotIsInstance(self.summary["snu_bars"], bool)

    def test_the_two_counters_carry_the_walks_own_values(self) -> None:
        # A type check alone cannot refute a hardcoded constant: both of these
        # survived being pinned to 0 with the whole suite green. The template
        # fills only its 68.00 rung out of a 1500 budget, so 900/1500 = 0.6.
        self.assertEqual(self.summary["filled_fraction"], 0.6)
        self.assertEqual(self.summary["snu_bars"], 0)

    def test_a_counted_bar_reaches_the_summary_as_a_non_zero_count(self) -> None:
        # Every other envelope test runs a document whose count is 0, so a
        # hardcoded zero would satisfy all of them. This one makes the bar
        # reach both a rung and an affordable tranche: the walk takes the stop,
        # counts the bar, and the count has to arrive in the summary.
        document = _document("pullback-trailing-stop")
        document["spec"]["tp_tranches"] = [{"price": 68.50, "tranche_pct": 100.0}]
        admitted = admit(document)
        plan = interpret(admitted.intent, admitted.document)
        config = _config()
        bars = (_bar(WALK_START, 67.00, 68.60, 66.40),)
        result = walk(plan, config, bars)
        built = envelope.build(
            intent=admitted.intent,
            plan=plan,
            config=config,
            result=result,
            measures=summarise(result, declared_floor=plan.declared_floor),
            bars=bars,
        )
        self.assertEqual(result.snu_bars, 1)
        self.assertEqual(built["summary"]["snu_bars"], 1)

    def test_the_cash_fields_carry_the_documents_own_currency(self) -> None:
        # ``spec.size.currency`` IS stated, so these carry a real code.
        self.assertEqual(self.summary["notional_spent"]["unit"], "EUR")
        self.assertEqual(self.summary["pnl_cash"]["unit"], "EUR")

    def test_the_price_fields_carry_the_symbolic_unit(self) -> None:
        # No document path states the INSTRUMENT's currency and section 4.3.1
        # puts it out of scope, so the tool must not resolve it.
        self.assertEqual(self.summary["avg_entry_price"]["unit"], "instrument_currency")
        self.assertEqual(self.summary["r_multiple"]["denominator"]["unit"], "instrument_currency")

    def test_the_percentage_carries_percent_and_not_fraction(self) -> None:
        self.assertEqual(self.summary["pnl_pct_of_spent"]["unit"], "percent")
        self.assertGreater(abs(self.summary["pnl_pct_of_spent"]["value"]), 1.0)

    def test_r_and_the_excursion_carry_the_r_unit(self) -> None:
        self.assertEqual(self.summary["r_multiple"]["unit"], "R")
        self.assertEqual(self.summary["mfe"]["unit"], "R")
        self.assertEqual(self.summary["mae"]["unit"], "R")

    def test_the_denominator_is_the_provenance_object_of_section_5_1(self) -> None:
        denominator = self.summary["r_multiple"]["denominator"]
        self.assertEqual(list(denominator), ["kind", "value", "unit", "source", "formula"])
        self.assertEqual(denominator["kind"], "placed_stop")
        self.assertEqual(denominator["source"], "spec.disaster_stop")
        self.assertEqual(denominator["formula"], "avg_entry_price - placed_stop")
        self.assertEqual(denominator["value"], 68.0 - 63.0)

    def test_the_published_identity_closes_on_the_rendered_numbers(self) -> None:
        # r = (pnl_pct / 100) * avg / den, section 5. Run against what the
        # envelope actually printed, not against the measures object.
        closed = (
            self.summary["pnl_pct_of_spent"]["value"]
            / 100.0
            * self.summary["avg_entry_price"]["value"]
            / self.summary["r_multiple"]["denominator"]["value"]
        )
        self.assertAlmostEqual(self.summary["r_multiple"]["value"], closed, places=12)


class NullShapesTest(unittest.TestCase):
    """Section 5.1 prints TWO different null shapes and the asymmetry is in the
    sentence itself: `r_multiple.value` is nested, `mfe` and `mae` are bare."""

    def _not_positive(self) -> dict[str, Any]:
        # A bar opening exactly at the disaster stop fills there, so the
        # average entry IS the floor and the denominator is exactly 0.0.
        document = _document("pullback-trailing-stop")
        document["spec"]["entry_tiers"] = [{"limit_price": 68.0, "alloc_pct": 100.0}]
        admitted = admit(document)
        plan = interpret(admitted.intent, admitted.document)
        config = _config()
        bars = (_bar(WALK_START, 63.0, 63.0, 63.0, 63.0),)
        result = walk(plan, config, bars)
        return envelope.build(
            intent=admitted.intent,
            plan=plan,
            config=config,
            result=result,
            measures=summarise(result, declared_floor=plan.declared_floor),
            bars=bars,
        )["summary"]

    def test_r_keeps_its_object_so_the_reader_sees_why(self) -> None:
        summary = self._not_positive()
        self.assertIsNone(summary["r_multiple"]["value"])
        self.assertEqual(summary["r_multiple"]["unit"], "R")
        self.assertEqual(summary["r_multiple"]["denominator"]["value"], 0.0)
        self.assertEqual(summary["r_multiple"]["denominator"]["kind"], "placed_stop")

    def test_the_excursion_is_a_bare_null_and_not_a_wrapped_one(self) -> None:
        summary = self._not_positive()
        self.assertIsNone(summary["mfe"])
        self.assertIsNone(summary["mae"])
        self.assertNotIsInstance(summary["mfe"], dict)
        self.assertNotIsInstance(summary["mae"], dict)

    def test_a_run_that_bought_nothing_nulls_every_measure_it_cannot_divide(self) -> None:
        bars = (_bar(WALK_START, 90.0, 91.0, 89.0),)
        summary = _built("pullback-trailing-stop", bars)["summary"]
        self.assertEqual(summary["notional_spent"]["value"], 0.0)
        self.assertIsNone(summary["avg_entry_price"])
        self.assertIsNone(summary["pnl_pct_of_spent"])
        self.assertIsNone(summary["mfe"])
        self.assertIsNone(summary["r_multiple"]["value"])
        self.assertIsNone(summary["r_multiple"]["denominator"]["value"])


def _spec_divergence_names() -> list[str]:
    """The first column of the section 5.2 `divergences` table."""
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("| entry | what the replay lacks | emitted when |")
    end = text.index("\n\n", start)
    rows = text[start:end].splitlines()[2:]
    return [re.sub(r"[`|]", "", row.split("|")[1]).strip() for row in rows]


class DivergenceRegistryTest(unittest.TestCase):
    def test_the_registry_is_the_spec_table_in_both_directions(self) -> None:
        self.assertEqual(sorted(envelope.DIVERGENCES), sorted(_spec_divergence_names()))
        self.assertEqual(len(envelope.DIVERGENCES), 5)

    def test_every_name_carries_the_reason_the_replay_lacks_the_fact(self) -> None:
        for name, why in envelope.DIVERGENCES.items():
            with self.subTest(name):
                self.assertTrue(why.strip())


class DivergencePredicateTest(unittest.TestCase):
    """One document that fires each name and one that does not. The predicates
    are transcribed from the section 5.2 table, so a wrong one is a report that
    does not describe the run."""

    def _names(self, name: str, **patch: Any) -> list[str]:
        return _built(name, TWO_BARS, **patch)["divergences"]

    def test_a_trailing_document_reports_the_order_state_guards(self) -> None:
        self.assertIn("daemon_trail_guards", self._names("pullback-trailing-stop"))

    def test_a_static_document_does_not_report_them(self) -> None:
        # Section 5.2: with ``exit: null`` the re-anchor arm returns before
        # either guard is reached, so silence is honest.
        self.assertNotIn("daemon_trail_guards", self._names("pullback-two-tiers", exit=None))

    def test_a_reanchoring_document_reports_both_the_guards_and_the_latch(self) -> None:
        names = self._names(
            "pullback-trailing-stop",
            exit={
                "initial_levels": None,
                "reaction_plan": [
                    {"kind": "reanchor_on_fill", "k_atr": 1.5, "atr": 1.2, "ceiling_price": None}
                ],
            },
        )
        self.assertIn("daemon_reanchor_latch_is_journal_lifetime", names)
        self.assertIn("daemon_trail_guards", names)

    def test_a_trailing_document_does_not_report_the_latch(self) -> None:
        names = self._names("pullback-trailing-stop")
        self.assertNotIn("daemon_reanchor_latch_is_journal_lifetime", names)

    def test_a_non_empty_ladder_reports_the_observation_time_and_the_currency(self) -> None:
        names = self._names("pullback-trailing-stop")
        self.assertIn("take_profit_observation_time", names)
        self.assertIn("cost_gate_prices_the_account_currency", names)

    def test_an_empty_ladder_reports_neither(self) -> None:
        document = _document("pullback-trailing-stop")
        document["spec"]["tp_tranches"] = []
        admitted = admit(document)
        plan = interpret(admitted.intent, admitted.document)
        names = envelope.divergences(plan, _config())
        self.assertNotIn("take_profit_observation_time", names)
        self.assertNotIn("cost_gate_prices_the_account_currency", names)

    def test_the_currency_entry_also_needs_the_minimum_commission_to_apply(self) -> None:
        # Two conditions, not one: with the per-fill minimum switched off the
        # two thresholds agree and the entry would be noise.
        document = _document("pullback-trailing-stop")
        admitted = admit(document)
        plan = interpret(admitted.intent, admitted.document)
        block = copy.deepcopy(dict(CANONICAL))
        block["costs"] = {**block["costs"], "min_commission_applies": False}
        from intent_replay.config import RunConfig

        names = envelope.divergences(plan, RunConfig.from_jsonable(block))
        self.assertIn("take_profit_observation_time", names)
        self.assertNotIn("cost_gate_prices_the_account_currency", names)

    def test_the_entry_trail_entry_cannot_be_reached_in_this_version(self) -> None:
        # The predicate is "the run STATES an entry-trail distance", and this
        # version refuses every stated distance (section 5.4,
        # ``entry_trail_not_modelled``). So the code emits the name and no
        # ACCEPTED configuration reaches it. Asserted as the gap it is, rather
        # than dressed up with a document that could not be run.
        self.assertIn("native_entry_trail_is_a_broker_model", envelope.DIVERGENCES)
        self.assertNotIn(
            "native_entry_trail_is_a_broker_model", self._names("pullback-trailing-stop")
        )

    def test_the_list_is_ordered_by_the_registry_and_not_by_discovery(self) -> None:
        names = self._names("pullback-trailing-stop")
        self.assertEqual(names, [n for n in envelope.DIVERGENCES if n in names])


class LadderResolutionTest(unittest.TestCase):
    """Two `divergences` predicates ask whether the RESOLVED ladder is empty,
    and only `walk.resolve_ladder` knows."""

    def test_a_document_that_placed_its_own_bracket_resolves_to_that_bracket(self) -> None:
        plan = _plan(entries=(PendingEntry(0, 68.0, 900.0),), take_profit=74.0, tranches=())
        names = envelope.divergences(plan, _config())
        # The author's ladder is empty and the placed one is not, so the
        # predicate must follow the PLACED instruction.
        self.assertIn("take_profit_observation_time", names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
