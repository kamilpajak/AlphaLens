"""Pin the live LLM model ids used by the thematic pipeline.

Regression guard for the 2026-05-27 production outage: a vendor retired
the pro-tier model and ``generateContent`` returned HTTP 404 "no longer
available", so the theme→beneficiary mapper failed for every theme and
the daily brief came out with zero candidates. The selection tests
reference these constants symbolically, so they could not catch a
constant that points at a retired model.

PR-G (2026-05-30) swapped Gemini → DeepSeek v4 via OpenRouter:
* ``gemini-3.5-flash``        → ``deepseek/deepseek-v4-flash``
* ``gemini-3.1-pro-preview``  → ``deepseek/deepseek-v4-pro``

This test pins the current OpenRouter slugs and blocks the legacy
Gemini ids that would silently fail post-swap (OPENROUTER_API_KEY
auth, Gemini ids → 404).
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.thematic.argumentation import generator
from alphalens_pipeline.thematic.extraction import event_extractor
from alphalens_pipeline.thematic.mapping import theme_mapper

# Models retired upstream (Gemini ids no longer routable under the new
# OpenRouter-backed pipeline; any prod constant pointing at these would
# 404 on every call).
RETIRED_MODELS = frozenset(
    {
        "gemini-3-pro-preview",  # Google retired 2026-05-26 (PR #257 fix)
        "gemini-3.1-pro-preview",  # superseded by deepseek/deepseek-v4-pro in PR-G
        "gemini-3.5-flash",  # superseded by deepseek/deepseek-v4-flash in PR-G
    }
)

CURRENT_PRO_MODEL = "deepseek/deepseek-v4-pro"
CURRENT_FLASH_MODEL = "deepseek/deepseek-v4-flash"


class TestThematicModelIds(unittest.TestCase):
    def test_pro_model_ids_are_current(self) -> None:
        self.assertEqual(theme_mapper.DEFAULT_MODEL, CURRENT_PRO_MODEL)
        self.assertEqual(generator.PRO_MODEL, CURRENT_PRO_MODEL)

    def test_flash_model_ids_are_current(self) -> None:
        self.assertEqual(generator.FLASH_MODEL, CURRENT_FLASH_MODEL)
        self.assertEqual(event_extractor.DEFAULT_MODEL, CURRENT_FLASH_MODEL)

    def test_no_prod_constant_points_at_a_retired_model(self) -> None:
        in_use = {
            "theme_mapper.DEFAULT_MODEL": theme_mapper.DEFAULT_MODEL,
            "generator.PRO_MODEL": generator.PRO_MODEL,
            "generator.FLASH_MODEL": generator.FLASH_MODEL,
            "event_extractor.DEFAULT_MODEL": event_extractor.DEFAULT_MODEL,
        }
        for name, model in in_use.items():
            self.assertNotIn(
                model,
                RETIRED_MODELS,
                msg=f"{name} points at retired model {model!r}",
            )


if __name__ == "__main__":
    unittest.main()
