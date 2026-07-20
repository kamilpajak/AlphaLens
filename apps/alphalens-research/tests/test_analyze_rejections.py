"""Tests for the ad-hoc rejection-analysis script's LLM classifier.

Guards the empty-content bug class (see PR #869): a structurally-valid but
semantically-empty LLM JSON response (`{}` or all-blank fields) must be bucketed
as a visible ``empty_content`` sentinel, NOT silently folded into ``unknown`` by
the downstream ``r.get("primary_reason", "unknown")`` aggregation.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from scripts.analyze_rejections import classify_with_llm


class _FakeClient:
    """Minimal OpenRouter-shaped stub: build_config + generate_content(text=...)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def build_config(self, **_kwargs):
        return {}

    def generate_content(self, *, model, contents, config):
        _ = (model, contents, config)
        return SimpleNamespace(text=self._text)


def _row() -> dict:
    return {
        "ticker": "TEST",
        "date": "2026-07-20",
        "rating": "SELL",
        "verdict_text": "Rejected: valuation extreme, negative FCF.",
    }


class TestClassifyWithLlmEmptyContent(unittest.TestCase):
    def test_empty_object_bucketed_as_empty_content(self):
        # Valid JSON, but no classification content — must NOT fold into 'unknown'.
        result = classify_with_llm(_row(), _FakeClient(json.dumps({})))
        self.assertEqual(result["primary_reason"], "empty_content")

    def test_blank_primary_reason_bucketed_as_empty_content(self):
        # A present-but-blank primary_reason is still no content.
        payload = {"primary_reason": "   ", "red_flags": [], "suggested_scorer_filter": ""}
        result = classify_with_llm(_row(), _FakeClient(json.dumps(payload)))
        self.assertEqual(result["primary_reason"], "empty_content")

    def test_substantive_classification_passes_through(self):
        payload = {"primary_reason": "valuation_extreme", "red_flags": ["P/S >100"]}
        result = classify_with_llm(_row(), _FakeClient(json.dumps(payload)))
        self.assertEqual(result["primary_reason"], "valuation_extreme")
        self.assertEqual(result["red_flags"], ["P/S >100"])

    def test_malformed_json_still_parse_error(self):
        # Pre-existing guard must be untouched: unparseable -> parse_error sentinel.
        result = classify_with_llm(_row(), _FakeClient("not json"))
        self.assertEqual(result["primary_reason"], "parse_error")


if __name__ == "__main__":
    unittest.main()
