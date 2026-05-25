"""Brief-render sort + dedup chain (zen-revised from 2026-05-18 design).

Sort priorities (DESC unless noted):
1. layer4_weighted_score        — primary aggregate (1-5)
2. catalyst_strength            — continuous [0,1], strongest driver
3. insider_score_usd            — conviction magnitude $
4. deep_drawdown_reversal       — True > False (binary setup)
5. magic_formula_rank ASC       — cohort value+quality (1 = best)
6. n_gates_passed               — verification breadth
7. gemini_confidence            — LLM fallback tiebreaker

Dedup happens AFTER sort so the strongest-context row per ticker wins
(critical when a ticker hits 2 themes with different catalysts). Cross-
theme appearances surface as ``also_in_themes`` badge on the kept row.
"""

from __future__ import annotations

import unittest

import pandas as pd
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
        "gemini_confidence": 0.85,
    }
    base.update(overrides)
    return base


class TestSortAndDedupForBrief(unittest.TestCase):
    """Order assertions for the brief-render sort chain."""

    def test_primary_sort_is_layer4_weighted_score_desc(self):
        df = pd.DataFrame(
            [
                _row(ticker="LOW", layer4_weighted_score=2),
                _row(ticker="HIGH", layer4_weighted_score=5),
                _row(ticker="MID", layer4_weighted_score=3),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["HIGH", "MID", "LOW"])

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

    def test_gemini_confidence_is_final_tiebreaker(self):
        df = pd.DataFrame(
            [
                _row(ticker="LOW_CONF", gemini_confidence=0.50),
                _row(ticker="HIGH_CONF", gemini_confidence=0.95),
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


if __name__ == "__main__":
    unittest.main()
