"""The mapper reports WHY it returned no candidate, and retries only failures.

Issue #982. ``propose_candidates`` used to collapse four different endings into
one ``{"candidates": [], "search_keywords": []}``: the call raised, the payload
did not parse, the model declined with a stated reason, or it succeeded with an
empty list. The orchestrator saw only the empty list, so its funnel line read
the same for a working refusal and for an outage.

That became more dangerous, not less, on 2026-08-03: the prompt deployed that
day gives the model an explicit licence to decline when no company has a
transmission channel from the event, so "proposed 0" is now a legitimate answer
and no longer stands out. On the first production run the theme ``iphone_sales``
returned an EMPTY string — the model produced nothing at all — and the funnel
reported it exactly as it reported five genuine declines.

The loss is not random. DeepSeek v4-pro is a reasoning model, so its reasoning
trace is charged against the output budget: an insufficient budget returns
``finish_reason='length'`` with empty content, preferentially on the inputs that
needed the most reasoning. The themes most likely to vanish are the hard ones.

These tests pin: the outcomes are distinguishable, an empty payload is retried,
a decline is never retried, and the retry re-issues the IDENTICAL request.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from alphalens_pipeline.thematic.mapping import theme_mapper
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload

_LOGGER = "alphalens_pipeline.thematic.mapping.theme_mapper"


def _catalyst() -> CatalystPayload:
    """The proposal is event-conditioned, so every call carries a catalyst."""
    return CatalystPayload(
        url="https://example.com/e",
        title="Autonomous drone maker wins a USAF award",
        published_at="2026-08-02",
        event_type="contract_award",
        primary_entities=[],
        confidence=0.8,
        second_order_implications=[],
        echo_count=1,
        trigger_url="https://example.com/e",
        trigger_published_at="2026-08-02",
        is_amplified=False,
        template_id=None,
        template_facts=None,
    )


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


_GOOD_PAYLOAD = {
    "candidates": [
        {
            "ticker": "AVAV",
            "company_name": "AeroVironment",
            "rationale": "Small unmanned aircraft prime",
            "transmission_channel": "the award funds drones -> USAF orders -> AVAV revenue",
            "confidence": 0.8,
        }
    ],
    "search_keywords": ["unmanned aircraft", "loitering munition"],
}

_DECLINE_PAYLOAD = {
    "event_read": "eBay settled a harassment suit for a one-time payout.",
    "candidates": [],
    "no_candidates_reason": (
        "a one-time litigation payout by eBay with no clear transmission channel "
        "to benefit any other U.S.-listed company materially"
    ),
    "search_keywords": ["workplace harassment litigation"],
}


def _propose(**kwargs):
    return theme_mapper.propose_candidates(
        theme="ai_defense", catalyst=_catalyst(), api_key="testkey", **kwargs
    )


class ProposeOutcomeClassificationTests(unittest.TestCase):
    """Each ending gets its own ``MapperOutcome``, not a shared empty list."""

    def test_success_carries_the_success_outcome(self):
        with mock.patch.object(
            theme_mapper, "_call_llm", return_value=_response(json.dumps(_GOOD_PAYLOAD))
        ):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.SUCCESS)
        self.assertEqual([c["ticker"] for c in result["candidates"]], ["AVAV"])

    def test_decline_is_reported_as_declined_with_the_model_reason(self):
        with mock.patch.object(
            theme_mapper, "_call_llm", return_value=_response(json.dumps(_DECLINE_PAYLOAD))
        ):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.DECLINED)
        self.assertEqual(result["candidates"], [])
        self.assertIn("no clear transmission channel", result["no_candidates_reason"])

    def test_empty_response_body_is_reported_as_empty_payload(self):
        # The observed #982 failure: the model returned nothing at all.
        with mock.patch.object(theme_mapper, "_call_llm", return_value=_response("")):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.EMPTY_PAYLOAD)
        self.assertEqual(result["candidates"], [])

    def test_whitespace_only_response_body_is_reported_as_empty_payload(self):
        # "No content" wearing whitespace is still no content, never malformed.
        with mock.patch.object(theme_mapper, "_call_llm", return_value=_response("  \n\t ")):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.EMPTY_PAYLOAD)

    def test_unparseable_body_is_reported_as_malformed_payload(self):
        # Distinct from EMPTY_PAYLOAD: there IS content, it is just not JSON.
        # Only the split makes "retry the empty one, not this one" expressible.
        with mock.patch.object(theme_mapper, "_call_llm", return_value=_response("not json")):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.MALFORMED_PAYLOAD)
        self.assertEqual(result["candidates"], [])

    def test_payload_without_a_candidates_key_is_malformed_not_a_decline(self):
        # Valid JSON that violates the response schema. Reading it as a decline
        # would credit the model with a judgement it never made.
        body = json.dumps({"search_keywords": ["drone"]})
        with mock.patch.object(theme_mapper, "_call_llm", return_value=_response(body)):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.MALFORMED_PAYLOAD)

    def test_raising_call_is_reported_as_call_failed(self):
        with mock.patch.object(theme_mapper, "_call_llm", side_effect=RuntimeError("boom")):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.CALL_FAILED)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["search_keywords"], [])

    def test_the_five_outcomes_are_five_distinct_values(self):
        # Positive control. Every assertion above compares an outcome against a
        # member of this enum; if two members ever collapsed onto one value the
        # whole file would keep passing while distinguishing nothing.
        values = [o.value for o in theme_mapper.MapperOutcome]
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(len(values), 5)

    def test_declined_carries_no_reason_when_the_model_supplied_none(self):
        # A decline without a stated reason is still a decline — the outcome
        # must not fall back to a failure kind just because the field is absent.
        body = json.dumps({"candidates": [], "search_keywords": ["drone"]})
        with mock.patch.object(theme_mapper, "_call_llm", return_value=_response(body)):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.DECLINED)
        self.assertEqual(result["no_candidates_reason"], "")

    def test_all_candidates_dropped_for_a_missing_channel_is_not_a_decline(self):
        # The model DID propose; ``_normalize`` dropped every entry for having
        # no transmission_channel. That is a response-shape defect, not a
        # judgement, so it must not be counted as the model declining.
        body = json.dumps(
            {"candidates": [{"ticker": "AVAV", "confidence": 0.9}], "search_keywords": ["drone"]}
        )
        with mock.patch.object(theme_mapper, "_call_llm", return_value=_response(body)):
            result = _propose()
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.MALFORMED_PAYLOAD)
        self.assertEqual(result["candidates"], [])


class ProposeRetryPolicyTests(unittest.TestCase):
    """An empty payload is worth another call. An answer is not."""

    def test_empty_payload_is_retried_and_can_succeed(self):
        responses = [_response(""), _response(json.dumps(_GOOD_PAYLOAD))]
        with mock.patch.object(theme_mapper, "_call_llm", side_effect=responses) as call:
            result = _propose()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.SUCCESS)
        self.assertEqual([c["ticker"] for c in result["candidates"]], ["AVAV"])

    def test_a_persistently_empty_payload_stops_after_one_retry(self):
        # Bounded: a dead model must not burn the theme budget in a loop.
        with mock.patch.object(
            theme_mapper, "_call_llm", side_effect=[_response(""), _response("")]
        ) as call:
            result = _propose()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.EMPTY_PAYLOAD)

    def test_a_decline_is_never_retried(self):
        # The point of the whole change. A decline is an ANSWER: re-asking pays
        # twice and nudges the model toward a different answer, which corrupts
        # the pre-registered measurement the proposal feeds.
        with mock.patch.object(
            theme_mapper, "_call_llm", return_value=_response(json.dumps(_DECLINE_PAYLOAD))
        ) as call:
            result = _propose()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["outcome"], theme_mapper.MapperOutcome.DECLINED)

    def test_a_successful_proposal_is_never_retried(self):
        with mock.patch.object(
            theme_mapper, "_call_llm", return_value=_response(json.dumps(_GOOD_PAYLOAD))
        ) as call:
            _propose()
        self.assertEqual(call.call_count, 1)

    def test_a_malformed_payload_is_not_retried(self):
        # Matches the brief generator: extra calls do not fix bad JSON.
        with mock.patch.object(
            theme_mapper, "_call_llm", return_value=_response("not json")
        ) as call:
            _propose()
        self.assertEqual(call.call_count, 1)

    def test_a_failed_call_is_not_retried(self):
        with mock.patch.object(theme_mapper, "_call_llm", side_effect=RuntimeError("boom")) as call:
            _propose()
        self.assertEqual(call.call_count, 1)

    def test_the_retry_re_issues_the_identical_request(self):
        # Load-bearing for TWO frozen artifacts: the golden characterization
        # cassette is keyed on a sha256 of the full request descriptor, and
        # ``mapper_config_version`` fingerprints the sampling config that the
        # frozen candidate parquet claims produced its rows. A retry that
        # changed the prompt, the model or the sampling would invalidate both.
        with mock.patch.object(
            theme_mapper,
            "_call_llm",
            side_effect=[_response(""), _response(json.dumps(_GOOD_PAYLOAD))],
        ) as call:
            _propose()
        first, second = call.call_args_list
        self.assertEqual(first, second)

    def test_the_retry_is_logged_so_a_silent_burn_is_visible(self):
        with (
            mock.patch.object(
                theme_mapper,
                "_call_llm",
                side_effect=[_response(""), _response(json.dumps(_GOOD_PAYLOAD))],
            ),
            self.assertLogs(_LOGGER, level="INFO") as cm,
        ):
            _propose()
        self.assertIn("ai_defense", "\n".join(cm.output))
        self.assertIn("retry", "\n".join(cm.output).lower())


if __name__ == "__main__":
    unittest.main()
