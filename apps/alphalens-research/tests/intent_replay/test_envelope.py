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
from intent_replay.config import RunConfig
from intent_replay.door import admit
from intent_replay.interpreter import PendingEntry, interpret
from intent_replay.measures import summarise
from intent_replay.walk import walk

from tests.intent_replay.test_config import ACCOUNT_CURRENCY, CANONICAL
from tests.intent_replay.test_walk import (
    MINUTE,
    WALK_START,
    _bar,
    _config,
    _cross_config,
    _plan,
)

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


def _built(
    name: str, bars: tuple[Bar, ...], *, run_config: RunConfig | None = None, **patch: Any
) -> dict[str, Any]:
    """The whole pipeline: door, interpreter, walk, measures, envelope.

    ``run_config`` is separate from ``patch``: the latter edits the DOCUMENT,
    and a divergence predicate can read either side.

    The default block is the CROSS-currency one (``test_config.CANONICAL``),
    which is the section 5 example's own configuration: a EUR budget on a USD
    instrument, with a rate, a round-trip cost rate and a 1 per cent sizing
    buffer. That is deliberate here and the opposite of ``test_walk``'s
    default -- this file is where the published example is checked against the
    code, so the two have to describe one run."""
    admitted = admit(_document(name, **patch))
    config = _cross_config() if run_config is None else run_config
    plan = interpret(admitted.intent, admitted.document, fx=config.costs.fx)
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

    def test_the_eleven_keys_are_the_spec_keys_in_the_spec_order(self) -> None:
        # ``fx`` sits between the echoed block and the divergences: what was
        # stated, then what was derived from it, then what no setting can close.
        self.assertEqual(
            list(self.built),
            [
                "schema",
                "intent_id",
                "instrument",
                "window",
                "config",
                "fx",
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
        # Section 5.4: the printed block states ``entry_trail_bps: 50`` and the
        # fixture -- and therefore the echo -- states null, because the fixture
        # is the baseline of the limit-ladder tests and a trailing test states
        # the distance itself. Asserted so the departure cannot GROW silently,
        # whatever its reason happens to be.
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
        config = _cross_config()
        plan = interpret(admitted.intent, admitted.document, fx=config.costs.fx)
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

    def test_the_price_fields_carry_the_stated_instrument_currency(self) -> None:
        # Both were the symbolic token ``instrument_currency`` until #1592,
        # because no fact named the currency. ``fx.instrument_currency`` names
        # it, so these carry a real code -- and a DIFFERENT one from the cash
        # fields above, which is the whole point of stating both.
        self.assertEqual(self.summary["avg_entry_price"]["unit"], "USD")
        self.assertEqual(self.summary["r_multiple"]["denominator"]["unit"], "USD")
        self.assertNotEqual(self.summary["notional_spent"]["unit"], "USD")

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

    def _not_positive(self, *, run_config: RunConfig | None = None) -> dict[str, Any]:
        # A bar opening exactly at the disaster stop fills there, so the
        # average entry IS the floor and the denominator is exactly 0.0.
        #
        # The SAME-currency block, and the reason is the fixture rather than
        # the shape under test: ``cash / units`` is ``units * 63.0 / units``,
        # which is exact at a budget of 1500 and rounds to 63.00000000000001 at
        # the buffered 1485, so the cross-currency block cannot build "exactly
        # zero" here at all. The null shapes are currency-free, and the
        # one-ulp case the buffer DOES build is asserted below in its own test,
        # where it is a section 5 property rather than an accident.
        document = _document("pullback-trailing-stop")
        document["spec"]["entry_tiers"] = [{"limit_price": 68.0, "alloc_pct": 100.0}]
        admitted = admit(document)
        config = _config() if run_config is None else run_config
        plan = interpret(admitted.intent, admitted.document, fx=config.costs.fx)
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

    def test_the_smallest_non_zero_denominator_is_one_ulp_and_r_survives_it(self) -> None:
        # Section 5: "the denominator is a difference of two prices of the same
        # size, so its smallest non-zero value is one unit in the last place --
        # 7.1e-15 at a price near 63". The stated 1 per cent buffer builds
        # exactly that case on this fixture, which is why the fixture above
        # states no buffer. R comes out 0.0 rather than null or infinite: the
        # cash is zero, so the tiny denominator divides a zero.
        summary = self._not_positive(run_config=_cross_config())
        self.assertEqual(summary["r_multiple"]["denominator"]["value"], 7.105427357601002e-15)
        self.assertEqual(summary["r_multiple"]["value"], 0.0)
        self.assertEqual(summary["pnl_cash"]["value"], 0.0)

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
        self.assertEqual(len(envelope.DIVERGENCES), 4)

    def test_the_retired_entry_is_in_neither_the_registry_nor_the_table(self) -> None:
        # ``cost_gate_prices_the_account_currency`` retired 2026-10-02 with
        # #1592: an entry here names a fact the replay LACKS, and the
        # instrument's currency is now stated. Named rather than merely absent,
        # because the equality above would also pass if the row were RENAMED --
        # and section 5 forbids that, since what remains of the difference is a
        # recorded scope cut rather than a missing fact.
        retired = "cost_gate_prices_the_account_currency"
        self.assertNotIn(retired, envelope.DIVERGENCES)
        self.assertNotIn(retired, _spec_divergence_names())
        # And the spec RECORDS the retirement rather than merely dropping the
        # row: section 5.2 says an entry is added by editing the section, and
        # the same rule has to hold in the other direction or a row can vanish
        # with nobody writing down what stopped being reported.
        spec = SPEC.read_text(encoding="utf-8")
        paragraph = spec[spec.index(f"`{retired}` was added") :][:400]
        self.assertIn("RETIRED", paragraph)

    def test_every_name_carries_the_reason_the_replay_lacks_the_fact(self) -> None:
        for name, why in envelope.DIVERGENCES.items():
            with self.subTest(name):
                self.assertTrue(why.strip())


class DivergencePredicateTest(unittest.TestCase):
    """One document that fires each name and one that does not. The predicates
    are transcribed from the section 5.2 table, so a wrong one is a report that
    does not describe the run."""

    def _names(self, name: str, *, run_config: RunConfig | None = None, **patch: Any) -> list[str]:
        return _built(name, TWO_BARS, run_config=run_config, **patch)["divergences"]

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

    def test_a_non_empty_ladder_reports_the_observation_time(self) -> None:
        names = self._names("pullback-trailing-stop")
        self.assertIn("take_profit_observation_time", names)

    def test_an_empty_ladder_reports_neither(self) -> None:
        document = _document("pullback-trailing-stop")
        document["spec"]["tp_tranches"] = []
        admitted = admit(document)
        config = _cross_config()
        plan = interpret(admitted.intent, admitted.document, fx=config.costs.fx)
        names = envelope.divergences(plan, config)
        self.assertNotIn("take_profit_observation_time", names)

    def test_switching_the_minimum_commission_off_changes_no_entry(self) -> None:
        # It used to: the retired currency entry fired only when the per-fill
        # minimum applied, because that is the arm where the two thresholds
        # disagree. Kept as the OTHER half of that retirement -- the flag now
        # reaches the cost gate and nothing else.
        document = _document("pullback-trailing-stop")
        admitted = admit(document)
        plan = interpret(admitted.intent, admitted.document, fx=_cross_config().costs.fx)
        block = copy.deepcopy(dict(CANONICAL))
        block["costs"] = {**block["costs"], "min_commission_applies": False}
        off = RunConfig.from_jsonable(block, account_currency=ACCOUNT_CURRENCY)
        self.assertEqual(
            envelope.divergences(plan, off), envelope.divergences(plan, _cross_config())
        )

    def test_a_stated_distance_reports_the_trail_as_the_BROKERS_model(self) -> None:
        # The predicate is "the run STATES an entry-trail distance". Until the
        # walk modelled the trail, every stated distance was refused, so the
        # name was registered and NO accepted configuration could reach it; the
        # test that stood here asserted exactly that gap. The gap is closed, so
        # the assertion inverts: a run that states a distance reports the name.
        self.assertIn("native_entry_trail_is_a_broker_model", envelope.DIVERGENCES)
        self.assertIn(
            "native_entry_trail_is_a_broker_model",
            self._names("pullback-trailing-stop", run_config=_config(entry_trail_bps=50)),
        )

    def test_a_run_that_states_no_distance_does_not_report_it(self) -> None:
        # The other half of this class's pattern: one document that fires the
        # name and one that does not. The canonical block states null.
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


def _spec_fx_block() -> dict[str, Any]:
    """The TOP-LEVEL `fx` block of the section 5 example, read out of the spec."""
    text = SPEC.read_text(encoding="utf-8")
    start = text.index('  "fx": {\n    "applies"')
    end = text.index('\n  "divergences"', start)
    return json.loads("{" + text[start:end].rstrip().rstrip(",") + "}")["fx"]


class DerivedFxBlockTest(unittest.TestCase):
    """What the conversion DID, which the echoed configuration cannot say (#1592).

    Two derived facts and no third: the flag a caller used to state, and the
    spend in the instrument's currency. They are a TOP-LEVEL block rather than
    keys inside the echoed `fx` because that echo has to round-trip through
    `RunConfig.from_jsonable`, which refuses a derived key on input exactly as
    the arming door does.
    """

    def test_the_block_carries_the_two_derived_keys_in_order(self) -> None:
        built = _built("pullback-trailing-stop", TWO_BARS)
        self.assertEqual(list(built["fx"]), ["applies", "notional_spent"])

    def test_the_flag_is_the_comparison_of_the_two_codes(self) -> None:
        cross = _built("pullback-trailing-stop", TWO_BARS)["fx"]
        self.assertTrue(cross["applies"])
        same = _built("pullback-trailing-stop", TWO_BARS, run_config=_config())["fx"]
        self.assertFalse(same["applies"])

    def test_the_derived_notional_is_the_spend_times_the_stated_rate(self) -> None:
        # 891.0 EUR at 1.08 USD per EUR. The whole point of printing it: an
        # inverted rate stays positive and leaves the gate's verdict unchanged
        # above the knee, so this figure is the only place a reader sees the
        # rate's magnitude (section 8.1).
        built = _built("pullback-trailing-stop", TWO_BARS)
        self.assertEqual(built["summary"]["notional_spent"]["value"], 891.0)
        self.assertEqual(built["fx"]["notional_spent"], {"value": 891.0 * 1.08, "unit": "USD"})

    def test_a_same_currency_run_prints_no_derived_notional(self) -> None:
        # The conversion is the identity there, so the account figure beside it
        # already IS the instrument figure and a copy would read as a second
        # measurement.
        built = _built("pullback-trailing-stop", TWO_BARS, run_config=_config())
        self.assertIsNone(built["fx"]["notional_spent"])
        self.assertEqual(built["summary"]["notional_spent"]["value"], 900.0)

    def test_a_run_that_bought_nothing_converts_a_zero(self) -> None:
        # Not null: nothing was spent, and zero in one currency is zero in the
        # other. A null here would read as "no conversion applies".
        built = _built("pullback-trailing-stop", (_bar(WALK_START, 90.0, 91.0, 89.0),))
        self.assertEqual(
            built["fx"], {"applies": True, "notional_spent": {"value": 0.0, "unit": "USD"}}
        )

    def test_the_block_is_the_one_the_spec_example_prints(self) -> None:
        # The example's own numbers, read out of the spec: the document, the
        # bars and the block together have to produce what section 5 publishes,
        # or the example describes a run the code cannot reach.
        printed = _spec_fx_block()
        self.assertEqual(printed["applies"], True)
        self.assertEqual(printed["notional_spent"]["unit"], "USD")
        self.assertAlmostEqual(printed["notional_spent"]["value"], 891.0 * 1.08, places=2)

    def test_the_derived_notional_is_the_notional_the_gate_priced(self) -> None:
        # One arithmetic, two names (``Fx.to_shares`` and
        # ``Fx.in_instrument_currency``). If they ever diverge, the figure the
        # result publishes stops describing the notional the threshold was
        # computed on, and a reader checking the rate would check the wrong one.
        fx = _cross_config().costs.fx
        built = _built("pullback-trailing-stop", TWO_BARS)
        spent = built["summary"]["notional_spent"]["value"]
        self.assertEqual(built["fx"]["notional_spent"]["value"], fx.to_shares(spent))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
