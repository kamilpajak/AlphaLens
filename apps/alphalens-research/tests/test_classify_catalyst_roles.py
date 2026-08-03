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

import contextlib
import datetime as dt
import io
import json
import unittest
from types import SimpleNamespace

from scripts.classify_catalyst_roles import (
    ANCHORS,
    DIRECTIONS,
    FRAMINGS,
    ROLES,
    anchor_report,
    build_role_prompt,
    classify_role,
    report_run,
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


class TestDirectionIsValidatedLikeTheRole(unittest.TestCase):
    """``direction`` is declared as an enum in the response schema, so an
    off-enum value is model misbehaviour. It must not fold into the record as if
    it were a real direction - same rule the role already follows. The role
    itself is still a usable answer, so the row stays ``ok``."""

    def test_a_valid_direction_passes_through(self):
        payload = {"role": "rival", "channel": "c", "direction": "adverse", "confidence": 0.5}
        result = classify_role(_row(), _FakeClient(json.dumps(payload)))
        self.assertEqual(result["direction"], "adverse")

    def test_an_off_enum_direction_is_marked_not_accepted(self):
        payload = {"role": "rival", "channel": "c", "direction": "bullish", "confidence": 0.5}
        result = classify_role(_row(), _FakeClient(json.dumps(payload)))
        self.assertEqual(result["parse_status"], "ok")
        self.assertNotIn(result["direction"], DIRECTIONS)
        self.assertIn("bullish", result["direction"])

    def test_a_missing_direction_is_marked_not_left_blank_looking_valid(self):
        payload = {"role": "rival", "channel": "c", "confidence": 0.5}
        result = classify_role(_row(), _FakeClient(json.dumps(payload)))
        self.assertNotIn(result["direction"], DIRECTIONS)


class TestEntityRendering(unittest.TestCase):
    def test_a_string_entity_field_is_not_shredded_into_characters(self):
        """A parquet column that stores entities as one string instead of a list
        would otherwise be iterated character by character, so the prompt reads
        ``E, B, A, Y`` and the instrument judges a channel from nonsense."""
        prompt = build_role_prompt(_row(primary_entities="EBAY"))
        self.assertIn("EBAY", prompt)
        self.assertNotIn("E, B, A, Y", prompt)


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


class _RaisingClient:
    """Raises on the first ``n_failures`` calls, then returns ``text``.

    Models the shape a 429 actually takes: the client raises, it does not
    return an empty body.
    """

    def __init__(self, n_failures: int, text: str) -> None:
        self._n_failures = n_failures
        self._text = text
        self.calls = 0

    def build_config(self, **_kwargs):
        return {}

    def generate_content(self, *, model, contents, config):
        _ = (model, contents, config)
        self.calls += 1
        if self.calls <= self._n_failures:
            raise RuntimeError("429 Too Many Requests")
        return SimpleNamespace(text=self._text)


class TestTransportErrorsAreRetried(unittest.TestCase):
    """The backoff exists as rate-limit insurance, and a rate limit surfaces as
    a raised exception rather than an empty body. Returning the error sentinel
    on the first exception would skip the backoff entirely and lose every
    rate-limited row permanently."""

    _PAYLOAD = json.dumps({"role": "rival", "channel": "competitor hit", "confidence": 0.6})
    _LOGGER = "scripts.classify_catalyst_roles"

    def test_a_rate_limited_call_is_retried_after_a_backoff(self):
        client = _RaisingClient(2, self._PAYLOAD)
        slept: list[float] = []
        with self.assertLogs(self._LOGGER, level="WARNING"):
            result = classify_role(_row(), client, max_attempts=5, sleep_fn=slept.append)
        self.assertEqual(result["parse_status"], "ok")
        self.assertEqual(result["role"], "rival")
        self.assertEqual(client.calls, 3)
        self.assertEqual(len(slept), 2)
        self.assertGreater(slept[1], slept[0])

    def test_a_persistent_transport_error_gives_up_with_the_error_sentinel(self):
        client = _RaisingClient(99, self._PAYLOAD)
        with self.assertLogs(self._LOGGER, level="WARNING") as logs:
            result = classify_role(_row(), client, max_attempts=3, sleep_fn=lambda _: None)
        self.assertEqual(result["parse_status"], "error")
        self.assertNotIn(result["role"], ROLES)
        self.assertEqual(client.calls, 3)
        # Every lost attempt is visible to the operator, not just the last one.
        self.assertEqual(len(logs.output), 3)


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


class TestAnchorGateKeysOnTheDateNotItsDtype(unittest.TestCase):
    """``ANCHORS`` stores bare date strings, but the labelled rows come out of a
    parquet whose ``brief_date`` column may be a timestamp. Keying on ``str()``
    alone turns every anchor into MISSING and fails the gate for a reason that
    has nothing to do with the instrument's judgement."""

    _ANCHORS = ({"ticker": "VRNS", "brief_date": "2026-07-29", "expected_role": "rival"},)

    def _report_for(self, brief_date):
        labelled = [{"ticker": "VRNS", "brief_date": brief_date, "role": "rival"}]
        return anchor_report(labelled, self._ANCHORS)

    def test_a_datetime_brief_date_matches_the_anchor_date(self):
        self.assertTrue(self._report_for(dt.datetime(2026, 7, 29, 0, 0))["passed"])

    def test_a_date_brief_date_matches_the_anchor_date(self):
        self.assertTrue(self._report_for(dt.date(2026, 7, 29))["passed"])

    def test_a_different_date_still_mismatches(self):
        self.assertFalse(self._report_for(dt.datetime(2026, 7, 30, 0, 0))["passed"])


class TestAnchorGateDecidesTheExitCode(unittest.TestCase):
    """A wrapper or CI job reads ``$?``. A failed anchor gate means the run's
    aggregate is not to be trusted, so it must not look like success."""

    @staticmethod
    def _frame(roles_by_key: dict[tuple[str, str], str]):
        import pandas as pd

        return pd.DataFrame(
            [
                {
                    "ticker": ticker,
                    "brief_date": brief_date,
                    "framing": framing,
                    "role": role,
                    "parse_status": "ok",
                }
                for (ticker, brief_date), role in roles_by_key.items()
                for framing in FRAMINGS
            ]
        )

    @staticmethod
    def _gate(frame) -> bool:
        """``report_run`` prints the run summary; only its verdict is asserted."""
        with contextlib.redirect_stdout(io.StringIO()):
            return report_run(frame)

    @staticmethod
    def _expected() -> dict[tuple[str, str], str]:
        return {(a["ticker"], a["brief_date"]): a["expected_role"] for a in ANCHORS}

    def test_every_anchor_matching_reports_a_passing_run(self):
        self.assertTrue(self._gate(self._frame(self._expected())))

    def test_one_wrong_anchor_role_reports_a_failing_run(self):
        roles = self._expected()
        roles[(ANCHORS[0]["ticker"], ANCHORS[0]["brief_date"])] = "rival"
        self.assertFalse(self._gate(self._frame(roles)))

    def test_a_missing_anchor_reports_a_failing_run(self):
        roles = self._expected()
        del roles[(ANCHORS[0]["ticker"], ANCHORS[0]["brief_date"])]
        self.assertFalse(self._gate(self._frame(roles)))

    def test_a_row_that_failed_to_parse_cannot_satisfy_an_anchor(self):
        frame = self._frame(self._expected())
        target = (frame["ticker"] == ANCHORS[0]["ticker"]) & (
            frame["brief_date"] == ANCHORS[0]["brief_date"]
        )
        frame.loc[target, "parse_status"] = "unparseable"
        self.assertFalse(self._gate(frame))


if __name__ == "__main__":
    unittest.main()
