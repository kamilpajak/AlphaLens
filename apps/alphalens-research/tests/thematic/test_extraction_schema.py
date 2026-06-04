import json
import unittest

from alphalens_pipeline.thematic.extraction import schema


class TestEventSchema(unittest.TestCase):
    def test_event_type_enum_covers_design_memo_values(self):
        # Per design memo §2: product_launch | M&A | regulatory | partnership | ...
        for t in [
            "product_launch",
            "m_and_a",
            "regulatory",
            "partnership",
            "earnings",
            "analyst",
            "macro",
            "other",
        ]:
            self.assertIn(t, schema.EVENT_TYPES)

    def test_sentiment_enum(self):
        self.assertEqual(set(schema.SENTIMENTS), {"positive", "negative", "neutral"})

    def test_response_schema_is_json_schema_dict(self):
        s = schema.EVENT_RESPONSE_SCHEMA
        self.assertEqual(s["type"], "object")
        self.assertIn("properties", s)
        self.assertIn("event_type", s["properties"])
        self.assertIn("themes", s["properties"])
        self.assertIn("sentiment", s["properties"])
        self.assertIn("confidence", s["properties"])
        self.assertEqual(set(s["properties"]["event_type"]["enum"]), set(schema.EVENT_TYPES))


class TestRefinedEventTaxonomy(unittest.TestCase):
    """The 2026-05-18 noise-filter audit found 58% of events were landing in
    'other' because legit catalysts (bankruptcy, financing, exec_change,
    breach, ipo, …) had no dedicated label. Per Perplexity research on
    financial NLP event taxonomies (TAXMORPH, TAXREC, FINEED), the right
    fix is to expand the enum to ~20-30 well-chosen types covering canonical
    catalysts AND adding an explicit non-catalyst branch."""

    def test_enum_covers_canonical_catalyst_subtypes(self):
        # Catalyst sub-types observed in real data 2026-05-18 'other' bucket
        # OR called out by Perplexity research as standard event-driven labels.
        for t in [
            "bankruptcy",
            "financing",
            "ipo",
            "secondary",
            "dividend",
            "buyback",
            "spinoff",
            "restructuring",
            "activist_position",
            "exec_change",
            "board_change",
            "strike",
            "layoffs",
            "litigation",
            "settlement",
            "investigation",
            "recall",
            "breach",
            "contract_award",
            "product_retirement",
            "rating_change",
            "price_target",
            "geopolitical",
            "central_bank",
        ]:
            self.assertIn(t, schema.EVENT_TYPES, f"missing canonical catalyst type: {t}")

    def test_enum_includes_explicit_noise_branch(self):
        # Per Perplexity §5.3: standard practice is an explicit
        # non-market-moving branch separate from 'other'.
        for t in ["opinion", "lifestyle", "listicle", "promo", "evergreen", "sponsored"]:
            self.assertIn(t, schema.EVENT_TYPES, f"missing noise type: {t}")

    def test_noise_event_types_helper_exposes_subset(self):
        # NOISE_EVENT_TYPES is the canonical handle used by downstream
        # filters (catalyst_resolver) so the "what counts as noise" list
        # lives in one place.
        self.assertTrue(hasattr(schema, "NOISE_EVENT_TYPES"))
        self.assertEqual(
            set(schema.NOISE_EVENT_TYPES),
            {"opinion", "lifestyle", "listicle", "promo", "evergreen", "sponsored"},
        )
        # All noise types must also be in the master enum (consistency).
        for t in schema.NOISE_EVENT_TYPES:
            self.assertIn(t, schema.EVENT_TYPES)

    def test_normalize_accepts_new_catalyst_types(self):
        for new_type in ["bankruptcy", "financing", "exec_change", "breach", "ipo"]:
            normalized = schema.normalize_extraction(
                {
                    "event_type": new_type,
                    "themes": ["whatever"],
                    "sentiment": "neutral",
                    "confidence": 0.5,
                }
            )
            self.assertEqual(normalized["event_type"], new_type)

    def test_normalize_accepts_noise_types(self):
        for noise_type in ["promo", "lifestyle", "listicle"]:
            normalized = schema.normalize_extraction(
                {
                    "event_type": noise_type,
                    "themes": ["whatever"],
                    "sentiment": "neutral",
                    "confidence": 0.5,
                }
            )
            self.assertEqual(normalized["event_type"], noise_type)

    def test_every_event_type_normalizes_to_itself(self):
        # Parametric guard: any future enum rename / typo that breaks
        # round-trip identity (canonical → normalize → same canonical) gets
        # caught immediately. Zen pre-push review L2 follow-up.
        for t in schema.EVENT_TYPES:
            normalized = schema.normalize_extraction({"event_type": t})
            self.assertEqual(
                normalized["event_type"],
                t,
                f"event_type {t!r} did not round-trip through normalize_extraction",
            )

    def test_legacy_other_still_supported(self):
        # Backward compat: existing rows with 'other' continue to parse
        # without coercion, and unknown values still coerce to 'other'.
        normalized = schema.normalize_extraction({"event_type": "other"})
        self.assertEqual(normalized["event_type"], "other")


class TestParseExtraction(unittest.TestCase):
    def test_parses_well_formed_json(self):
        raw = json.dumps(
            {
                "event_type": "product_launch",
                "primary_entities": ["NVDA"],
                "themes": ["quantum_computing", "AI_quantum_hybrid"],
                "sentiment": "positive",
                "second_order_implications": ["QUBT may benefit"],
                "confidence": 0.85,
            }
        )
        parsed = schema.parse_extraction(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["event_type"], "product_launch")
        self.assertEqual(parsed["sentiment"], "positive")
        self.assertEqual(parsed["confidence"], 0.85)

    def test_parses_json_with_preamble(self):
        raw = 'Sure thing! Here is the answer:\n```json\n{"event_type":"earnings","themes":["semiconductors"],"sentiment":"positive","confidence":0.7}\n```'
        parsed = schema.parse_extraction(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["event_type"], "earnings")

    def test_returns_none_on_invalid_json(self):
        self.assertIsNone(schema.parse_extraction("not json at all"))
        self.assertIsNone(schema.parse_extraction(""))

    def test_normalize_lowercases_event_type_and_sentiment(self):
        normalized = schema.normalize_extraction(
            {
                "event_type": "Product_Launch",
                "primary_entities": ["nvda"],
                "themes": ["Quantum Computing"],
                "sentiment": "POSITIVE",
                "second_order_implications": [],
                "confidence": 0.9,
            }
        )
        self.assertEqual(normalized["event_type"], "product_launch")
        self.assertEqual(normalized["sentiment"], "positive")
        self.assertEqual(normalized["primary_entities"], ["NVDA"])

    def test_normalize_slugifies_themes_and_drops_empties(self):
        normalized = schema.normalize_extraction(
            {
                "event_type": "earnings",
                "themes": ["AI ethics", "defense_procurement", "oil & gas", "   "],
                "sentiment": "neutral",
                "confidence": 0.5,
            }
        )
        # Mixed-format themes canonicalise to slugs; the all-whitespace one drops.
        self.assertEqual(normalized["themes"], ["ai_ethics", "defense_procurement", "oil_gas"])

    def test_normalize_coerces_unknown_event_type_to_other(self):
        normalized = schema.normalize_extraction(
            {
                "event_type": "alien_invasion",
                "primary_entities": [],
                "themes": [],
                "sentiment": "neutral",
                "second_order_implications": [],
                "confidence": 0.1,
            }
        )
        self.assertEqual(normalized["event_type"], "other")

    def test_normalize_fills_missing_optionals(self):
        normalized = schema.normalize_extraction(
            {
                "event_type": "earnings",
                "themes": ["semiconductors"],
                "sentiment": "positive",
                "confidence": 0.6,
            }
        )
        self.assertEqual(normalized["primary_entities"], [])
        self.assertEqual(normalized["second_order_implications"], [])

    def test_normalize_clamps_confidence_to_0_1(self):
        for raw, expected in [(1.5, 1.0), (-0.2, 0.0), (0.7, 0.7)]:
            n = schema.normalize_extraction(
                {
                    "event_type": "other",
                    "themes": [],
                    "sentiment": "neutral",
                    "confidence": raw,
                }
            )
            self.assertEqual(n["confidence"], expected)


if __name__ == "__main__":
    unittest.main()
