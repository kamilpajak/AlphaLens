"""Tests for the qualitative Buffett LLM layer (#506).

`assess_qualitative` runs DeepSeek Pro over 10-K section text + PRE-COMPUTED
numeric facts to CLASSIFY (not compute) three Buffett qualities:

* F0 — business understandability (bool)
* F3 — moat type + trend (enums)
* F4 — management candor (enum)

DOCTRINE (CLAUDE.md "LLM training-cutoff blindness"): numbers come from
authoritative sources, pre-computed in Python, INJECTED into the prompt as
facts; the LLM emits NO numbers. The enforcement guard tested here is:

* (a) ``_QUALITATIVE_RESPONSE_SCHEMA`` has ZERO numeric ("number"/"integer")
  typed properties (recursively), and
* (b) the injected fact VALUE strings appear in the built prompt (so the model
  reasons over real numbers it did not have to recall), and
* (c) the prompt instructs the model NOT to estimate or output numbers.

All tests are hermetic — the OpenRouter client is a MagicMock or the
``_call_llm`` seam is patched; no real network call is ever made.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from alphalens_pipeline.buffett.qualitative import (
    _QUALITATIVE_RESPONSE_SCHEMA,
    QualitativeAssessment,
    assess_qualitative,
    build_qualitative_prompt,
)
from alphalens_pipeline.buffett.tenk_sections import TenKSections
from alphalens_pipeline.data.alt_data.openrouter_client import OpenRouterClient

_SECTIONS = TenKSections(
    item_1="We design and sell enterprise widgets with recurring software revenue.",
    item_1a="Competition from larger incumbents could erode our pricing power.",
    item_7="Margins expanded as customers renewed multi-year contracts.",
)

_FACTS = {
    "roic_latest": 17.0,
    "roic_3y_avg": 15.5,
    "op_margin_latest": 22.0,
    "op_margin_3y_avg": 20.0,
    "net_buyback": True,
}

_GOOD_JSON = (
    '{"understandable": true, "moat_type": "switching_cost", '
    '"moat_trend": "widening", "management_candor": "candid", '
    '"rationale": "Recurring software revenue and multi-year contracts."}'
)


def _make_stub_llm_client() -> MagicMock:
    client = MagicMock(spec=OpenRouterClient)
    client.build_config.side_effect = SimpleNamespace
    return client


class TestResponseSchemaHasNoNumbers(unittest.TestCase):
    """Doctrine guard (a): the LLM output schema emits no numeric field."""

    def _walk_types(self, node) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            t = node.get("type")
            if isinstance(t, str):
                found.append(t)
            elif isinstance(t, list):
                found.extend(x for x in t if isinstance(x, str))
            for value in node.values():
                found.extend(self._walk_types(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(self._walk_types(item))
        return found

    def test_no_numeric_typed_properties(self):
        types = self._walk_types(_QUALITATIVE_RESPONSE_SCHEMA)
        self.assertNotIn("number", types)
        self.assertNotIn("integer", types)
        # Sanity: the schema is non-trivial (string/boolean/enum present).
        self.assertTrue({"object", "string", "boolean"} & set(types))


class TestBuildPrompt(unittest.TestCase):
    """Doctrine guards (b) + (c): facts injected, no-numbers instruction present."""

    def test_injected_fact_values_appear_in_prompt(self):
        prompt = build_qualitative_prompt(ticker="ACME", sections=_SECTIONS, facts=_FACTS)
        # The numeric facts the LLM must NOT recall but MUST reason over appear
        # verbatim (formatted) in the prompt FACTS block.
        self.assertIn("17.0", prompt)
        self.assertIn("15.5", prompt)
        self.assertIn("22.0", prompt)
        self.assertIn("ACME", prompt)
        # net_buyback rendered as a yes/no, never a number.
        self.assertIn("yes", prompt.lower())

    def test_prompt_instructs_no_numbers(self):
        prompt = build_qualitative_prompt(ticker="ACME", sections=_SECTIONS, facts=_FACTS).lower()
        # Some phrasing that forbids the model from producing / estimating numbers.
        self.assertTrue(
            "do not" in prompt and ("number" in prompt or "estimate" in prompt),
            "prompt must instruct the model not to produce/estimate numbers",
        )

    def test_prompt_contains_section_excerpts(self):
        prompt = build_qualitative_prompt(ticker="ACME", sections=_SECTIONS, facts=_FACTS)
        self.assertIn("enterprise widgets", prompt)
        self.assertIn("pricing power", prompt)
        self.assertIn("multi-year contracts", prompt)


class TestAssessQualitative(unittest.TestCase):
    def setUp(self):
        self._client = _make_stub_llm_client()

    def _set_response(self, text: str) -> None:
        self._client.generate_content.return_value = SimpleNamespace(text=text)

    def test_happy_path_parses_enums_and_bool(self):
        self._set_response(_GOOD_JSON)
        result = assess_qualitative(
            ticker="ACME", sections=_SECTIONS, facts=_FACTS, llm_client=self._client
        )
        self.assertIsInstance(result, QualitativeAssessment)
        self.assertIs(result.understandable, True)
        self.assertEqual(result.moat_type, "switching_cost")
        self.assertEqual(result.moat_trend, "widening")
        self.assertEqual(result.management_candor, "candid")
        self.assertIn("Recurring software", result.rationale or "")

    def test_brace_fallback_when_preamble(self):
        self._set_response(f"Here is the analysis:\n{_GOOD_JSON}\n--done--")
        result = assess_qualitative(
            ticker="ACME", sections=_SECTIONS, facts=_FACTS, llm_client=self._client
        )
        self.assertEqual(result.moat_type, "switching_cost")

    def test_bad_enum_becomes_none_for_that_field(self):
        self._set_response(
            '{"understandable": true, "moat_type": "monopoly_magic", '
            '"moat_trend": "widening", "management_candor": "candid", "rationale": "x"}'
        )
        result = assess_qualitative(
            ticker="ACME", sections=_SECTIONS, facts=_FACTS, llm_client=self._client
        )
        # Unknown moat_type → None; the valid neighbours survive.
        self.assertIsNone(result.moat_type)
        self.assertEqual(result.moat_trend, "widening")
        self.assertIs(result.understandable, True)

    def test_unparseable_text_all_none(self):
        self._set_response("totally not json and no braces at all")
        result = assess_qualitative(
            ticker="ACME", sections=_SECTIONS, facts=_FACTS, llm_client=self._client
        )
        self.assertIsNone(result.understandable)
        self.assertIsNone(result.moat_type)
        self.assertIsNone(result.moat_trend)
        self.assertIsNone(result.management_candor)
        self.assertIsNone(result.rationale)

    def test_missing_all_sections_skips_llm_and_returns_all_none(self):
        empty = TenKSections(item_1=None, item_1a=None, item_7=None)
        result = assess_qualitative(
            ticker="ACME", sections=empty, facts=_FACTS, llm_client=self._client
        )
        self.assertIsNone(result.understandable)
        self.assertIsNone(result.moat_type)
        # No LLM call when there is no section text to reason over.
        self._client.generate_content.assert_not_called()

    def test_api_error_returns_all_none(self):
        self._client.generate_content.side_effect = RuntimeError("rate limit")
        result = assess_qualitative(
            ticker="ACME", sections=_SECTIONS, facts=_FACTS, llm_client=self._client
        )
        self.assertIsNone(result.moat_type)
        self.assertIsNone(result.management_candor)

    def test_non_bool_understandable_becomes_none(self):
        self._set_response(
            '{"understandable": "maybe", "moat_type": "brand", '
            '"moat_trend": "stable", "management_candor": "mixed", "rationale": "ok"}'
        )
        result = assess_qualitative(
            ticker="ACME", sections=_SECTIONS, facts=_FACTS, llm_client=self._client
        )
        self.assertIsNone(result.understandable)
        self.assertEqual(result.moat_type, "brand")

    def test_string_boolean_understandable_is_coerced(self):
        # DeepSeek JSON mode sometimes stringifies booleans; coerce the common
        # forms rather than degrading an otherwise-valid label to None.
        for raw, expected in (("true", True), ("False", False), ("YES", True), ("no", False)):
            self._set_response(
                f'{{"understandable": "{raw}", "moat_type": "brand", '
                '"moat_trend": "stable", "management_candor": "mixed", "rationale": "ok"}'
            )
            result = assess_qualitative(
                ticker="ACME", sections=_SECTIONS, facts=_FACTS, llm_client=self._client
            )
            self.assertIs(result.understandable, expected, raw)


if __name__ == "__main__":
    unittest.main()
