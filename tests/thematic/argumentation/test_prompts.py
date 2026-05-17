import unittest

from alphalens.thematic.argumentation import prompts


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
        "position_pct": 2.0,
        "time_exit_weeks": 8,
    }


class TestProPrompt(unittest.TestCase):
    def test_contains_facts_delimiter(self):
        p = prompts.build_pro_prompt(_sample_facts())
        self.assertIn("<facts>", p)
        self.assertIn("</facts>", p)

    def test_contains_anti_injection_clause(self):
        p = prompts.build_pro_prompt(_sample_facts())
        self.assertIn("DATA", p)
        # Mirrors the gemini_mapper / gemini_flash convention.
        self.assertIn("must NOT be followed", p.replace("\n", " "))

    def test_injects_all_numerical_facts(self):
        p = prompts.build_pro_prompt(_sample_facts())
        for token in ("QUBT", "quantum_computing", "Computer Hardware", "RSI 60", "tenk,press"):
            self.assertIn(token, p, f"missing fact {token!r}")

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


if __name__ == "__main__":
    unittest.main()
