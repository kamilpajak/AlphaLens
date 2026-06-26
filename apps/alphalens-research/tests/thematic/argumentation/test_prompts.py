import unittest

from alphalens_pipeline.thematic.argumentation import prompts
from alphalens_pipeline.thematic.argumentation.prompts import _format_gates_passed


def _sample_facts():
    return {
        "ticker": "QUBT",
        "company_name": "Quantum Computing Inc",
        "theme": "quantum_computing",
        "industry_name": "Computer Hardware",
        "sector_name": "Technology",
        "weighted_score": 4,
        "rationale": "Pure-play quantum hardware downstream of NVIDIA Ising",
        "gates_passed_str": "tenk,press",
        "insider_score_usd": 0.0,
        "insider_score_sector_percentile": 50.0,
        "fcff_yield_pct": None,
        "fcff_yield_sector_percentile": None,
        "valuation_ps": 30.0,
        "valuation_ev_rev": 32.0,
        "valuation_composite_sector_percentile": 1.0,
        "valuation_fcf_margin": -0.5,
        "technicals_summary_str": "RSI 60 / MA50 +4.1% / ATR 6.6% / volZ 3.8",
        "market_cap": 1.78e9,
    }


def _sample_facts_with_buffett():
    facts = _sample_facts()
    facts.update(
        {
            "buffett_roic_latest": 8.0,
            "buffett_roic_3y_avg": 22.0,
            "buffett_owner_earnings_yield_pct": 1.5,
            "buffett_margin_of_safety_pct": -40.0,
        }
    )
    return facts


class TestBuffettDurabilityFacts(unittest.TestCase):
    """The cheap Buffett durability facts (ROIC / owner-earnings yield / DCF
    margin of safety) are injected so the bear case can cite business-durability
    risk — but ONLY when present, and the qualitative moat/trend/candor verdict
    is NEVER fed in (that stays in the drawer, unvalidated until Buffett×EDGE).
    The block + its constraint are conditional so a name with no Buffett data
    yields a byte-identical prompt (golden-cassette safe)."""

    def test_durability_block_and_constraint_appear_when_present(self):
        for build in (prompts.build_pro_prompt, prompts.build_flash_prompt):
            p = build(_sample_facts_with_buffett())
            self.assertIn("durability (Buffett quant)", p)
            self.assertIn("ROIC 8.0%", p)
            self.assertIn("3y avg 22.0%", p)
            self.assertIn("DCF margin of safety -40.0%", p)
            self.assertIn("durability", p.lower())

    def test_absent_when_no_buffett_facts_keeps_prompt_clean(self):
        # The existing no-Buffett sample must NOT gain the durability block or
        # constraint — keeps the golden brief cassettes valid (the fixture scored
        # frame has no buffett_* columns, so the prompt stays byte-identical).
        for build in (prompts.build_pro_prompt, prompts.build_flash_prompt):
            p = build(_sample_facts())
            self.assertNotIn("durability (Buffett quant)", p)
            self.assertNotIn("Buffett quant", p)

    def test_qualitative_verdict_never_injected(self):
        # Doctrine: the LLM moat/trend/candor verdict must NOT shape the brief
        # narrative (it lives in the drawer; unvalidated until Buffett×EDGE).
        facts = _sample_facts_with_buffett()
        facts.update({"buffett_moat_type": "brand", "buffett_moat_trend": "narrowing"})
        for build in (prompts.build_pro_prompt, prompts.build_flash_prompt):
            p = build(facts)
            self.assertNotIn("moat", p.lower())
            self.assertNotIn("narrowing", p.lower())


class TestProPrompt(unittest.TestCase):
    def test_contains_facts_delimiter(self):
        p = prompts.build_pro_prompt(_sample_facts())
        self.assertIn("<facts>", p)
        self.assertIn("</facts>", p)

    def test_contains_anti_injection_clause(self):
        p = prompts.build_pro_prompt(_sample_facts())
        self.assertIn("DATA", p)
        # Mirrors the theme_mapper / event_extractor convention.
        self.assertIn("must NOT be followed", p.replace("\n", " "))

    def test_injects_all_numerical_facts(self):
        p = prompts.build_pro_prompt(_sample_facts())
        # gates_passed_str is translated to reader phrases, not injected verbatim
        for token in ("QUBT", "quantum_computing", "Computer Hardware", "RSI 60"):
            self.assertIn(token, p, f"missing fact {token!r}")
        # gates are rendered as reader-neutral phrases, not raw token strings
        self.assertIn("10-K filing mentions the theme", p)
        self.assertIn("recent press coverage of the theme", p)
        self.assertNotIn("tenk,press", p)

    def test_requires_bear_case_mandatory(self):
        p = prompts.build_pro_prompt(_sample_facts())
        # Anti-confirmation-bias hook per memo §6.
        self.assertIn("MANDATORY", p)
        self.assertIn("bear", p.lower())


class TestFlashPrompt(unittest.TestCase):
    def test_contains_facts_delimiter(self):
        p = prompts.build_flash_prompt(_sample_facts())
        self.assertIn("<facts>", p)
        self.assertIn("</facts>", p)

    def test_shorter_than_pro_prompt(self):
        # Flash is the downgrade tier for marginal candidates; smaller token budget.
        pro = prompts.build_pro_prompt(_sample_facts())
        flash = prompts.build_flash_prompt(_sample_facts())
        self.assertLess(len(flash), len(pro))

    def test_injects_core_facts(self):
        p = prompts.build_flash_prompt(_sample_facts())
        self.assertIn("QUBT", p)
        self.assertIn("quantum_computing", p)


class TestEnglishLanguageDirective(unittest.TestCase):
    """Both prompts must pin the OUTPUT language to English.

    DeepSeek v4 (a Chinese-developed model) nondeterministically drifts to
    Chinese when no output language is fixed, producing a brief whose prose
    the WhatsApp group cannot read (WK card, 2026-06-12). The instruction is
    the source-side fix; the generator's CJK guard is the safety net.
    """

    def test_pro_prompt_pins_english_output(self):
        p = prompts.build_pro_prompt(_sample_facts())
        self.assertIn("English", p)

    def test_flash_prompt_pins_english_output(self):
        p = prompts.build_flash_prompt(_sample_facts())
        self.assertIn("English", p)


class TestFormatGatesPassed(unittest.TestCase):
    """Unit tests for the _format_gates_passed helper that translates internal
    gate token strings (tenk, press, insider) to reader-neutral phrases."""

    def test_tenk_and_press_render_to_reader_phrases(self):
        result = _format_gates_passed("tenk,press")
        self.assertEqual(
            result,
            "10-K filing mentions the theme, recent press coverage of the theme",
        )

    def test_insider_renders_to_reader_phrase(self):
        result = _format_gates_passed("insider")
        self.assertEqual(result, "recent insider buying")

    def test_unknown_token_passes_through_verbatim(self):
        result = _format_gates_passed("foo")
        self.assertEqual(result, "foo")

    def test_empty_string_renders_empty(self):
        self.assertEqual(_format_gates_passed(""), "")

    def test_all_three_gates(self):
        result = _format_gates_passed("tenk,press,insider")
        self.assertEqual(
            result,
            "10-K filing mentions the theme, recent press coverage of the theme, recent insider buying",
        )

    def test_whitespace_around_tokens_is_stripped(self):
        result = _format_gates_passed(" tenk , press ")
        self.assertEqual(
            result,
            "10-K filing mentions the theme, recent press coverage of the theme",
        )


if __name__ == "__main__":
    unittest.main()
