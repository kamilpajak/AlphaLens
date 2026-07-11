"""T6 brief-faithfulness pilot (v1) — deterministic gate over the golden cassettes.

Implements the LOCKED design memo
``docs/research/reading_quality_eval_design_2026_07_11.md`` (§6, §8, §11):

* **Parser round-trip guard** — the fact-index parser recovers KNOWN typed facts
  for the 4 golden cassettes, so a red gate is never a regex-parser bug
  misattributed to the model (memo §6.4 "Parser-correctness guard").
* **Gating assertions** — over the 4 real cassettes,
  ``fabricated_numeric_date_atoms == 0`` AND ``characterization_violations == 0``
  (the ONLY registered v1 success criteria, memo §2 pre-registration clause).
* **Seeded positive-control** — a synthetic brief carrying a KNOWN fabricated
  number AND a KNOWN forbidden characterization MUST fire red, mirroring the
  repo's ``test_no_raw_*_http`` "positive-control so the regex cannot rot to
  empty" pattern (memo §6.7).
* **Boundary pins** — GROUNDED-with-sign-strip, GROUNDED-after-rounding, and a
  seeded DISTORTED that proves the DISTORTED branch is reachable (memo §6.4).

The fact-index source at test time: the golden parquet's
``brief_template_facts_json`` is NULL for all 4 rows (verified 2026-07-11), so
the harness uses the memo's documented FALLBACK — parse the rendered ``<facts>``
string from the cassette ``contents``. The cassettes are self-contained: each
carries ``contents`` (facts block) + ``openrouter_response`` (brief JSON).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from alphalens_research.eval.faithfulness import (
    extract_atoms,
    parse_facts_index,
    score_brief,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "brief_day"
_CASSETTES = _FIXTURES / "cassettes"


def _load_cassettes() -> dict[str, dict]:
    """Load the 4 golden cassettes keyed by ticker.

    Each record → ``{ticker: {"contents": <facts prompt>, "brief": {field: text}}}``.
    Ticker is parsed off the ``ticker:`` line inside the rendered ``<facts>``
    block (the same line the fallback fact-index parser keys on).
    """
    out: dict[str, dict] = {}
    for path in sorted(_CASSETTES.glob("*.json")):
        record = json.loads(path.read_text())
        contents = record["contents"]
        brief_json = record["openrouter_response"]["choices"][0]["message"]["content"]
        brief = json.loads(brief_json)
        facts = parse_facts_index(contents)
        ticker = facts["ticker"]
        out[ticker] = {"contents": contents, "brief": brief, "facts": facts}
    return out


# Known typed facts for the 4 golden cassettes, read directly off the rendered
# <facts> blocks (verified against the cassette contents 2026-07-11). The parser
# round-trip guard asserts parse_facts_index() recovers exactly these.
_KNOWN_FACTS = {
    "QUBT": {
        "ticker": "QUBT",
        "valuation_ps": 4052.9,
        "valuation_ev_rev": 2985.3,
        "technical_pct_off_52w_high": -50.0,
        "technical_ma200_distance_pct": 0.7,
    },
    "MANH": {
        "ticker": "MANH",
        "valuation_ps": 7.5,
        "valuation_ev_rev": 7.2,
        "technical_pct_off_52w_high": -39.2,
        "technical_ma200_distance_pct": -18.3,
    },
    "DFIN": {
        "ticker": "DFIN",
        "technical_pct_off_52w_high": -40.5,
        "technical_ma200_distance_pct": -21.0,
    },
    "QLYS": {
        "ticker": "QLYS",
        "technical_pct_off_52w_high": -33.4,
        "technical_ma200_distance_pct": -14.4,
    },
}


class TestParserRoundTrip(unittest.TestCase):
    """The parser recovers KNOWN typed facts — a red gate is never a parser bug."""

    def test_all_four_cassettes_parse(self):
        cassettes = _load_cassettes()
        self.assertEqual(set(cassettes), set(_KNOWN_FACTS))

    def test_known_facts_round_trip(self):
        cassettes = _load_cassettes()
        for ticker, known in _KNOWN_FACTS.items():
            facts = cassettes[ticker]["facts"]
            for key, expected in known.items():
                self.assertIn(key, facts, f"{ticker}: missing {key}")
                got = facts[key]
                if isinstance(expected, float):
                    self.assertAlmostEqual(float(got), expected, places=1, msg=f"{ticker}.{key}")
                else:
                    self.assertEqual(got, expected, f"{ticker}.{key}")


class TestGoldenGate(unittest.TestCase):
    """GATING: fabricated_numeric_date == 0 AND characterization_violations == 0."""

    def test_no_fabricated_numeric_date_over_golden(self):
        cassettes = _load_cassettes()
        # Explicit fixture-presence guard: the gate loop below is only
        # non-vacuous if the 4 cassettes are present. Do not rely on a sibling
        # test to prove the fixtures exist (mirrors the repo "positive-control
        # so the regex cannot rot to empty" doctrine, applied to fixtures).
        self.assertEqual(set(cassettes), set(_KNOWN_FACTS))
        for ticker, data in cassettes.items():
            result = score_brief(data["facts"], data["brief"])
            self.assertEqual(
                result.fabricated_numeric_date_atoms,
                0,
                f"{ticker}: unexpected fabricated numeric/date atoms: "
                f"{[a for a in result.atoms if a.gating and a.kind in ('numeric', 'date')]}",
            )

    def test_no_characterization_violations_over_golden(self):
        cassettes = _load_cassettes()
        self.assertEqual(set(cassettes), set(_KNOWN_FACTS))
        for ticker, data in cassettes.items():
            result = score_brief(data["facts"], data["brief"])
            self.assertEqual(
                result.characterization_violations,
                0,
                f"{ticker}: unexpected characterization violations: "
                f"{[a.span for a in result.atoms if a.kind == 'characterization' and a.gating]}",
            )

    def test_manh_negation_guard_does_not_fire(self):
        # MANH bear/reasoning both say "not a bargain" — the compliant framing.
        cassettes = _load_cassettes()
        result = score_brief(cassettes["MANH"]["facts"], cassettes["MANH"]["brief"])
        self.assertEqual(result.characterization_violations, 0)


class TestSeededPositiveControl(unittest.TestCase):
    """The gate MUST fire red on a known fabrication + a known violation."""

    def _manh_facts(self) -> dict:
        return _load_cassettes()["MANH"]["facts"]

    def test_positive_control_fires_both_metrics(self):
        # Synthetic brief against MANH facts (memo §6.5 contrast case):
        #   "$999M" — a market_cap NOT in the facts → FABRICATED numeric.
        #   "cheap entry" — forbidden characterization → VIOLATION.
        seeded = {
            "tldr": "MANH is a cheap entry with $999M in fresh insider buying.",
            "supply_chain_reasoning": "Grounded prose with no atoms.",
            "bear_summary": "Momentum laggard risk and valuation risk.",
            "catalyst_failure_exit": "Exit if the catalyst fails.",
        }
        result = score_brief(self._manh_facts(), seeded)
        self.assertGreaterEqual(
            result.fabricated_numeric_date_atoms,
            1,
            "seeded fabricated $999M must be flagged",
        )
        self.assertGreaterEqual(
            result.characterization_violations,
            1,
            "seeded 'cheap entry' must be flagged",
        )

    def test_positive_control_forecast_verb_fires(self):
        # Forecast verb adjacent to next_earnings_date fires a VIOLATION.
        facts = dict(self._manh_facts())
        facts["next_earnings_date"] = "2026-06-15"
        seeded = {
            "tldr": "We are expecting a beat at the next_earnings_date of 2026-06-15.",
            "supply_chain_reasoning": "x",
            "bear_summary": "x and y",
            "catalyst_failure_exit": "x",
        }
        result = score_brief(facts, seeded)
        self.assertGreaterEqual(result.characterization_violations, 1)

    def test_forecast_verb_fires_on_date_only_reference(self):
        # A date-only earnings reference ("a strong print on 2026-06-15") anchors
        # the forecast-verb check even without the literal word "earnings".
        facts = dict(self._manh_facts())
        facts["next_earnings_date"] = "2026-06-15"
        seeded = {
            "tldr": "We are anticipating a strong print on 2026-06-15.",
            "supply_chain_reasoning": "x",
            "bear_summary": "x and y",
            "catalyst_failure_exit": "x",
        }
        result = score_brief(facts, seeded)
        self.assertGreaterEqual(result.characterization_violations, 1)

    def test_incidental_date_substring_is_fabricated(self):
        # A fabricated date that appears only as an INCIDENTAL substring of a
        # longer digit run in a fact string must NOT ground (word-boundary date
        # match).
        facts = {"source_event_title": "revenue up 2026-05-2410 percent"}
        result = score_brief(facts, {"tldr": "the event landed on 2026-05-24"})
        date_atoms = [a for a in result.atoms if a.kind == "date"]
        self.assertTrue(date_atoms)
        self.assertTrue(
            all(a.verdict == "FABRICATED" for a in date_atoms),
            [(a.span, a.verdict) for a in date_atoms],
        )
        self.assertGreaterEqual(result.fabricated_numeric_date_atoms, 1)


class TestBoundaryPins(unittest.TestCase):
    """Pin the GROUNDED / DISTORTED / FABRICATED boundary over real values."""

    def _facts(self, ticker: str) -> dict:
        return _load_cassettes()[ticker]["facts"]

    def test_grounded_sign_strip(self):
        # QUBT "50% drawdown from 52w high" vs fact -50.0% → GROUNDED (sign stripped).
        # Pin WHICH fact grounded it — a coincidental cross-fact collision must
        # not be able to pass this test.
        result = score_brief(
            self._facts("QUBT"), {"supply_chain_reasoning": "50% drawdown from 52w high"}
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        grounded = [a for a in numeric if a.verdict == "GROUNDED"]
        self.assertTrue(grounded, numeric)
        self.assertTrue(
            any(a.matched_fact == "technical_pct_off_52w_high" for a in grounded),
            [(a.span, a.matched_fact) for a in grounded],
        )
        self.assertEqual(result.fabricated_numeric_date_atoms, 0)

    def test_extract_atoms_returns_numeric_atom(self):
        # extract_atoms is callable and yields a numeric atom for a % span.
        atoms = extract_atoms("supply_chain_reasoning", "50% drawdown from 52w high")
        self.assertTrue(any(a.kind == "numeric" for a in atoms), atoms)

    def test_grounded_after_rounding(self):
        # DFIN "21% below MA200" vs fact -21.0% → GROUNDED (rounded to brief precision).
        result = score_brief(
            self._facts("DFIN"), {"supply_chain_reasoning": "trading 21% below MA200"}
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        grounded = [a for a in numeric if a.verdict == "GROUNDED"]
        self.assertTrue(grounded, numeric)
        self.assertTrue(
            any(a.matched_fact == "technical_ma200_distance_pct" for a in grounded),
            [(a.span, a.matched_fact) for a in grounded],
        )
        self.assertEqual(result.fabricated_numeric_date_atoms, 0)

    def test_grounded_40_5_below_52w_high(self):
        # DFIN "40.5% below its 52-week high" vs fact -40.5% → GROUNDED.
        result = score_brief(
            self._facts("DFIN"),
            {"supply_chain_reasoning": "trading 40.5% below its 52-week high"},
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        grounded = [a for a in numeric if a.verdict == "GROUNDED"]
        self.assertTrue(
            any(a.matched_fact == "technical_pct_off_52w_high" for a in grounded),
            [(a.span, a.matched_fact) for a in grounded],
        )

    def test_distorted_branch_is_reachable(self):
        # Seeded: MANH "50% drawdown from 52w high" vs the 52w-high fact -39.2%.
        # 50 vs |39.2| = 27% relative → inside the 40%-of-fact DISTORTED band but
        # not exact after rounding → DISTORTED. Pin the DRIVING fact so the
        # verdict cannot come from an unrelated same-digit fact (the "%" atom is
        # unit-locked to "%" facts, so RSI 53 can no longer collide).
        result = score_brief(
            self._facts("MANH"),
            {"supply_chain_reasoning": "a 50% drawdown from 52w high"},
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        distorted = [a for a in numeric if a.verdict == "DISTORTED"]
        self.assertTrue(
            distorted,
            f"DISTORTED branch not reached: {[(a.span, a.verdict) for a in numeric]}",
        )
        self.assertEqual(
            distorted[0].matched_fact,
            "technical_pct_off_52w_high",
            f"DISTORTED matched the wrong fact: {[(a.span, a.matched_fact) for a in distorted]}",
        )

    def test_gross_overstatement_is_fabricated_not_distorted(self):
        # A "%" drawdown claim overstating the -39.2% fact by >40% relative
        # (55% vs 39.2 = 40.3% over) must be FABRICATED, not DISTORTED — the
        # DISTORTED band is scaled to the FACT magnitude only (symmetric),
        # so tolerance does not grow as the brief overstates.
        result = score_brief(
            self._facts("MANH"),
            {"supply_chain_reasoning": "a 55% drawdown from 52w high"},
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertTrue(
            any(a.verdict == "FABRICATED" for a in numeric),
            [(a.span, a.verdict, a.matched_fact) for a in numeric],
        )
        self.assertGreaterEqual(result.fabricated_numeric_date_atoms, 1)

    def test_fabricated_numeric_gates(self):
        # A number nowhere in the facts, far outside any band → FABRICATED (gating).
        result = score_brief(
            self._facts("MANH"),
            {"supply_chain_reasoning": "P/S of 12345.6 signals froth"},
        )
        self.assertGreaterEqual(result.fabricated_numeric_date_atoms, 1)

    def test_spaced_magnitude_does_not_false_ground(self):
        # "$7.5 billion" is a $-magnitude (7.5e9), NOT the P/S ratio 7.5 — the
        # spaced magnitude word must be consumed so it cannot false-ground
        # against an unrelated same-digit ratio fact.
        result = score_brief(
            {"valuation_ps": 7.5},
            {"tldr": "a $7.5 billion market cap looks stretched"},
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertTrue(
            all(a.verdict != "GROUNDED" for a in numeric),
            [(a.span, a.extracted_value, a.verdict, a.matched_fact) for a in numeric],
        )
        self.assertGreaterEqual(result.fabricated_numeric_date_atoms, 1)

    def test_multiple_atom_does_not_ground_percent_fact(self):
        # "4.2x sales" is a ratio-multiple, not a percentage — it must not ground
        # against a % fact of equal magnitude (unit-aware matcher).
        result = score_brief(
            {"technical_pct_off_52w_high": -4.2},
            {"tldr": "trading at 4.2x sales"},
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertTrue(
            all(a.verdict != "GROUNDED" for a in numeric),
            [(a.span, a.verdict, a.matched_fact) for a in numeric],
        )


class TestNegationFalseNegativeGuard(unittest.TestCase):
    """Pin that the widened academic-refusal lexicon does NOT suppress a real
    affirmative bargain/cheap claim that merely sits near a `requires`/`fail`
    token (the latent gate hole flagged in review)."""

    def test_affirmative_cheap_near_requires_still_fires(self):
        result = score_brief(
            {},
            {"tldr": "the thesis requires buying now because the stock is cheap"},
        )
        self.assertGreaterEqual(
            result.characterization_violations,
            1,
            "an affirmative 'cheap' claim must fire even with 'requires' nearby",
        )

    def test_affirmative_on_sale_near_fails_still_fires(self):
        result = score_brief(
            {},
            {"tldr": "momentum fails but these shares are on sale right now"},
        )
        self.assertGreaterEqual(result.characterization_violations, 1)

    def test_cheap_after_now_substring_still_fires(self):
        # "now" contains the substring "no" — the old raw-substring negation
        # guard silently suppressed this; whole-word matching must let it fire.
        result = score_brief({}, {"tldr": "right now the stock is cheap"})
        self.assertGreaterEqual(result.characterization_violations, 1)

    def test_academic_refusal_stays_compliant(self):
        # The genuine DFIN-style academic refusal must STILL be suppressed.
        result = score_brief(
            {},
            {"bear_summary": "insider buying is absent, failing to corroborate a bargain thesis."},
        )
        self.assertEqual(result.characterization_violations, 0)

    def test_prior_sentence_negation_does_not_leak(self):
        # A negation in an EARLIER sentence must not suppress a violation in the
        # next one (clause-scoped negation window).
        result = score_brief(
            {},
            {"tldr": "This is not a momentum name. The stock is cheap."},
        )
        self.assertGreaterEqual(result.characterization_violations, 1)


class TestReviewHardening(unittest.TestCase):
    """Regressions from the zen pre-merge review: spaced M/K/B magnitude, the
    durability (Buffett quant) facts line, and the documented integer-only gap."""

    def test_spaced_magnitude_m_grounds_dollar_fact(self):
        # "$500 M" is 5e8 dollars — a spaced 'M' magnitude must be consumed so a
        # legit market-cap citation grounds instead of false-firing FABRICATED.
        result = score_brief(
            {"market_cap": 5e8},
            {"tldr": "a $500 M market cap after the drop"},
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertTrue(
            any(a.verdict == "GROUNDED" and a.matched_fact == "market_cap" for a in numeric),
            [(a.span, a.extracted_value, a.verdict, a.matched_fact) for a in numeric],
        )
        self.assertEqual(result.fabricated_numeric_date_atoms, 0)

    def test_durability_line_facts_are_parsed_and_ground(self):
        # The durability (Buffett quant) line is part of the standard <facts>
        # block; a brief that faithfully cites its ROIC must ground, not FABRICATE.
        contents = (
            "<facts>\n"
            "ticker: TEST\n"
            "company: Test Co\n"
            "theme: widgets\n"
            "- durability (Buffett quant): ROIC 12.3% (3y avg 11.0%),"
            " owner-earnings yield 4.5%, DCF margin of safety -8.0%\n"
            "</facts>"
        )
        facts = parse_facts_index(contents)
        self.assertAlmostEqual(facts["buffett_roic_pct"], 12.3, places=1)
        self.assertAlmostEqual(facts["buffett_roic_3y_avg_pct"], 11.0, places=1)
        self.assertAlmostEqual(facts["buffett_owner_earnings_yield_pct"], 4.5, places=1)
        self.assertAlmostEqual(facts["buffett_margin_of_safety_pct"], -8.0, places=1)
        result = score_brief(facts, {"bear_summary": "ROIC 12.3% durability, plus valuation risk"})
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertTrue(
            any(a.verdict == "GROUNDED" and a.matched_fact == "buffett_roic_pct" for a in numeric),
            [(a.span, a.verdict, a.matched_fact) for a in numeric],
        )
        self.assertEqual(result.fabricated_numeric_date_atoms, 0)

    def test_integer_only_metric_is_not_gated_known_gap(self):
        # DOCUMENTED v1 LIMITATION (memo §6.2 / §10): a fabricated integer-valued
        # metric with no unit or decimal (e.g. "RSI 99") is a structural
        # reference, not a checkable atom, so it is NOT extracted or gated. Pin
        # the KNOWN gap so a future change that closes it is noticed.
        result = score_brief(
            {"technical_rsi": 53.0},
            {"tldr": "RSI 99 shows extreme momentum"},
        )
        numeric = [a for a in result.atoms if a.kind == "numeric"]
        self.assertEqual(numeric, [], "integer-only metric unexpectedly extracted")
        self.assertEqual(result.fabricated_numeric_date_atoms, 0)


if __name__ == "__main__":
    unittest.main()
