import unittest
from unittest import mock

from alphalens_pipeline.literature_scanner.perplexity_client import (
    AskResult,
    PerplexityClient,
)

_FAKE_RESPONSE = {
    "choices": [{"message": {"content": '{"stories": []}'}}],
    "citations": ["https://a.com", "https://b.com"],
    "search_results": [
        {"title": "A", "url": "https://a.com", "date": "2026-06-23"},
        {"title": "B", "url": "https://b.com", "date": "2026-06-22"},
    ],
}


class TestAskWithCitations(unittest.TestCase):
    def test_parses_content_and_sources(self):
        client = PerplexityClient(api_key="k")
        fake = mock.Mock()
        fake.json.return_value = _FAKE_RESPONSE
        fake.raise_for_status.return_value = None
        with mock.patch(
            "alphalens_pipeline.literature_scanner.perplexity_client.requests.post",
            return_value=fake,
        ) as post:
            result = client.ask_with_citations(
                "q",
                search_after_date_filter="06/16/2026",
                search_before_date_filter="06/23/2026",
            )
        self.assertIsInstance(result, AskResult)
        self.assertEqual(result.content, '{"stories": []}')
        self.assertEqual(result.citations, ["https://a.com", "https://b.com"])
        self.assertEqual(len(result.search_results), 2)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["search_after_date_filter"], "06/16/2026")
        self.assertEqual(sent["search_before_date_filter"], "06/23/2026")

    def test_missing_sources_default_empty(self):
        client = PerplexityClient(api_key="k")
        fake = mock.Mock()
        fake.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        fake.raise_for_status.return_value = None
        with mock.patch(
            "alphalens_pipeline.literature_scanner.perplexity_client.requests.post",
            return_value=fake,
        ):
            result = client.ask_with_citations("q")
        self.assertEqual(result.content, "x")
        self.assertEqual(result.citations, [])
        self.assertEqual(result.search_results, [])
