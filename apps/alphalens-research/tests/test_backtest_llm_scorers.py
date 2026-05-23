"""Characterization tests for `alphalens_research.backtest.llm_scorers.gemini_flash_tractability_scorer`.

Locks down API-error / parse-fallback / verdict-coercion behavior. Tests
inject a stub :class:`GeminiClient` via the ``gemini_client=`` kwarg so
the real google-genai SDK is never touched.
"""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_stub_gemini_client() -> MagicMock:
    """Build a MagicMock that looks enough like a GeminiClient for the
    scorer to use: it has ``generate_content`` and ``build_config``."""
    client = MagicMock()
    client.build_config.side_effect = lambda **kw: SimpleNamespace(**kw)
    return client


class TestGeminiFlashTractabilityScorer(unittest.TestCase):
    def setUp(self):
        self._client = _make_stub_gemini_client()

    def _set_response(self, text: str) -> None:
        self._client.generate_content.return_value = SimpleNamespace(text=text)

    def test_happy_path_parses_json_and_returns_verdict(self):
        from alphalens_research.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._set_response('{"verdict":"accept","confidence":0.85,"reasoning":"strong base"}')
        v = gemini_flash_tractability_scorer(
            "MSFT",
            date(2025, 6, 30),
            {"rank": 1, "momentum_score": 0.92, "themes": ["AI"]},
            gemini_client=self._client,
        )

        self.assertEqual(v.verdict, "accept")
        self.assertAlmostEqual(v.confidence, 0.85)
        self.assertEqual(v.reasoning, "strong base")
        self.assertGreater(v.cost_usd, 0.0)

    def test_falls_back_to_brace_extraction_when_preamble_present(self):
        from alphalens_research.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._set_response(
            'Here is my answer:\n{"verdict":"reject","confidence":0.3,"reasoning":"choppy"}\n--end--'
        )
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {}, gemini_client=self._client)

        self.assertEqual(v.verdict, "reject")
        self.assertAlmostEqual(v.confidence, 0.3)

    def test_returns_uncertain_when_parse_completely_fails(self):
        from alphalens_research.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._set_response("totally not json and no braces either")
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {}, gemini_client=self._client)

        self.assertEqual(v.verdict, "uncertain")
        self.assertEqual(v.confidence, 0.0)
        self.assertIn("parse error", v.reasoning)

    def test_returns_uncertain_on_api_error(self):
        from alphalens_research.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._client.generate_content.side_effect = RuntimeError("rate limit")
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {}, gemini_client=self._client)

        self.assertEqual(v.verdict, "uncertain")
        self.assertEqual(v.cost_usd, 0.0)
        self.assertIn("Gemini API error", v.reasoning)

    def test_invalid_verdict_value_is_coerced_to_uncertain(self):
        from alphalens_research.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._set_response('{"verdict":"strong-buy","confidence":0.9,"reasoning":"ok"}')
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {}, gemini_client=self._client)

        self.assertEqual(v.verdict, "uncertain")

    def test_reasoning_truncated_to_280_chars(self):
        from alphalens_research.backtest.llm_scorers import gemini_flash_tractability_scorer

        long_reason = "x" * 500
        self._set_response(f'{{"verdict":"accept","confidence":0.5,"reasoning":"{long_reason}"}}')
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {}, gemini_client=self._client)

        self.assertEqual(len(v.reasoning), 280)

    def test_missing_api_key_degrades_to_uncertain(self):
        """Per zen pre-merge HIGH 2026-05-20: when neither gemini_client nor
        api_key is supplied AND GOOGLE_API_KEY is unset,
        get_default_gemini_client() raises ValueError but it is caught and
        the scorer returns LLMVerdict('uncertain'). This keeps the
        historical_validation loop from crashing on misconfigured nodes."""
        from alphalens_pipeline.data.alt_data import gemini_client as gc_mod
        from alphalens_research.backtest.llm_scorers import gemini_flash_tractability_scorer

        gc_mod._reset_default_client_for_tests()
        try:
            with patch.dict("os.environ", {}, clear=True):
                v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {})
            self.assertEqual(v.verdict, "uncertain")
            self.assertIn("GOOGLE_API_KEY", v.reasoning)
        finally:
            gc_mod._reset_default_client_for_tests()


if __name__ == "__main__":
    unittest.main()
