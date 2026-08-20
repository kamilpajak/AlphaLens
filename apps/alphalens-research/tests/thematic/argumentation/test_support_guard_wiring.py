"""The guard withholds PROSE. It never withholds a ROW.

The retrospective's lesson is that deleting candidates on an LLM judgement
backfired: the gate's keep/refuse decision ran the wrong way against realised
returns and crowded out 96% of the small/mid-cap tickers it touched. So the
honesty repair must not become a second deletion gate wearing new words.

When the guard trips, the ladder is: log at WARNING, regenerate ONCE at
temperature 0 (never a Python rewrite — an inserted "may" would fabricate
hedging the model never reasoned about and destroy the audit trail), and if the
second draw also violates, keep the row and withhold only the four prose
strings through the EXISTING graceful-degradation path.

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
