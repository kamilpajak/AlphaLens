"""Brief-render sort + dedup chain (zen-revised from 2026-05-18 design).

Sort priorities (DESC unless noted):
1. layer4_weighted_score        — primary aggregate (1-5)
2. catalyst_strength            — continuous [0,1], strongest driver
3. insider_score_usd            — conviction magnitude $
4. deep_drawdown_reversal       — True > False (binary setup)
5. magic_formula_rank ASC       — cohort value+quality (1 = best)
6. n_gates_passed               — verification breadth
7. llm_confidence            — LLM fallback tiebreaker

Dedup happens AFTER sort so the strongest-context row per ticker wins
(critical when a ticker hits 2 themes with different catalysts). Cross-
theme appearances surface as ``also_in_themes`` badge on the kept row.
"""

from __future__ import annotations

import unittest

import pandas as pd
from alphalens_pipeline.experts.registry import expert_ids
from alphalens_pipeline.thematic.argumentation import orchestrator


def _row(**overrides) -> dict:
    """Build a Phase D-scored row with sensible defaults for sort testing."""
    base = {
        "theme": "quantum_computing",
        "ticker": "QBTS",
        "company_name": "D-Wave Quantum Inc",
        "verified": True,
        "layer4_weighted_score": 4,
        "catalyst_strength": 0.50,
        "insider_score_usd": 0.0,
        "deep_drawdown_reversal": False,
        "magic_formula_rank": 5,
        "magic_formula_cohort_n": 10,
        "n_gates_passed": 2,
        "llm_confidence": 0.85,
    }
    base.update(overrides)
    # Mirror scorer.py: selection_score defaults to layer4_weighted_score so
    # existing tests remain valid unless a test explicitly overrides it.
    if "selection_score" not in overrides:
        base["selection_score"] = float(base["layer4_weighted_score"])
    return base


class TestSortAndDedupForBrief(unittest.TestCase):
    """Order assertions for the brief-render sort chain."""

    def test_primary_sort_is_selection_score_desc(self):
        df = pd.DataFrame(
            [
                _row(ticker="LOW", layer4_weighted_score=2),
                _row(ticker="HIGH", layer4_weighted_score=5),
                _row(ticker="MID", layer4_weighted_score=3),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["HIGH", "MID", "LOW"])

    def test_high_atr_name_demoted_below_equal_layer4_calm_name(self):
        # When two rows share the same layer4_weighted_score, the one with a
        # LOWER selection_score (penalized by ATR) sorts below the calm name.
        df = pd.DataFrame(
            [
                _row(ticker="VOLATILE", layer4_weighted_score=4, selection_score=3.0),
                _row(ticker="CALM", layer4_weighted_score=4, selection_score=4.0),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(
            list(out["ticker"]),
            ["CALM", "VOLATILE"],
            "high-ATR (lower selection_score) name must rank below calm name with equal layer4",
        )

    def test_tiebreak_catalyst_strength_before_reversal_zen_correction(self):
        # Zen pre-design HIGH finding: catalyst_strength is CONTINUOUS [0,1]
        # and represents the strongest driver of cohort lift. It must beat
        # the BINARY deep_drawdown_reversal flag at tie time. A strong
        # product_launch (0.85) is structurally safer than a weak 'other'
        # event (0.30) with an oversold setup.
        df = pd.DataFrame(
            [
                # Same score, weak catalyst but reversal=True
                _row(
                    ticker="WEAK_CAT_REVERSAL",
                    catalyst_strength=0.30,
                    deep_drawdown_reversal=True,
                ),
                # Same score, strong catalyst but reversal=False
                _row(
                    ticker="STRONG_CAT_NO_REVERSAL",
                    catalyst_strength=0.85,
                    deep_drawdown_reversal=False,
                ),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(
            list(out["ticker"]),
            ["STRONG_CAT_NO_REVERSAL", "WEAK_CAT_REVERSAL"],
            "strong continuous catalyst must rank above binary reversal flag",
        )

    def test_tiebreak_insider_usd_before_reversal(self):
        # Real money behind the name discriminates more than a binary setup
        # flag. $250k insider buy at same catalyst level wins.
        df = pd.DataFrame(
            [
                _row(ticker="NO_INSIDER_REV", insider_score_usd=0.0, deep_drawdown_reversal=True),
                _row(
                    ticker="BIG_INSIDER_NO_REV",
                    insider_score_usd=250_000.0,
                    deep_drawdown_reversal=False,
                ),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["BIG_INSIDER_NO_REV", "NO_INSIDER_REV"])

    def test_reversal_wins_when_catalyst_and_insider_tied(self):
        # When earlier tiebreakers are equal, reversal (True > False) wins.
        df = pd.DataFrame(
            [
                _row(ticker="NO_REV", deep_drawdown_reversal=False),
                _row(ticker="REV", deep_drawdown_reversal=True),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["REV", "NO_REV"])

    def test_magic_formula_rank_ascending_lower_is_better(self):
        # rank=1 is the BEST cohort position; should come first.
        df = pd.DataFrame(
            [
                _row(ticker="RANK_5", magic_formula_rank=5),
                _row(ticker="RANK_1", magic_formula_rank=1),
                _row(ticker="RANK_3", magic_formula_rank=3),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["RANK_1", "RANK_3", "RANK_5"])

    def test_n_gates_passed_tiebreaker_after_magic_formula(self):
        df = pd.DataFrame(
            [
                _row(ticker="ONE_GATE", n_gates_passed=1),
                _row(ticker="THREE_GATES", n_gates_passed=3),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["THREE_GATES", "ONE_GATE"])

    def test_llm_confidence_is_final_tiebreaker(self):
        df = pd.DataFrame(
            [
                _row(ticker="LOW_CONF", llm_confidence=0.50),
                _row(ticker="HIGH_CONF", llm_confidence=0.95),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["HIGH_CONF", "LOW_CONF"])

    def test_sort_sentinel_does_not_leak_into_output_rows(self):
        # Empirical 2026-05-18 incident: when ``magic_formula_rank`` was
        # NaN in the input, the sort fillna'd it with ``float("inf")`` and
        # left the sentinel in the returned frame. Downstream renderer
        # called ``int(rank)`` → OverflowError. The sort sentinel must be
        # ephemeral — original NaN survives to the output so the renderer
        # sees the same data it would have without our sort layer.
        import math

        df = pd.DataFrame(
            [
                _row(ticker="HAS_RANK", magic_formula_rank=3),
                _row(ticker="NAN_RANK", magic_formula_rank=float("nan")),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        # Find the NAN_RANK row in the sorted output.
        nan_row = out[out["ticker"] == "NAN_RANK"].iloc[0]
        self.assertTrue(
            math.isnan(nan_row["magic_formula_rank"]),
            f"sort leaked sentinel; got magic_formula_rank={nan_row['magic_formula_rank']}",
        )

    def test_handles_missing_sort_columns_defensively(self):
        # Phase D scoring is still evolving; new columns may not be present
        # on older parquets. Missing column = neutral default (won't crash
        # the sort, won't perversely promote/demote a candidate).
        df = pd.DataFrame(
            [
                {
                    "ticker": "OLD_PARQUET",
                    "theme": "x",
                    "verified": True,
                    "layer4_weighted_score": 3,
                },
                {
                    "ticker": "NEW_PARQUET",
                    "theme": "x",
                    "verified": True,
                    "layer4_weighted_score": 4,
                    "catalyst_strength": 0.78,
                },
            ]
        )
        # Should not raise; score 4 beats score 3.
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["NEW_PARQUET", "OLD_PARQUET"])


class TestDedupKeepsStrongestThemeRow(unittest.TestCase):
    """When a ticker appears in 2+ themes, dedup must keep the row with
    the strongest sort context (zen pre-design HIGH finding)."""

    def test_dedup_keeps_row_with_stronger_catalyst(self):
        # Same ticker RGTI, two themes, different catalyst_strength.
        # Without sort-before-dedup, the WEAKER row might survive on
        # index-order fallback. Sort + dedup keep=first must keep the
        # stronger.
        df = pd.DataFrame(
            [
                _row(
                    ticker="RGTI",
                    theme="quantum_error_correction",
                    catalyst_strength=0.40,  # weaker
                ),
                _row(
                    ticker="RGTI",
                    theme="quantum_computing",
                    catalyst_strength=0.85,  # stronger
                ),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["theme"], "quantum_computing")
        self.assertAlmostEqual(out.iloc[0]["catalyst_strength"], 0.85)

    def test_dedup_attaches_also_in_themes_list_of_dropped_themes(self):
        # The kept row should carry an ``also_in_themes`` list with the
        # OTHER themes the ticker hit — operator sees the multi-thematic
        # signal even though we collapsed to one row.
        df = pd.DataFrame(
            [
                _row(ticker="RGTI", theme="quantum_computing", catalyst_strength=0.85),
                _row(ticker="RGTI", theme="quantum_error_correction", catalyst_strength=0.40),
                _row(ticker="RGTI", theme="AI_models", catalyst_strength=0.50),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(len(out), 1)
        self.assertIn("also_in_themes", out.columns)
        also = out.iloc[0]["also_in_themes"]
        # Kept row's theme is quantum_computing; others surface in badge.
        self.assertEqual(
            sorted(also),
            sorted(["AI_models", "quantum_error_correction"]),
        )

    def test_dedup_collapses_repeated_themes_in_also_in_themes(self):
        # Zen pre-merge LOW finding: if upstream parquet has multiple rows
        # for the same (ticker, theme) pair, ``also_in_themes`` must not
        # render the theme twice in the badge. Dedup via ``dict.fromkeys``
        # in the orchestrator prevents UI spam like "also in: AI_models,
        # AI_models".
        df = pd.DataFrame(
            [
                _row(ticker="RGTI", theme="quantum_computing", catalyst_strength=0.85),
                _row(ticker="RGTI", theme="quantum_error_correction", catalyst_strength=0.40),
                # Same theme as the row above — upstream Phase D bug or
                # noisy event-rollup duplicating a (ticker, theme) pair.
                _row(ticker="RGTI", theme="quantum_error_correction", catalyst_strength=0.30),
                _row(ticker="RGTI", theme="AI_models", catalyst_strength=0.50),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        also = out.iloc[0]["also_in_themes"]
        self.assertEqual(
            sorted(also),
            sorted(["AI_models", "quantum_error_correction"]),
            f"duplicate themes leaked into badge: {also}",
        )

    def test_single_theme_ticker_has_empty_also_in_themes(self):
        df = pd.DataFrame([_row(ticker="ONLY_ONCE", theme="quantum_computing")])
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(list(out.iloc[0]["also_in_themes"]), [])


class TestRankInDayColumn(unittest.TestCase):
    """After sort + dedup, each surviving row gets a 1-based ``rank_in_day``
    so the renderer can show ``rank 1/6`` in the header."""

    def test_rank_in_day_is_one_based(self):
        df = pd.DataFrame(
            [
                _row(ticker="A", layer4_weighted_score=5),
                _row(ticker="B", layer4_weighted_score=4),
                _row(ticker="C", layer4_weighted_score=3),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertIn("rank_in_day", out.columns)
        self.assertEqual(list(out["rank_in_day"]), [1, 2, 3])

    def test_rank_in_day_reflects_post_dedup_size(self):
        # 3 input rows, 1 unique ticker → cohort_size in day = 1.
        df = pd.DataFrame(
            [
                _row(ticker="DUP", theme="t1"),
                _row(ticker="DUP", theme="t2"),
                _row(ticker="DUP", theme="t3"),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["rank_in_day"], 1)


# The frozen allowlist of the ONLY columns permitted in the brief sort chain.
# Every entry is a NON-expert signal (a screener/catalyst/verification primitive).
# An expert assessment — any buffett_*, oneil_*, or the panel-level expert_*
# disagreement scalars — must NEVER appear here. Experts stay display-only until
# each one's Expert×EDGE correlation is validated (N>=30 matured outcomes,
# ~2026-09+). A new NON-expert sort key requires adding it to BOTH this allowlist
# and the ordered equality pin below — one without the other fails a test.
_NON_EXPERT_SORT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "selection_score",
        "layer4_weighted_score",
        "catalyst_strength",
        "insider_score_usd",
        "deep_drawdown_reversal",
        "magic_formula_rank",
        "n_gates_passed",
        "llm_confidence",
        "_template_facts_richness",
    }
)

# Panel-level disagreement scalars (PR-8) — the single most tempting things to
# rank on once the panel chip ships. Pinned out of the sort BY NAME so a future
# PR wiring the chip cannot quietly add one as a tie-break tuple.
_DISAGREEMENT_SCALARS: frozenset[str] = frozenset(
    {"expert_spread", "expert_consensus_tone", "expert_split"}
)


def _sort_chain_keys() -> tuple[str, ...]:
    return tuple(col for col, _asc, _default in orchestrator._BRIEF_SORT_KEYS)


def _forbidden_expert_prefixes() -> tuple[str, ...]:
    """Sort-key prefixes that mark an expert column. Derived from the live registry
    (so a newly-registered expert is auto-covered) UNION the two known expert ids
    not yet registered (``oneil`` lands in PR-7) UNION the generic panel-level
    ``expert_`` namespace (the disagreement scalars)."""
    # `buffett` is already registered (so it also comes from expert_ids()); it is
    # listed explicitly to keep the set self-documenting. `oneil` is NOT yet
    # registered (lands in PR-7) — naming it here closes the gap window so an
    # `oneil_*` sort key is caught the moment it could be added, not only after
    # the registry knows about it. The set union dedups the overlap.
    ids = set(expert_ids()) | {"buffett", "oneil"}
    return tuple(sorted({f"{eid}_" for eid in ids} | {"expert_"}))


def _allowlist_offenders(keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(k for k in keys if k not in _NON_EXPERT_SORT_ALLOWLIST)


def _prefix_offenders(keys: tuple[str, ...], prefixes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(k for k in keys if any(k.startswith(p) for p in prefixes))


class TestSortKeyExpertLock(unittest.TestCase):
    """ENFORCE the locked design decision: NO expert term (Buffett, O'Neil, or any
    future lens) enters the brief sort until that expert's Expert×EDGE study
    validates it (N>=30 matured outcomes, ~2026-09+). The qualitative LLM verdict
    NEVER enters the sort; the cheap per-expert composite score is display-only with
    hand-chosen, unvalidated weights; the panel disagreement scalars are display-only.

    Generalised from the single-Buffett guard (epic #541 PR-6): an allowlist makes
    the lock N-safe — any expert prefix is excluded by construction rather than
    enumerated one prefix at a time. See docs/research/expert_panel_design_2026_06_13.md
    and docs/research/buffett_card_surfacing_design_2026_06_12.md §5.
    """

    def test_every_sort_key_is_in_non_expert_allowlist(self):
        # The primary N-safe guard: ANY key not on the frozen non-expert allowlist
        # trips this — including every expert prefix, with no per-prefix enumeration.
        offenders = _allowlist_offenders(_sort_chain_keys())
        self.assertEqual(
            offenders,
            (),
            f"Sort keys not on the non-expert allowlist entered _BRIEF_SORT_KEYS: "
            f"{offenders}. If this is a NEW non-expert signal, add it to "
            f"_NON_EXPERT_SORT_ALLOWLIST and the equality pin. If it is an expert "
            f"column, it is gated on the deferred Expert×EDGE validation — do not wire it.",
        )

    def test_no_expert_prefixed_key_in_sort_chain(self):
        # Belt-and-suspenders to the allowlist: explicitly satisfies the acceptance
        # that any expert_*/buffett_*/oneil_* key fires this guard.
        prefixes = _forbidden_expert_prefixes()
        offenders = _prefix_offenders(_sort_chain_keys(), prefixes)
        self.assertEqual(
            offenders,
            (),
            f"Expert-prefixed keys entered _BRIEF_SORT_KEYS: {offenders} "
            f"(forbidden prefixes: {prefixes}).",
        )

    def test_disagreement_scalars_never_in_sort(self):
        present = _DISAGREEMENT_SCALARS & set(_sort_chain_keys())
        self.assertEqual(
            present,
            set(),
            f"Panel disagreement scalar(s) entered the sort: {present}. These are "
            f"display-only until validated; ranking on them manufactures authority.",
        )

    def test_allowlist_matches_documented_chain(self):
        # The allowlist and the ordered chain must describe the SAME set — no stale
        # allowlist entry that is not an actual sort key, and no sort key missing
        # from the allowlist. Keeps the two definitions in lockstep.
        self.assertEqual(set(_sort_chain_keys()), set(_NON_EXPERT_SORT_ALLOWLIST))

    def test_sort_chain_is_exactly_the_documented_set(self):
        # The real N-safe backstop: pins the full ORDERED chain so ANY addition
        # (expert or otherwise) trips this and forces a deliberate review, not a
        # silent ordering change.
        self.assertEqual(
            _sort_chain_keys(),
            (
                "selection_score",
                "layer4_weighted_score",
                "catalyst_strength",
                "insider_score_usd",
                "deep_drawdown_reversal",
                "magic_formula_rank",
                "n_gates_passed",
                "llm_confidence",
                "_template_facts_richness",
            ),
        )

    def test_prefix_guard_covers_every_registered_expert(self):
        # Meta-test (guard is not vacuous): every registered expert's prefix is
        # actually caught, so a newly-registered expert cannot slip a sort key in
        # under a prefix the guard forgot to derive.
        prefixes = _forbidden_expert_prefixes()
        for eid in expert_ids():
            injected = (f"{eid}_quality_score",)
            self.assertEqual(
                _prefix_offenders(injected, prefixes),
                injected,
                f"Prefix guard does not cover registered expert {eid!r}.",
            )

    def test_allowlist_guard_catches_injected_expert_key(self):
        # Meta-test (guard is not vacuous): the allowlist check actually flags an
        # expert key appended to the real chain — proving a green run means the
        # lock holds, not that the assertion is unreachable.
        injected = (*_sort_chain_keys(), "oneil_rs_rating")
        self.assertEqual(_allowlist_offenders(injected), ("oneil_rs_rating",))


if __name__ == "__main__":
    unittest.main()
