"""Characterization tests for `alphalens_research.backtest.llm_scorers.llm_tractability_scorer`.

Locks down API-error / parse-fallback / verdict-coercion behavior. Tests
inject a stub :class:`OpenRouterClient` via the ``llm_client=`` kwarg so
the real OpenRouter HTTP path is never touched.
"""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from alphalens_pipeline.data.alt_data.openrouter_client import OpenRouterClient


def _make_stub_llm_client() -> MagicMock:
    """Build a MagicMock spec'd to OpenRouterClient so the scorer can use its
    ``generate_content`` / ``build_config`` surface.

    ``spec=OpenRouterClient`` pins the attribute names: if the real client ever
    renames a method the scorer calls, these tests fail loudly instead of
    passing vacuously against a bare MagicMock (zen behaviour-lens 2026-05-31)."""
    client = MagicMock(spec=OpenRouterClient)
    client.build_config.side_effect = SimpleNamespace
    return client


class TestLLMTractabilityScorer(unittest.TestCase):
    def setUp(self):
        self._client = _make_stub_llm_client()

    def _set_response(self, text: str) -> None:
        self._client.generate_content.return_value = SimpleNamespace(text=text)

    def test_happy_path_parses_json_and_returns_verdict(self):
        from alphalens_research.backtest.llm_scorers import llm_tractability_scorer

        self._set_response('{"verdict":"accept","confidence":0.85,"reasoning":"strong base"}')
        v = llm_tractability_scorer(
            "MSFT",
            date(2025, 6, 30),
            {"rank": 1, "momentum_score": 0.92, "themes": ["AI"]},
            llm_client=self._client,
        )

        self.assertEqual(v.verdict, "accept")
        self.assertAlmostEqual(v.confidence, 0.85)
        self.assertEqual(v.reasoning, "strong base")
        self.assertGreater(v.cost_usd, 0.0)

    def test_falls_back_to_brace_extraction_when_preamble_present(self):
        from alphalens_research.backtest.llm_scorers import llm_tractability_scorer

        self._set_response(
            'Here is my answer:\n{"verdict":"reject","confidence":0.3,"reasoning":"choppy"}\n--end--'
        )
        v = llm_tractability_scorer("X", date(2025, 1, 1), {}, llm_client=self._client)

        self.assertEqual(v.verdict, "reject")
        self.assertAlmostEqual(v.confidence, 0.3)

    def test_returns_uncertain_when_parse_completely_fails(self):
        from alphalens_research.backtest.llm_scorers import llm_tractability_scorer

        self._set_response("totally not json and no braces either")
        v = llm_tractability_scorer("X", date(2025, 1, 1), {}, llm_client=self._client)

        self.assertEqual(v.verdict, "uncertain")
        self.assertEqual(v.confidence, 0.0)
        self.assertIn("parse error", v.reasoning)

    def test_returns_uncertain_on_api_error(self):
        from alphalens_research.backtest.llm_scorers import llm_tractability_scorer

        self._client.generate_content.side_effect = RuntimeError("rate limit")
        v = llm_tractability_scorer("X", date(2025, 1, 1), {}, llm_client=self._client)

        self.assertEqual(v.verdict, "uncertain")
        self.assertEqual(v.cost_usd, 0.0)
        self.assertIn("LLM API error", v.reasoning)

    def test_invalid_verdict_value_is_coerced_to_uncertain(self):
        from alphalens_research.backtest.llm_scorers import llm_tractability_scorer

        self._set_response('{"verdict":"strong-buy","confidence":0.9,"reasoning":"ok"}')
        v = llm_tractability_scorer("X", date(2025, 1, 1), {}, llm_client=self._client)

        self.assertEqual(v.verdict, "uncertain")

    def test_reasoning_truncated_to_280_chars(self):
        from alphalens_research.backtest.llm_scorers import llm_tractability_scorer

        long_reason = "x" * 500
        self._set_response(f'{{"verdict":"accept","confidence":0.5,"reasoning":"{long_reason}"}}')
        v = llm_tractability_scorer("X", date(2025, 1, 1), {}, llm_client=self._client)

        self.assertEqual(len(v.reasoning), 280)

    def test_missing_api_key_degrades_to_uncertain(self):
        """Per zen pre-merge HIGH 2026-05-20: when neither llm_client nor
        api_key is supplied AND OPENROUTER_API_KEY is unset,
        get_default_openrouter_client() raises ValueError but it is caught and
        the scorer returns LLMVerdict('uncertain'). This keeps the
        historical_validation loop from crashing on misconfigured nodes."""
        from alphalens_pipeline.data.alt_data import openrouter_client as or_mod
        from alphalens_research.backtest.llm_scorers import llm_tractability_scorer

        or_mod._reset_default_client_for_tests()
        try:
            with patch.dict("os.environ", {}, clear=True):
                v = llm_tractability_scorer("X", date(2025, 1, 1), {})
            self.assertEqual(v.verdict, "uncertain")
            self.assertIn("OPENROUTER_API_KEY", v.reasoning)
        finally:
            or_mod._reset_default_client_for_tests()


if __name__ == "__main__":
    unittest.main()
