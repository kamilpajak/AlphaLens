import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alphalens.thematic.mapping import gemini_mapper, orchestrator

SAMPLE_MAPPER_RESPONSE = {
    "candidates": [
        {
            "ticker": "QBTS",
            "company_name": "D-Wave Quantum Inc",
            "rationale": "Pure-play quantum annealing hardware vendor",
            "confidence": 0.85,
        },
        {
            "ticker": "IONQ",
            "company_name": "IonQ Inc",
            "rationale": "Trapped-ion quantum hardware specialist",
            "confidence": 0.9,
        },
        {
            "ticker": "MADEUP",
            "company_name": "Made Up Inc",
            "rationale": "Hallucinated candidate",
            "confidence": 0.3,
        },
    ]
}


# ============================================================
# Gemini 3 Pro mapper
# ============================================================


class TestGeminiMapperPromptBuilding(unittest.TestCase):
    def test_prompt_lists_theme_and_constraints(self):
        prompt = gemini_mapper.build_prompt(
            theme="quantum_computing", market_cap_range=(500_000_000, 10_000_000_000)
        )
        self.assertIn("quantum_computing", prompt)
        self.assertIn("500", prompt)
        self.assertIn("small", prompt.lower())


class TestPropose(unittest.TestCase):
    def test_propose_returns_normalized_candidates(self):
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_MAPPER_RESPONSE))
        with patch.object(gemini_mapper, "_call_gemini", return_value=fake_response):
            candidates = gemini_mapper.propose_candidates(
                theme="quantum_computing", api_key="testkey"
            )
        self.assertEqual(len(candidates), 3)
        # Tickers uppercased
        self.assertEqual(candidates[0]["ticker"], "QBTS")
        # Confidence preserved
        self.assertEqual(candidates[1]["confidence"], 0.9)

    def test_propose_returns_empty_on_api_error(self):
        with patch.object(gemini_mapper, "_call_gemini", side_effect=RuntimeError("boom")):
            candidates = gemini_mapper.propose_candidates(
                theme="quantum_computing", api_key="testkey"
            )
        self.assertEqual(candidates, [])

    def test_propose_returns_empty_on_unparseable(self):
        bad_response = SimpleNamespace(text="not json")
        with patch.object(gemini_mapper, "_call_gemini", return_value=bad_response):
            candidates = gemini_mapper.propose_candidates(
                theme="quantum_computing", api_key="testkey"
            )
        self.assertEqual(candidates, [])


# ============================================================
# Orchestrator
# ============================================================


class TestVerifyCandidate(unittest.TestCase):
    def test_verify_runs_all_four_gates_and_collects_passes(self):
        with (
            patch.object(orchestrator, "_gate_etf", return_value=True),
            patch.object(orchestrator, "_gate_tenk", return_value=False),
            patch.object(orchestrator, "_gate_press", return_value=True),
            patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            result = orchestrator.verify_candidate(
                ticker="QBTS",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
                api_key="testkey",
            )
        self.assertEqual(set(result["gates_passed"]), {"etf", "press"})
        self.assertTrue(result["verified"])

    def test_verify_returns_unverified_when_zero_gates_pass(self):
        with (
            patch.object(orchestrator, "_gate_etf", return_value=False),
            patch.object(orchestrator, "_gate_tenk", return_value=False),
            patch.object(orchestrator, "_gate_press", return_value=False),
            patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            result = orchestrator.verify_candidate(
                ticker="MADEUP",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
                api_key="testkey",
            )
        self.assertEqual(result["gates_passed"], [])
        self.assertFalse(result["verified"])

    def test_verify_handles_individual_gate_failure(self):
        # Insider gate raises -> treated as gate not passing, other gates still run
        with (
            patch.object(orchestrator, "_gate_etf", return_value=True),
            patch.object(orchestrator, "_gate_tenk", return_value=False),
            patch.object(orchestrator, "_gate_press", return_value=False),
            patch.object(orchestrator, "_gate_insider", side_effect=RuntimeError("io")),
        ):
            result = orchestrator.verify_candidate(
                ticker="QBTS",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
                api_key="testkey",
            )
        self.assertEqual(result["gates_passed"], ["etf"])
        self.assertTrue(result["verified"])


class TestMapThemes(unittest.TestCase):
    def test_map_themes_writes_parquet_with_verified_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)

            # Mock Gemini proposal to deterministic output
            with (
                patch.object(
                    orchestrator.gemini_mapper,
                    "propose_candidates",
                    return_value=[
                        {"ticker": "QBTS", "rationale": "quantum", "confidence": 0.9},
                        {"ticker": "MADEUP", "rationale": "halluc", "confidence": 0.3},
                    ],
                ),
                patch.object(orchestrator, "_gate_etf", side_effect=[True, False]),
                patch.object(orchestrator, "_gate_tenk", return_value=False),
                patch.object(orchestrator, "_gate_press", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
                    api_key="testkey",
                    output_dir=cache_dir,
                    keep_unverified=False,
                )

            self.assertEqual(len(df), 1)  # only QBTS verified
            self.assertEqual(df.iloc[0]["ticker"], "QBTS")
            self.assertTrue(df.iloc[0]["verified"])
            out = cache_dir / "2026-05-15.parquet"
            self.assertTrue(out.exists())

    def test_map_themes_keeps_unverified_when_flag_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    orchestrator.gemini_mapper,
                    "propose_candidates",
                    return_value=[
                        {"ticker": "MADEUP", "rationale": "halluc", "confidence": 0.3},
                    ],
                ),
                patch.object(orchestrator, "_gate_etf", return_value=False),
                patch.object(orchestrator, "_gate_tenk", return_value=False),
                patch.object(orchestrator, "_gate_press", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
                    api_key="testkey",
                    output_dir=cache_dir,
                    keep_unverified=True,
                )
            self.assertEqual(len(df), 1)
            self.assertFalse(df.iloc[0]["verified"])

    def test_map_themes_empty_when_no_proposals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(orchestrator.gemini_mapper, "propose_candidates", return_value=[]):
                df = orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
                    api_key="testkey",
                    output_dir=Path(tmpdir),
                )
            self.assertEqual(len(df), 0)


if __name__ == "__main__":
    unittest.main()
