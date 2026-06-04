import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alphalens_pipeline.thematic.mapping import orchestrator, theme_mapper
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload


def _catalyst_payload(
    *,
    url: str = "https://example.com/catalyst",
    title: str = "Stub catalyst event",
    published_at: str = "2026-05-14",
) -> CatalystPayload:
    """Build a CatalystPayload stub for the orchestrator map_themes tests
    (orchestrator._build_row only reads url / title / published_at)."""
    return CatalystPayload(
        url=url,
        title=title,
        published_at=published_at,
        event_type="m_and_a",
        confidence=0.9,
        second_order_implications=[],
        echo_count=1,
        trigger_url=url,
        trigger_published_at=published_at,
        is_amplified=False,
        template_id=None,
        template_facts=None,
    )


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
    ],
    "search_keywords": [
        "quantum computing",
        "qubit",
        "quantum annealing",
        "trapped-ion",
    ],
}


def _mapper_result(
    candidates: list[dict] | None = None,
    search_keywords: list[str] | None = None,
) -> dict:
    """Build the dict shape returned by ``theme_mapper.propose_candidates``."""
    return {
        "candidates": list(candidates or []),
        "search_keywords": list(search_keywords or []),
    }


# ============================================================
# Gemini 3 Pro mapper
# ============================================================


class TestGeminiMapperPromptBuilding(unittest.TestCase):
    def test_prompt_includes_theme(self):
        prompt = theme_mapper.build_prompt(theme="quantum_computing")
        self.assertIn("quantum_computing", prompt)

    def test_prompt_does_not_constrain_market_cap(self):
        # Mcap brackets in the prompt are unreliable: Pro filters against its
        # training-cutoff mcap snapshot, not real-time. (Probe 2026-05-17:
        # Pro believed QUBT mcap = $50M vs real $1.78B.) Filtering belongs in
        # the orchestrator post-LLM via yfinance.
        prompt = theme_mapper.build_prompt(theme="quantum_computing")
        for token in ("market cap", "market_cap", "small-cap", "mid-cap", "small/mid"):
            self.assertNotIn(token.lower(), prompt.lower())

    def test_prompt_asks_for_search_keywords(self):
        # Verification gates (press, 10-K) need theme-level keywords with
        # synonyms / common phrasings, not just the snake_case theme name.
        # The naive ``_theme_keywords`` swap proved too narrow during the
        # 2023-01-23 MSFT-OpenAI retrospective (theme "AI development" never
        # matched real-world press headlines that say "artificial intelligence"
        # / "generative AI"). Pro already understands the theme — ask it for
        # the search vocabulary in the same call rather than maintaining a
        # synonym YAML or paying a second LLM hop.
        prompt = theme_mapper.build_prompt(theme="quantum_computing")
        self.assertIn("search_keywords", prompt)


class TestPropose(unittest.TestCase):
    def test_propose_returns_dict_with_candidates_and_keywords(self):
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_MAPPER_RESPONSE))
        with patch.object(theme_mapper, "_call_llm", return_value=fake_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        self.assertIsInstance(result, dict)
        self.assertIn("candidates", result)
        self.assertIn("search_keywords", result)
        self.assertEqual(len(result["candidates"]), 3)
        # Tickers uppercased
        self.assertEqual(result["candidates"][0]["ticker"], "QBTS")
        # Confidence preserved
        self.assertEqual(result["candidates"][1]["confidence"], 0.9)
        # Pro-supplied keywords surfaced
        self.assertIn("quantum computing", result["search_keywords"])
        self.assertIn("qubit", result["search_keywords"])

    def test_propose_normalizes_keywords(self):
        # Whitespace, casing, duplicates, blanks — all normalized so downstream
        # consumers can substring-match without reapplying.
        payload = {
            "candidates": SAMPLE_MAPPER_RESPONSE["candidates"],
            "search_keywords": [
                "  Quantum Computing  ",
                "QUANTUM COMPUTING",
                "qubit",
                "",
                "   ",
                "trapped-ion",
            ],
        }
        fake_response = SimpleNamespace(text=json.dumps(payload))
        with patch.object(theme_mapper, "_call_llm", return_value=fake_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        kws = result["search_keywords"]
        # Dedup on case-folded form; preserves first-seen casing minus trim.
        self.assertEqual(
            sorted(k.lower() for k in kws),
            sorted(["qubit", "quantum computing", "trapped-ion"]),
        )
        self.assertNotIn("", kws)

    def test_propose_keywords_default_to_theme_swap_when_missing(self):
        # Older Pro responses (pre-schema bump) lack `search_keywords`. Fall
        # back to the snake↔space swap so verification gates still get
        # *something* searchable, even if narrow.
        payload = {"candidates": SAMPLE_MAPPER_RESPONSE["candidates"]}
        fake_response = SimpleNamespace(text=json.dumps(payload))
        with patch.object(theme_mapper, "_call_llm", return_value=fake_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        self.assertEqual(
            sorted(result["search_keywords"]),
            sorted(["quantum_computing", "quantum computing"]),
        )

    def test_propose_returns_empty_on_api_error(self):
        with patch.object(theme_mapper, "_call_llm", side_effect=RuntimeError("boom")):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["search_keywords"], [])

    def test_propose_returns_empty_on_unparseable(self):
        bad_response = SimpleNamespace(text="not json")
        with patch.object(theme_mapper, "_call_llm", return_value=bad_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["search_keywords"], [])

    def test_propose_drops_malformed_candidate_entries(self):
        # Zen pre-push HIGH finding: Pro may violate schema and return a dict
        # where a list is expected, or non-dict items inside the list. Without
        # type guards in ``_normalize`` the ``it.get("ticker")`` call raises
        # AttributeError and crashes the whole batch run.
        payload = {
            "candidates": [
                {"ticker": "QBTS", "rationale": "ok", "confidence": 0.9},
                "not a dict",  # malformed: string instead of dict
                None,  # malformed: None instead of dict
                ["also", "wrong"],  # malformed: list instead of dict
            ],
            "search_keywords": ["quantum"],
        }
        fake_response = SimpleNamespace(text=json.dumps(payload))
        with patch.object(theme_mapper, "_call_llm", return_value=fake_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        # Only the well-formed entry survives.
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["ticker"], "QBTS")

    def test_propose_handles_candidates_as_dict_not_list(self):
        # Pro returning a single object instead of an array. ``_normalize``
        # must not iterate over a dict's keys — that would yield string keys
        # and crash on ``str.get``.
        payload = {
            "candidates": {"ticker": "QBTS", "rationale": "ok", "confidence": 0.9},
            "search_keywords": ["quantum"],
        }
        fake_response = SimpleNamespace(text=json.dumps(payload))
        with patch.object(theme_mapper, "_call_llm", return_value=fake_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        # Non-list candidates payload coerces to empty rather than crashing.
        self.assertEqual(result["candidates"], [])

    def test_propose_handles_search_keywords_as_bare_string(self):
        # Zen pre-push CRITICAL finding: if Pro returns ``"quantum"`` (a bare
        # string) instead of ``["quantum"]``, iterating over it yields
        # characters. Single-char keywords substring-match every press headline
        # and 10-K paragraph, silently false-verifying every candidate.
        payload = {
            "candidates": SAMPLE_MAPPER_RESPONSE["candidates"],
            "search_keywords": "quantum",  # bare string, not a list
        }
        fake_response = SimpleNamespace(text=json.dumps(payload))
        with patch.object(theme_mapper, "_call_llm", return_value=fake_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        kws = result["search_keywords"]
        # Bare string must NOT explode into ['q','u','a',...]. Either wrap
        # into a single-element list OR drop and fall back to swap — both are
        # acceptable; what's unacceptable is character-level iteration.
        self.assertTrue(
            all(len(k) >= 2 for k in kws),
            f"keywords contain a 1-char entry that would false-match: {kws}",
        )

    def test_propose_drops_non_string_keywords(self):
        payload = {
            "candidates": SAMPLE_MAPPER_RESPONSE["candidates"],
            "search_keywords": [
                "valid keyword",
                123,  # int — drop
                None,  # None — drop
                {"keyword": "quantum"},  # dict — drop
                "another valid",
            ],
        }
        fake_response = SimpleNamespace(text=json.dumps(payload))
        with patch.object(theme_mapper, "_call_llm", return_value=fake_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        self.assertEqual(
            sorted(k.lower() for k in result["search_keywords"]),
            sorted(["valid keyword", "another valid"]),
        )

    def test_propose_drops_single_character_keywords(self):
        # Defense-in-depth: even if a 1-char keyword survives normalisation
        # somehow (e.g. Pro returns ["A","I","ML"]), it must be filtered out
        # before reaching the gates — substring-matching "A" against any 10-K
        # paragraph would return True for every candidate.
        payload = {
            "candidates": SAMPLE_MAPPER_RESPONSE["candidates"],
            "search_keywords": ["A", "I", "ML", "machine learning"],
        }
        fake_response = SimpleNamespace(text=json.dumps(payload))
        with patch.object(theme_mapper, "_call_llm", return_value=fake_response):
            result = theme_mapper.propose_candidates(theme="quantum_computing", api_key="testkey")
        kws = result["search_keywords"]
        self.assertNotIn("A", kws)
        self.assertNotIn("I", kws)
        self.assertIn("ML", kws)  # 2-char abbreviation is fine
        self.assertIn("machine learning", kws)


# ============================================================
# Orchestrator
# ============================================================


class TestVerifyCandidate(unittest.TestCase):
    def test_verify_runs_all_three_gates_and_collects_passes(self):
        with (
            patch.object(orchestrator, "_gate_tenk", return_value=True),
            patch.object(orchestrator, "_gate_press", return_value=True),
            patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            result = orchestrator.verify_candidate(
                ticker="QBTS",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
            )
        self.assertEqual(set(result["gates_passed"]), {"tenk", "press"})
        self.assertEqual(set(result["gates_failed"]), {"insider"})
        self.assertEqual(result["gates_unknown"], [])
        self.assertTrue(result["verified"])

    def test_verify_returns_unverified_when_zero_gates_pass(self):
        with (
            patch.object(orchestrator, "_gate_tenk", return_value=False),
            patch.object(orchestrator, "_gate_press", return_value=False),
            patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            result = orchestrator.verify_candidate(
                ticker="MADEUP",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
            )
        self.assertEqual(result["gates_passed"], [])
        self.assertEqual(set(result["gates_failed"]), {"tenk", "press", "insider"})
        self.assertEqual(result["gates_unknown"], [])
        self.assertFalse(result["verified"])

    def test_verify_records_unknown_when_gate_returns_none(self):
        # tenk + insider return None (CIK miss / fetch fail) -> go to
        # gates_unknown, not gates_failed. Verified rule unchanged: needs
        # ≥1 pass.
        with (
            patch.object(orchestrator, "_gate_tenk", return_value=None),
            patch.object(orchestrator, "_gate_press", return_value=True),
            patch.object(orchestrator, "_gate_insider", return_value=None),
        ):
            result = orchestrator.verify_candidate(
                ticker="FOREIGN",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
            )
        self.assertEqual(result["gates_passed"], ["press"])
        self.assertEqual(result["gates_failed"], [])
        self.assertEqual(set(result["gates_unknown"]), {"tenk", "insider"})
        self.assertTrue(result["verified"])  # press passed = verified

    def test_verify_unknown_alone_does_not_promote_verified(self):
        # Conservative rule: unknowns don't promote verified=True. Pinning
        # the rule from the 2026-05-17 plan §C5 lock.
        with (
            patch.object(orchestrator, "_gate_tenk", return_value=None),
            patch.object(orchestrator, "_gate_press", return_value=None),
            patch.object(orchestrator, "_gate_insider", return_value=None),
        ):
            result = orchestrator.verify_candidate(
                ticker="OPAQUE",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
            )
        self.assertEqual(result["gates_passed"], [])
        self.assertEqual(result["gates_failed"], [])
        self.assertEqual(len(result["gates_unknown"]), 3)
        self.assertFalse(result["verified"])

    def test_verify_safe_wrapper_treats_exception_as_unknown(self):
        # An exception inside a gate is "we don't know", not False.
        with (
            patch.object(orchestrator, "_gate_tenk", side_effect=RuntimeError("boom")),
            patch.object(orchestrator, "_gate_press", return_value=True),
            patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            result = orchestrator.verify_candidate(
                ticker="QBTS",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
            )
        self.assertEqual(result["gates_passed"], ["press"])
        self.assertEqual(set(result["gates_failed"]), {"insider"})
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

        def fake_press(*, ticker, theme_keywords, asof, polygon_client=None, press_df=None):
            captured["press"] = list(theme_keywords)
            return False

        with (
            patch.object(orchestrator, "_gate_tenk", side_effect=fake_tenk),
            patch.object(orchestrator, "_gate_press", side_effect=fake_press),
            patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            orchestrator.verify_candidate(
                ticker="QBTS",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
            )
        self.assertIn("quantum_computing", captured["tenk"])
        self.assertIn("quantum computing", captured["tenk"])
        self.assertIn("quantum_computing", captured["press"])
        self.assertIn("quantum computing", captured["press"])

    def test_verify_handles_individual_gate_failure(self):
        # Insider gate raises -> treated as gate not passing, other gates still run
        with (
            patch.object(orchestrator, "_gate_tenk", return_value=True),
            patch.object(orchestrator, "_gate_press", return_value=False),
            patch.object(orchestrator, "_gate_insider", side_effect=RuntimeError("io")),
        ):
            result = orchestrator.verify_candidate(
                ticker="QBTS",
                themes=["quantum_computing"],
                asof=dt.date(2026, 5, 15),
            )
        self.assertEqual(result["gates_passed"], ["tenk"])
        self.assertTrue(result["verified"])


_DEFAULT_CATALYST = _catalyst_payload()


def _install_default_catalyst(testcase: unittest.TestCase) -> None:
    """Patch ``find_trigger_event`` so legacy ``map_themes`` tests still
    produce rows. Per ``TestSkipsThemesWithoutCatalyst``, a falsy catalyst
    now short-circuits the theme — tests that don't care about that path
    must inject a truthy stub.
    """
    patcher = patch.object(
        orchestrator.catalyst_resolver,
        "find_trigger_event",
        return_value=_DEFAULT_CATALYST,
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


class TestMapThemes(unittest.TestCase):
    def setUp(self) -> None:
        _install_default_catalyst(self)

    def test_map_themes_writes_parquet_with_verified_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)

            # Mock Gemini proposal to deterministic output
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[
                            {"ticker": "QBTS", "rationale": "quantum", "confidence": 0.9},
                            {"ticker": "MADEUP", "rationale": "halluc", "confidence": 0.3},
                        ],
                        search_keywords=["quantum computing", "qubit"],
                    ),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QBTS": 1_000_000_000, "MADEUP": 1_000_000_000},
                ),
                patch.object(orchestrator, "_gate_tenk", side_effect=[True, False]),
                patch.object(orchestrator, "_gate_press", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=cache_dir,
                    keep_unverified=False,
                )

            self.assertEqual(len(df), 1)  # only QBTS verified
            self.assertEqual(df.iloc[0]["ticker"], "QBTS")
            self.assertTrue(df.iloc[0]["verified"])
            # Pro-supplied keywords persisted alongside the candidate so
            # downstream consumers (briefs, audits) see what the verification
            # gates actually searched against.
            self.assertIn("theme_search_keywords", df.columns)
            self.assertEqual(
                sorted(df.iloc[0]["theme_search_keywords"]),
                sorted(["quantum computing", "qubit"]),
            )
            out = cache_dir / "2026-05-15.parquet"
            self.assertTrue(out.exists())

    def test_map_themes_keeps_unverified_when_flag_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[
                            {"ticker": "MADEUP", "rationale": "halluc", "confidence": 0.3},
                        ],
                    ),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"MADEUP": 1_000_000_000},
                ),
                patch.object(orchestrator, "_gate_tenk", return_value=False),
                patch.object(orchestrator, "_gate_press", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
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
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[
                            {"ticker": "QUBT", "rationale": "pure-play", "confidence": 0.9},
                            {"ticker": "NVDA", "rationale": "mega-cap", "confidence": 0.5},
                            {"ticker": "MICRO", "rationale": "tiny", "confidence": 0.4},
                        ],
                    ),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QUBT": 1_780_000_000},
                ),
                patch.object(orchestrator, "_gate_tenk", return_value=True),
                patch.object(orchestrator, "_gate_press", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
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
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[
                            {"ticker": "QBTS", "rationale": "x", "confidence": 0.9},  # verified
                            {"ticker": "MISS1", "rationale": "x", "confidence": 0.5},  # all-failed
                            {"ticker": "MISS2", "rationale": "x", "confidence": 0.5},  # all-unknown
                        ],
                    ),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QBTS": 1e9, "MISS1": 1e9, "MISS2": 1e9},
                ),
                # QBTS passes tenk; MISS1 fails all (real no); MISS2 unknown all.
                patch.object(orchestrator, "_gate_tenk", side_effect=[True, False, None]),
                patch.object(orchestrator, "_gate_press", side_effect=[False, False, None]),
                patch.object(orchestrator, "_gate_insider", side_effect=[False, False, None]),
                self.assertLogs(
                    "alphalens_pipeline.thematic.mapping.orchestrator", level=logging.INFO
                ) as cm,
            ):
                df = orchestrator.map_themes(
                    themes=["quantum"],
                    asof=dt.date(2026, 5, 15),
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
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[{"ticker": "QBTS", "rationale": "x", "confidence": 0.9}],
                    ),
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
                patch.object(orchestrator, "_gate_tenk", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum"],
                    asof=dt.date(2026, 5, 15),
                    polygon_api_key="px",
                    output_dir=cache_dir,
                    keep_unverified=True,
                )
            self.assertEqual(len(df), 1)
            self.assertIn("press", df.iloc[0]["gates_unknown"])
            self.assertNotIn("press", df.iloc[0]["gates_failed"])

    def test_map_themes_empty_when_no_proposals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_mapper_result(),
            ):
                df = orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=Path(tmpdir),
                )
            self.assertEqual(len(df), 0)

    def test_map_themes_passes_pro_keywords_to_verify(self):
        # Bug surfaced by 2023-01-23 MSFT-OpenAI retrospective: the naive
        # ``_theme_keywords`` swap on theme "AI development" returned only
        # ["AI development"] — too narrow to substring-match real press
        # headlines that say "artificial intelligence" / "machine learning".
        # When Pro supplies keywords, they MUST be threaded through to the
        # gates instead of the fallback swap.
        captured: dict[str, list[str] | None] = {}

        def _capture_tenk(*, ticker, theme_keywords, asof):
            captured["tenk_keywords"] = list(theme_keywords)
            return False

        def _capture_press(*, ticker, theme_keywords, asof, polygon_client=None, press_df=None):
            captured["press_keywords"] = list(theme_keywords)
            return False

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[{"ticker": "VRT", "rationale": "x", "confidence": 0.9}],
                        search_keywords=[
                            "artificial intelligence",
                            "machine learning",
                            "generative AI",
                        ],
                    ),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"VRT": 1_000_000_000},
                ),
                patch.object(orchestrator, "_gate_tenk", side_effect=_capture_tenk),
                patch.object(orchestrator, "_gate_press", side_effect=_capture_press),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                orchestrator.map_themes(
                    themes=["AI development"],
                    asof=dt.date(2023, 1, 23),
                    output_dir=Path(tmpdir),
                    keep_unverified=True,
                )

        self.assertEqual(
            captured["tenk_keywords"],
            ["artificial intelligence", "machine learning", "generative AI"],
        )
        self.assertEqual(
            captured["press_keywords"],
            ["artificial intelligence", "machine learning", "generative AI"],
        )

    def test_map_themes_falls_back_to_swap_when_pro_keywords_missing(self):
        # If Pro returns no keywords (older response shape or empty list),
        # gates still receive the snake↔space swap so they have *something*
        # to substring-match against — narrow, but not empty.
        captured: dict[str, list[str] | None] = {}

        def _capture_tenk(*, ticker, theme_keywords, asof):
            captured["tenk_keywords"] = list(theme_keywords)
            return False

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[{"ticker": "QBTS", "rationale": "x", "confidence": 0.9}],
                        # Pro returned nothing for keywords
                        search_keywords=[],
                    ),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QBTS": 1_000_000_000},
                ),
                patch.object(orchestrator, "_gate_tenk", side_effect=_capture_tenk),
                patch.object(orchestrator, "_gate_press", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                orchestrator.map_themes(
                    themes=["quantum_computing"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=Path(tmpdir),
                    keep_unverified=True,
                )
        self.assertEqual(
            sorted(captured["tenk_keywords"]),
            sorted(["quantum_computing", "quantum computing"]),
        )


class TestGateWrappers(unittest.TestCase):
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
                )
            )

    def test_gate_press_falls_back_to_per_ticker_when_frame_returns_none(self):
        # Issue #149 fix: when the batch frame has no rows for the ticker,
        # ``has_theme_in_press_frame`` returns None (we don't know). The
        # orchestrator must fall through to the per-ticker fetch instead of
        # propagating None as a real "no". This catches the silent
        # false-negative pattern seen on the 2023-01-23 retrospective.
        import pandas as pd

        with (
            patch.object(
                orchestrator.recent_press, "has_theme_in_press_frame", return_value=None
            ) as mock_frame,
            patch.object(
                orchestrator.recent_press, "has_theme_in_recent_press", return_value=True
            ) as mock_per_ticker,
        ):
            result = orchestrator._gate_press(
                ticker="VRT",
                theme_keywords=["AI"],
                asof=dt.date(2023, 1, 23),
                press_df=pd.DataFrame({"placeholder": [1]}),
            )
        self.assertTrue(result)
        mock_frame.assert_called_once()
        mock_per_ticker.assert_called_once()

    def test_gate_press_trusts_frame_when_it_returns_real_negative(self):
        # When the frame DOES have rows for the ticker but no keyword hit,
        # ``has_theme_in_press_frame`` returns False — that's a real "no" and
        # the per-ticker fallback must NOT run (wasted API call, defeats
        # the batch optimisation).
        import pandas as pd

        with (
            patch.object(
                orchestrator.recent_press, "has_theme_in_press_frame", return_value=False
            ) as mock_frame,
            patch.object(orchestrator.recent_press, "has_theme_in_recent_press") as mock_per_ticker,
        ):
            result = orchestrator._gate_press(
                ticker="NVDA",
                theme_keywords=["quantum"],
                asof=dt.date(2026, 5, 15),
                press_df=pd.DataFrame({"placeholder": [1]}),
            )
        self.assertIs(result, False)
        mock_frame.assert_called_once()
        mock_per_ticker.assert_not_called()

    def test_gate_insider_delegates(self):
        with patch.object(orchestrator.insider, "has_opportunistic_buy", return_value=False):
            self.assertFalse(orchestrator._gate_insider(ticker="NVDA", asof=dt.date(2026, 5, 15)))

    def test_theme_keywords_expands_snake_case(self):
        kws = orchestrator._theme_keywords("quantum_computing")
        self.assertIn("quantum_computing", kws)
        self.assertIn("quantum computing", kws)


class TestMapThemesWritesGatesPassedStr(unittest.TestCase):
    def setUp(self) -> None:
        _install_default_catalyst(self)

    def test_gates_passed_str_column_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[{"ticker": "QBTS", "rationale": "x", "confidence": 0.9}],
                    ),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"QBTS": 1_000_000_000},
                ),
                patch.object(orchestrator, "_gate_tenk", return_value=True),
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
                    polygon_api_key="px",
                    output_dir=cache_dir,
                )
            self.assertIn("gates_passed_str", df.columns)
            self.assertEqual(df.iloc[0]["gates_passed_str"], "tenk,press")


# ============================================================
# Diversity guardrail + verify-backfill (Phase D)
# ============================================================


def _five_candidates_unsorted_confidences() -> list[dict]:
    """5 candidates with confidences [0.5, 0.9, 0.6, 0.8, 0.7] in insertion order.

    Sorting desc yields the order T1(0.9), T3(0.8), T4(0.7), T2(0.6), T0(0.5)
    — verify_candidate must be called in that order.
    """
    confs = [0.5, 0.9, 0.6, 0.8, 0.7]
    return [{"ticker": f"T{i}", "rationale": "x", "confidence": c} for i, c in enumerate(confs)]


def _verify_dict(ticker: str, *, verified: bool, all_unknown: bool = False) -> dict:
    if verified:
        return {
            "ticker": ticker,
            "verified": True,
            "gates_passed": ["tenk"],
            "gates_failed": [],
            "gates_unknown": [],
        }
    if all_unknown:
        return {
            "ticker": ticker,
            "verified": False,
            "gates_passed": [],
            "gates_failed": [],
            "gates_unknown": list(orchestrator.GATE_NAMES),
        }
    return {
        "ticker": ticker,
        "verified": False,
        "gates_passed": [],
        "gates_failed": list(orchestrator.GATE_NAMES),
        "gates_unknown": [],
    }


class TestDiversityGuardrail(unittest.TestCase):
    def setUp(self) -> None:
        _install_default_catalyst(self)

    def test_module_constants_locked(self):
        self.assertEqual(orchestrator._MAX_CANDIDATES_PER_THEME, 3)
        self.assertEqual(orchestrator._MAX_VERIFY_ATTEMPTS_PER_THEME, 5)

    def test_etf_dropped_from_gate_names(self):
        # ETF gate dropped per docs/research/thematic_verification_gate_audit_2026_05_22.md.
        # Thematic ETFs cover ~9 narrow industries; Layer 2's free-form theme
        # vocabulary rarely matches the YAML keys, so this gate was 100% UNK
        # in production. Keeping it in GATE_NAMES inflated the gates_unknown
        # column without providing signal.
        self.assertNotIn("etf", orchestrator.GATE_NAMES)
        self.assertEqual(orchestrator.GATE_NAMES, ("tenk", "press", "insider"))

    def test_top3_by_confidence_passed_to_verify(self):
        candidates = _five_candidates_unsorted_confidences()
        mcap = {c["ticker"]: 1_000_000_000 for c in candidates}
        # Verify called in confidence-desc order: T1(0.9), T3(0.8), T4(0.7).
        # All three pass; cap reached → no further verify calls.
        verify_results = [
            _verify_dict("T1", verified=True),
            _verify_dict("T3", verified=True),
            _verify_dict("T4", verified=True),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(candidates=candidates),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value=mcap,
                ),
                patch.object(
                    orchestrator,
                    "verify_candidate",
                    side_effect=verify_results,
                ) as mock_verify,
            ):
                df = orchestrator.map_themes(
                    themes=["quantum"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=Path(tmpdir),
                )
        self.assertEqual(mock_verify.call_count, 3)
        self.assertEqual(len(df), 3)
        self.assertEqual(set(df["ticker"]), {"T1", "T3", "T4"})

    def test_backfills_after_verification_failure(self):
        # Order: T1(0.9), T3(0.8), T4(0.7), T2(0.6), T0(0.5).
        # T3 hard-fails → backfill to T2 → 4 verify calls total, 3 kept.
        candidates = _five_candidates_unsorted_confidences()
        mcap = {c["ticker"]: 1_000_000_000 for c in candidates}
        verify_results = [
            _verify_dict("T1", verified=True),
            _verify_dict("T3", verified=False),  # hard-fail
            _verify_dict("T4", verified=True),
            _verify_dict("T2", verified=True),  # backfill
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(candidates=candidates),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value=mcap,
                ),
                patch.object(
                    orchestrator,
                    "verify_candidate",
                    side_effect=verify_results,
                ) as mock_verify,
            ):
                df = orchestrator.map_themes(
                    themes=["quantum"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=Path(tmpdir),
                )
        self.assertEqual(mock_verify.call_count, 4)
        self.assertEqual(len(df), 3)
        self.assertEqual(set(df["ticker"]), {"T1", "T4", "T2"})

    def test_backfill_capped_at_max_attempts(self):
        # All 5 candidates hard-fail; cap at MAX_VERIFY_ATTEMPTS_PER_THEME=5.
        candidates = _five_candidates_unsorted_confidences()
        mcap = {c["ticker"]: 1_000_000_000 for c in candidates}
        ordered_tickers = ["T1", "T3", "T4", "T2", "T0"]
        verify_results = [_verify_dict(t, verified=False) for t in ordered_tickers]
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(candidates=candidates),
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value=mcap,
                ),
                patch.object(
                    orchestrator,
                    "verify_candidate",
                    side_effect=verify_results,
                ) as mock_verify,
            ):
                df = orchestrator.map_themes(
                    themes=["quantum"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=Path(tmpdir),
                )
        self.assertEqual(mock_verify.call_count, 5)
        self.assertEqual(len(df), 0)

    def test_two_themes_each_capped_independently(self):
        # Two themes, each with 5 candidates all verifying True.
        # Each theme should contribute exactly 3 rows; total 6.
        candidates_a = _five_candidates_unsorted_confidences()
        candidates_b = [
            {"ticker": f"S{i}", "rationale": "x", "confidence": c}
            for i, c in enumerate([0.5, 0.9, 0.6, 0.8, 0.7])
        ]
        mcap = {c["ticker"]: 1_000_000_000 for c in (candidates_a + candidates_b)}

        # Mapper returns different candidates per theme call.
        def _propose_side_effect(*, theme, api_key, llm_client):
            if theme == "theme_a":
                return _mapper_result(candidates=candidates_a)
            return _mapper_result(candidates=candidates_b)

        verify_count = {"n": 0}

        def _verify_side_effect(**kwargs):
            verify_count["n"] += 1
            return _verify_dict(kwargs["ticker"], verified=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    side_effect=_propose_side_effect,
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value=mcap,
                ),
                patch.object(
                    orchestrator,
                    "verify_candidate",
                    side_effect=_verify_side_effect,
                ),
            ):
                df = orchestrator.map_themes(
                    themes=["theme_a", "theme_b"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=Path(tmpdir),
                )
        self.assertEqual(len(df), 6)  # 3 per theme
        self.assertEqual(verify_count["n"], 6)
        self.assertEqual(len(df[df["theme"] == "theme_a"]), 3)
        self.assertEqual(len(df[df["theme"] == "theme_b"]), 3)


# ============================================================
# Skip themes whose catalyst resolver returns no event
# ============================================================


class TestSkipsThemesWithoutCatalyst(unittest.TestCase):
    """When ``_resolve_catalyst`` returns nothing for a theme (no
    non-noise events tagged with the theme in the lookback window),
    ``map_themes`` must skip the whole theme — no Pro proposal, no
    candidates, no rows. Pinned 2026-05-22 after GO/OLLI surfaced in the
    brief with empty ``source_event_url``: the ``discounts`` theme had
    only ``event_type=promo`` events, all dropped by
    ``NOISE_EVENT_TYPES`` filter, so the catalyst payload was empty and
    every emitted row pointed at nowhere.
    """

    def test_theme_without_catalyst_does_not_call_pro_proposal(self):
        propose_calls: list[str] = []

        def _track_propose(*, theme, api_key, llm_client):
            propose_calls.append(theme)
            return _mapper_result(
                candidates=[{"ticker": "FOO", "rationale": "x", "confidence": 0.9}],
            )

        def _resolver(*, theme, asof):
            if theme == "discounts":
                return None
            return _catalyst_payload(
                url=f"https://example.com/{theme}",
                title=f"{theme} catalyst",
                published_at="2026-05-15",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    orchestrator.catalyst_resolver,
                    "find_trigger_event",
                    side_effect=_resolver,
                ),
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    side_effect=_track_propose,
                ),
                patch.object(
                    orchestrator.mcap_filter,
                    "filter_by_mcap",
                    return_value={"FOO": 1_000_000_000},
                ),
                patch.object(orchestrator, "_gate_tenk", return_value=True),
                patch.object(orchestrator, "_gate_press", return_value=False),
                patch.object(orchestrator, "_gate_insider", return_value=False),
            ):
                df = orchestrator.map_themes(
                    themes=["discounts", "quantum_computing"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=Path(tmpdir),
                )

        # Pro proposal called only for the theme that has a catalyst.
        self.assertEqual(propose_calls, ["quantum_computing"])
        # df has exactly one row from the surviving theme.
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["theme"], "quantum_computing")
        self.assertEqual(df.iloc[0]["ticker"], "FOO")
        # And that row has a non-empty source URL (sanity — the invariant
        # the skip is enforcing is that surviving candidates carry a real
        # provenance link).
        self.assertTrue(df.iloc[0]["source_event_url"])

    def test_all_themes_without_catalyst_produces_empty_df(self):
        # Edge case: zero themes have a catalyst → df is empty but schema-
        # complete (downstream readers shouldn't crash on a 0-row frame).
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    orchestrator.catalyst_resolver,
                    "find_trigger_event",
                    return_value=None,
                ),
                patch.object(
                    orchestrator.theme_mapper,
                    "propose_candidates",
                    return_value=_mapper_result(
                        candidates=[{"ticker": "FOO", "confidence": 0.9}],
                    ),
                ) as mock_propose,
            ):
                df = orchestrator.map_themes(
                    themes=["discounts", "lifestyle_brands"],
                    asof=dt.date(2026, 5, 15),
                    output_dir=Path(tmpdir),
                )

        mock_propose.assert_not_called()
        self.assertEqual(len(df), 0)
        # Schema preserved on empty frame (downstream parquet consumers
        # do ``df.columns`` introspection).
        for col in (
            "theme",
            "ticker",
            "source_event_url",
            "source_event_title",
            "source_event_published_at",
        ):
            self.assertIn(col, df.columns)


if __name__ == "__main__":
    unittest.main()
