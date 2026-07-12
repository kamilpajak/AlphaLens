"""T6.5 financing-claims detector — unit tests (RED first).

A deterministic (NO-LLM) detector that flags a brief text field asserting a
financing EVENT (capital raise / dilution / buyback / secondary or convertible
offering / share issuance / proceeds) when the row's fact index carries NO
financing fact — which by construction of the current prompts.py facts block is
ALWAYS true (no financing / shares-outstanding line is rendered).

DIAGNOSTIC-ONLY in v1 (never joined to any outcome ledger, not folded into the
frozen ``FaithfulnessResult.is_clean`` gate). Reuses the frozen scorer helpers
(``score_row`` / ``fact_index_from_brief_row`` / ``_sentence_window`` /
``_coerce_rows`` / ``wilson_interval`` / ``_rate_block`` / ``_year_month_of_row``
+ the negation/quote/clause machinery) — does NOT reimplement scoring.

Implements the locked spec (``/tmp/financing_detector_spec.json``): Tier-1 /
Tier-2 lexicon with a same-clause hard-anchor gate for Tier-2, DILUTIVE vs
RETURN_OF_CAPITAL subtype, subtype-matched ``source_event_title`` escape, a
financing-specific hypothetical/forward-conditional guard, and the reused
negation + quotation guards.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from alphalens_research.eval.faithfulness import FAITHFULNESS_SCORER_VERSION
from alphalens_research.eval.financing_claims import (
    FINANCING_DETECTOR_VERSION,
    FinancingFlag,
    build_financing_audit_worksheet,
    detect_financing_claims,
    measure_financing_fabrication,
)
from alphalens_research.eval.measurement import _COLUMN_TO_FACT_KEY

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "brief_day"
_CASSETTES = _FIXTURES / "cassettes"
_GOLDEN_PARQUET = _FIXTURES / "golden" / "2026-05-24.parquet"


def _fired(row: dict) -> list[FinancingFlag]:
    """The FIRED financing flags of a row (suppressed_by is None)."""
    return [f for f in detect_financing_claims(row) if f.suppressed_by is None]


# ---------------------------------------------------------------------------
# Negation / guard suppression
# ---------------------------------------------------------------------------


class TestFinancingGuards(unittest.TestCase):
    """The reused + financing-specific guards suppress the right hits."""

    def test_forward_conditional_secondary_offering_exit_not_flagged(self) -> None:
        # A legit future exit trigger — the hypothetical/forward guard suppresses.
        row = {
            "ticker": "FWD",
            "brief_catalyst_failure_exit": "exit if the company announces a secondary offering",
        }
        self.assertEqual(_fired(row), [])

    def test_negated_no_capital_raise_not_flagged(self) -> None:
        # Reused _is_negated: 'no' / 'avoid' cues in the clause before the phrase.
        row = {
            "ticker": "NEG",
            "brief_bear_summary_md": "the company faces no capital raise and avoids dilution",
        }
        self.assertEqual(_fired(row), [])

    def test_quoted_guidance_mention_not_flagged(self) -> None:
        # A brief quoting the prompt ban (phrase inside double-quotes) — reused
        # _is_quoted suppresses.
        row = {
            "ticker": "QUO",
            "brief_bear_summary_md": (
                'the guidance says "do not fabricate a capital raise" and we comply'
            ),
        }
        self.assertEqual(_fired(row), [])

    def test_bare_product_offering_not_flagged(self) -> None:
        # Tier-2 anchor gate: no financing anchor in the clause → no fire.
        rows = [
            {"ticker": "P1", "brief_tldr": "the company expanded its cloud product offering"},
            {"ticker": "P2", "brief_tldr": "management raised full-year guidance"},
        ]
        for row in rows:
            self.assertEqual(_fired(row), [], row["ticker"])

    def test_litigation_finance_business_model_not_flagged(self) -> None:
        # v1.1 business-context guard (post-deploy over-fire, ticker BUR): a
        # litigation-finance / capital-provider firm describing its MODEL —
        # "provides capital to ... in exchange for a portion of judgment
        # proceeds" — is revenue context, not a corporate financing EVENT. The
        # Tier-2 'proceeds' token anchored on 'capital' and false-fired in v1.
        row = {
            "ticker": "BUR",
            "brief_supply_chain_md": (
                "Burford Capital provides capital to plaintiffs and law firms in "
                "exchange for a portion of judgment proceeds. The catalyst drives "
                "demand for external funding."
            ),
        }
        self.assertEqual(_fired(row), [])
        flags = detect_financing_claims(row)
        self.assertTrue(
            any(f.suppressed_by == "business_context" for f in flags),
            [(f.matched_phrase, f.suppressed_by) for f in flags],
        )

    def test_mixed_clause_business_context_plus_real_raise_still_fires(self) -> None:
        # zen HIGH: the business-context guard must bind to the SPECIFIC token, not
        # the whole clause — a genuine fabrication sharing a clause with a
        # business-model phrase must still fire (else a real raise is silenced).
        row = {
            "ticker": "MIX",
            "brief_supply_chain_md": (
                "Burford provides capital in exchange for judgment proceeds and "
                "will raise capital via a dilutive secondary offering"
            ),
        }
        fired = _fired(row)
        # The genuine raise fires despite the business-model 'judgment proceeds'
        # phrase in the same clause (the recovery 'proceeds' token is the same
        # DILUTIVE subtype, so it collapses behind the fired raise — the point is
        # that a real fabrication is NOT silenced).
        self.assertTrue(
            any("raise" in f.matched_phrase or "offering" in f.matched_phrase for f in fired),
            [f.matched_phrase for f in fired],
        )

    def test_real_raise_with_nearby_capital_word_still_fires(self) -> None:
        # The business-context guard must NOT swallow a genuine raise just because
        # the word 'capital' is present — "raise capital via a secondary offering"
        # is a real financing assertion and MUST fire.
        row = {
            "ticker": "REAL",
            "brief_bear_summary_md": "the company will raise capital via a $500M dilutive secondary offering",
        }
        self.assertTrue(_fired(row), "a genuine raise must still fire")

    def test_adpt_real_raise_title_escape_suppresses(self) -> None:
        # A dilutive-raise assertion WITH a DILUTIVE offering in the title →
        # subtype-matched title escape suppresses; suppressed_by == 'title_escape'.
        row = {
            "ticker": "ADPT",
            "source_event_title": "Adaptive prices a $300M public offering of common stock",
            "brief_bear_summary_md": "the dilutive equity raise will pressure the share count",
        }
        self.assertEqual(_fired(row), [])
        flags = detect_financing_claims(row)
        self.assertTrue(flags, "expected at least one (suppressed) flag")
        self.assertTrue(
            any(f.suppressed_by == "title_escape" for f in flags),
            [(f.matched_phrase, f.suppressed_by) for f in flags],
        )


# ---------------------------------------------------------------------------
# Positive controls
# ---------------------------------------------------------------------------


class TestFinancingPositiveControls(unittest.TestCase):
    """Fabricated financing framing fires."""

    def test_avav_revenue_misframed_as_raise_flagged(self) -> None:
        # A revenue figure reframed as a capital raise, with a title that carries
        # a $ but NO financing verb → no subtype → no escape → FIRE (DILUTIVE).
        row = {
            "ticker": "AVAV",
            "source_event_title": "AeroVironment reports Q4 revenue of $641.6M",
            "brief_bear_summary_md": "a $641.6M capital raise will dilute holders significantly",
        }
        flags = _fired(row)
        self.assertEqual(len(flags), 1, [(f.matched_phrase, f.subtype) for f in flags])
        self.assertEqual(flags[0].subtype, "DILUTIVE")

    def test_fcn_buyback_subtype_mismatch_flagged(self) -> None:
        # A DILUTIVE 'raise -> dilution' assertion with a RETURN_OF_CAPITAL buyback
        # title → subtypes differ, escape does NOT apply → FIRE.
        row = {
            "ticker": "FCN",
            "source_event_title": "FTI Consulting authorizes a $370M share buyback program",
            "brief_bear_summary_md": "the equity raise ahead will drive dilution of existing holders",
        }
        flags = _fired(row)
        self.assertEqual(len(flags), 1, [(f.matched_phrase, f.subtype) for f in flags])
        self.assertEqual(flags[0].subtype, "DILUTIVE")

    def test_seeded_secondary_offering_dilution_flagged(self) -> None:
        # The anti-rot control: an affirmative secondary-offering dilution
        # assertion + empty financing facts + no financing-subtype title → FIRE.
        row = {
            "ticker": "SEED",
            "source_event_title": "Company reports a strong quarter",
            "brief_bear_summary_md": "the $500M secondary offering will dilute holders",
        }
        flags = _fired(row)
        self.assertEqual(len(flags), 1, [(f.matched_phrase, f.subtype) for f in flags])
        self.assertEqual(flags[0].subtype, "DILUTIVE")


# ---------------------------------------------------------------------------
# Must-not-fire on real grounded briefs
# ---------------------------------------------------------------------------


class TestFinancingCorpusMustNotFire(unittest.TestCase):
    """The 4 frozen golden cassettes carry no financing assertion by construction."""

    def test_manh_and_all_golden_cassettes_no_financing_fire(self) -> None:
        cassettes = sorted(_CASSETTES.glob("*.json"))
        self.assertEqual(len(cassettes), 4, "expected 4 golden cassettes")
        for path in cassettes:
            record = json.loads(path.read_text())
            contents = record["contents"]
            brief_json = record["openrouter_response"]["choices"][0]["message"]["content"]
            brief = json.loads(brief_json)
            # Map the schema brief fields onto their parquet TEXT columns so the
            # detector reads them the same way it reads a corpus row.
            row = {
                "brief_tldr": brief.get("tldr", ""),
                "brief_supply_chain_md": brief.get("supply_chain_reasoning", ""),
                "brief_bear_summary_md": brief.get("bear_summary", ""),
                "brief_catalyst_failure_exit": brief.get("catalyst_failure_exit", ""),
                "source_event_title": _title_from_contents(contents),
            }
            fired = _fired(row)
            self.assertEqual(fired, [], f"{path.name}: {[f.span for f in fired]}")


def _title_from_contents(contents: str) -> str:
    """Pull the catalyst title line out of a rendered <facts> block."""
    for line in contents.splitlines():
        stripped = line.strip()
        if stripped.startswith("title:"):
            return stripped[len("title:") :].strip()
    return ""


# ---------------------------------------------------------------------------
# Precondition + contract pins
# ---------------------------------------------------------------------------


class TestFinancingPrecondition(unittest.TestCase):
    """The 'no financing fact by construction' precondition is pinned."""

    def test_no_financing_fact_key_in_column_map(self) -> None:
        import re

        pattern = re.compile(r"financ|shares_out|proceeds|offering|dilut", re.IGNORECASE)
        offenders = [
            (col, key)
            for col, key in _COLUMN_TO_FACT_KEY.items()
            if pattern.search(col) or pattern.search(key)
        ]
        self.assertEqual(
            offenders,
            [],
            "a financing fact appeared in the column map; wire _FINANCING_FACT_KEYS",
        )


class TestScorerHelperContract(unittest.TestCase):
    """The detector reuses several UNDERSCORE-private helpers across the three
    frozen eval modules. A rename must fail loudly here, not silently at import."""

    def test_faithfulness_private_helpers_present(self) -> None:
        from alphalens_research.eval import faithfulness

        for name in (
            "_is_negated",
            "_is_quoted",
            "_clause_before",
            "_NEGATION_CUE_RES",
            "FAITHFULNESS_SCORER_VERSION",
        ):
            self.assertTrue(hasattr(faithfulness, name), f"faithfulness.{name} missing")

    def test_measurement_helpers_present(self) -> None:
        from alphalens_research.eval import measurement

        for name in (
            "_year_month_of_row",
            "_rate_block",
            "wilson_interval",
            "fact_index_from_brief_row",
            "score_row",
        ):
            self.assertTrue(hasattr(measurement, name), f"measurement.{name} missing")

    def test_triage_helpers_present(self) -> None:
        from alphalens_research.eval import fabrication_triage

        for name in ("_coerce_rows", "_sentence_window"):
            self.assertTrue(hasattr(fabrication_triage, name), f"fabrication_triage.{name} missing")


# ---------------------------------------------------------------------------
# Corpus report
# ---------------------------------------------------------------------------


class TestVersionBumpAndDualStamp(unittest.TestCase):
    """The negation patch bumped FAITHFULNESS_SCORER_VERSION to t6-v1.3; the 4
    golden cassettes still score clean WITHOUT a re-record; the report dual-stamps
    both poolability keys."""

    def test_faithfulness_version_bumped_to_v1_3(self) -> None:
        self.assertEqual(FAITHFULNESS_SCORER_VERSION, "t6-v1.3-2026-07-11")

    def test_golden_cassettes_still_clean_no_rerecord(self) -> None:
        from alphalens_research.eval.faithfulness import parse_facts_index, score_brief

        cassettes = sorted(_CASSETTES.glob("*.json"))
        self.assertEqual(len(cassettes), 4)
        for path in cassettes:
            record = json.loads(path.read_text())
            facts = parse_facts_index(record["contents"])
            brief_json = record["openrouter_response"]["choices"][0]["message"]["content"]
            brief = json.loads(brief_json)
            result = score_brief(facts, brief)
            self.assertEqual(result.characterization_violations, 0, path.name)
            self.assertEqual(result.fabricated_numeric_date_atoms, 0, path.name)

    def test_report_dual_stamps_both_versions(self) -> None:
        report = measure_financing_fabrication(
            [{"ticker": "X", "brief_tldr": "no financing here", "brief_model_used": "m"}]
        )
        self.assertEqual(report["scorer_version"], FAITHFULNESS_SCORER_VERSION)
        self.assertEqual(report["financing_detector_version"], FINANCING_DETECTOR_VERSION)


class TestMeasureFinancingFabrication(unittest.TestCase):
    """measure_financing_fabrication returns a poolability-stamped report."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = [
            {  # fires
                "ticker": "AVAV",
                "source_event_title": "AeroVironment reports Q4 revenue of $641.6M",
                "brief_bear_summary_md": "a $641.6M capital raise will dilute holders",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {  # fires
                "ticker": "SEED",
                "source_event_title": "Company reports a strong quarter",
                "brief_bear_summary_md": "the $500M secondary offering will dilute holders",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {  # suppressed (title escape) — no fired flag
                "ticker": "ADPT",
                "source_event_title": "Adaptive prices a $300M public offering of common stock",
                "brief_bear_summary_md": "the dilutive equity raise pressures the share count",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {  # clean — no financing token at all
                "ticker": "CLEAN",
                "source_event_title": "Company reports a strong quarter",
                "brief_bear_summary_md": "valuation risk and momentum risk apply",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
        ]
        cls.report = measure_financing_fabrication(cls.rows)

    def test_report_top_level_keys(self) -> None:
        for key in (
            "scorer_version",
            "financing_detector_version",
            "n_briefs",
            "corpus_rate",
            "per_field",
            "per_stratum",
            "total_fired_spans",
            "total_suppressed_spans",
        ):
            self.assertIn(key, self.report)

    def test_dual_version_stamp(self) -> None:
        self.assertEqual(self.report["scorer_version"], FAITHFULNESS_SCORER_VERSION)
        self.assertEqual(self.report["financing_detector_version"], FINANCING_DETECTOR_VERSION)

    def test_corpus_rate_counts_briefs_with_a_fired_flag(self) -> None:
        block = self.report["corpus_rate"]
        self.assertEqual(block["n"], 4)
        self.assertEqual(block["k"], 2)  # AVAV + SEED fire; ADPT suppressed, CLEAN none
        for key in ("rate", "ci_low", "ci_high", "k", "n"):
            self.assertIn(key, block)

    def test_suppressed_spans_visible(self) -> None:
        # ADPT's suppressed title-escape hit must be counted so over-suppression
        # is visible (not silently dropped).
        self.assertGreaterEqual(self.report["total_suppressed_spans"], 1)
        self.assertGreaterEqual(self.report["total_fired_spans"], 2)

    def test_brief_date_stratum_from_filename(self) -> None:
        # A parquet path stem is threaded in as brief_date so per-day strata exist
        # even though the live parquet has no brief_date column.
        report = measure_financing_fabrication([str(_GOLDEN_PARQUET)])
        self.assertIn("brief_date", report["per_stratum"])
        self.assertIn("2026-05-24", report["per_stratum"]["brief_date"])


class TestBuildFinancingAuditWorksheet(unittest.TestCase):
    """build_financing_audit_worksheet stages fired + a sample of suppressed hits."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = [
            {
                "ticker": "AVAV",
                "source_event_title": "AeroVironment reports Q4 revenue of $641.6M",
                "source_event_url": "https://example.com/avav",
                "brief_bear_summary_md": "a $641.6M capital raise will dilute holders",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
            {
                "ticker": "ADPT",
                "source_event_title": "Adaptive prices a $300M public offering of common stock",
                "source_event_url": "https://example.com/adpt",
                "brief_bear_summary_md": "the dilutive equity raise pressures the share count",
                "brief_model_used": "deepseek/deepseek-v4-pro",
            },
        ]

    def test_records_carry_required_keys(self) -> None:
        records = build_financing_audit_worksheet(self.rows, per_bucket=25)
        self.assertTrue(records)
        required = {
            "ticker",
            "brief_date",
            "field",
            "span",
            "sentence_window",
            "source_event_title",
            "source_event_url",
            "matched_phrase",
            "subtype",
            "suppressed_by",
            "bucket",
            "human_label",
        }
        for rec in records:
            self.assertTrue(required.issubset(rec.keys()), f"missing keys in {rec.keys()}")
            self.assertEqual(rec["human_label"], "")
            self.assertIn(rec["bucket"], ("asserted_financing", "suppressed_sample"))

    def test_two_buckets_present(self) -> None:
        records = build_financing_audit_worksheet(self.rows, per_bucket=25)
        buckets = {r["bucket"] for r in records}
        self.assertIn("asserted_financing", buckets)  # AVAV fired
        self.assertIn("suppressed_sample", buckets)  # ADPT suppressed

    def test_deterministic_ordering(self) -> None:
        a = build_financing_audit_worksheet(self.rows, per_bucket=25)
        b = build_financing_audit_worksheet(self.rows, per_bucket=25)
        self.assertEqual(a, b)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
