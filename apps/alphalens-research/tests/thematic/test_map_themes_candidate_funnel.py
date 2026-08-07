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
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from alphalens_pipeline.thematic.mapping.theme_mapper import MapperOutcome


def _mcap_from(mcaps):
    """Stub the mcap LOOKUP, not the bracket.

    Listed tickers come back with that market cap; everything else comes back
    unknown, and the real bracket comparison decides. Patching this deeper seam
    (rather than the bracket function itself) keeps these tests exercising the
    filter they describe instead of replacing it with a hand-written answer.
    """
    return lambda ticker, **_: mcaps.get(ticker)


_LOGGER = "alphalens_pipeline.thematic.mapping.orchestrator"


def _catalyst() -> CatalystPayload:
    """The proposal is event-conditioned, so every call carries a catalyst."""
    return CatalystPayload(
        url="https://example.com/e",
        title="Autonomous drone maker wins a USAF award",
        published_at="2026-07-24",
        event_type="contract_award",
        primary_entities=[],
        confidence=0.8,
        second_order_implications=[],
        echo_count=1,
        trigger_url="https://example.com/e",
        trigger_published_at="2026-07-24",
        is_amplified=False,
        template_id=None,
        template_facts=None,
    )


def _proposal(
    *,
    tickers: list[str],
    outcome: MapperOutcome,
    no_candidates_reason: str = "",
) -> dict:
    return {
        "candidates": [{"ticker": t, "confidence": 0.9} for t in tickers],
        "search_keywords": [],
        "outcome": outcome,
        "no_candidates_reason": no_candidates_reason,
    }


class MapThemesCandidateFunnelLoggingTests(unittest.TestCase):
    def _run(self, proposal: dict, in_bracket: list[str], *, level: str = "INFO"):
        with (
            mock.patch.object(
                orchestrator.theme_mapper, "propose_candidates", return_value=proposal
            ),
            mock.patch.object(
                orchestrator.mcap_filter,
                "fetch_mcap",
                side_effect=_mcap_from(dict.fromkeys(in_bracket, 1_000_000_000.0)),
            ),
            self.assertLogs(_LOGGER, level=level) as cm,
        ):
            candidates, _mcap, _keywords, outcome = orchestrator._propose_and_filter_candidates(
                theme="ai_defense",
                catalyst=_catalyst(),
                api_key="k",
                pro_client=None,
                min_cap=500_000_000,
                max_cap=10_000_000_000,
                asof=dt.date(2026, 7, 25),
            )
        return candidates, outcome, "\n".join(cm.output)

    def _propose(self, *, proposed_tickers: list[str], in_bracket: list[str]):
        candidates, _outcome, logs = self._run(
            _proposal(tickers=proposed_tickers, outcome=MapperOutcome.SUCCESS), in_bracket
        )
        return candidates, logs

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

    def test_the_dropped_names_are_listed_with_their_verdict(self):
        # "17 dropped" cannot tell a mega-cap-only answer apart from a yfinance
        # outage; the per-ticker verdict can. Pin the rendered detail suffix.
        _candidates, logs = self._propose(proposed_tickers=["AAA", "BBB"], in_bracket=["AAA"])
        self.assertIn("(1 dropped off-bracket / no mcap: BBB=no_mcap)", logs)

    def test_nothing_dropped_renders_no_detail_suffix(self):
        _candidates, logs = self._propose(proposed_tickers=["AAA"], in_bracket=["AAA"])
        self.assertIn("(0 dropped off-bracket / no mcap)", logs)

    def test_more_dropped_than_the_cap_are_summarised_with_an_overflow_count(self):
        proposed = [f"T{i:02d}" for i in range(orchestrator._MAX_LOGGED_DROPPED_TICKERS + 3)]
        _candidates, logs = self._propose(proposed_tickers=proposed, in_bracket=[])
        self.assertIn(f"{proposed[orchestrator._MAX_LOGGED_DROPPED_TICKERS - 1]}=no_mcap", logs)
        self.assertNotIn(f"{proposed[orchestrator._MAX_LOGGED_DROPPED_TICKERS]}=no_mcap", logs)
        self.assertIn("(+3 more)", logs)

    def test_a_decline_names_the_model_reason_in_the_funnel_line(self):
        # Issue #982. A decline is a judgement; the funnel must say so AND carry
        # the model's own words, so no second log line has to be cross-referenced.
        candidates, outcome, logs = self._run(
            _proposal(
                tickers=[],
                outcome=MapperOutcome.DECLINED,
                no_candidates_reason="a one-time litigation payout with no transmission channel",
            ),
            [],
        )
        self.assertEqual(candidates, [])
        self.assertEqual(outcome, MapperOutcome.DECLINED)
        self.assertIn("declined", logs.lower())
        self.assertIn("one-time litigation payout", logs)

    def test_a_failure_is_logged_at_warning_and_is_not_worded_as_a_decline(self):
        # The theme was LOST, not judged. It must be greppable apart from a
        # decline and must clear a WARNING-level journal filter.
        candidates, outcome, logs = self._run(
            _proposal(tickers=[], outcome=MapperOutcome.EMPTY_PAYLOAD), [], level="WARNING"
        )
        self.assertEqual(candidates, [])
        self.assertEqual(outcome, MapperOutcome.EMPTY_PAYLOAD)
        self.assertIn("WARNING", logs)
        self.assertIn("empty_payload", logs)
        self.assertNotIn("declined", logs.lower())

    def test_a_decline_and_a_failure_do_not_produce_the_same_funnel_line(self):
        # The whole point of #982: before the fix these two rendered identically.
        _c, _o, declined_logs = self._run(
            _proposal(
                tickers=[], outcome=MapperOutcome.DECLINED, no_candidates_reason="no channel"
            ),
            [],
        )
        _c2, _o2, failed_logs = self._run(
            _proposal(tickers=[], outcome=MapperOutcome.EMPTY_PAYLOAD), []
        )
        self.assertNotEqual(declined_logs, failed_logs)

    def test_every_failure_outcome_is_logged_as_a_failure(self):
        # Positive control against the guard rotting to "only EMPTY_PAYLOAD is a
        # failure": a new member added to the failure side must be covered here.
        for outcome in (
            MapperOutcome.EMPTY_PAYLOAD,
            MapperOutcome.MALFORMED_PAYLOAD,
            MapperOutcome.CALL_FAILED,
        ):
            with self.subTest(outcome=outcome):
                _c, _o, logs = self._run(
                    _proposal(tickers=[], outcome=outcome), [], level="WARNING"
                )
                self.assertIn(outcome.value, logs)

    def test_returns_the_in_bracket_subset_sorted_by_confidence(self):
        # Behaviour-preservation lock: the log addition must not alter WHICH
        # candidates are returned. Result = the in-bracket subset, confidence desc.
        proposal = {
            "candidates": [
                {"ticker": "LOW", "confidence": 0.3},
                {"ticker": "HIGH", "confidence": 0.9},
                {"ticker": "OUT", "confidence": 0.8},  # dropped by mcap
                {"ticker": "MID", "confidence": 0.6},
            ],
            "search_keywords": [],
            "outcome": MapperOutcome.SUCCESS,
            "no_candidates_reason": "",
        }
        with (
            mock.patch.object(
                orchestrator.theme_mapper, "propose_candidates", return_value=proposal
            ),
            mock.patch.object(
                orchestrator.mcap_filter,
                "fetch_mcap",
                side_effect=_mcap_from(
                    {
                        "HIGH": 1_000_000_000.0,
                        "MID": 2_000_000_000.0,
                        "LOW": 3_000_000_000.0,
                    }
                ),
            ),
        ):
            candidates, in_bracket, _keywords, _outcome = (
                orchestrator._propose_and_filter_candidates(
                    theme="ai_defense",
                    catalyst=_catalyst(),
                    api_key="k",
                    pro_client=None,
                    min_cap=500_000_000,
                    max_cap=10_000_000_000,
                    asof=dt.date(2026, 7, 25),
                )
            )
        # OUT is filtered (not in bracket); the rest are sorted by confidence desc.
        self.assertEqual([c["ticker"] for c in candidates], ["HIGH", "MID", "LOW"])
        self.assertEqual(set(in_bracket), {"HIGH", "MID", "LOW"})


if __name__ == "__main__":
    unittest.main()
