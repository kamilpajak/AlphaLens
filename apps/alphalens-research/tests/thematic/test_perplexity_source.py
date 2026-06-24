import unittest

from alphalens_pipeline.thematic.sources import perplexity


class TestPerplexityHelpers(unittest.TestCase):
    def test_prompt_has_date_and_no_source_steering(self):
        p = perplexity.build_prompt("2026-06-12")
        self.assertIn("2026-06-12", p)
        self.assertIn("JSON", p)
        low = p.lower()
        for banned in (
            "reuters",
            "bloomberg",
            "avoid blog",
            "reddit",
            "reputable",
            "price target",
            "market cap",
        ):
            self.assertNotIn(banned, low)

    def test_parse_well_formed(self):
        content = (
            '{"stories": [{"headline": "SpaceX IPO", "summary": "Debut.", "url": "https://a.com"},'
            '{"headline": "Iran deal", "summary": "Oil falls.", "url": "https://b.com"}]}'
        )
        out = perplexity.parse_stories(content)
        self.assertEqual([s["headline"] for s in out], ["SpaceX IPO", "Iran deal"])
        self.assertEqual(out[0]["url"], "https://a.com")

    def test_parse_tolerates_trailing_prose_and_fence(self):
        content = '```json\n{"stories": [{"headline": "H", "summary": "S", "url": "u"}]}\n```\nHope this helps!'
        out = perplexity.parse_stories(content)
        self.assertEqual([s["headline"] for s in out], ["H"])

    def test_parse_skips_malformed_and_nonjson(self):
        self.assertEqual(perplexity.parse_stories("sorry, no json"), [])
        content = '{"stories": ["notadict", {"headline": "", "summary": "s", "url": "u"}, {"headline": "OK", "summary": "s", "url": "u"}]}'
        self.assertEqual([s["headline"] for s in perplexity.parse_stories(content)], ["OK"])

    def test_stable_id_deterministic(self):
        self.assertEqual(
            perplexity._stable_id("https://a.com"), perplexity._stable_id("https://a.com")
        )
        self.assertNotEqual(
            perplexity._stable_id("https://a.com"), perplexity._stable_id("https://b.com")
        )
