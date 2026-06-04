"""Unit tests for the canonical theme-string helpers."""

from __future__ import annotations

import unittest

from alphalens_pipeline.thematic.theme_text import slugify_theme


class TestSlugifyTheme(unittest.TestCase):
    def test_space_separated_becomes_underscore_slug(self):
        self.assertEqual(slugify_theme("AI ethics"), "ai_ethics")
        self.assertEqual(slugify_theme("high gas prices"), "high_gas_prices")

    def test_already_slug_is_unchanged_but_lowercased(self):
        self.assertEqual(slugify_theme("defense_procurement"), "defense_procurement")
        self.assertEqual(slugify_theme("AI_chatbots"), "ai_chatbots")

    def test_punctuation_and_repeats_collapse(self):
        self.assertEqual(slugify_theme("oil & gas"), "oil_gas")
        self.assertEqual(slugify_theme("AI / ML"), "ai_ml")
        self.assertEqual(slugify_theme("5G  rollout"), "5g_rollout")

    def test_trims_leading_trailing_separators_and_whitespace(self):
        self.assertEqual(slugify_theme("  spaced  "), "spaced")
        self.assertEqual(slugify_theme("__weird__"), "weird")
        self.assertEqual(slugify_theme("-dash-"), "dash")

    def test_empty_and_all_separator_yield_empty(self):
        self.assertEqual(slugify_theme(""), "")
        self.assertEqual(slugify_theme("   "), "")
        self.assertEqual(slugify_theme("&&&"), "")

    def test_idempotent(self):
        for raw in ["AI ethics", "oil & gas", "defense_procurement", "5G rollout"]:
            once = slugify_theme(raw)
            self.assertEqual(slugify_theme(once), once)


if __name__ == "__main__":
    unittest.main()
