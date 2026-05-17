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
    """``generate_brief`` returns ``(brief | None, BriefErrorKind)`` so the
    retry wrapper can branch on the exact failure mode (Perplexity 2026-05-17)."""

    def test_returns_parsed_brief_on_success(self):
        fake_response = SimpleNamespace(text=json.dumps(_SAMPLE_BRIEF))
        with patch.object(generator, "_call_gemini", return_value=fake_response):
            brief, kind = generator.generate_brief(_facts(weighted_score=4), api_key="testkey")
        self.assertEqual(kind, generator.BriefErrorKind.NONE)
        self.assertEqual(brief["tldr"], _SAMPLE_BRIEF["tldr"])
        self.assertIn("bear_summary", brief)
        self.assertEqual(brief["model_used"], generator.PRO_MODEL)

    def test_uses_flash_model_for_low_conviction(self):
        captured: dict[str, str] = {}

        def fake_call(client, prompt, *, model, types_mod, max_output_tokens, temperature):
            captured["model"] = model
            return SimpleNamespace(text=json.dumps(_SAMPLE_BRIEF))

        with patch.object(generator, "_call_gemini", side_effect=fake_call):
            generator.generate_brief(_facts(weighted_score=2), api_key="testkey")
        self.assertEqual(captured["model"], generator.FLASH_MODEL)

    def test_transport_exception_classified(self):
        with patch.object(generator, "_call_gemini", side_effect=RuntimeError("rate limit")):
            brief, kind = generator.generate_brief(_facts(weighted_score=4), api_key="testkey")
        self.assertIsNone(brief)
        self.assertEqual(kind, generator.BriefErrorKind.TRANSPORT)

    def test_malformed_json_classified(self):
        resp = SimpleNamespace(
            text="not json",
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        )
        with patch.object(generator, "_call_gemini", return_value=resp):
            brief, kind = generator.generate_brief(_facts(weighted_score=4), api_key="testkey")
        self.assertIsNone(brief)
        self.assertEqual(kind, generator.BriefErrorKind.MALFORMED_JSON)

    def test_truncation_classified(self):
        resp = _truncated_response()
        with patch.object(generator, "_call_gemini", return_value=resp):
            brief, kind = generator.generate_brief(_facts(weighted_score=4), api_key="testkey")
        self.assertIsNone(brief)
        self.assertEqual(kind, generator.BriefErrorKind.TRUNCATED)

    def test_max_output_tokens_param_propagated(self):
        captured: dict[str, int | float | None] = {"max_tokens": None, "temperature": None}

        def fake_call(client, prompt, *, model, types_mod, max_output_tokens, temperature):
            captured["max_tokens"] = max_output_tokens
            captured["temperature"] = temperature
            return SimpleNamespace(text=json.dumps(_SAMPLE_BRIEF))

        with patch.object(generator, "_call_gemini", side_effect=fake_call):
            generator.generate_brief(
                _facts(weighted_score=2), api_key="k", max_output_tokens=4096, temperature=0.0
            )
        self.assertEqual(captured["max_tokens"], 4096)
        self.assertEqual(captured["temperature"], 0.0)

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


def _truncated_response(finish_reason_name: str = "MAX_TOKENS"):
    """SDK-shaped response with non-STOP finish_reason (mid-string truncation)."""
    cand = SimpleNamespace(
        finish_reason=SimpleNamespace(name=finish_reason_name),
        content=SimpleNamespace(parts=[SimpleNamespace(text='{"tldr": "cut')]),
    )
    return SimpleNamespace(text='{"tldr": "cut', candidates=[cand])


class TestClassifyFinishReason(unittest.TestCase):
    """Per Perplexity 2026-05-17: distinguish MAX_TOKENS / SAFETY / STOP."""

    def test_max_tokens_classified_as_truncated(self):
        self.assertEqual(
            generator._classify_finish_reason(_truncated_response("MAX_TOKENS")),
            generator.BriefErrorKind.TRUNCATED,
        )

    def test_safety_classified_as_safety(self):
        self.assertEqual(
            generator._classify_finish_reason(_truncated_response("SAFETY")),
            generator.BriefErrorKind.SAFETY,
        )

    def test_stop_or_missing_returns_none(self):
        self.assertIsNone(generator._classify_finish_reason(_truncated_response("STOP")))
        self.assertIsNone(generator._classify_finish_reason(SimpleNamespace(text="...")))

    def test_string_finish_reason_also_recognised(self):
        # Some SDK versions surface finish_reason as a bare string.
        resp = SimpleNamespace(
            text="cut",
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
        )
        self.assertEqual(
            generator._classify_finish_reason(resp), generator.BriefErrorKind.TRUNCATED
        )


class TestGenerateBriefWithRetry(unittest.TestCase):
    """Retry policy: on MAX_TOKENS, retry once with double cap + temperature=0.

    Other failure kinds (MALFORMED_JSON, SAFETY, TRANSPORT) do NOT retry —
    they will not be helped by more tokens or different temperature.
    """

    def test_no_retry_on_success(self):
        fake = SimpleNamespace(text=json.dumps(_SAMPLE_BRIEF))
        with patch.object(generator, "_call_gemini", return_value=fake) as mock_call:
            brief = generator.generate_brief_with_retry(_facts(weighted_score=4), api_key="k")
        self.assertIsNotNone(brief)
        self.assertEqual(mock_call.call_count, 1)

    def test_retry_doubles_max_tokens_and_drops_temperature(self):
        captured: list[dict] = []

        def fake_call(client, prompt, *, model, types_mod, max_output_tokens, temperature):
            captured.append({"max": max_output_tokens, "temp": temperature})
            if len(captured) == 1:
                return _truncated_response()
            return SimpleNamespace(text=json.dumps(_SAMPLE_BRIEF))

        with patch.object(generator, "_call_gemini", side_effect=fake_call):
            brief = generator.generate_brief_with_retry(
                _facts(weighted_score=2), api_key="k", base_max_output_tokens=2000
            )
        self.assertIsNotNone(brief)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["max"], 2000)
        self.assertEqual(captured[1]["max"], 4000)
        # Retry must use deterministic decode (greedy).
        self.assertEqual(captured[1]["temp"], 0.0)

    def test_no_retry_on_malformed_json(self):
        # MALFORMED_JSON with finish_reason STOP — model finished but bad
        # output; retrying with more tokens won't help.
        resp = SimpleNamespace(
            text="not json",
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        )
        with patch.object(generator, "_call_gemini", return_value=resp) as mock_call:
            brief = generator.generate_brief_with_retry(_facts(weighted_score=2), api_key="k")
        self.assertIsNone(brief)
        self.assertEqual(mock_call.call_count, 1)

    def test_no_retry_on_safety(self):
        with patch.object(
            generator, "_call_gemini", return_value=_truncated_response("SAFETY")
        ) as mock_call:
            brief = generator.generate_brief_with_retry(_facts(weighted_score=2), api_key="k")
        self.assertIsNone(brief)
        self.assertEqual(mock_call.call_count, 1)

    def test_no_retry_on_transport_error(self):
        # Network exceptions are not retried at this layer — let the operator
        # / outer cron decide on backoff.
        with patch.object(
            generator, "_call_gemini", side_effect=RuntimeError("transport boom")
        ) as mock_call:
            brief = generator.generate_brief_with_retry(_facts(weighted_score=2), api_key="k")
        self.assertIsNone(brief)
        self.assertEqual(mock_call.call_count, 1)

    def test_two_truncations_give_up(self):
        with patch.object(
            generator, "_call_gemini", side_effect=[_truncated_response(), _truncated_response()]
        ) as mock_call:
            brief = generator.generate_brief_with_retry(_facts(weighted_score=2), api_key="k")
        self.assertIsNone(brief)
        self.assertEqual(mock_call.call_count, 2)


class TestDefensiveClientArgs(unittest.TestCase):
    def test_raises_when_hoisted_client_passed_without_types_mod(self):
        # Partial hoisting (client_pro without types_mod) would silently
        # discard the user's client and lazy-build a fresh one. Better
        # to raise so the caller knows they made a mistake.
        with self.assertRaises(ValueError):
            generator.generate_brief(_facts(weighted_score=4), client_pro=object())


if __name__ == "__main__":
    unittest.main()
