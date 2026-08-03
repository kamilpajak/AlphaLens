"""Tests for the retrospective (event, ticker) ROLE classifier script.

The measurement this script feeds asks one question: does a transmission
channel exist between the catalyst event and the candidate? Two properties
have to hold for the answer to mean anything:

1. **Blindness** — the prompt must not leak the pipeline's own verdict
   (``layer4_weighted_score``, ``rank_in_day``, the mapper ``rationale``).
   Feeding those back would measure "is the rationale self-consistent",
   not "is there a real channel".
2. **No silent degradation** — an empty or malformed LLM response must
   surface as its own sentinel, never fold into a real role. Same bug class
   as PR #869 in ``analyze_rejections``.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from scripts.classify_catalyst_roles import (
    FRAMINGS,
    ROLES,
    anchor_report,
    build_role_prompt,
    classify_role,
)


class _FakeClient:
    """Minimal OpenRouter-shaped stub: build_config + generate_content(text=...)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.last_contents: str | None = None

    def build_config(self, **_kwargs):
        return {}

    def generate_content(self, *, model, contents, config):
        _ = (model, config)
        self.last_contents = contents
        return SimpleNamespace(text=self._text)


def _row(**overrides) -> dict:
    row = {
        "ticker": "LYFT",
        "company_name": "Lyft, Inc.",
        "sector_name": "Services",
        "industry_name": "Services-Business Services, NEC",
        "theme": "harassment",
        "source_event_title": "'Crush this lady': how eBay harassment campaign led to $56mn payout",
        "catalyst_event_type": "litigation",
        "sentiment": "negative",
        "primary_entities": ["EBAY"],
        "second_order_implications": [],
        # Pipeline verdict fields — must never reach the prompt.
        "layer4_weighted_score": 2,
        "rank_in_day": 5,
        "llm_confidence": 0.85,
        "rationale": "Peer-to-peer ridesharing company facing similar safety risks as Uber.",
    }
    row.update(overrides)
    return row


class TestPromptBlindness(unittest.TestCase):
    def test_prompt_omits_pipeline_verdict_fields(self):
        prompt = build_role_prompt(_row())
        # The mapper's own rationale is the pipeline's claim under test.
        self.assertNotIn("Peer-to-peer ridesharing company facing", prompt)
        for banned in ("layer4", "weighted_score", "rank_in_day", "llm_confidence", "rationale"):
            self.assertNotIn(banned, prompt.lower())

    def test_prompt_carries_the_facts_needed_to_judge_a_channel(self):
        prompt = build_role_prompt(_row())
        for needed in (
            "LYFT",
            "Lyft, Inc.",
            "harassment",
            "eBay harassment campaign",
            "litigation",
        ):
            self.assertIn(needed, prompt)
        # primary_entities is what separates "is the subject" from "is a bystander".
        self.assertIn("EBAY", prompt)

    def test_prompt_enumerates_every_allowed_role(self):
        prompt = build_role_prompt(_row())
        for role in ROLES:
            self.assertIn(role, prompt)


class TestClassifyRoleParsing(unittest.TestCase):
    def test_valid_response_returns_role_and_channel(self):
        payload = {"role": "unaffected", "channel": "no link to Lyft cash flows", "confidence": 0.9}
        result = classify_role(_row(), _FakeClient(json.dumps(payload)))
        self.assertEqual(result["role"], "unaffected")
        self.assertEqual(result["parse_status"], "ok")

    def test_fenced_json_is_recovered(self):
        text = '```json\n{"role": "rival", "channel": "competitor loses share", "confidence": 0.7}\n```'
        result = classify_role(_row(), _FakeClient(text))
        self.assertEqual(result["role"], "rival")
        self.assertEqual(result["parse_status"], "ok")

    def test_empty_object_is_its_own_sentinel_not_a_role(self):
        result = classify_role(_row(), _FakeClient(json.dumps({})))
        self.assertEqual(result["parse_status"], "empty_content")
        self.assertNotIn(result["role"], ROLES)

    def test_blank_text_is_its_own_sentinel(self):
        result = classify_role(_row(), _FakeClient(""), sleep_fn=lambda _: None)
        self.assertEqual(result["parse_status"], "empty_content")
        self.assertNotIn(result["role"], ROLES)

    def test_malformed_json_is_its_own_sentinel(self):
        result = classify_role(_row(), _FakeClient("not json at all"))
        self.assertEqual(result["parse_status"], "unparseable")
        self.assertNotIn(result["role"], ROLES)

    def test_role_outside_the_taxonomy_is_rejected_not_accepted(self):
        payload = {"role": "beneficiary", "channel": "vibes", "confidence": 1.0}
        result = classify_role(_row(), _FakeClient(json.dumps(payload)))
        self.assertEqual(result["parse_status"], "invalid_role")
        self.assertNotIn(result["role"], ROLES)


class TestFramings(unittest.TestCase):
    """The strict/permissive split exists because the solution-provider vs
    unaffected boundary is a judgement call, and the strict wording alone was
    observed to drive the headline number. Two framings turn a point estimate
    into a band, so the instrument's own bias is visible instead of hidden."""

    def test_both_framings_exist_and_produce_different_prompts(self):
        self.assertEqual(set(FRAMINGS), {"strict", "permissive"})
        strict = build_role_prompt(_row(), framing="strict")
        permissive = build_role_prompt(_row(), framing="permissive")
        self.assertNotEqual(strict, permissive)

    def test_every_framing_keeps_the_full_taxonomy_and_the_facts(self):
        for framing in FRAMINGS:
            prompt = build_role_prompt(_row(), framing=framing)
            for role in ROLES:
                self.assertIn(role, prompt)
            self.assertIn("LYFT", prompt)
            self.assertIn("EBAY", prompt)

    def test_every_framing_stays_blind_to_the_pipeline_verdict(self):
        for framing in FRAMINGS:
            prompt = build_role_prompt(_row(), framing=framing)
            self.assertNotIn("Peer-to-peer ridesharing company facing", prompt)
            for banned in ("layer4", "rank_in_day", "llm_confidence"):
                self.assertNotIn(banned, prompt.lower())

    def test_unknown_framing_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            build_role_prompt(_row(), framing="whatever")


class _SequenceClient:
    """Returns a scripted sequence of texts, one per generate_content call."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls = 0

    def build_config(self, **_kwargs):
        return {}

    def generate_content(self, *, model, contents, config):
        _ = (model, contents, config)
        self.calls += 1
        return SimpleNamespace(text=self._texts[min(self.calls - 1, len(self._texts) - 1)])


class _ConfigRecordingClient:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def build_config(self, **kwargs):
        self.kwargs = kwargs
        return {}

    def generate_content(self, *, model, contents, config):
        _ = (model, contents, config)
        return SimpleNamespace(
            text=json.dumps({"role": "unaffected", "channel": "none", "confidence": 0.9})
        )


class TestOutputBudgetCoversReasoning(unittest.TestCase):
    """Root cause of the empty-response epidemic: deepseek-v4-pro is a reasoning
    model and its reasoning tokens are charged against max_tokens. At 400 the
    endpoint returned finish_reason='length' with 1963 chars of reasoning and an
    EMPTY content field; at 2000 it answered in 75 tokens. The budget must leave
    room for reasoning or hard rows silently vanish - and they vanish
    non-randomly, biasing exactly the rows where a channel takes work to establish."""

    _REASONING_FLOOR_TOKENS = 1500

    def test_requested_budget_leaves_room_for_reasoning(self):
        client = _ConfigRecordingClient()
        classify_role(_row(), client)
        self.assertGreaterEqual(
            client.kwargs.get("max_output_tokens", 0), self._REASONING_FLOOR_TOKENS
        )


class TestEmptyContentRetry(unittest.TestCase):
    """DeepSeek's JSON mode intermittently returns no choices (see the client
    docstring). One empty response must not silently cost a row."""

    def test_empty_first_response_is_retried_and_recovered(self):
        payload = json.dumps({"role": "rival", "channel": "competitor hit", "confidence": 0.6})
        client = _SequenceClient(["", payload])
        result = classify_role(_row(), client, sleep_fn=lambda _: None)
        self.assertEqual(result["parse_status"], "ok")
        self.assertEqual(result["role"], "rival")
        self.assertEqual(client.calls, 2)

    def test_recovers_after_several_empties(self):
        """Observed empty rate on the real endpoint was ~50% at 2 attempts;
        the sweep must not lose half the corpus to a transient provider quirk."""
        payload = json.dumps({"role": "unaffected", "channel": "none", "confidence": 0.9})
        client = _SequenceClient(["", "", "", payload])
        result = classify_role(_row(), client, max_attempts=5, sleep_fn=lambda _: None)
        self.assertEqual(result["parse_status"], "ok")
        self.assertEqual(client.calls, 4)

    def test_persistently_empty_gives_up_and_does_not_loop(self):
        client = _SequenceClient(["", ""])
        result = classify_role(_row(), client, max_attempts=2, sleep_fn=lambda _: None)
        self.assertEqual(result["parse_status"], "empty_content")
        self.assertEqual(client.calls, 2)

    def test_retries_back_off_instead_of_hammering(self):
        """Failure rate climbed 25% -> 57% across a 794-call run: the endpoint
        rate-limits, so retries must wait, and wait longer each time."""
        payload = json.dumps({"role": "unaffected", "channel": "none", "confidence": 0.9})
        client = _SequenceClient(["", "", payload])
        slept: list[float] = []
        classify_role(_row(), client, max_attempts=5, sleep_fn=slept.append)
        self.assertEqual(len(slept), 2)
        self.assertGreater(slept[0], 0)
        self.assertGreater(slept[1], slept[0])

    def test_no_sleep_when_the_first_response_is_good(self):
        payload = json.dumps({"role": "unaffected", "channel": "none", "confidence": 0.9})
        slept: list[float] = []
        classify_role(_row(), _SequenceClient([payload]), sleep_fn=slept.append)
        self.assertEqual(slept, [])

    def test_a_good_first_response_is_not_retried(self):
        payload = json.dumps({"role": "unaffected", "channel": "none", "confidence": 0.9})
        client = _SequenceClient([payload])
        classify_role(_row(), client)
        self.assertEqual(client.calls, 1)


class TestAnchorGate(unittest.TestCase):
    """The labels are themselves unvalidated LLM output — the run is only
    trustworthy if known-answer cases come back right."""

    def test_all_anchors_matching_passes(self):
        anchors = [
            {"ticker": "VRNS", "brief_date": "2026-07-29", "expected_role": "solution-provider"},
            {"ticker": "LYFT", "brief_date": "2026-08-02", "expected_role": "unaffected"},
        ]
        labelled = [
            {"ticker": "VRNS", "brief_date": "2026-07-29", "role": "solution-provider"},
            {"ticker": "LYFT", "brief_date": "2026-08-02", "role": "unaffected"},
        ]
        report = anchor_report(labelled, anchors)
        self.assertTrue(report["passed"])
        self.assertEqual(report["mismatches"], [])

    def test_a_single_mismatch_fails_the_gate(self):
        anchors = [
            {"ticker": "VRNS", "brief_date": "2026-07-29", "expected_role": "solution-provider"},
        ]
        labelled = [{"ticker": "VRNS", "brief_date": "2026-07-29", "role": "unaffected"}]
        report = anchor_report(labelled, anchors)
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["mismatches"]), 1)

    def test_missing_anchor_row_fails_rather_than_silently_passing(self):
        anchors = [{"ticker": "GONE", "brief_date": "2026-07-29", "expected_role": "rival"}]
        report = anchor_report([], anchors)
        self.assertFalse(report["passed"])
        self.assertEqual(report["mismatches"][0]["got"], "MISSING")


if __name__ == "__main__":
    unittest.main()
