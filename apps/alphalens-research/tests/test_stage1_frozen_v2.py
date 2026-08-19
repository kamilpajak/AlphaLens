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


if __name__ == "__main__":
    unittest.main()
