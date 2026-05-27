"""Pin the live Gemini model ids used by the thematic pipeline.

Regression guard for the 2026-05-27 production outage: Google retired
``gemini-3-pro-preview`` (``generateContent`` returned HTTP 404
"no longer available"), so the theme→beneficiary mapper failed for every
theme and the daily brief came out with zero candidates. The selection
tests reference these constants symbolically, so they could not catch a
constant that points at a retired model. This test pins the current ids
and blocks the known-dead ones.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.thematic.argumentation import generator
from alphalens_pipeline.thematic.extraction import gemini_flash
from alphalens_pipeline.thematic.mapping import gemini_mapper

# Models that Google has retired for generateContent. A prod constant
# pointing at any of these silently zeroes out the daily brief.
RETIRED_MODELS = frozenset({"gemini-3-pro-preview"})

CURRENT_PRO_MODEL = "gemini-3.1-pro-preview"
CURRENT_FLASH_MODEL = "gemini-3.5-flash"


class TestThematicModelIds(unittest.TestCase):
    def test_pro_model_ids_are_current(self) -> None:
        self.assertEqual(gemini_mapper.DEFAULT_MODEL, CURRENT_PRO_MODEL)
        self.assertEqual(generator.PRO_MODEL, CURRENT_PRO_MODEL)

    def test_flash_model_ids_are_current(self) -> None:
        self.assertEqual(generator.FLASH_MODEL, CURRENT_FLASH_MODEL)
        self.assertEqual(gemini_flash.DEFAULT_MODEL, CURRENT_FLASH_MODEL)

    def test_no_prod_constant_points_at_a_retired_model(self) -> None:
        in_use = {
            "gemini_mapper.DEFAULT_MODEL": gemini_mapper.DEFAULT_MODEL,
            "generator.PRO_MODEL": generator.PRO_MODEL,
            "generator.FLASH_MODEL": generator.FLASH_MODEL,
            "gemini_flash.DEFAULT_MODEL": gemini_flash.DEFAULT_MODEL,
        }
        for name, model in in_use.items():
            self.assertNotIn(
                model,
                RETIRED_MODELS,
                msg=f"{name} points at retired model {model!r}",
            )


if __name__ == "__main__":
    unittest.main()
