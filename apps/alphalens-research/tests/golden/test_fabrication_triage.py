"""Fabrication-TRIAGE calibration harness — unit tests (RED first).

Implements the honest-limits framing of the LOCKED design memo
``docs/research/reading_quality_eval_design_2026_07_11.md`` §10: the raw T6
``fabricated_numeric_date_atoms`` count is fidelity-to-the-facts-block, NOT
fidelity-to-truth. A number the model legitimately pulls from the catalyst
article (a contract size, a revenue figure) counts as FABRICATED only because it
is not in the injected ``<facts>``. This harness calibrates that rate by triaging
each FABRICATED numeric/date atom into a likely-source BUCKET (mostly
deterministic) and stages a small human worksheet for the ambiguous ones.

Buckets (deterministic, precedence top-to-bottom):

* ``in_catalyst_title``    — the atom's digits are IN the ``source_event_title``
                             (adapter coverage gap; groundable, NOT a lie).
* ``near_miss_same_kind``  — a same-unit-kind fact sits within a WIDER band than
                             the DISTORTED band (derivation/rounding).
* ``dollar_out_of_facts``  — a ``$``-magnitude atom with no market_cap / insider
                             match (article-derived contract/revenue/TAM).
* ``ungrounded_other``     — none of the above (bare ratio / % with no nearby
                             fact and not in the title) — STRONGEST hallucination.

Doctrine (memo §2/§9): telemetry only — NO outcome/return ledger, NO selection,
NO network. Reuses the frozen scorer (``score_row`` / ``score_brief`` /
``fact_index_from_brief_row``); does NOT reimplement scoring.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from alphalens_research.eval.fabrication_triage import (
    build_audit_worksheet,
    triage_atom,
    triage_corpus,
)
from alphalens_research.eval.faithfulness import FAITHFULNESS_SCORER_VERSION
from alphalens_research.eval.measurement import fact_index_from_brief_row, score_row

_GOLDEN_PARQUET = (
    Path(__file__).resolve().parent / "fixtures" / "brief_day" / "golden" / "2026-05-24.parquet"
)


def _fabricated_atoms(row: dict):
    """The FABRICATED numeric/date atoms of a row (reuse the frozen scorer)."""
    result = score_row(row)
    return [a for a in result.atoms if a.kind in ("numeric", "date") and a.verdict == "FABRICATED"]


class TestTriageAtomBuckets(unittest.TestCase):
    """triage_atom returns the correct bucket for each synthetic case."""

    def test_dollar_figure_in_title_is_in_catalyst_title(self) -> None:
        # A $ figure that IS in the source_event_title → adapter coverage gap.
        row = {
            "ticker": "CTRT",
            "market_cap": 8.2e9,
            "source_event_title": "Acme wins a $450 million defense contract",
            "brief_supply_chain_md": "the new $450 million award lifts backlog materially.",
        }
        facts = fact_index_from_brief_row(row)
        fabricated = _fabricated_atoms(row)
        self.assertTrue(fabricated, "expected a fabricated $ atom in the fixture")
        buckets = {triage_atom(a, row, facts) for a in fabricated}
        self.assertIn("in_catalyst_title", buckets)

    def test_percent_in_title_is_in_catalyst_title(self) -> None:
        # A % figure present in the title normalizes (strip %) and matches.
        row = {
            "ticker": "PCT",
            "source_event_title": "Shares jump after guidance raised by 12%",
            "brief_tldr": "management raised guidance by 12% on the print.",
        }
        facts = fact_index_from_brief_row(row)
        fabricated = _fabricated_atoms(row)
        self.assertTrue(fabricated)
        buckets = {triage_atom(a, row, facts) for a in fabricated}
        self.assertIn("in_catalyst_title", buckets)

    def test_dollar_out_of_facts_when_not_in_title_and_no_dollar_fact(self) -> None:
        # A $ magnitude NOT in the title and with no market_cap/insider fact →
        # likely article-derived (contract/revenue/TAM).
        row = {
            "ticker": "DOF",
            "valuation_ps": 7.5,  # a ratio fact, NOT a $ fact
            "source_event_title": "Company reports strong quarter",
            "brief_supply_chain_md": "the addressable market is roughly $12 billion by 2027.",
        }
        facts = fact_index_from_brief_row(row)
        fabricated = _fabricated_atoms(row)
        self.assertTrue(fabricated)
        buckets = {triage_atom(a, row, facts) for a in fabricated}
        self.assertIn("dollar_out_of_facts", buckets)

    def test_wild_bare_ratio_is_ungrounded_other(self) -> None:
        # A bare ratio with no nearby fact and not in the title → strongest
        # hallucination candidate.
        row = {
            "ticker": "UGO",
            "valuation_ps": 7.5,
            "source_event_title": "Company reports strong quarter",
            "brief_supply_chain_md": "it trades at an absurd 4052.9x sales, unheard of.",
        }
        facts = fact_index_from_brief_row(row)
        fabricated = _fabricated_atoms(row)
        self.assertTrue(fabricated)
        buckets = {triage_atom(a, row, facts) for a in fabricated}
        self.assertIn("ungrounded_other", buckets)

    def test_near_miss_same_kind_percent(self) -> None:
        # A % fact exists but the brief value is beyond the DISTORTED band yet
        # inside the WIDER near-miss band → near_miss_same_kind.
        # fact -39.2%, brief "18%": 18 vs 39.2 is |21.2|/39.2 = 0.54 relative →
        # beyond DISTORTED (0.40) but inside near-miss (0.75).
        row = {
            "ticker": "NMS",
            "technical_pct_off_52w_high": -39.2,
            "source_event_title": "Company reports strong quarter",
            "brief_supply_chain_md": "the stock sits about 18% below its 52-week high.",
        }
        facts = fact_index_from_brief_row(row)
        fabricated = _fabricated_atoms(row)
        self.assertTrue(fabricated, "expected the 18% to be FABRICATED (beyond DISTORTED band)")
        buckets = {triage_atom(a, row, facts) for a in fabricated}
        self.assertIn("near_miss_same_kind", buckets)

    def test_fabricated_date_year_only_in_title_is_not_in_catalyst_title(self) -> None:
        # A fabricated DATE whose bare YEAR (but NOT the full date) appears in the
        # headline must NOT bucket as in_catalyst_title. The digit signature of a
        # date collapses to its year, so a year-only headline mention would
        # otherwise route a real date hallucination into the benign adapter-gap
        # bucket and bias the hallucination floor downward.
        row = {
            "ticker": "DTE",
            "source_event_title": "Big deal expected to close sometime in 2026",
            "brief_supply_chain_md": "management guided a signing on 2026-06-05 at the latest.",
        }
        facts = fact_index_from_brief_row(row)
        fabricated = _fabricated_atoms(row)
        date_atoms = [a for a in fabricated if a.kind == "date"]
        self.assertTrue(date_atoms, "expected the 2026-06-05 date to be FABRICATED")
        for atom in date_atoms:
            self.assertNotEqual(
                triage_atom(atom, row, facts),
                "in_catalyst_title",
                "a year-only headline match must not credit a fabricated date",
            )

    def test_unglyphed_magnitude_is_dollar_out_of_facts(self) -> None:
        # A magnitude word WITHOUT the $ glyph ("12 billion") is tokenised as unit
        # "count"; with no matching fact it is an out-of-facts contract/TAM figure,
        # so it belongs with dollar_out_of_facts, NOT the strongest-hallucination
        # bucket (matching the identical "$12 billion" routing).
        row = {
            "ticker": "CNT",
            "valuation_ps": 7.5,  # a ratio fact, not a $ fact
            "source_event_title": "Company reports strong quarter",
            "brief_supply_chain_md": "the market is roughly 12 billion units by 2027.",
        }
        facts = fact_index_from_brief_row(row)
        fabricated = _fabricated_atoms(row)
        count_atoms = [a for a in fabricated if a.extracted_value.endswith("count")]
        self.assertTrue(count_atoms, "expected an unglyphed 'count'-unit magnitude atom")
        for atom in count_atoms:
            self.assertEqual(triage_atom(atom, row, facts), "dollar_out_of_facts")

    def test_precedence_title_beats_dollar_out_of_facts(self) -> None:
        # A $ atom that is BOTH out-of-facts AND in the title must bucket as
        # in_catalyst_title (title precedence is top of the list).
        row = {
            "ticker": "PREC",
            "valuation_ps": 7.5,
            "source_event_title": "Acme lands a $450 million contract",
            "brief_supply_chain_md": "the $450 million award is the story here.",
        }
        facts = fact_index_from_brief_row(row)
        fabricated = _fabricated_atoms(row)
        self.assertTrue(fabricated)
        buckets = {triage_atom(a, row, facts) for a in fabricated}
        self.assertEqual(buckets, {"in_catalyst_title"})
        self.assertNotIn("dollar_out_of_facts", buckets)


class TestTriageCorpus(unittest.TestCase):
    """triage_corpus over a mixed synthetic corpus returns a coherent report."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = [
            {  # in_catalyst_title
                "ticker": "CTRT",
                "source_event_title": "Acme wins a $450 million defense contract",
                "brief_supply_chain_md": "the new $450 million award lifts backlog.",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {  # dollar_out_of_facts
                "ticker": "DOF",
                "valuation_ps": 7.5,
                "source_event_title": "Company reports strong quarter",
                "brief_supply_chain_md": "the addressable market is roughly $12 billion.",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {  # ungrounded_other
                "ticker": "UGO",
                "valuation_ps": 7.5,
                "source_event_title": "Company reports strong quarter",
                "brief_supply_chain_md": "it trades at an absurd 4052.9x sales.",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {  # clean — no fabricated atoms
                "ticker": "CLEAN",
                "valuation_ps": 7.5,
                "technical_pct_off_52w_high": -39.2,
                "source_event_title": "Company reports strong quarter",
                "brief_supply_chain_md": "trades at 7.5x sales, 39.2% below the 52-week high.",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
        ]
        cls.report = triage_corpus(cls.rows)

    def test_report_top_level_keys(self) -> None:
        for key in (
            "scorer_version",
            "n_briefs",
            "n_briefs_with_fabrication",
            "total_fabricated_atoms",
            "buckets",
            "estimated_true_hallucination_band",
        ):
            self.assertIn(key, self.report)

    def test_scorer_version_stamped(self) -> None:
        self.assertEqual(self.report["scorer_version"], FAITHFULNESS_SCORER_VERSION)

    def test_bucket_counts_sum_to_total(self) -> None:
        buckets = self.report["buckets"]
        for name in (
            "in_catalyst_title",
            "near_miss_same_kind",
            "dollar_out_of_facts",
            "ungrounded_other",
        ):
            self.assertIn(name, buckets)
            self.assertIn("count", buckets[name])
            self.assertIn("share", buckets[name])
        total = sum(buckets[name]["count"] for name in buckets)
        self.assertEqual(total, self.report["total_fabricated_atoms"])

    def test_three_briefs_carry_fabrication(self) -> None:
        # CTRT, DOF, UGO carry a fabricated atom; CLEAN does not.
        self.assertEqual(self.report["n_briefs"], 4)
        self.assertEqual(self.report["n_briefs_with_fabrication"], 3)

    def test_each_expected_bucket_populated(self) -> None:
        buckets = self.report["buckets"]
        self.assertGreaterEqual(buckets["in_catalyst_title"]["count"], 1)
        self.assertGreaterEqual(buckets["dollar_out_of_facts"]["count"], 1)
        self.assertGreaterEqual(buckets["ungrounded_other"]["count"], 1)

    def test_estimated_band_floor_leq_ceiling(self) -> None:
        band = self.report["estimated_true_hallucination_band"]
        self.assertIn("floor_atoms", band)
        self.assertIn("ceiling_atoms", band)
        self.assertLessEqual(band["floor_atoms"], band["ceiling_atoms"])
        # floor = ungrounded_other only; ceiling adds dollar_out_of_facts + near_miss.
        buckets = self.report["buckets"]
        self.assertEqual(band["floor_atoms"], buckets["ungrounded_other"]["count"])
        self.assertEqual(
            band["ceiling_atoms"],
            buckets["ungrounded_other"]["count"]
            + buckets["dollar_out_of_facts"]["count"]
            + buckets["near_miss_same_kind"]["count"],
        )

    def test_shares_sum_to_about_one(self) -> None:
        buckets = self.report["buckets"]
        total_share = sum(buckets[name]["share"] for name in buckets)
        self.assertAlmostEqual(total_share, 1.0, places=6)

    def test_band_basis_is_stamped_heuristic(self) -> None:
        # The band's floor/ceiling shares are a heuristic source-guess residual,
        # NOT a human-confirmed rate — the basis key labels that so a downstream
        # reader cannot quote floor_share_of_briefs as a measured hallucination
        # rate.
        band = self.report["estimated_true_hallucination_band"]
        self.assertEqual(band["basis"], "heuristic_source_guess_unvalidated")


class TestTriageCorpusOverParquetFixture(unittest.TestCase):
    """triage_corpus accepts parquet paths (reuse measurement loader) and the
    known-clean golden fixture yields zero fabricated atoms + a zero band."""

    def test_golden_fixture_is_clean(self) -> None:
        report = triage_corpus([str(_GOLDEN_PARQUET)])
        self.assertEqual(report["n_briefs"], 4)
        self.assertEqual(report["total_fabricated_atoms"], 0)
        band = report["estimated_true_hallucination_band"]
        self.assertEqual(band["floor_atoms"], 0)
        self.assertEqual(band["ceiling_atoms"], 0)


class TestBuildAuditWorksheet(unittest.TestCase):
    """build_audit_worksheet yields records for the ambiguous buckets with the
    required keys, deterministic ordering, and an empty human_label slot."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = [
            {
                "ticker": "DOF",
                "source_event_title": "Company reports strong quarter",
                "source_event_url": "https://example.com/dof",
                "source_event_published_at": "2026-05-23",
                "valuation_ps": 7.5,
                "brief_supply_chain_md": (
                    "The addressable market is roughly $12 billion by 2027, "
                    "which management believes it can capture over time."
                ),
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {
                "ticker": "UGO",
                "source_event_title": "Company reports strong quarter",
                "source_event_url": "https://example.com/ugo",
                "source_event_published_at": "2026-05-23",
                "valuation_ps": 7.5,
                "brief_supply_chain_md": "it trades at an absurd 4052.9x sales, unheard of.",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {  # in_catalyst_title — must NOT be in the worksheet (not ambiguous)
                "ticker": "CTRT",
                "source_event_title": "Acme wins a $450 million defense contract",
                "source_event_url": "https://example.com/ctrt",
                "source_event_published_at": "2026-05-23",
                "brief_supply_chain_md": "the new $450 million award lifts backlog.",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
        ]

    def test_records_carry_required_keys(self) -> None:
        records = build_audit_worksheet(self.rows, per_bucket=15)
        self.assertTrue(records)
        required = {
            "ticker",
            "brief_date",
            "field",
            "span",
            "sentence_window",
            "source_event_title",
            "source_event_url",
            "bucket",
            "human_label",
        }
        for rec in records:
            self.assertTrue(required.issubset(rec.keys()), f"missing keys in {rec.keys()}")
            self.assertEqual(rec["human_label"], "")
            self.assertIn(rec["bucket"], ("dollar_out_of_facts", "ungrounded_other"))

    def test_only_ambiguous_buckets_included(self) -> None:
        records = build_audit_worksheet(self.rows, per_bucket=15)
        tickers = {r["ticker"] for r in records}
        # CTRT is in_catalyst_title (not ambiguous) → excluded.
        self.assertNotIn("CTRT", tickers)
        self.assertIn("DOF", tickers)
        self.assertIn("UGO", tickers)

    def test_sentence_window_contains_span_and_is_bounded(self) -> None:
        records = build_audit_worksheet(self.rows, per_bucket=15)
        for rec in records:
            self.assertIn(rec["span"], rec["sentence_window"])
            # ~120-char window (allow slack for the span itself).
            self.assertLessEqual(len(rec["sentence_window"]), 200)

    def test_deterministic_ordering(self) -> None:
        a = build_audit_worksheet(self.rows, per_bucket=15)
        b = build_audit_worksheet(self.rows, per_bucket=15)
        self.assertEqual(a, b)

    def test_per_bucket_cap_respected(self) -> None:
        # Build many ungrounded_other rows; cap at 2 per bucket.
        many = [
            {
                "ticker": f"U{i}",
                "source_event_title": "noise",
                "source_event_url": f"https://example.com/u{i}",
                "source_event_published_at": "2026-05-23",
                "valuation_ps": 7.5,
                "brief_supply_chain_md": f"trades at an absurd {4000 + i}.9x sales.",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            }
            for i in range(10)
        ]
        records = build_audit_worksheet(many, per_bucket=2)
        ungrounded = [r for r in records if r["bucket"] == "ungrounded_other"]
        self.assertLessEqual(len(ungrounded), 2)


class TestScorerHelperContract(unittest.TestCase):
    """The triage module reuses several UNDERSCORE-private helpers from the frozen
    scorer (faithfulness) + measurement loader. They are not in either module's
    ``__all__``, so a scorer refactor that renames or removes one would break
    triage SILENTLY at import time. This meta-test pins the exact contract so such
    a refactor fails loudly here instead."""

    def test_faithfulness_private_helpers_present(self) -> None:
        from alphalens_research.eval import faithfulness

        for name in (
            "_ATOM_UNIT_TO_FACT_KINDS",
            "_DIRECTIONAL_FACT_KEYS",
            "_DISTORTED_REL_BAND",
            "FAITHFULNESS_SCORER_VERSION",
            "Atom",
            "_atom_unit",
            "_canonical_numeric_from_span",
            "_fact_unit_kind",
            "_numeric_fact_candidates",
        ):
            self.assertTrue(
                hasattr(faithfulness, name),
                f"triage depends on faithfulness.{name}; scorer refactor dropped it",
            )

    def test_measurement_helpers_present(self) -> None:
        from alphalens_research.eval import measurement

        for name in (
            "_DATE_STRATUM_COLUMNS",
            "_load_rows_from_parquet",
            "fact_index_from_brief_row",
            "score_row",
        ):
            self.assertTrue(
                hasattr(measurement, name),
                f"triage depends on measurement.{name}; refactor dropped it",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
