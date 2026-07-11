"""T6 measurement harness (research, non-gating) — unit tests.

Implements the LOCKED design memo
``docs/research/reading_quality_eval_design_2026_07_11.md`` §6.7 ("Measurement
harness (research, non-gating)"), §5 (metrics + Wilson CI), §6.6, §10.

The GATING pilot (``test_golden_brief_faithfulness.py``) scores the 4 golden
CASSETTES. This measurement layer runs the SAME ``score_brief`` over the real
brief PARQUET corpus (``~/.alphalens/thematic_briefs/*.parquet``), where
``brief_template_facts_json`` is null on ~14/15 rows, so the fact index is built
from the ROW COLUMNS via :func:`fact_index_from_brief_row` (memo §12 "read the
live ``thematic_briefs`` parquet directly").

Doctrine (memo §2, §9): telemetry only. The measurement module must NOT import
or join any outcome/return ledger and must NOT touch selection/ordering. Every
report is stamped with ``FAITHFULNESS_SCORER_VERSION`` so rates are never pooled
across a scorer change.
"""

from __future__ import annotations

import math
import unittest
from pathlib import Path

import pandas as pd
from alphalens_research.eval.faithfulness import (
    FAITHFULNESS_SCORER_VERSION,
    FaithfulnessResult,
    _fact_unit_kind,
)
from alphalens_research.eval.measurement import (
    brief_fields_from_row,
    fact_index_from_brief_row,
    measure_corpus,
    wilson_interval,
)

_GOLDEN_PARQUET = (
    Path(__file__).resolve().parent / "fixtures" / "brief_day" / "golden" / "2026-05-24.parquet"
)


class TestFactIndexFromBriefRow(unittest.TestCase):
    """(a) column -> fact-index key mapping, roic rename, NaN drop."""

    def test_roic_rename_lands_as_percent_fact(self) -> None:
        # ARRANGE — a synthetic row carrying the two roic columns.
        row = {
            "buffett_roic_latest": 12.3,
            "buffett_roic_3y_avg": 11.0,
            "market_cap": 8.2e9,
            "valuation_ps": 7.5,
        }
        # ACT
        index = fact_index_from_brief_row(row)
        # ASSERT — renamed keys land, original names are gone.
        self.assertIn("buffett_roic_pct", index)
        self.assertEqual(index["buffett_roic_pct"], 12.3)
        self.assertIn("buffett_roic_3y_avg_pct", index)
        self.assertEqual(index["buffett_roic_3y_avg_pct"], 11.0)
        self.assertNotIn("buffett_roic_latest", index)
        self.assertNotIn("buffett_roic_3y_avg", index)
        # ASSERT — the renamed key is classified as a % fact by the scorer, so a
        # brief citing "ROIC 12.3%" grounds instead of firing FABRICATED.
        self.assertEqual(_fact_unit_kind("buffett_roic_pct"), "%")
        self.assertEqual(_fact_unit_kind("buffett_roic_3y_avg_pct"), "%")

    def test_nan_and_none_columns_are_dropped(self) -> None:
        row = {
            "market_cap": float("nan"),
            "valuation_ps": None,
            "valuation_pe": 24.0,
            "fcff_yield_pct": float("nan"),
            "technical_rsi": 53.0,
        }
        index = fact_index_from_brief_row(row)
        self.assertNotIn("market_cap", index)
        self.assertNotIn("valuation_ps", index)
        self.assertNotIn("fcff_yield_pct", index)
        self.assertEqual(index["valuation_pe"], 24.0)
        self.assertEqual(index["technical_rsi"], 53.0)

    def test_unmapped_columns_are_ignored(self) -> None:
        # A row with extra pipeline columns not in the mapping must not leak.
        row = {
            "valuation_ps": 7.5,
            "fcff_yield_sector_percentile": 39.0,  # NOT mapped
            "brief_trade_setup": "{}",  # NOT mapped
            "options_iv_atm_pct": 44.0,  # NOT mapped
        }
        index = fact_index_from_brief_row(row)
        self.assertEqual(index, {"valuation_ps": 7.5})

    def test_text_grounding_columns_are_carried(self) -> None:
        row = {
            "source_event_title": "Walmart and Target are about to show shifts",
            "source_event_published_at": "2026-05-23T12:00:00Z",
            "next_earnings_date": "2026-06-24",
        }
        index = fact_index_from_brief_row(row)
        self.assertEqual(index["source_event_title"], "Walmart and Target are about to show shifts")
        self.assertEqual(index["source_event_published_at"], "2026-05-23T12:00:00Z")
        self.assertEqual(index["next_earnings_date"], "2026-06-24")

    def test_fcf_margin_is_dual_emitted_fraction_and_percent(self) -> None:
        # valuation_fcf_margin is stored as a FRACTION (0.31). It is dual-emitted:
        # the raw fraction under the ratio key AND a ×100 percent copy, so BOTH
        # citation styles ("0.31" and "31%") can ground.
        row = {"valuation_fcf_margin": 0.31}
        index = fact_index_from_brief_row(row)
        self.assertIn("valuation_fcf_margin", index)
        self.assertAlmostEqual(index["valuation_fcf_margin"], 0.31, places=9)
        self.assertIn("valuation_fcf_margin_pct", index)
        self.assertAlmostEqual(index["valuation_fcf_margin_pct"], 31.0, places=9)
        # the ratio key stays a ratio fact; the pct key is a % fact.
        self.assertEqual(_fact_unit_kind("valuation_fcf_margin"), "ratio")
        self.assertEqual(_fact_unit_kind("valuation_fcf_margin_pct"), "%")

    def test_fcf_margin_percent_atom_grounds_via_dual_key(self) -> None:
        from alphalens_research.eval.measurement import score_row

        # Regression: fact 0.31 fraction + a brief saying "31% FCF margin" must
        # NOT count as fabricated (was a false positive — the % atom could not
        # ground against the bare fraction before the dual %-key was added).
        row = {
            "ticker": "FCFM",
            "valuation_fcf_margin": 0.31,
            "brief_supply_chain_md": "the business runs a healthy 31% FCF margin.",
        }
        result = score_row(row)
        self.assertEqual(result.fabricated_numeric_date_atoms, 0)
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertTrue(
            any(
                a.verdict == "GROUNDED" and a.matched_fact == "valuation_fcf_margin_pct"
                for a in numeric
            ),
            f"expected GROUNDED against valuation_fcf_margin_pct, got "
            f"{[(a.span, a.verdict, a.matched_fact) for a in numeric]}",
        )

    def test_fcf_margin_fraction_atom_still_grounds(self) -> None:
        from alphalens_research.eval.measurement import score_row

        # Regression guard for the OTHER style (the golden-fixture form): a brief
        # citing the bare fraction "FCF margin 0.34" must STILL ground against the
        # retained ratio key — dual-emit must not break this.
        row = {
            "ticker": "FCFR",
            "valuation_fcf_margin": 0.34,
            "brief_supply_chain_md": "unremarkable metrics: FCF margin 0.34, nothing notable.",
        }
        result = score_row(row)
        self.assertEqual(result.fabricated_numeric_date_atoms, 0)
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertTrue(
            any(
                a.verdict == "GROUNDED" and a.matched_fact == "valuation_fcf_margin"
                for a in numeric
            ),
            f"expected GROUNDED against valuation_fcf_margin, got "
            f"{[(a.span, a.verdict, a.matched_fact) for a in numeric]}",
        )

    def test_nat_and_pandas_na_cells_are_dropped(self) -> None:
        # A future typed date column yields pd.NaT for a missing cell; it must be
        # treated as missing (not leak the literal string "NaT" into the index).
        row = {
            "next_earnings_date": pd.NaT,
            "source_event_published_at": pd.NA,
            "valuation_ps": 7.5,
        }
        index = fact_index_from_brief_row(row)
        self.assertNotIn("next_earnings_date", index)
        self.assertNotIn("source_event_published_at", index)
        self.assertEqual(index["valuation_ps"], 7.5)


class TestDirectionalSignStrip(unittest.TestCase):
    """(b) directional sign-strip via a technical_pct_off_52w_high row + a brief
    citing the absolute value → GROUNDED."""

    def test_brief_citing_abs_of_negative_directional_fact_grounds(self) -> None:
        from alphalens_research.eval.measurement import score_row

        row = {
            "ticker": "MANH",
            "technical_pct_off_52w_high": -39.2,
            "brief_supply_chain_md": "technicals show lagging momentum: 39.2% below the 52-week high.",
        }
        result = score_row(row)
        self.assertIsInstance(result, FaithfulnessResult)
        # No fabrication — the brief's abs value grounds against the -39.2% fact.
        self.assertEqual(result.fabricated_numeric_date_atoms, 0)
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertTrue(numeric, "expected a numeric atom extracted")
        self.assertTrue(
            any(
                a.verdict == "GROUNDED" and a.matched_fact == "technical_pct_off_52w_high"
                for a in numeric
            ),
            f"expected GROUNDED against technical_pct_off_52w_high, got {[(a.span, a.verdict, a.matched_fact) for a in numeric]}",
        )


class TestBriefFieldsFromRow(unittest.TestCase):
    """The 4 brief_* text columns map to the SCHEMA field names."""

    def test_maps_four_text_columns_to_schema_names(self) -> None:
        row = {
            "brief_tldr": "a tldr",
            "brief_supply_chain_md": "the reasoning",
            "brief_bear_summary_md": "the bear case",
            "brief_catalyst_failure_exit": "the exit",
            "brief_model_used": "deepseek/deepseek-v4-pro",  # not a field
        }
        fields = brief_fields_from_row(row)
        self.assertEqual(
            fields,
            {
                "tldr": "a tldr",
                "supply_chain_reasoning": "the reasoning",
                "bear_summary": "the bear case",
                "catalyst_failure_exit": "the exit",
            },
        )

    def test_missing_and_nan_text_columns_become_empty(self) -> None:
        row = {"brief_tldr": "only a tldr", "brief_bear_summary_md": float("nan")}
        fields = brief_fields_from_row(row)
        self.assertEqual(fields["tldr"], "only a tldr")
        self.assertEqual(fields["supply_chain_reasoning"], "")
        self.assertEqual(fields["bear_summary"], "")
        self.assertEqual(fields["catalyst_failure_exit"], "")


class TestWilsonInterval(unittest.TestCase):
    """Wilson 95% CI — pure-python, brackets the point estimate."""

    def test_wilson_brackets_point_estimate(self) -> None:
        lo, hi = wilson_interval(3, 10)
        self.assertLessEqual(lo, 0.3)
        self.assertGreaterEqual(hi, 0.3)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 1.0)

    def test_wilson_zero_successes(self) -> None:
        lo, hi = wilson_interval(0, 20)
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0.0)
        self.assertLess(hi, 1.0)

    def test_wilson_all_successes(self) -> None:
        lo, hi = wilson_interval(20, 20)
        self.assertLess(lo, 1.0)
        self.assertGreater(lo, 0.0)
        self.assertEqual(hi, 1.0)

    def test_wilson_zero_denominator_is_nan_pair(self) -> None:
        lo, hi = wilson_interval(0, 0)
        self.assertTrue(math.isnan(lo))
        self.assertTrue(math.isnan(hi))


class TestMeasureCorpus(unittest.TestCase):
    """(c) measure_corpus over the existing golden fixture (4 real rows,
    deterministic, no network) returns a structured report with the expected
    keys, per-field breakdown, and Wilson CIs that bracket the point estimate."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.df = pd.read_parquet(_GOLDEN_PARQUET)
        cls.rows = cls.df.to_dict("records")
        cls.report = measure_corpus(cls.rows)

    def test_report_top_level_keys(self) -> None:
        for key in (
            "scorer_version",
            "n_briefs",
            "corpus_rates",
            "diagnostic_means",
            "per_field_fabrication",
            "per_stratum",
        ):
            self.assertIn(key, self.report)

    def test_scorer_version_is_stamped(self) -> None:
        self.assertEqual(self.report["scorer_version"], FAITHFULNESS_SCORER_VERSION)

    def test_corpus_size(self) -> None:
        self.assertEqual(self.report["n_briefs"], 4)

    def test_corpus_rates_have_wilson_ci_bracketing_point(self) -> None:
        rates = self.report["corpus_rates"]
        for metric in ("any_fabricated", "any_violation"):
            self.assertIn(metric, rates)
            block = rates[metric]
            self.assertIn("rate", block)
            self.assertIn("ci_low", block)
            self.assertIn("ci_high", block)
            self.assertIn("k", block)
            self.assertIn("n", block)
            self.assertLessEqual(block["ci_low"], block["rate"])
            self.assertGreaterEqual(block["ci_high"], block["rate"])

    def test_diagnostic_means_present(self) -> None:
        means = self.report["diagnostic_means"]
        self.assertIn("groundedness_rate", means)
        self.assertIn("checkable_coverage", means)

    def test_per_field_fabrication_has_all_four_fields(self) -> None:
        pff = self.report["per_field_fabrication"]
        for field in ("tldr", "supply_chain_reasoning", "bear_summary", "catalyst_failure_exit"):
            self.assertIn(field, pff)

    def test_per_stratum_by_model_and_yearmonth(self) -> None:
        strata = self.report["per_stratum"]
        self.assertIn("brief_model_used", strata)
        # Decoupled from the fixture's specific model strings: the harness must
        # produce exactly one bucket per distinct brief_model_used value in the
        # fixture (a future golden re-record onto a single model must not break
        # this measurement test on unrelated grounds).
        model_strata = strata["brief_model_used"]
        expected_models = {
            ("unknown" if pd.isna(v) else str(v)) for v in self.df["brief_model_used"]
        }
        self.assertEqual(set(model_strata.keys()), expected_models)
        # year-month stratum is always available (derived from the row date).
        self.assertIn("year_month", strata)

    def test_per_brief_counts_include_deferred_entity_atoms(self) -> None:
        # memo §6.6 secondary metric — present and structurally 0 in v1.
        counts = self.report["per_brief_counts"]
        self.assertIn("deferred_entity_atoms", counts)
        self.assertEqual(counts["deferred_entity_atoms"], 0)

    def test_golden_corpus_is_clean(self) -> None:
        # The 4 golden briefs are the known-clean set (they pass the gate), so
        # the measurement over them shows zero corpus fabrication/violation.
        self.assertEqual(self.report["corpus_rates"]["any_fabricated"]["k"], 0)
        self.assertEqual(self.report["corpus_rates"]["any_violation"]["k"], 0)


class TestSeededFabricationSurfaces(unittest.TestCase):
    """(d) a seeded row with a fabricated number in supply_chain_reasoning shows
    up in the fabricated count AND the per-field breakdown."""

    def test_seeded_supply_chain_fabrication(self) -> None:
        # ARRANGE — a real-ish fact index plus a supply_chain_reasoning that
        # cites a made-up P/S of 999.9x with no matching fact.
        seeded = {
            "ticker": "SEED",
            "valuation_ps": 7.5,
            "technical_pct_off_52w_high": -39.2,
            "brief_tldr": "clean tldr with the 39.2% drawdown noted.",
            "brief_supply_chain_md": "trades at a wild 999.9x sales, unheard of.",
            "brief_bear_summary_md": "two clear risks apply.",
            "brief_catalyst_failure_exit": "exit on a break.",
            "brief_model_used": "deepseek/deepseek-v4-pro",
        }
        clean = {
            "ticker": "CLEAN",
            "valuation_ps": 7.5,
            "technical_pct_off_52w_high": -39.2,
            "brief_tldr": "clean tldr.",
            "brief_supply_chain_md": "trades at 7.5x sales, 39.2% below the 52-week high.",
            "brief_bear_summary_md": "two clear risks apply.",
            "brief_catalyst_failure_exit": "exit on a break.",
            "brief_model_used": "deepseek/deepseek-v4-pro",
        }
        report = measure_corpus([seeded, clean])
        # ASSERT — exactly one brief carries a fabricated atom.
        self.assertEqual(report["corpus_rates"]["any_fabricated"]["k"], 1)
        self.assertEqual(report["corpus_rates"]["any_fabricated"]["n"], 2)
        # ASSERT — the fabrication is attributed to supply_chain_reasoning.
        pff = report["per_field_fabrication"]
        self.assertGreaterEqual(pff["supply_chain_reasoning"]["fabricated_atoms"], 1)
        self.assertEqual(pff["tldr"]["fabricated_atoms"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
