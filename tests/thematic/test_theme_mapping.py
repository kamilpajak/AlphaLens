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
    def test_prompt_includes_theme(self):
        prompt = gemini_mapper.build_prompt(theme="quantum_computing")
        self.assertIn("quantum_computing", prompt)

    def test_prompt_does_not_constrain_market_cap(self):
        # Mcap brackets in the prompt are unreliable: Pro filters against its
        # training-cutoff mcap snapshot, not real-time. (Probe 2026-05-17:
        # Pro believed QUBT mcap = $50M vs real $1.78B.) Filtering belongs in
        # the orchestrator post-LLM via yfinance.
        prompt = gemini_mapper.build_prompt(theme="quantum_computing")
        for token in ("market cap", "market_cap", "small-cap", "mid-cap", "small/mid"):
            self.assertNotIn(token.lower(), prompt.lower())


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
        self.assertEqual(set(result["gates_failed"]), {"tenk", "insider"})
        self.assertEqual(result["gates_unknown"], [])
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
        self.assertEqual(set(result["gates_failed"]), {"etf", "tenk", "press", "insider"})
        self.assertEqual(result["gates_unknown"], [])
        self.assertFalse(result["verified"])

    def test_verify_records_unknown_when_gate_returns_none(self):
        # tenk returns None (CIK miss / fetch fail) -> goes to gates_unknown,
        # not gates_failed. Verified rule unchanged: needs ≥1 pass.
        with (
            patch.object(orchestrator, "_gate_etf", return_value=True),
            patch.object(orchestrator, "_gate_tenk", return_value=None),
            patch.object(orchestrator, "_gate_press", return_value=False),
            patch.object(orchestrator, "_gate_insider", return_value=None),
        ):
            result = orchestrator.verify_candidate(
                ticker="FOREIGN",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
                api_key="testkey",
            )
        self.assertEqual(result["gates_passed"], ["etf"])
        self.assertEqual(result["gates_failed"], ["press"])
        self.assertEqual(set(result["gates_unknown"]), {"tenk", "insider"})
        self.assertTrue(result["verified"])  # etf passed = verified

    def test_verify_unknown_alone_does_not_promote_verified(self):
        # Conservative rule: unknowns don't promote verified=True. Pinning
        # the rule from the 2026-05-17 plan §C5 lock.
        with (
            patch.object(orchestrator, "_gate_etf", return_value=None),
            patch.object(orchestrator, "_gate_tenk", return_value=None),
            patch.object(orchestrator, "_gate_press", return_value=None),
            patch.object(orchestrator, "_gate_insider", return_value=None),
        ):
            result = orchestrator.verify_candidate(
                ticker="OPAQUE",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
                api_key="testkey",
            )
        self.assertEqual(result["gates_passed"], [])
        self.assertEqual(result["gates_failed"], [])
        self.assertEqual(len(result["gates_unknown"]), 4)
        self.assertFalse(result["verified"])

    def test_verify_safe_wrapper_treats_exception_as_unknown(self):
        # An exception inside a gate is "we don't know", not False.
        with (
            patch.object(orchestrator, "_gate_etf", return_value=True),
            patch.object(orchestrator, "_gate_tenk", side_effect=RuntimeError("boom")),
            patch.object(orchestrator, "_gate_press", return_value=False),
            patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            result = orchestrator.verify_candidate(
                ticker="QBTS",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
                api_key="testkey",
            )
        self.assertEqual(result["gates_passed"], ["etf"])
        self.assertEqual(set(result["gates_failed"]), {"press", "insider"})
        self.assertEqual(result["gates_unknown"], ["tenk"])
        self.assertTrue(result["verified"])

    def test_verify_expands_snake_case_themes_when_no_explicit_keywords(self):
        # When the caller passes themes=["quantum_computing"] without
        # theme_keywords=, the orchestrator must expand to BOTH the underscore
        # and the space-separated form before invoking each gate — otherwise
        # 10-K/press gates miss any document that spells the phrase normally.
        captured: dict[str, list[str]] = {}

        def fake_tenk(*, ticker, theme_keywords, asof):
            captured["tenk"] = list(theme_keywords)
            return False

        def fake_press(*, ticker, theme_keywords, asof, api_key, press_df=None):
            captured["press"] = list(theme_keywords)
            return False

        with (
            patch.object(orchestrator, "_gate_etf", return_value=False),
            patch.object(orchestrator, "_gate_tenk", side_effect=fake_tenk),
            patch.object(orchestrator, "_gate_press", side_effect=fake_press),
            patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            orchestrator.verify_candidate(
                ticker="QBTS",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
                api_key="testkey",
            )
        self.assertIn("quantum_computing", captured["tenk"])
        self.assertIn("quantum computing", captured["tenk"])
        self.assertIn("quantum_computing", captured["press"])
        self.assertIn("quantum computing", captured["press"])

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
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QBTS": 1_000_000_000, "MADEUP": 1_000_000_000},
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
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"MADEUP": 1_000_000_000},
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

    def test_map_themes_post_filters_out_of_bracket_candidates(self):
        # Pro mapper now returns candidates without an mcap filter (because its
        # training-data mcap is stale). The orchestrator applies the real-time
        # bracket via yfinance BEFORE running the 4 verification gates — so
        # mega-cap NVDA and micro-cap MICRO never reach the gates.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    orchestrator.gemini_mapper,
                    "propose_candidates",
                    return_value=[
                        {"ticker": "QUBT", "rationale": "pure-play", "confidence": 0.9},
                        {"ticker": "NVDA", "rationale": "mega-cap", "confidence": 0.5},
                        {"ticker": "MICRO", "rationale": "tiny", "confidence": 0.4},
                    ],
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QUBT": 1_780_000_000},
                ),
                patch.object(orchestrator, "_gate_etf", return_value=False),
                patch.object(orchestrator, "_gate_tenk", return_value=True),
                patch.object(orchestrator, "_gate_press", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
                    api_key="testkey",
                    output_dir=cache_dir,
                    market_cap_range=(500_000_000, 10_000_000_000),
                )
            self.assertEqual(list(df["ticker"]), ["QUBT"])
            self.assertIn("market_cap", df.columns)
            self.assertEqual(df.iloc[0]["market_cap"], 1_780_000_000)

    def test_map_themes_logs_dropped_candidate_counts(self):
        # When keep_unverified=False drops candidates, operator should see
        # how many dropped total and how many had all-4 gates unknown
        # (distinct from real-negative). Logged at INFO so production output
        # is auditable without polluting the parquet schema.
        import logging

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    orchestrator.gemini_mapper,
                    "propose_candidates",
                    return_value=[
                        {"ticker": "QBTS", "rationale": "x", "confidence": 0.9},  # verified
                        {"ticker": "MISS1", "rationale": "x", "confidence": 0.5},  # all-failed
                        {"ticker": "MISS2", "rationale": "x", "confidence": 0.5},  # all-unknown
                    ],
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QBTS": 1e9, "MISS1": 1e9, "MISS2": 1e9},
                ),
                # QBTS passes etf; MISS1 fails all (real no); MISS2 unknown all.
                patch.object(orchestrator, "_gate_etf", side_effect=[True, False, None]),
                patch.object(orchestrator, "_gate_tenk", side_effect=[False, False, None]),
                patch.object(orchestrator, "_gate_press", side_effect=[False, False, None]),
                patch.object(orchestrator, "_gate_insider", side_effect=[False, False, None]),
                self.assertLogs(
                    "alphalens.thematic.mapping.orchestrator", level=logging.INFO
                ) as cm,
            ):
                df = orchestrator.map_themes(
                    themes=["quantum"],
                    asof=dt.date(2026, 5, 15),
                    api_key="testkey",
                    output_dir=cache_dir,
                    keep_unverified=False,
                )
        self.assertEqual(len(df), 1)
        joined = "\n".join(cm.output)
        self.assertIn("dropped 2", joined)
        self.assertIn("all-unknown 1", joined)

    def test_map_themes_press_window_failure_marks_press_unknown(self):
        # When fetch_window_universe raises (Polygon rate limit / network),
        # orchestrator must propagate None to _gate_press so the per-ticker
        # fallback runs (also tri-state). If left as empty DataFrame, the
        # frame variant returns False for every candidate — silently
        # converting an unknown into a false-negative for ALL candidates
        # affected by one batch outage.

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    orchestrator.gemini_mapper,
                    "propose_candidates",
                    return_value=[{"ticker": "QBTS", "rationale": "x", "confidence": 0.9}],
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QBTS": 1_000_000_000},
                ),
                patch.object(
                    orchestrator.recent_press,
                    "fetch_window_universe",
                    side_effect=RuntimeError("polygon down"),
                ),
                # Per-ticker fallback also fails -> tri-state None
                patch.object(
                    orchestrator.recent_press,
                    "has_theme_in_recent_press",
                    return_value=None,
                ),
                patch.object(orchestrator, "_gate_etf", return_value=False),
                patch.object(orchestrator, "_gate_tenk", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum"],
                    asof=dt.date(2026, 5, 15),
                    api_key="testkey",
                    polygon_api_key="px",
                    output_dir=cache_dir,
                    keep_unverified=True,
                )
            self.assertEqual(len(df), 1)
            self.assertIn("press", df.iloc[0]["gates_unknown"])
            self.assertNotIn("press", df.iloc[0]["gates_failed"])

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


class TestGateWrappers(unittest.TestCase):
    def test_gate_etf_delegates(self):
        with patch.object(orchestrator.etf_holdings, "is_in_thematic_etf", return_value=True):
            self.assertTrue(
                orchestrator._gate_etf(ticker="NVDA", themes=["q"], asof=dt.date(2026, 5, 15))
            )

    def test_gate_tenk_delegates(self):
        with patch.object(orchestrator.tenk_grep, "has_theme_keywords_in_10k", return_value=False):
            self.assertFalse(
                orchestrator._gate_tenk(
                    ticker="NVDA", theme_keywords=["q"], asof=dt.date(2026, 5, 15)
                )
            )

    def test_gate_press_uses_frame_when_provided(self):
        import pandas as pd

        with patch.object(orchestrator.recent_press, "has_theme_in_press_frame", return_value=True):
            self.assertTrue(
                orchestrator._gate_press(
                    ticker="NVDA",
                    theme_keywords=["q"],
                    asof=dt.date(2026, 5, 15),
                    api_key="k",
                    press_df=pd.DataFrame(),
                )
            )

    def test_gate_press_falls_back_when_no_frame(self):
        with patch.object(
            orchestrator.recent_press, "has_theme_in_recent_press", return_value=True
        ):
            self.assertTrue(
                orchestrator._gate_press(
                    ticker="NVDA",
                    theme_keywords=["q"],
                    asof=dt.date(2026, 5, 15),
                    api_key="k",
                )
            )

    def test_gate_insider_delegates(self):
        with patch.object(orchestrator.insider, "has_opportunistic_buy", return_value=False):
            self.assertFalse(orchestrator._gate_insider(ticker="NVDA", asof=dt.date(2026, 5, 15)))

    def test_theme_keywords_expands_snake_case(self):
        kws = orchestrator._theme_keywords("quantum_computing")
        self.assertIn("quantum_computing", kws)
        self.assertIn("quantum computing", kws)


class TestMapThemesWritesGatesPassedStr(unittest.TestCase):
    def test_gates_passed_str_column_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    orchestrator.gemini_mapper,
                    "propose_candidates",
                    return_value=[{"ticker": "QBTS", "rationale": "x", "confidence": 0.9}],
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QBTS": 1_000_000_000},
                ),
                patch.object(orchestrator, "_gate_etf", return_value=True),
                patch.object(orchestrator, "_gate_tenk", return_value=False),
                patch.object(orchestrator, "_gate_press", return_value=True),
                patch.object(orchestrator, "_gate_insider", return_value=False),
                patch.object(
                    orchestrator.recent_press,
                    "fetch_window_universe",
                    return_value=__import__("pandas").DataFrame(),
                ),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum"],
                    asof=dt.date(2026, 5, 15),
                    api_key="testkey",
                    polygon_api_key="px",
                    output_dir=cache_dir,
                )
            self.assertIn("gates_passed_str", df.columns)
            self.assertEqual(df.iloc[0]["gates_passed_str"], "etf,press")


if __name__ == "__main__":
    unittest.main()
