"""Characterization tests for `alphalens.backtest.llm_scorers.gemini_flash_tractability_scorer`.

Locks down API-error / parse-fallback / verdict-coercion behavior so the
planned cognitive-complexity refactor (issue #26 Tier 3) can be validated.
External Gemini SDK is mocked via `_load_genai_sdk` helper to avoid sys.modules
pollution (which would break downstream tests that import the real SDK).
"""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_genai_stubs() -> tuple[MagicMock, MagicMock, MagicMock]:
    fake_client = MagicMock()
    fake_genai = MagicMock()
    fake_genai.Client.return_value = fake_client
    fake_types = MagicMock()
    return fake_genai, fake_types, fake_client


class TestGeminiFlashTractabilityScorer(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict("os.environ", {"GOOGLE_API_KEY": "stub-key"})
        self._env_patch.start()
        self._fake_genai, self._fake_types, self._client = _make_genai_stubs()
        self._sdk_patch = patch(
            "alphalens.backtest.llm_scorers._load_genai_sdk",
            return_value=(self._fake_genai, self._fake_types),
        )
        self._sdk_patch.start()

    def tearDown(self):
        self._sdk_patch.stop()
        self._env_patch.stop()

    def _set_response(self, text: str) -> None:
        self._client.models.generate_content.return_value = SimpleNamespace(text=text)

    def test_happy_path_parses_json_and_returns_verdict(self):
        from alphalens.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._set_response('{"verdict":"accept","confidence":0.85,"reasoning":"strong base"}')
        v = gemini_flash_tractability_scorer(
            "MSFT", date(2025, 6, 30), {"rank": 1, "momentum_score": 0.92, "themes": ["AI"]}
        )

        self.assertEqual(v.verdict, "accept")
        self.assertAlmostEqual(v.confidence, 0.85)
        self.assertEqual(v.reasoning, "strong base")
        self.assertGreater(v.cost_usd, 0.0)

    def test_falls_back_to_brace_extraction_when_preamble_present(self):
        from alphalens.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._set_response(
            'Here is my answer:\n{"verdict":"reject","confidence":0.3,"reasoning":"choppy"}\n--end--'
        )
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {})

        self.assertEqual(v.verdict, "reject")
        self.assertAlmostEqual(v.confidence, 0.3)

    def test_returns_uncertain_when_parse_completely_fails(self):
        from alphalens.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._set_response("totally not json and no braces either")
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {})

        self.assertEqual(v.verdict, "uncertain")
        self.assertEqual(v.confidence, 0.0)
        self.assertIn("parse error", v.reasoning)

    def test_returns_uncertain_on_api_error(self):
        from alphalens.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._client.models.generate_content.side_effect = RuntimeError("rate limit")
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {})

        self.assertEqual(v.verdict, "uncertain")
        self.assertEqual(v.cost_usd, 0.0)
        self.assertIn("Gemini API error", v.reasoning)

    def test_invalid_verdict_value_is_coerced_to_uncertain(self):
        from alphalens.backtest.llm_scorers import gemini_flash_tractability_scorer

        self._set_response('{"verdict":"strong-buy","confidence":0.9,"reasoning":"ok"}')
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {})

        self.assertEqual(v.verdict, "uncertain")

    def test_reasoning_truncated_to_280_chars(self):
        from alphalens.backtest.llm_scorers import gemini_flash_tractability_scorer

        long_reason = "x" * 500
        self._set_response(f'{{"verdict":"accept","confidence":0.5,"reasoning":"{long_reason}"}}')
        v = gemini_flash_tractability_scorer("X", date(2025, 1, 1), {})

        self.assertEqual(len(v.reasoning), 280)

    def test_missing_api_key_raises(self):
        from alphalens.backtest.llm_scorers import gemini_flash_tractability_scorer

        with patch.dict("os.environ", {}, clear=True), self.assertRaises(RuntimeError) as cm:
            gemini_flash_tractability_scorer("X", date(2025, 1, 1), {})

        self.assertIn("GOOGLE_API_KEY", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
