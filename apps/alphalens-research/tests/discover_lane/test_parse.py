import unittest

from alphalens_research.discover_lane.parse import parse_discover_response

_SOURCES = [{"url": "https://a.com"}, {"url": "https://b.com"}]


class TestParse(unittest.TestCase):
    def test_parses_well_formed(self):
        content = (
            '{"stories": [{"event_title": "AI chips", "event_url": "https://a.com",'
            ' "beneficiaries": ['
            '{"ticker": "nvda", "company": "NVIDIA", "reason": "AI demand"},'
            '{"ticker": "AMD", "company": "AMD", "reason": "GPU share"}]}]}'
        )
        out = parse_discover_response(content, _SOURCES)
        self.assertEqual([c.ticker for c in out], ["NVDA", "AMD"])
        self.assertEqual(out[0].citation_count, 2)
        self.assertEqual(out[0].theme, "AI chips")
        self.assertEqual(out[0].source_event_url, "https://a.com")

    def test_skips_malformed_entries(self):
        content = (
            '{"stories": [{"event_title": "x", "event_url": "u", "beneficiaries": ['
            '{"ticker": "", "company": "no ticker", "reason": "r"},'
            '"notadict",'
            '{"ticker": "GOOD", "company": "Good Co", "reason": "r"}]}]}'
        )
        out = parse_discover_response(content, _SOURCES)
        self.assertEqual([c.ticker for c in out], ["GOOD"])

    def test_handles_code_fenced_json(self):
        content = '```json\n{"stories": [{"event_title": "t", "event_url": "u", "beneficiaries": [{"ticker": "X", "company": "X Co", "reason": "r"}]}]}\n```'
        out = parse_discover_response(content, _SOURCES)
        self.assertEqual([c.ticker for c in out], ["X"])

    def test_non_json_returns_empty(self):
        self.assertEqual(parse_discover_response("sorry, no JSON here", _SOURCES), [])
