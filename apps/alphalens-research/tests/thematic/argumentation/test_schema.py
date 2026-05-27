import unittest

from alphalens_pipeline.thematic.argumentation import schema


class TestBriefResponseSchema(unittest.TestCase):
    def test_has_required_brief_fields(self):
        s = schema.BRIEF_RESPONSE_SCHEMA
        self.assertEqual(s["type"], "object")
        required = set(s["required"])
        # The 4 LLM-composed prose fields. Entry/exit price levels are NOT
        # here — they are deterministic (brief_trade_setup); entry_price_note
        # was removed when the Trade-Setup ladder replaced it (2026-05-27).
        expected = {
            "tldr",
            "supply_chain_reasoning",
            "bear_summary",
            "catalyst_failure_exit",
        }
        self.assertEqual(required, expected)

    def test_all_fields_are_strings(self):
        for field, props in schema.BRIEF_RESPONSE_SCHEMA["properties"].items():
            self.assertEqual(props["type"], "string", f"{field} must be a string")


if __name__ == "__main__":
    unittest.main()
