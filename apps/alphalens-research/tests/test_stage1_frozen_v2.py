"""The pre-registered Stage-1 instrument stays replayable after the live prompt moves.

``scripts/stage1_retro_label_pairs.py`` hard-asserts its ``FROZEN_MCV`` literal
before making any call — that assertion IS the pre-registration's guarantee that
the replayed instrument is the one that was registered. The live
``theme_mapper`` left that shape on 2026-08-19 (mapper-freeze-v3), so the frozen
v2 surface now lives in its own module and the script points there.

These tests are what make the snapshot trustworthy: the token it produces must
equal the pre-registered literal byte-for-byte, and its normaliser must still
drop a channel-less candidate — the exact behaviour the live one stopped doing.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from alphalens_pipeline.thematic.mapping import theme_mapper
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from alphalens_research.retrospective_audit import stage1_frozen_v2
from scripts.stage1_retro_label_pairs import FROZEN_MCV

_MCAP = (500_000_000, 10_000_000_000)


def _catalyst() -> CatalystPayload:
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


class ARepoWideRenameCannotCorruptTheFrozenInstrument(unittest.TestCase):
    """The 2026-08-20 causal-support rename must stop at this module's door.

    The live assessor dropped the ``verified`` / ``partial`` / ``unverified``
    vocabulary and the free-text ``transmission_channel`` column. This module is
    a byte copy of the FROZEN Stage-1 instrument: it has to keep both, because
    the retrospective's replayability is the pre-registration's guarantee. A
    find-and-replace that swept this file would break that silently — the token
    test below would still pass if the rename were confined to prose, so the
    vocabulary is pinned explicitly rather than left to a reviewer's memory.
    """

    def test_the_frozen_prompt_and_schema_keep_the_retired_channel_field(self):
        source = Path(stage1_frozen_v2.__file__).read_text()
        self.assertIn('"transmission_channel"', source)
        self.assertIn("transmission_channel", stage1_frozen_v2._PROMPT_TEMPLATE)
        self.assertIn(
            "transmission_channel",
            stage1_frozen_v2._MAPPER_RESPONSE_SCHEMA["properties"]["candidates"]["items"][
                "required"
            ],
        )

    def test_the_frozen_module_never_imports_the_live_assessor(self):
        # The live module's constants move with the live cohort; binding to them
        # would make the frozen replay follow a vocabulary it never ran under.
        source = Path(stage1_frozen_v2.__file__).read_text()
        self.assertNotIn("channel_assessor", source)

    def test_the_frozen_token_still_reproduces_after_the_live_rename(self):
        self.assertEqual(
            stage1_frozen_v2.frozen_mapper_config_version(market_cap_range=_MCAP), FROZEN_MCV
        )


class FrozenTokenReproducesThePreRegisteredLiteral(unittest.TestCase):
    def test_token_matches_frozen_mcv_byte_for_byte(self):
        self.assertEqual(
            stage1_frozen_v2.frozen_mapper_config_version(market_cap_range=_MCAP), FROZEN_MCV
        )

    def test_the_token_carries_the_v2_schema_tag(self):
        payload = json.loads(stage1_frozen_v2.frozen_mapper_config_version(market_cap_range=_MCAP))
        self.assertEqual(payload["schema"], "mapper-freeze-v2")

    def test_the_live_mapper_has_moved_off_the_frozen_shape(self):
        # Positive control for the whole reason this module exists. If the live
        # token ever equalled the frozen one again, the snapshot would be
        # redundant AND the cohort boundary would have been erased.
        self.assertNotEqual(theme_mapper._MAPPER_FREEZE_SCHEMA, "mapper-freeze-v2")


class FrozenSurfaceIsAByteCopyOfV2(unittest.TestCase):
    def test_the_prompt_still_requires_a_transmission_channel(self):
        prompt = stage1_frozen_v2.build_prompt_frozen(theme="ai_defense", catalyst=_catalyst())
        self.assertIn("TRANSMISSION CHANNEL", prompt)
        self.assertIn("FAVOURABLY", prompt)

    def test_the_response_schema_still_requires_the_channel(self):
        self.assertIn(
            "transmission_channel",
            stage1_frozen_v2._MAPPER_RESPONSE_SCHEMA["properties"]["candidates"]["items"][
                "required"
            ],
        )

    def test_a_channel_less_candidate_is_still_dropped(self):
        # The behaviour the LIVE normaliser reversed. The frozen replay must keep
        # it, or the retro's instrument is not the registered one.
        payload = {
            "candidates": [
                {"ticker": "GOOD", "confidence": 0.9, "transmission_channel": "a -> b -> c"},
                {"ticker": "BARE", "confidence": 0.9},
            ],
            "search_keywords": ["drone"],
        }
        with mock.patch.object(
            stage1_frozen_v2, "_call_llm", return_value=SimpleNamespace(text=json.dumps(payload))
        ):
            result = stage1_frozen_v2.propose_candidates_frozen(
                theme="ai_defense", catalyst=_catalyst(), llm_client=object()
            )
        self.assertEqual([c["ticker"] for c in result["candidates"]], ["GOOD"])

    def test_it_still_reports_the_free_text_decline_reason(self):
        payload = {"candidates": [], "no_candidates_reason": "no transmission channel"}
        with mock.patch.object(
            stage1_frozen_v2, "_call_llm", return_value=SimpleNamespace(text=json.dumps(payload))
        ):
            result = stage1_frozen_v2.propose_candidates_frozen(
                theme="ai_defense", catalyst=_catalyst(), llm_client=object()
            )
        self.assertIs(result["outcome"], theme_mapper.MapperOutcome.DECLINED)
        self.assertEqual(result["no_candidates_reason"], "no transmission channel")


class TheErrorLadderIsPartOfTheFrozenContract(unittest.TestCase):
    """The retry policy is the byte-copy's easiest thing to get subtly wrong.

    ``propose_candidates_frozen`` documents "same contract as the v2
    ``propose_candidates``": ONE re-roll on an empty body, nothing else retried.
    A copy that re-rolled a malformed payload, or that stopped re-rolling an
    empty one, would draw from a different instrument than the pre-registration
    names — and would do so silently, because both variants return a
    well-formed proposal dict.
    """

    def _propose(self, *responses):
        it = iter(responses)

        def _fake(*_args, **_kwargs):
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return SimpleNamespace(text=item)

        with mock.patch.object(stage1_frozen_v2, "_call_llm", side_effect=_fake) as call:
            result = stage1_frozen_v2.propose_candidates_frozen(
                theme="ai_defense", catalyst=_catalyst(), llm_client=object()
            )
        return result, call.call_count

    def test_an_empty_body_that_re_rolls_into_an_answer_is_a_success(self):
        payload = json.dumps(
            {
                "candidates": [
                    {"ticker": "GOOD", "confidence": 0.9, "transmission_channel": "a -> b -> c"}
                ],
                "search_keywords": ["drone"],
            }
        )
        result, calls = self._propose("", payload)
        self.assertIs(result["outcome"], theme_mapper.MapperOutcome.SUCCESS)
        self.assertEqual([c["ticker"] for c in result["candidates"]], ["GOOD"])
        self.assertEqual(calls, 2)

    def test_an_empty_body_is_re_rolled_exactly_once(self):
        result, calls = self._propose("", "   ")
        self.assertIs(result["outcome"], theme_mapper.MapperOutcome.EMPTY_PAYLOAD)
        self.assertEqual(calls, 2)

    def test_a_malformed_body_is_not_re_rolled(self):
        result, calls = self._propose("not json at all")
        self.assertIs(result["outcome"], theme_mapper.MapperOutcome.MALFORMED_PAYLOAD)
        self.assertEqual(calls, 1)

    def test_a_body_without_a_candidates_key_is_malformed(self):
        result, calls = self._propose(json.dumps({"search_keywords": ["drone"]}))
        self.assertIs(result["outcome"], theme_mapper.MapperOutcome.MALFORMED_PAYLOAD)
        self.assertEqual(calls, 1)

    def test_a_non_empty_candidates_list_that_normalises_to_nothing_is_malformed(self):
        # Distinct from a DECLINE: the model DID propose, and every entry was
        # unusable. Counting it as a decline would credit the model with a
        # judgement it never made — and would move the retro's refusal shares.
        body = json.dumps({"candidates": [{"confidence": 0.9}], "search_keywords": ["drone"]})
        result, calls = self._propose(body)
        self.assertIs(result["outcome"], theme_mapper.MapperOutcome.MALFORMED_PAYLOAD)
        self.assertEqual(calls, 1)

    def test_a_raising_call_is_a_call_failure_and_is_not_re_rolled(self):
        result, calls = self._propose(RuntimeError("socket"))
        self.assertIs(result["outcome"], theme_mapper.MapperOutcome.CALL_FAILED)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(calls, 1)

    def test_a_client_init_failure_is_a_call_failure_not_a_raise(self):
        with mock.patch.object(
            stage1_frozen_v2,
            "get_default_openrouter_client",
            side_effect=RuntimeError("no key"),
        ):
            result = stage1_frozen_v2.propose_candidates_frozen(
                theme="ai_defense", catalyst=_catalyst()
            )
        self.assertIs(result["outcome"], theme_mapper.MapperOutcome.CALL_FAILED)


if __name__ == "__main__":
    unittest.main()
