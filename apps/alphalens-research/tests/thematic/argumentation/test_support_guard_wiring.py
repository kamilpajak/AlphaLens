"""The guard withholds PROSE. It never withholds a ROW.

The retrospective's lesson is that deleting candidates on an LLM judgement
backfired: the gate's keep/refuse decision ran the wrong way against realised
returns and crowded out 96% of the small/mid-cap tickers it touched. So the
honesty repair must not become a second deletion gate wearing new words.

When the guard trips, the ladder is: log at WARNING, re-roll at temperature 0
(never a Python rewrite — an inserted "may" would fabricate hedging the model
never reasoned about and destroy the audit trail), and if the next draw also
violates, keep the row and withhold only the four prose strings through the
EXISTING graceful-degradation path. The re-roll shares the brief's single retry
budget, so the stamped status has to say how many draws the guard actually saw
— ``clean`` may never absorb a fire, nor a row that produced no prose at all.

``test_a_twice_violating_row_still_ships_with_its_prose_withheld`` and
``test_the_row_count_is_identical_across_every_guard_outcome`` are the positive
controls for that: they fail loudly the moment the withholding becomes a drop.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.argumentation import (
    generator,
    support_guard,
)
from alphalens_pipeline.thematic.argumentation import (
    orchestrator as brief_orchestrator,
)

_ASOF = dt.date(2026, 8, 20)

_BENEFIT_BRIEF = {
    "tldr": "QUBT benefits from the theme and is positioned to win share.",
    "supply_chain_reasoning": "Datacenter capex boosts demand for its hardware.",
    "bear_summary": "P/S 30 sits in the 1st sector percentile.",
    "catalyst_failure_exit": "Exit if a competitor announces a comparable product.",
}

_CLEAN_BRIEF = {
    "tldr": "QUBT surfaced from the round-up; no company-specific cash-flow path "
    "from that event to this company was established.",
    "supply_chain_reasoning": "The event names no link to this company.",
    "bear_summary": "P/S 30 sits in the 1st sector percentile.",
    "catalyst_failure_exit": "Exit if no further event ties this company to the "
    "theme within the setup horizon.",
}


# Every required field blank -> EMPTY_CONTENT, a terminal kind that is NOT the
# guard's. Used to script "the guard fired, then the re-roll died for another
# reason".
_BLANK_BRIEF = {
    "tldr": "",
    "supply_chain_reasoning": "",
    "bear_summary": "",
    "catalyst_failure_exit": "",
}

# Compliant prose that nonetheless TRIPS a lexicon entry and is then suppressed:
# the conditional escape hatch the contract asks for at a missing link.
_HEDGED_BRIEF = {
    **_CLEAN_BRIEF,
    "supply_chain_reasoning": "If the reported contract is confirmed, QUBT "
    "benefits from a new customer; the event does not state that link.",
}


def _scored_row(**over) -> dict:
    row = {
        "ticker": "QUBT",
        "company_name": "Quantum Computing Inc",
        "theme": "quantum_computing",
        "verified": True,
        "layer4_weighted_score": 4,
        "rationale": "Pure-play quantum hardware",
        "gates_passed_str": "tenk",
        "technicals_summary_str": "RSI 60",
        "channel_support_status": "not_established",
        "channel_grounding_status": "grounded",
        "channel_type": "none",
        "channel_text": "",
        "channel_evidence": "",
        "channel_falsifier": "",
        "channel_assessment_outcome": "success",
        "catalyst_event_type": "macro",
    }
    row.update(over)
    return row


# Issue #1070, verbatim: a record graded `suggestive` + `grounded` whose chain
# ends in harm to the candidate. Before the direction arm this row was out of
# scope entirely, so the prose below shipped unchallenged.
_HARM_CHANNEL_OLLI = (
    "As a small-cap retailer, Ollie's could see reduced customer demand, leading "
    "to lower revenue in the near term."
)

# The SAME ticker's real benefit-direction record, from the live store. The two
# differ only in where the chain points, which is the whole discrimination.
_BENEFIT_CHANNEL_OLLI = (
    "The event notes that discount retailers could benefit if inflation drives "
    "trade-down behavior -> consumers shift purchases from higher-priced "
    "retailers toward discounters -> Ollie's Bargain Outlet, as a "
    "closeout/discount retailer, sees increased customer traffic and revenue "
    "from trade-down demand over subsequent quarters."
)

_OLLI_BENEFIT_BRIEF = {
    "tldr": "Ollie's benefits from softer consumer spending and is positioned to win share.",
    "supply_chain_reasoning": "Trade-down behaviour boosts demand for its closeout assortment.",
    "bear_summary": "P/S sits in the 1st sector percentile.",
    "catalyst_failure_exit": "Exit if the trade-down behaviour does not show up in comps.",
}


def _harm_direction_row(**over) -> dict:
    """A `suggestive` + `grounded` row whose record points at HARM."""
    fields = {
        "ticker": "OLLI",
        "company_name": "Ollie's Bargain Outlet Holdings",
        "theme": "consumer_trade_down",
        "channel_support_status": "suggestive",
        "channel_grounding_status": "grounded",
        "channel_type": "customer_demand",
        "channel_text": _HARM_CHANNEL_OLLI,
    }
    fields.update(over)
    return _scored_row(**fields)


def _scored(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class _RunBriefs:
    """Drive the REAL ``generate_briefs`` over scripted LLM response bodies.

    The LLM transport is stubbed, NOT ``generate_brief`` — the guard lives inside
    ``generate_brief``, so stubbing that function would test nothing and would
    pass whatever the guard did.
    """

    @staticmethod
    def run(out_dir: Path, rows: list[dict], drafts: list[dict]):
        bodies = iter(drafts)
        calls: list[str] = []

        def _fake_call_llm(_client, prompt, **_kwargs):
            calls.append(prompt)
            return SimpleNamespace(
                text=json.dumps(next(bodies)),
                candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
            )

        with (
            mock.patch.object(generator, "_call_llm", side_effect=_fake_call_llm),
            mock.patch.object(generator, "_resolve_llm_client", return_value=object()),
            mock.patch.object(
                brief_orchestrator, "_build_clients", return_value=(object(), object())
            ),
            mock.patch.object(
                brief_orchestrator, "_enrich_facts_with_earnings", side_effect=lambda f, _a: f
            ),
            mock.patch.object(brief_orchestrator, "_enrich_event_titles", side_effect=lambda d: d),
        ):
            df = brief_orchestrator.generate_briefs(
                _scored(rows),
                asof=_ASOF,
                output_dir=out_dir,
                ohlcv_loader=lambda *_a: pd.DataFrame(),
            )
        return df, calls


class TheChannelRecordReachesThePrompt(unittest.TestCase):
    def test_the_facts_projection_carries_the_record(self):
        facts = brief_orchestrator._row_to_facts(pd.Series(_scored_row()))
        self.assertEqual(facts["causal_support"], "not_established")
        self.assertEqual(facts["channel_grounding"], "grounded")
        self.assertEqual(facts["catalyst_event_type"], "macro")

    def test_a_failed_assessment_projects_no_record_not_a_level(self):
        # An outage carries the LOWEST support level by construction. Projecting
        # it as `not_established` would make the model write "no path was
        # established" — asserting a judgement no model ever made.
        facts = brief_orchestrator._row_to_facts(
            pd.Series(_scored_row(channel_assessment_outcome="call_failed"))
        )
        self.assertEqual(facts["causal_support"], support_guard.NO_RECORD)

    def test_a_row_predating_the_columns_projects_no_record(self):
        row = _scored_row()
        for key in (
            "channel_support_status",
            "channel_grounding_status",
            "channel_assessment_outcome",
        ):
            row.pop(key)
        facts = brief_orchestrator._row_to_facts(pd.Series(row))
        self.assertEqual(facts["causal_support"], support_guard.NO_RECORD)

    def test_instrument_telemetry_is_not_projected(self):
        facts = brief_orchestrator._row_to_facts(pd.Series(_scored_row()))
        for key in ("channel_confidence", "channel_vote_k", "channel_support_dispersion"):
            self.assertNotIn(key, facts)


class TheGuardRegeneratesOnceThenWithholdsProse(unittest.TestCase):
    def test_a_violating_first_draw_is_regenerated_once_and_ships_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            df, calls = _RunBriefs.run(Path(tmp), [_scored_row()], [_BENEFIT_BRIEF, _CLEAN_BRIEF])
        self.assertEqual(len(calls), 2, "exactly one regeneration, never a rewrite")
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["brief_support_guard_status"], "repaired")
        self.assertEqual(df.iloc[0]["brief_status"], "ok")
        self.assertIn("no company-specific cash-flow path", df.iloc[0]["brief_tldr"])

    def test_a_twice_violating_row_still_ships_with_its_prose_withheld(self):
        """POSITIVE CONTROL 2 — the wiring cannot rot into a drop."""
        with tempfile.TemporaryDirectory() as tmp:
            df, _calls = _RunBriefs.run(
                Path(tmp), [_scored_row()], [_BENEFIT_BRIEF, _BENEFIT_BRIEF]
            )
        self.assertEqual(len(df), 1, "the ROW ships; only the prose is withheld")
        row = df.iloc[0]
        self.assertEqual(row["brief_status"], "unavailable")
        self.assertEqual(row["brief_error_kind"], "unsupported_benefit_claim")
        self.assertEqual(row["brief_support_guard_status"], "withheld")
        self.assertIsNone(row["brief_tldr"])
        # The deterministic half of the card is untouched — the 2026-05-17 QUBT
        # graceful-degradation contract.
        self.assertTrue(row["brief_trade_setup"])
        self.assertEqual(row["ticker"], "QUBT")

    def test_a_clean_first_draw_is_not_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            df, calls = _RunBriefs.run(Path(tmp), [_scored_row()], [_CLEAN_BRIEF])
        self.assertEqual(len(calls), 1)
        self.assertEqual(df.iloc[0]["brief_support_guard_status"], "clean")

    def test_an_established_row_is_not_applicable_and_never_regenerated(self):
        # Scope control at the wiring level: benefit prose on a well-grounded
        # row is not the guard's business.
        with tempfile.TemporaryDirectory() as tmp:
            df, calls = _RunBriefs.run(
                Path(tmp),
                [_scored_row(channel_support_status="established", channel_type="customer_demand")],
                [_BENEFIT_BRIEF],
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(df.iloc[0]["brief_support_guard_status"], "not_applicable")
        self.assertEqual(df.iloc[0]["brief_status"], "ok")

    def test_a_non_grounded_row_is_in_scope_even_at_established(self):
        with tempfile.TemporaryDirectory() as tmp:
            df, calls = _RunBriefs.run(
                Path(tmp),
                [
                    _scored_row(
                        channel_support_status="established",
                        channel_grounding_status="theme_misroute",
                    )
                ],
                [_BENEFIT_BRIEF, _BENEFIT_BRIEF],
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(df.iloc[0]["brief_support_guard_status"], "withheld")
        self.assertEqual(len(df), 1)

    def test_the_row_count_is_identical_across_every_guard_outcome(self):
        cases = {
            "clean": [_CLEAN_BRIEF],
            "repaired": [_BENEFIT_BRIEF, _CLEAN_BRIEF],
            "withheld": [_BENEFIT_BRIEF, _BENEFIT_BRIEF],
        }
        counts = set()
        for name, drafts in cases.items():
            with self.subTest(outcome=name), tempfile.TemporaryDirectory() as tmp:
                df, _calls = _RunBriefs.run(Path(tmp), [_scored_row()], drafts)
                counts.add(len(df))
        self.assertEqual(counts, {1})

    def test_the_guard_columns_are_stamped_on_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            df, _calls = _RunBriefs.run(Path(tmp), [_scored_row()], [_CLEAN_BRIEF])
        for column in brief_orchestrator._SUPPORT_GUARD_COLUMNS:
            self.assertIn(column, df.columns)
        self.assertEqual(
            df.iloc[0]["brief_support_guard_version"], support_guard.SUPPORT_GUARD_VERSION
        )
        self.assertEqual(df.iloc[0]["brief_causal_support"], "not_established")
        self.assertEqual(df.iloc[0]["brief_channel_grounding"], "grounded")

    def test_the_empty_day_schema_lists_every_guard_column(self):
        # A quiet day's parquet must not be a different shape from a normal
        # day's — the defect the map-side schema test already guards.
        for column in brief_orchestrator._SUPPORT_GUARD_COLUMNS:
            self.assertIn(column, brief_orchestrator._EMPTY_OUT_COLUMNS)

    def test_no_guard_column_is_a_brief_sort_key(self):
        keys = [k for k, *_rest in brief_orchestrator._BRIEF_SORT_KEYS]
        planted = ["brief_support_guard_status", "brief_causal_support"]
        self.assertFalse(
            [k for k in keys if k.startswith(("brief_support_guard_", "brief_causal_support"))]
        )
        # Positive control: the prefix test would catch a planted key.
        self.assertTrue(
            [k for k in planted if k.startswith(("brief_support_guard_", "brief_causal_support"))]
        )


class AHarmDirectionRecordIsWithheldAndStampedAsSuchEndToEnd(unittest.TestCase):
    """POSITIVE CONTROL 3 — the direction arm cannot rot into a no-op.

    The row is `suggestive` + `grounded`, which was OUT OF SCOPE before issue
    #1070, and its own channel record ends in "lower revenue". Benefit prose
    beside it contradicts the row's own record, so it must be re-rolled once and
    then withheld — while the ROW ships exactly as every other outcome does.
    """

    def _run(self, drafts):
        with tempfile.TemporaryDirectory() as tmp:
            return _RunBriefs.run(Path(tmp), [_harm_direction_row()], drafts)

    def test_a_suggestive_grounded_harm_record_withholds_benefit_prose(self):
        df, calls = self._run([_OLLI_BENEFIT_BRIEF, _OLLI_BENEFIT_BRIEF])
        self.assertEqual(len(calls), 2, "exactly one regeneration, never a rewrite")
        self.assertEqual(len(df), 1, "the ROW ships; only the prose is withheld")
        row = df.iloc[0]
        self.assertEqual(row["brief_error_kind"], "unsupported_benefit_claim")
        self.assertIsNone(row["brief_tldr"])
        self.assertTrue(row["brief_trade_setup"])

    def test_the_stamped_status_agrees_with_the_withheld_prose(self):
        """The two `guard_applies` call sites must move together.

        The generator decides whether to SCAN, and the orchestrator re-derives
        the same question to decide what to STAMP. Teaching only the generator
        about direction would ship a row whose `brief_error_kind` says the guard
        withheld the prose while `brief_support_guard_status` says the guard
        never ran — a contradiction inside one row, and no other test would see
        it because every other fixture carries an empty `channel_text`.
        """
        df, _calls = self._run([_OLLI_BENEFIT_BRIEF, _OLLI_BENEFIT_BRIEF])
        row = df.iloc[0]
        self.assertEqual(row["brief_support_guard_status"], "withheld")
        self.assertEqual(row["brief_causal_support"], "suggestive")
        self.assertEqual(row["brief_channel_grounding"], "grounded")
        self.assertGreaterEqual(int(row["brief_support_guard_violations"]), 1)

    def test_the_same_row_with_a_benefit_record_stays_not_applicable(self):
        """Anti-inertness control in the other direction.

        Same ticker, same prose, same support level — only the record's
        DIRECTION differs. If this ever reports `withheld`, the arm has stopped
        discriminating and is withholding honest prose.
        """
        with tempfile.TemporaryDirectory() as tmp:
            df, calls = _RunBriefs.run(
                Path(tmp),
                [_harm_direction_row(channel_text=_BENEFIT_CHANNEL_OLLI)],
                [_OLLI_BENEFIT_BRIEF],
            )
        self.assertEqual(len(calls), 1, "a benefit record must not cost a re-roll")
        self.assertEqual(df.iloc[0]["brief_support_guard_status"], "not_applicable")
        self.assertEqual(df.iloc[0]["brief_status"], "ok")


class TheGuardStatusNeverReportsAFireAsClean(unittest.TestCase):
    """`clean` must mean "prose was produced and it complied" — nothing else.

    The whole authorisation for this detector is DETECT-STAMP-KEEP-and-MEASURE,
    and a later pre-registered decision about whether it may gate anything will
    read these counters. Two paths merged a fire, or a row that produced no
    prose at all, into `clean`; both bias the compliance rate in the optimistic
    direction, which is the direction that would wrongly argue the detector is
    accurate.
    """

    def test_a_first_draw_fire_survives_a_retry_that_dies_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            df, calls = _RunBriefs.run(Path(tmp), [_scored_row()], [_BENEFIT_BRIEF, _BLANK_BRIEF])
        self.assertEqual(len(calls), 2)
        row = df.iloc[0]
        self.assertEqual(row["brief_support_guard_status"], "fired_unrecovered")
        self.assertGreaterEqual(int(row["brief_support_guard_violations"]), 1)
        self.assertTrue(row["brief_support_guard_spans_json"])
        # The row still ships, and the terminal kind is the RETRY's — the guard
        # status carries the guard's own fact, it does not overwrite the LLM's.
        self.assertEqual(len(df), 1)
        self.assertEqual(row["brief_error_kind"], "empty_content")

    def test_a_row_that_never_produced_prose_is_not_reported_as_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            df, _calls = _RunBriefs.run(Path(tmp), [_scored_row()], [_BLANK_BRIEF, _BLANK_BRIEF])
        row = df.iloc[0]
        self.assertEqual(row["brief_support_guard_status"], "no_prose")
        self.assertEqual(int(row["brief_support_guard_violations"]), 0)

    def test_clean_still_means_scanned_and_compliant(self):
        """Anti-inertness control: the two new statuses must not swallow `clean`."""
        with tempfile.TemporaryDirectory() as tmp:
            df, _calls = _RunBriefs.run(Path(tmp), [_scored_row()], [_CLEAN_BRIEF])
        self.assertEqual(df.iloc[0]["brief_support_guard_status"], "clean")

    def test_every_non_guard_terminal_kind_reports_no_prose(self):
        facts = {"causal_support": "not_established", "channel_grounding": "grounded"}
        for kind in (
            generator.BriefErrorKind.TRANSPORT,
            generator.BriefErrorKind.TRUNCATED,
            generator.BriefErrorKind.MALFORMED_JSON,
            generator.BriefErrorKind.SAFETY,
            generator.BriefErrorKind.EMPTY,
            generator.BriefErrorKind.EMPTY_CONTENT,
            generator.BriefErrorKind.LANGUAGE_DRIFT,
            generator.BriefErrorKind.SECTION_COLLAPSE,
        ):
            with self.subTest(kind=kind.value):
                outcome = brief_orchestrator._guard_outcome(
                    facts, brief=None, terminal_kind=kind, violations=[]
                )
                self.assertEqual(outcome.status, "no_prose")

    def test_the_suppressed_matches_are_stamped_beside_the_fired_ones(self):
        """The near-miss column: a suppressor that misfires must be visible.

        Without it a suppressed match is indistinguishable from no match at
        all, and the first weeks would have to trust the suppressors rather
        than read them.
        """
        with tempfile.TemporaryDirectory() as tmp:
            df, _calls = _RunBriefs.run(Path(tmp), [_scored_row()], [_HEDGED_BRIEF])
        row = df.iloc[0]
        self.assertEqual(row["brief_support_guard_status"], "clean")
        self.assertEqual(int(row["brief_support_guard_violations"]), 0)
        self.assertGreaterEqual(int(row["brief_support_guard_suppressed"]), 1)


class TheFiredGaugeCountsEveryFire(unittest.TestCase):
    """`..._fired_total` is the only counter that answers "how often is the
    prose contract being broken", so a fire it cannot see biases the read."""

    @staticmethod
    def _metrics(statuses: list[str]) -> dict[str, int]:
        from alphalens_cli.commands.thematic import _support_guard_metrics

        return _support_guard_metrics(pd.DataFrame({"brief_support_guard_status": statuses}))

    def test_an_unrecovered_fire_counts_as_fired(self):
        metrics = self._metrics(["fired_unrecovered"])
        self.assertEqual(metrics["alphalens_thematic_brief_support_guard_fired_total"], 1)

    def test_fired_is_the_sum_of_every_fire_shape(self):
        metrics = self._metrics(["repaired", "withheld", "fired_unrecovered", "clean", "no_prose"])
        self.assertEqual(metrics["alphalens_thematic_brief_support_guard_fired_total"], 3)

    def test_a_row_with_no_prose_is_not_a_fire(self):
        metrics = self._metrics(["no_prose", "clean", "not_applicable"])
        self.assertEqual(metrics["alphalens_thematic_brief_support_guard_fired_total"], 0)


class TheErrorKindJoinsTheSingleRerollSet(unittest.TestCase):
    def test_the_kind_exists_and_names_itself(self):
        self.assertEqual(
            generator.BriefErrorKind.UNSUPPORTED_BENEFIT_CLAIM.value,
            "unsupported_benefit_claim",
        )

    def test_it_is_distinct_from_language_drift(self):
        self.assertIsNot(
            generator.BriefErrorKind.UNSUPPORTED_BENEFIT_CLAIM,
            generator.BriefErrorKind.LANGUAGE_DRIFT,
        )


if __name__ == "__main__":
    unittest.main()
