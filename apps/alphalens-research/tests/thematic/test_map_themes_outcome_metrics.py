"""``map_themes`` counts declines and mapper failures separately (issue #982).

A rising mapper-failure rate must be visible without reading logs. Since the
event-conditioned prompt made "0 candidates" a legitimate answer, the existing
stage-volume gauges cannot carry the signal: a day where every theme was LOST
and a day where every theme was correctly declined both emit ``output_rows=0``.

``map_themes`` therefore counts the two apart and hands them to the CLI through
``df.attrs`` — the same channel ``dropped_total`` already uses.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.mapping import orchestrator
from alphalens_pipeline.thematic.mapping.theme_mapper import MapperOutcome

from tests.thematic.mapping_stubs import stub_assessor, theme_proposal

from .test_theme_mapping import _catalyst_payload

ASOF = dt.date(2026, 8, 2)


def _survivor_row(theme: str, ticker: str) -> dict:
    return {
        "theme": theme,
        "ticker": ticker,
        "company_name": f"{ticker} Inc",
        "rationale": "stub",
        "llm_confidence": 0.7,
        "market_cap": 1_000_000_000,
        "n_gates_passed": 1,
        "verified": True,
    }


class MapThemesOutcomeCountTests(unittest.TestCase):
    def setUp(self) -> None:
        # Stage B calls OpenRouter once per in-bracket candidate; without
        # this stub these tests hit the live API.
        stub_assessor(self)

    def _run(self, per_theme: dict[str, MapperOutcome], *, catalyst_for=None) -> pd.DataFrame:
        """Drive ``map_themes`` with one scripted mapper outcome per theme."""

        def _propose(*, theme, **_kwargs):
            outcome = per_theme[theme]
            candidates = (
                [{"ticker": f"{theme.upper()}X", "confidence": 0.9}]
                if outcome is MapperOutcome.SUCCESS
                else []
            )
            mcap = {c["ticker"]: 1_000_000_000.0 for c in candidates}
            return theme_proposal(proposed=candidates, in_bracket=mcap, outcome=outcome)

        def _verify(*, theme, candidates, **_kwargs):
            return ([_survivor_row(theme, c["ticker"]) for c in candidates], 0, 0)

        def _catalyst(theme, _asof, _cache):
            if catalyst_for is not None and theme not in catalyst_for:
                return None
            return _catalyst_payload()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(orchestrator, "_init_pro_client", return_value=object()),
                patch.object(orchestrator, "_fetch_press_window", return_value=pd.DataFrame()),
                patch.object(orchestrator, "_resolve_catalyst", side_effect=_catalyst),
                patch.object(orchestrator, "_propose_and_bracket", side_effect=_propose),
                patch.object(orchestrator, "_verify_candidates_for_theme", side_effect=_verify),
                patch.object(orchestrator.proposal_shadow, "write_proposal_shadow"),
            ):
                return orchestrator.map_themes(
                    themes=list(per_theme),
                    asof=ASOF,
                    api_key="dummy",
                    output_dir=Path(tmp),
                    rebuild=True,
                )

    def test_declines_and_failures_are_counted_apart(self):
        df = self._run(
            {
                "good": MapperOutcome.SUCCESS,
                "quiet": MapperOutcome.DECLINED,
                "lost": MapperOutcome.EMPTY_PAYLOAD,
                "broken": MapperOutcome.CALL_FAILED,
            }
        )
        self.assertEqual(df.attrs["themes_declined"], 1)
        self.assertEqual(df.attrs["themes_failed"], 2)

    def test_a_clean_run_reports_zero_of_each(self):
        # Positive control for the counters: they must be able to read 0, so a
        # test that only ever asserts "> 0" cannot pass on a stuck constant.
        df = self._run({"good": MapperOutcome.SUCCESS})
        self.assertEqual(df.attrs["themes_declined"], 0)
        self.assertEqual(df.attrs["themes_failed"], 0)

    def test_every_failure_outcome_lands_in_the_failed_counter(self):
        df = self._run(
            {
                "a": MapperOutcome.EMPTY_PAYLOAD,
                "b": MapperOutcome.MALFORMED_PAYLOAD,
                "c": MapperOutcome.CALL_FAILED,
            }
        )
        self.assertEqual(df.attrs["themes_failed"], 3)
        self.assertEqual(df.attrs["themes_declined"], 0)

    def test_a_theme_skipped_for_no_catalyst_counts_as_neither(self):
        # The mapper was never called, so the theme is not a decline and not a
        # mapper failure. Folding it into either would make the failure gauge
        # track catalyst-resolution quality instead of mapper health.
        df = self._run(
            {"mapped": MapperOutcome.SUCCESS, "skipped": MapperOutcome.SUCCESS},
            catalyst_for={"mapped"},
        )
        self.assertEqual(df.attrs["themes_declined"], 0)
        self.assertEqual(df.attrs["themes_failed"], 0)

    def test_a_reused_frozen_set_reports_zero_of_each(self):
        # The freeze path makes NO LLM calls, so there is nothing to count. The
        # keys must still be present: the CLI reads them on every run and a
        # missing gauge is itself an alertable condition.
        per_theme = {"good": MapperOutcome.SUCCESS}

        def _propose(*, theme, **_kwargs):
            return theme_proposal(
                proposed=[{"ticker": "GOODX", "confidence": 0.9}],
                in_bracket={"GOODX": 1e9},
                outcome=per_theme[theme],
            )

        def _verify(*, theme, candidates, **_kwargs):
            return ([_survivor_row(theme, c["ticker"]) for c in candidates], 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (
                patch.object(orchestrator, "_init_pro_client", return_value=object()),
                patch.object(orchestrator, "_fetch_press_window", return_value=pd.DataFrame()),
                patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst_payload()),
                patch.object(orchestrator, "_propose_and_bracket", side_effect=_propose) as propose,
                patch.object(orchestrator, "_verify_candidates_for_theme", side_effect=_verify),
                patch.object(orchestrator.proposal_shadow, "write_proposal_shadow"),
            ):
                first = orchestrator.map_themes(
                    themes=["good"], asof=ASOF, api_key="dummy", output_dir=out, rebuild=True
                )
                self.assertFalse(first.empty)
                calls_after_build = propose.call_count
                frozen = orchestrator.map_themes(
                    themes=["good"], asof=ASOF, api_key="dummy", output_dir=out
                )
                # Without this the 0/0 below proves nothing: a RECOMPUTE of a
                # SUCCESS theme also reports 0 declines and 0 failures, so the
                # counts alone cannot tell the frozen path from a rebuild.
                self.assertEqual(propose.call_count, calls_after_build)
        self.assertEqual(frozen.attrs["themes_declined"], 0)
        self.assertEqual(frozen.attrs["themes_failed"], 0)


if __name__ == "__main__":
    unittest.main()
