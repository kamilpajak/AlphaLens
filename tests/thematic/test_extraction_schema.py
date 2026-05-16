import json
import unittest

from alphalens.thematic.extraction import schema


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
