import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from alphalens.thematic.argumentation import generator


def _facts(weighted_score: int = 4):
    return {
        "ticker": "QUBT",
        "company_name": "Quantum Computing Inc",
        "theme": "quantum_computing",
        "industry_name": "Computer Hardware",
        "sector_name": "Technology",
        "weighted_score": weighted_score,
        "rationale": "Pure-play quantum",
        "gates_passed_str": "tenk,press",
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


_SAMPLE_BRIEF = {
    "tldr": "QUBT is a pure-play quantum hardware vendor benefiting from NVIDIA's Ising tooling push.",
    "supply_chain_reasoning": "NVIDIA Ising lowers the bar for quantum researchers to deploy at scale, raising demand for QUBT's photonic processors. Adjacent ETF inclusion in QTUM signals institutional thematic exposure.",
    "bear_summary": "Pre-revenue, dilution risk. Zero insider open-market buying in 90d post-event. Valuation composite at 1st percentile vs industry.",
    "catalyst_failure_exit": "Exit if Q1 earnings shows wider cash burn or NVIDIA drops quantum from Roadmap.",
    "entry_price_note": "prefer 5-10 bps below current; wait for RSI < 60 retest.",
}


class TestRouteSelection(unittest.TestCase):
    def test_pro_for_weighted_score_4_and_5(self):
        self.assertEqual(generator.choose_model(weighted_score=4), generator.PRO_MODEL)
        self.assertEqual(generator.choose_model(weighted_score=5), generator.PRO_MODEL)

    def test_flash_for_weighted_score_1_through_3(self):
        for w in (1, 2, 3):
            self.assertEqual(generator.choose_model(weighted_score=w), generator.FLASH_MODEL)

    def test_flash_when_weighted_score_missing(self):
        # Defensive: if Phase D didn't emit a score for some reason, use the
        # cheaper tier rather than burning Pro on unknown candidates.
        self.assertEqual(generator.choose_model(weighted_score=None), generator.FLASH_MODEL)


class TestGenerateBrief(unittest.TestCase):
    def test_returns_parsed_brief_on_success(self):
        fake_response = SimpleNamespace(text=json.dumps(_SAMPLE_BRIEF))
        with patch.object(generator, "_call_gemini", return_value=fake_response):
            brief = generator.generate_brief(_facts(weighted_score=4), api_key="testkey")
        self.assertEqual(brief["tldr"], _SAMPLE_BRIEF["tldr"])
        self.assertIn("bear_summary", brief)
        self.assertEqual(brief["model_used"], generator.PRO_MODEL)

    def test_uses_flash_model_for_low_conviction(self):
        captured: dict[str, str] = {}

        def fake_call(client, prompt, *, model, types_mod):
            captured["model"] = model
            return SimpleNamespace(text=json.dumps(_SAMPLE_BRIEF))

        with patch.object(generator, "_call_gemini", side_effect=fake_call):
            generator.generate_brief(_facts(weighted_score=2), api_key="testkey")
        self.assertEqual(captured["model"], generator.FLASH_MODEL)

    def test_returns_none_on_api_failure(self):
        with patch.object(generator, "_call_gemini", side_effect=RuntimeError("rate limit")):
            brief = generator.generate_brief(_facts(weighted_score=4), api_key="testkey")
        self.assertIsNone(brief)

    def test_returns_none_on_unparseable_response(self):
        with patch.object(generator, "_call_gemini", return_value=SimpleNamespace(text="not json")):
            brief = generator.generate_brief(_facts(weighted_score=4), api_key="testkey")
        self.assertIsNone(brief)

    def test_reuses_passed_clients_no_handshake_per_call(self):
        # Mirror the orchestrator hoisting pattern from gemini_mapper / scorer.
        fake_response = SimpleNamespace(text=json.dumps(_SAMPLE_BRIEF))
        sentinel_client = object()
        sentinel_types = object()
        with patch.object(generator, "_call_gemini", return_value=fake_response) as mock_call:
            generator.generate_brief(
                _facts(weighted_score=4),
                api_key=None,
                client_pro=sentinel_client,
                client_flash=sentinel_client,
                types_mod=sentinel_types,
            )
        call_kwargs = mock_call.call_args.kwargs
        self.assertIs(mock_call.call_args.args[0], sentinel_client)
        self.assertIs(call_kwargs["types_mod"], sentinel_types)


if __name__ == "__main__":
    unittest.main()
