"""Hermetic G1-G4 integration test for the NVDA -> QUBT/IONQ thematic path.

This is the CI-side counterpart of ``scripts/replay_nvda_qubt.py`` (a manual,
unverified replay). The replay broke silently three ways via the cosmetic
PR #328 because nothing pinned the call contracts it depends on:

  1. ``event_extractor.extract_one`` -> ``api_key=`` kwarg
  2. ``theme_mapper.propose_candidates`` -> now returns ``dict["candidates"]``
     (was a bare list)
  3. ``orchestrator.verify_candidate`` -> ``polygon_client=`` kwarg (was
     ``api_key=``)

Every external call is mocked (the LLM via the ``_call_llm`` seam, the gates
via ``patch.object``), so this runs in the default ``unittest discover`` with
no network, no keys, and full determinism. It pins both the G1-G4 boolean
logic the replay checks AND the three drift-prone signatures, so a future
refactor of the kind #328 introduced fails loudly here instead of rotting an
uncalled script. End-to-end data health (live model + Polygon) is the job of
``tests/live/test_nvda_qubt_live.py`` (opt-in), not this test.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alphalens_pipeline.thematic.extraction import event_extractor
from alphalens_pipeline.thematic.mapping import orchestrator, theme_mapper

ASOF = dt.date(2026, 4, 14)
TARGET_SECOND_ORDER = "QUBT"  # the 2nd-order beneficiary the pipeline must surface
POSITIVE_CONTROL = "IONQ"  # an obvious pure-play; if this is missed, mapping is broken

# Minimal news row — content is irrelevant to the hermetic path (the LLM is
# mocked) but must be a valid row for build_prompt / _news_row_to_article.
NEWS_ROW = {
    "id": "nvda_ising_2026_04_14",
    "source": "nvidianews.nvidia.com",
    "tickers": ["NVDA"],
    "title": (
        "NVIDIA Launches Ising, the World's First Open AI Models to Accelerate "
        "the Path to Useful Quantum Computers"
    ),
    "body": (
        "NVIDIA today announced NVIDIA Ising, open AI models for quantum error "
        "correction and calibration, complementing CUDA-Q and NVQLink."
    ),
    "published_at": "2026-04-14T13:00:00Z",
}

# Canonical Flash-shape extraction (pre-normalize) the LLM would return for the
# NVDA quantum launch. event_type is coerced to a valid enum by
# normalize_extraction; we only assert on themes here.
CANNED_EVENT = {
    "event_type": "product_launch",
    "primary_entities": ["NVDA"],
    "themes": ["quantum_computing", "quantum_error_correction"],
    "sentiment": "positive",
    "second_order_implications": [
        "Small/mid-cap quantum hardware names (QUBT, IONQ) may benefit from "
        "NVIDIA validating the quantum-error-correction tooling stack.",
    ],
    "confidence": 0.8,
}

# Layer-3 mapper response: QUBT (2nd-order target) + IONQ (positive control) +
# a low-confidence name. Tickers arrive mixed-case to also pin the uppercasing.
CANNED_MAPPER = {
    "candidates": [
        {
            "ticker": "qubt",
            "company_name": "Quantum Computing Inc",
            "rationale": "Photonic quantum hardware; benefits from QEC tooling",
            "confidence": 0.82,
        },
        {
            "ticker": "IONQ",
            "company_name": "IonQ Inc",
            "rationale": "Trapped-ion pure-play named in the release",
            "confidence": 0.95,
        },
        {
            "ticker": "MADEUP",
            "company_name": "Made Up Inc",
            "rationale": "Low-confidence filler",
            "confidence": 0.3,
        },
    ],
    "search_keywords": ["quantum computing", "qubit", "quantum error correction"],
}


def _fake_llm_response(payload: dict) -> SimpleNamespace:
    """Mirror the OpenRouterClient response surface (`.text`) the parse path reads."""
    return SimpleNamespace(text=json.dumps(payload))


def _run_extract() -> dict:
    """Drive extract_one down the LLM path with a mocked _call_llm.

    The template path is forced to miss via injected engine/resolver doubles so
    the deterministic canned LLM response is what gets parsed.
    """
    no_match_engine = Mock()
    no_match_engine.match.return_value = None
    empty_resolver = Mock()
    empty_resolver.resolve.return_value = []
    with patch.object(event_extractor, "_call_llm", return_value=_fake_llm_response(CANNED_EVENT)):
        event = event_extractor.extract_one(
            NEWS_ROW,
            api_key="testkey",
            engine=no_match_engine,
            resolver=empty_resolver,
        )
    assert event is not None
    return event


def _run_propose() -> dict:
    with patch.object(theme_mapper, "_call_llm", return_value=_fake_llm_response(CANNED_MAPPER)):
        return theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")


class TestNvdaQubtGates(unittest.TestCase):
    """G1-G4: the four gates the manual replay checks, pinned deterministically."""

    def test_g1_layer2_extracts_quantum_theme(self):
        event = _run_extract()
        themes = event["themes"]
        self.assertTrue(
            any("quantum" in t.lower() for t in themes),
            f"G1 failed: no quantum theme in {themes}",
        )

    def test_g2_layer3_surfaces_target_second_order(self):
        candidates = _run_propose()["candidates"]
        tickers = [c["ticker"] for c in candidates]
        self.assertIn(TARGET_SECOND_ORDER, tickers, f"G2 failed: QUBT not in {tickers}")

    def test_g3_layer3_surfaces_positive_control(self):
        candidates = _run_propose()["candidates"]
        tickers = [c["ticker"] for c in candidates]
        self.assertIn(POSITIVE_CONTROL, tickers, f"G3 failed: IONQ not in {tickers}")

    def test_g4_verified_when_one_gate_passes(self):
        # verified := at least one gate passed (orchestrator.verify_candidate).
        with (
            patch.object(orchestrator, "_gate_tenk", return_value=None),
            patch.object(orchestrator, "_gate_press", return_value=True),
            patch.object(orchestrator, "_gate_insider", return_value=None),
        ):
            verdict = orchestrator.verify_candidate(
                ticker=TARGET_SECOND_ORDER,
                themes=["quantum_computing"],
                asof=ASOF,
                polygon_client=None,
            )
        self.assertTrue(verdict["verified"])
        self.assertIn("press", verdict["gates_passed"])

    def test_g4_not_verified_when_all_gates_unknown(self):
        # Conservative rule (2026-05-22): all-None -> not verified.
        with (
            patch.object(orchestrator, "_gate_tenk", return_value=None),
            patch.object(orchestrator, "_gate_press", return_value=None),
            patch.object(orchestrator, "_gate_insider", return_value=None),
        ):
            verdict = orchestrator.verify_candidate(
                ticker=TARGET_SECOND_ORDER,
                themes=["quantum_computing"],
                asof=ASOF,
                polygon_client=None,
            )
        self.assertFalse(verdict["verified"])
        self.assertEqual(verdict["gates_passed"], [])


class TestNvdaQubtCallContracts(unittest.TestCase):
    """Pin the three call signatures the cosmetic PR #328 silently drifted.

    These give a readable failure (TypeError / missing-key / signature mismatch)
    the instant a refactor changes a contract a downstream consumer relies on,
    instead of letting an uncalled script rot until someone runs it by hand.
    """

    def test_extract_one_accepts_api_key_kwarg(self):
        params = inspect.signature(event_extractor.extract_one).parameters
        self.assertIn("api_key", params)

    def test_propose_candidates_accepts_api_key_kwarg(self):
        params = inspect.signature(theme_mapper.propose_candidates).parameters
        self.assertIn("api_key", params)

    def test_propose_candidates_returns_dict_with_candidates_key(self):
        # The #328 break: this used to be a bare list. A dict with "candidates"
        # is the contract; assert it explicitly so a regression reads clearly
        # instead of surfacing as a deep KeyError in a consumer.
        result = _run_propose()
        self.assertIsInstance(result, dict)
        self.assertIn("candidates", result)
        self.assertIsInstance(result["candidates"], list)

    def test_verify_candidate_takes_polygon_client_not_api_key(self):
        params = inspect.signature(orchestrator.verify_candidate).parameters
        self.assertIn("polygon_client", params)
        self.assertNotIn("api_key", params)

    def test_verify_candidate_returns_verdict_keys(self):
        with (
            patch.object(orchestrator, "_gate_tenk", return_value=None),
            patch.object(orchestrator, "_gate_press", return_value=None),
            patch.object(orchestrator, "_gate_insider", return_value=None),
        ):
            verdict = orchestrator.verify_candidate(
                ticker=POSITIVE_CONTROL,
                themes=["quantum_computing"],
                asof=ASOF,
                polygon_client=None,
            )
        for key in ("ticker", "gates_passed", "gates_failed", "gates_unknown", "verified"):
            self.assertIn(key, verdict)


if __name__ == "__main__":
    unittest.main()
