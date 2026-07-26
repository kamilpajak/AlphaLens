"""The map-themes candidate funnel is logged per theme.

The mcap stage is otherwise SILENT when it drops every candidate (nothing reaches
the later kept/dropped log). That is exactly how the 2026-07-25 incident collapsed
a whole day's briefs to zero candidates invisibly: a yfinance rate-limit made the
PIT mcap lookup return nothing, so every LLM-proposed candidate fell out of the
bracket with no trace. These tests pin the per-theme funnel line so a mass drop is
diagnosable at a glance.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.thematic.mapping import orchestrator

_LOGGER = "alphalens_pipeline.thematic.mapping.orchestrator"


class MapThemesCandidateFunnelLoggingTests(unittest.TestCase):
    def _propose(self, *, proposed_tickers: list[str], in_bracket: list[str]):
        proposal = {
            "candidates": [{"ticker": t, "confidence": 0.9} for t in proposed_tickers],
            "search_keywords": [],
        }
        with (
            mock.patch.object(
                orchestrator.theme_mapper, "propose_candidates", return_value=proposal
            ),
            mock.patch.object(
                orchestrator.mcap_filter,
                "filter_by_mcap",
                return_value=dict.fromkeys(in_bracket, 1_000_000_000.0),
            ),
            self.assertLogs(_LOGGER, level="INFO") as cm,
        ):
            candidates, _mcap, _keywords = orchestrator._propose_and_filter_candidates(
                theme="ai_defense",
                api_key="k",
                pro_client=None,
                min_cap=500_000_000,
                max_cap=10_000_000_000,
                asof=dt.date(2026, 7, 25),
            )
        return candidates, "\n".join(cm.output)

    def test_logs_the_funnel_when_some_candidates_drop(self):
        candidates, logs = self._propose(proposed_tickers=["AAA", "BBB", "CCC"], in_bracket=["AAA"])
        self.assertEqual([c["ticker"] for c in candidates], ["AAA"])
        self.assertIn("proposed 3, in mcap bracket 1 (2 dropped", logs)

    def test_logs_the_total_mcap_collapse(self):
        # The 2026-07-25 incident: candidates proposed but the mcap lookup returned
        # nothing, so ALL dropped. This must be visible, not silent.
        candidates, logs = self._propose(proposed_tickers=["AAA", "BBB"], in_bracket=[])
        self.assertEqual(candidates, [])
        self.assertIn("proposed 2, in mcap bracket 0 (2 dropped", logs)

    def test_logs_when_the_llm_proposes_nothing(self):
        candidates, logs = self._propose(proposed_tickers=[], in_bracket=[])
        self.assertEqual(candidates, [])
        self.assertIn("proposed 0 (LLM returned no candidate)", logs)


if __name__ == "__main__":
    unittest.main()
