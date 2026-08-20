"""The resolved catalyst reaches the ticker-proposal LLM call.

Before this, the proposal call received ONLY a bare theme slug ("harassment",
"supreme_court") and the article was attached afterwards as the candidate's
provenance. That produces the appearance of grounding with no causal
dependence: the model estimates P(company | topic) where the pipeline needs
P(material impact | event, company). Measured over 45 days / 397 (event,
ticker) pairs, a large share of candidates had no transmission channel from
the event to the company's economics at all — Sturm Ruger on an Apple/Epic
Supreme Court headline, Lyft on an eBay harassment prosecution.

The catalyst was already a local variable one frame above the proposal call
(``_rows_for_theme`` resolves it and hard-returns when there is none), so the
whole fix is a parameter chain. These tests pin that chain end to end:
``_rows_for_theme`` -> ``_propose_and_bracket`` -> ``_assess_channels_for_theme`` ->
``theme_mapper.propose_candidates`` -> ``build_prompt``.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.thematic.mapping import channel_assessor, orchestrator, theme_mapper
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload

from tests.thematic.mapping_stubs import theme_proposal


def _mcap_from(mcaps):
    """Stub the mcap LOOKUP, not the bracket.

    Listed tickers come back with that market cap; everything else comes back
    unknown, and the real bracket comparison decides. Patching this deeper seam
    (rather than the bracket function itself) keeps these tests exercising the
    filter they describe instead of replacing it with a hand-written answer.
    """
    return lambda ticker, **_: mcaps.get(ticker)


_ASOF = dt.date(2026, 7, 25)


def _catalyst(title: str = "eBay to pay $56m over its harassment campaign") -> CatalystPayload:
    return CatalystPayload(
        url="https://example.com/ebay-harassment",
        title=title,
        published_at="2026-07-24",
        event_type="litigation",
        primary_entities=["EBAY"],
        confidence=0.8,
        second_order_implications=[],
        echo_count=1,
        trigger_url="https://example.com/ebay-harassment",
        trigger_published_at="2026-07-24",
        is_amplified=False,
        template_id=None,
        template_facts=None,
    )


class ProposalCallIsEventConditionedTests(unittest.TestCase):
    def test_propose_and_bracket_forwards_the_catalyst_to_the_mapper(self):
        catalyst = _catalyst()
        proposal = {
            "candidates": [{"ticker": "AAA", "confidence": 0.9}],
            "search_keywords": [],
            "outcome": theme_mapper.MapperOutcome.SUCCESS,
            "decline_reason": "",
        }
        with (
            mock.patch.object(
                orchestrator.theme_mapper, "propose_candidates", return_value=proposal
            ) as propose,
            mock.patch.object(
                orchestrator.mcap_filter,
                "fetch_mcap",
                side_effect=_mcap_from({"AAA": 1_000_000_000.0}),
            ),
        ):
            orchestrator._propose_and_bracket(
                theme="harassment",
                catalyst=catalyst,
                api_key="k",
                pro_client=None,
                min_cap=500_000_000,
                max_cap=10_000_000_000,
                asof=_ASOF,
            )
        self.assertIs(propose.call_args.kwargs["catalyst"], catalyst)

    def test_propose_and_bracket_requires_a_catalyst(self):
        # Non-optional by design: ``_rows_for_theme`` hard-returns before this
        # call when no catalyst resolved, so an ungrounded proposal should be
        # unrepresentable rather than merely unlikely.
        with self.assertRaises(TypeError):
            orchestrator._propose_and_bracket(
                theme="harassment",
                api_key="k",
                pro_client=None,
                min_cap=1,
                max_cap=2,
                asof=_ASOF,
            )

    def test_rows_for_theme_forwards_the_object_it_resolved(self):
        # Identity, not equality: a future refactor that re-resolves the
        # catalyst inside the proposal path instead of threading the one the
        # row will be stamped with would silently reintroduce the split
        # between "the event we reasoned from" and "the event we cite".
        catalyst = _catalyst()
        with (
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=catalyst) as resolve,
            mock.patch.object(
                orchestrator,
                "_propose_and_bracket",
                return_value=theme_proposal(outcome=theme_mapper.MapperOutcome.DECLINED),
            ) as propose,
        ):
            orchestrator._rows_for_theme(
                "harassment",
                asof=_ASOF,
                catalyst_cache={},
                api_key="k",
                pro_client=None,
                min_cap=1,
                max_cap=2,
                model=None,
                polygon_client=None,
                press_df=None,
                keep_unverified=False,
            )
        self.assertEqual(resolve.call_count, 1)
        self.assertIs(propose.call_args.kwargs["catalyst"], catalyst)

    def test_rows_for_theme_forwards_the_same_object_to_the_assessor(self):
        # Identity again, one layer down. Stage B must judge the channel against
        # the event the row will cite as provenance — a re-resolve here would
        # split "the event we assessed" from "the event we show".
        catalyst = _catalyst()
        with (
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=catalyst),
            mock.patch.object(
                orchestrator,
                "_propose_and_bracket",
                return_value=theme_proposal(proposed=[{"ticker": "AAA", "confidence": 0.9}]),
            ),
            mock.patch.object(
                orchestrator.channel_assessor,
                "assess_candidates",
                return_value=[channel_assessor.unassessed()],
            ) as assess,
            mock.patch.object(
                orchestrator, "_verify_candidates_for_theme", return_value=([], 0, 0)
            ),
        ):
            orchestrator._rows_for_theme(
                "harassment",
                asof=_ASOF,
                catalyst_cache={},
                api_key="k",
                pro_client=None,
                min_cap=1,
                max_cap=2,
                model=None,
                polygon_client=None,
                press_df=None,
                keep_unverified=False,
            )
        self.assertIs(assess.call_args.kwargs["catalyst"], catalyst)

    def test_propose_candidates_conditions_the_prompt_on_the_catalyst(self):
        catalyst = _catalyst()
        with (
            mock.patch.object(theme_mapper, "build_prompt", return_value="p") as build,
            mock.patch.object(theme_mapper, "_call_llm", side_effect=RuntimeError("stop")),
        ):
            theme_mapper.propose_candidates(
                theme="harassment", catalyst=catalyst, api_key="testkey"
            )
        self.assertIs(build.call_args.kwargs["catalyst"], catalyst)

    def test_the_event_headline_reaches_the_llm_call(self):
        # End-to-end over the real build_prompt: the article the card will cite
        # as provenance is the article the model actually reasoned from.
        sent: dict[str, str] = {}

        def _capture(_client, prompt, *, model):
            sent["prompt"] = prompt
            raise RuntimeError("stop after capture")

        with mock.patch.object(theme_mapper, "_call_llm", side_effect=_capture):
            theme_mapper.propose_candidates(
                theme="harassment", catalyst=_catalyst(), api_key="testkey"
            )
        self.assertIn("eBay to pay $56m over its harassment campaign", sent["prompt"])
        self.assertIn("litigation", sent["prompt"])
        self.assertIn("EBAY", sent["prompt"])


class ChannelAssessmentIsPersistedTests(unittest.TestCase):
    def test_build_row_persists_the_channel_assessment(self):
        # Without persisting it the change is unfalsifiable: the (event, ticker)
        # plausibility audit cannot be re-run on the new output, and the forward
        # KEPT-vs-REFUSED contrast has nothing to read. The free-text
        # ``transmission_channel`` column is gone — its content is now
        # ``channel_text`` with a real status beside it.
        assessment = channel_assessor.ChannelAssessment(
            support_status="suggestive",
            grounding_status=channel_assessor.GROUNDING_GROUNDED,
            grounding_quote="the event states x",
            grounding_reason="",
            grounding_agree_n=3,
            grounding_quote_verbatim=True,
            channel_type="customer_demand",
            text="payout -> legal spend rises -> AAA fees rise",
            evidence="the event states a settlement",
            falsifier="AAA's 10-K names no litigation-services segment",
            confidence=0.5,
            votes=3,
            valid_n=3,
            support_dispersion=1,
            outcome=channel_assessor.AssessmentOutcome.SUCCESS,
            assessed_at="2026-08-19T00:00:00+00:00",
        )
        row = orchestrator._build_row(
            theme="harassment",
            cand={
                "ticker": "AAA",
                "rationale": "does x",
                "confidence": 0.9,
                "channel": assessment,
            },
            verdict={
                "gates_passed": [],
                "gates_failed": [],
                "gates_unknown": [],
                "verified": False,
            },
            market_cap=1_000_000_000.0,
            catalyst=_catalyst(),
            keywords=["employment litigation"],
            shadow=channel_assessor.ShadowVerdict(channel_assessor.SHADOW_REFUSE, 0, 1, 0),
        )
        self.assertEqual(row["channel_text"], "payout -> legal spend rises -> AAA fees rise")
        self.assertEqual(row["channel_support_status"], "suggestive")
        self.assertEqual(row["channel_support_dispersion"], 1)
        self.assertNotIn("transmission_channel", orchestrator._MAP_THEMES_COLUMNS)
        for column in channel_assessor.CHANNEL_ROW_COLUMNS:
            self.assertIn(column, orchestrator._MAP_THEMES_COLUMNS)


class MapperFreezeTokenTests(unittest.TestCase):
    def test_freeze_schema_records_the_event_conditioned_cohort(self):
        # ``mapper_config_version`` fingerprints the prompt template, so each
        # rewrite already changes the token. The schema tag is bumped as well
        # because ``_normalize`` changed shape (a code-level change the data
        # hash cannot describe) and because a shifted 12-char sha is not
        # legible to a human reading the cohort boundary. v3 = the channel
        # stopped being a proposal-time requirement (2026-08-19).
        token = theme_mapper.mapper_config_version(
            market_cap_range=(1, 2),
            channel_config_version=channel_assessor.channel_config_version(),
        )
        self.assertIn("mapper-freeze-v4", token)


if __name__ == "__main__":
    unittest.main()
