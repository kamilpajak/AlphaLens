import unittest

from alphalens_research.discover_lane.prompt import build_discover_prompt


class TestPrompt(unittest.TestCase):
    def test_contains_date_and_required_fields(self):
        p = build_discover_prompt("2026-06-23")
        self.assertIn("2026-06-23", p)
        for token in ("ticker", "company", "reason", "event", "JSON"):
            self.assertIn(token, p)

    def test_no_numeric_bracket_constraints(self):
        p = build_discover_prompt("2026-06-23").lower()
        for banned in ("market cap", "market-cap", "small-cap", "mid-cap", "p/e", "valuation"):
            self.assertNotIn(banned, p)
