"""Property-based tests for the LLM brief generator's content classification.

The load-bearing property is the BICONDICTIONAL between "substantive content"
and "success": for any JSON body the model returns (valid JSON, finish_reason
STOP), ``generate_brief`` classifies it ``NONE`` **iff** at least one required
narrative field carries non-whitespace text — otherwise it must classify the
retryable ``EMPTY_CONTENT`` and return no brief.

This pins the exact gap that produced the MC (Moelis & Company) empty-card
incident (2026-07-19 run): DeepSeek v4 Pro returned a *valid* JSON body whose
required fields were all blank, and the generator accepted it as success
(stamping ``model_used``) instead of driving a retry. A mutation that drops the
all-blank guard, or weakens it to "any field present" (ignoring emptiness), or
flips the at-least-one threshold to all-four, diverges from this property over
some generated field combination.

The generated field values deliberately span {non-blank ASCII text, empty
string, several whitespace-only strings} so the ``.strip()`` in the guard is
exercised (a whitespace-only field is NOT substantive).
"""

from __future__ import annotations

import json
import string
from types import SimpleNamespace
from unittest.mock import patch

from alphalens_pipeline.thematic.argumentation import generator
from alphalens_pipeline.thematic.argumentation.schema import BRIEF_RESPONSE_SCHEMA
from hypothesis import given
from hypothesis import strategies as st

from .base import PropertyTestCase

_REQUIRED = tuple(BRIEF_RESPONSE_SCHEMA["required"])

# Non-blank ASCII text only: letters are never whitespace, so ``.strip()`` is
# always truthy, and staying ASCII avoids tripping the separate CJK
# LANGUAGE_DRIFT guard (which would confound the NONE assertion).
_SUBSTANTIVE = st.text(alphabet=string.ascii_letters + string.digits + " .", min_size=1).filter(
    lambda s: s.strip() != ""
)
# Blank values: empty + whitespace-only variants that must all strip to "".
_BLANK = st.sampled_from(["", " ", "   ", "\n", "\t", "  \n\t "])
_FIELD_VALUE = st.one_of(_BLANK, _SUBSTANTIVE)


def _facts() -> dict:
    """Complete facts dict — Pro route (weighted_score >= 4). Full so the real
    ``build_pro_prompt`` runs; the LLM call itself is patched per test."""
    return {
        "ticker": "TEST",
        "company_name": "Test Corp",
        "theme": "test_theme",
        "industry_name": "Test Industry",
        "sector_name": "Test Sector",
        "weighted_score": 4,
        "rationale": "Test rationale",
        "gates_passed_str": "tenk",
        "insider_score_usd": 0.0,
        "insider_score_sector_percentile": 50.0,
        "fcff_yield_pct": None,
        "fcff_yield_sector_percentile": None,
        "valuation_ps": 30.0,
        "valuation_ev_rev": 32.0,
        "valuation_fcf_margin": -0.5,
        "valuation_composite_sector_percentile": 1.0,
        "technicals_summary_str": "RSI 60",
        "market_cap": 1.78e9,
        "position_pct": 2.0,
        "time_exit_weeks": 8,
    }


def _is_substantive(value: object) -> bool:
    """Independent reference: a required field counts iff it strips non-empty."""
    return isinstance(value, str) and value.strip() != ""


@st.composite
def _response_fields(draw) -> dict[str, str]:
    """All four required keys present, each an independently-chosen blank/text."""
    return {key: draw(_FIELD_VALUE) for key in _REQUIRED}


class TestBriefContentClassification(PropertyTestCase):
    """``generate_brief`` accepts a parsed brief iff it has substantive content."""

    @given(fields=_response_fields())
    def test_success_iff_any_required_field_substantive(self, fields: dict[str, str]) -> None:
        payload = json.dumps(fields)
        response = SimpleNamespace(text=payload)  # no candidates -> finish_reason STOP/absent
        with patch.object(generator, "_call_llm", return_value=response):
            brief, kind = generator.generate_brief(_facts(), api_key="k")

        any_substantive = any(_is_substantive(fields[key]) for key in _REQUIRED)
        if any_substantive:
            # Substantive content -> success, model stamped, content preserved.
            self.assertEqual(kind, generator.BriefErrorKind.NONE)
            self.assertIsNotNone(brief)
            assert brief is not None  # narrow for type-checkers
            self.assertEqual(brief["model_used"], generator.PRO_MODEL)
            self.assertTrue(
                any(_is_substantive(brief.get(key)) for key in _REQUIRED),
                msg="a NONE brief must carry at least one substantive required field",
            )
        else:
            # Every required field blank -> retryable EMPTY_CONTENT, no brief.
            self.assertIsNone(brief)
            self.assertEqual(kind, generator.BriefErrorKind.EMPTY_CONTENT)

    @given(fields=_response_fields())
    def test_all_blank_never_classified_success(self, fields: dict[str, str]) -> None:
        # Force the all-blank case explicitly (blank every field) so shrinking
        # always has the pure counterexample available: it must NEVER be NONE.
        blank = dict.fromkeys(fields, "")
        response = SimpleNamespace(text=json.dumps(blank))
        with patch.object(generator, "_call_llm", return_value=response):
            brief, kind = generator.generate_brief(_facts(), api_key="k")
        self.assertIsNone(brief)
        self.assertEqual(kind, generator.BriefErrorKind.EMPTY_CONTENT)
