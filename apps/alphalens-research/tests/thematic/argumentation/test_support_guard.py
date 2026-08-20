"""The prose may not assert a benefit the channel record does not support.

WHY A MACHINE CHECK AND NOT A BADGE
-----------------------------------
Bansal et al. (CHI 2021) measured that adding explanations raised the rate at
which people accepted a system's recommendation WHETHER OR NOT it was correct: a
fluent explanation raises belief roughly independently of accuracy. Steyvers et
al. (Nature Machine Intelligence, 2025) point at the intervention that does
close the calibration gap — aligning the explanation's OWN hedging with the
model's uncertainty, not attaching a label beside otherwise-confident text.

In hazard-control terms a chip next to a confident paragraph is an
administrative control, near the bottom of the ordering. The engineering control
is to generate the prose FROM the record and check the rendered shape against
it. That is what this module tests.

The check is LEXICAL, so it cannot claim to make the unsupported shape
unrenderable: a paraphrase outside the list renders exactly as before. What the
tests here pin is the two directions that would make the instrument LIE — an
entry that can never fire, and a suppressor that disarms on prose the prompt
itself mandates.

SCOPE IS THE WHOLE DESIGN
-------------------------
The guard is INERT unless the record actually fails to support a benefit claim.
A guard that also policed `established` rows would start rewriting well-grounded
prose and would drift into an editorial filter, which is a different (and
unauthorised) thing.

AND IT IS NOT A DELETION GATE
-----------------------------
When it trips twice the ROW STILL SHIPS — same rank, same trade setup, same
deterministic signals — and only the four prose strings are withheld, stamped
and gauged. `test_a_twice_violating_row_still_ships_with_its_prose_withheld` is
the positive control that proves the wiring cannot rot into a drop.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.thematic.argumentation import support_guard
from alphalens_pipeline.thematic.mapping import channel_assessor


def _brief(**over) -> dict:
    base = {
        "tldr": "QUBT surfaced from the Air Force award; no company-specific "
        "cash-flow path from that event to this company was established.",
        "supply_chain_reasoning": "The event names no link to this company.",
        "bear_summary": "P/S 30 sits in the 1st sector percentile.",
        "catalyst_failure_exit": "Exit if no further event ties this company to "
        "the theme within the setup horizon.",
    }
    base.update(over)
    return base


class TheGuardFiresOnAnUnsupportedBenefitClaim(unittest.TestCase):
    def test_a_not_established_brief_asserting_a_benefit_fires(self):
        """POSITIVE CONTROL 1 — the lexicon cannot rot to empty.

        Without this the banned-verb tuple could shrink to ``()`` and every
        other test in this file would still pass while guarding nothing.
        """
        violations = support_guard.check_support_language(
            _brief(
                tldr="QUBT benefits from rising datacenter capex and is positioned to win share."
            ),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        fired = [v for v in violations if v.suppressed_by is None]
        self.assertGreaterEqual(len(fired), 1)
        self.assertEqual(fired[0].field, "tldr")

    def test_the_same_prose_is_clean_at_established(self):
        """Anti-inertness control: the guard is SCOPED, not a style police."""
        violations = support_guard.check_support_language(
            _brief(
                tldr="QUBT benefits from rising datacenter capex and is positioned to win share."
            ),
            causal_support=channel_assessor.SUPPORT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        self.assertEqual(violations, [])

    def test_it_is_inert_at_suggestive(self):
        self.assertFalse(
            support_guard.guard_applies(
                causal_support=channel_assessor.SUPPORT_SUGGESTIVE,
                grounding=channel_assessor.GROUNDING_GROUNDED,
            )
        )

    def test_a_non_grounded_row_is_in_scope_at_any_support_level(self):
        # The overlay: an event that is not about the theme cannot support a
        # benefit claim however confident the chain reads.
        self.assertTrue(
            support_guard.guard_applies(
                causal_support=channel_assessor.SUPPORT_ESTABLISHED,
                grounding=channel_assessor.GROUNDING_THEME_MISROUTE,
            )
        )

    def test_no_record_is_in_scope(self):
        # An assessor outage carries no judgement at all, so the prose may not
        # borrow one in either direction.
        self.assertTrue(
            support_guard.guard_applies(
                causal_support=support_guard.NO_RECORD,
                grounding=channel_assessor.GROUNDING_UNKNOWN,
            )
        )

    def test_every_prose_field_is_scanned(self):
        for field in (
            "tldr",
            "supply_chain_reasoning",
            "bear_summary",
            "catalyst_failure_exit",
        ):
            with self.subTest(field=field):
                violations = support_guard.check_support_language(
                    _brief(**{field: "The company benefits from the theme."}),
                    causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
                    grounding=channel_assessor.GROUNDING_GROUNDED,
                    ticker="QUBT",
                )
                self.assertEqual([v.field for v in violations if v.suppressed_by is None], [field])

    def test_two_benefit_claims_in_one_field_collapse_to_one_violation(self):
        violations = support_guard.check_support_language(
            _brief(tldr="QUBT benefits from the theme and also gains share."),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        self.assertEqual(len([v for v in violations if v.suppressed_by is None]), 1)


class TheSuppressorsLetHonestHedgingThrough(unittest.TestCase):
    """A fired match must survive negation, conditional and quotation.

    The conditional suppressor IS the contract's escape hatch: a forward
    statement is allowed exactly when it is explicitly conditional on the link
    the record says is missing.
    """

    def _scan(self, text: str):
        return support_guard.check_support_language(
            _brief(tldr=text),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )

    def _fired(self, text: str) -> int:
        return len([v for v in self._scan(text) if v.suppressed_by is None])

    def test_a_negated_claim_passes(self):
        self.assertEqual(self._fired("The event does not show that QUBT benefits."), 0)

    def test_no_evidence_that_it_benefits_passes(self):
        self.assertEqual(self._fired("There is no evidence that QUBT benefits here."), 0)

    def test_an_explicit_conditional_passes(self):
        # The escape hatch, exercised with a phrase that IS in the lexicon —
        # otherwise this test would pass on a word the guard never looks for.
        text = "If the reported contract is confirmed, QUBT benefits from a new customer."
        self.assertEqual(self._fired(text), 0)
        self.assertEqual(
            [v.matched_phrase for v in self._scan(text)],
            ["benefits"],
            "the conditional must SUPPRESS a real lexicon hit, not miss it",
        )

    def test_a_quoted_headline_passes(self):
        self.assertEqual(self._fired('The headline reads "Quantum firms benefit".'), 0)

    def test_an_unconditional_claim_still_fires(self):
        self.assertEqual(self._fired("QUBT benefits from the theme."), 1)

    def test_a_negation_in_an_earlier_sentence_does_not_suppress(self):
        # Clause-bounded, like the eval-side primitives: a negation two
        # sentences back must not license an unconditional claim here.
        self.assertEqual(self._fired("No contract was named. QUBT benefits from the theme."), 1)

    def test_the_suppressed_match_is_still_reported(self):
        # Suppressed is not invisible: the span is recorded so the first-weeks
        # manual read can check the suppressors themselves.
        violations = self._scan("If the contract is confirmed, QUBT benefits from a new customer.")
        self.assertTrue(violations)
        self.assertEqual(violations[0].suppressed_by, "conditional")
        self.assertEqual(violations[0].matched_phrase, "benefits")


class Tier2TokensNeedAnEconomicAnchor(unittest.TestCase):
    """`drive`, `capture` and `lift` are polysemous and would over-fire bare."""

    def _fired(self, text: str) -> int:
        violations = support_guard.check_support_language(
            _brief(supply_chain_reasoning=text),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        return len([v for v in violations if v.suppressed_by is None])

    def test_an_unanchored_tier2_token_does_not_fire(self):
        self.assertEqual(self._fired("The drive train is unrelated to this event."), 0)

    def test_capture_rate_is_not_a_benefit_claim(self):
        self.assertEqual(self._fired("The capture rate of the sensor is 40 Hz."), 0)

    def test_an_anchored_tier2_token_fires(self):
        self.assertEqual(self._fired("The award drives revenue for this company."), 1)

    def test_lift_with_margin_fires(self):
        self.assertEqual(self._fired("The change lifts margin at this company."), 1)


class ASuppressorMustGovernThePhraseNotTheSentence(unittest.TestCase):
    """The cue has to apply TO the benefit phrase, not merely share a sentence.

    This is the failure mode that made the guard anti-correlated with the risk
    it exists to catch. The prompt MANDATES a negated sentence at
    ``not_established`` ("no company-specific cash-flow path ... was
    established") and MANDATES hedged risk prose in ``bear_summary``, so a
    sentence-wide suppressor is disarmed by the model's own instructions:
    the naive violation the model rarely writes fired, and the
    hedge-plus-assertion shape the prompt actively teaches went quiet.
    """

    def _fired(self, text: str) -> int:
        violations = support_guard.check_support_language(
            _brief(tldr=text),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        return len([v for v in violations if v.suppressed_by is None])

    def test_a_negation_in_an_earlier_comma_segment_does_not_suppress(self):
        # The mandated not_established sentence with a benefit claim bolted on
        # after the comma. The negation governs "was established", not
        # "benefits".
        self.assertEqual(
            self._fired(
                "QUBT surfaced from the Yahoo Finance market round-up; no "
                "company-specific cash-flow path from that event to this company "
                "was established, though the datacenter buildout benefits its "
                "optics line."
            ),
            1,
        )

    def test_a_negation_before_an_adversative_does_not_suppress(self):
        self.assertEqual(
            self._fired(
                "The event names no link to this company (it is a macro item "
                "about the category), so treat the pairing as unreliable, but "
                "QUBT is positioned to win share in optics."
            ),
            1,
        )

    def test_a_modal_in_a_trailing_segment_does_not_suppress(self):
        self.assertEqual(
            self._fired(
                "QUBT benefits from the AI capex cycle, and the shares could "
                "stay volatile into the print."
            ),
            1,
        )

    def test_a_conditional_governing_an_earlier_clause_does_not_suppress(self):
        self.assertEqual(
            self._fired("Exit if the buildout stalls, since QUBT benefits from continued capex."),
            1,
        )

    def test_a_concessive_modal_does_not_suppress(self):
        self.assertEqual(
            self._fired(
                "P/S 30 is rich and momentum could fade, even though QUBT "
                "benefits from the datacenter cycle."
            ),
            1,
        )

    def test_the_honest_escape_hatch_still_suppresses(self):
        """Anti-inertness control for the narrowed scope.

        Narrowing the suppressors must not delete the contract's own escape
        hatch: a forward statement conditional on the missing link is the shape
        the prompt asks for at ``suggestive``, and it must stay allowed.
        """
        text = "If the reported contract is confirmed, QUBT benefits from a new customer."
        self.assertEqual(self._fired(text), 0)
        violations = support_guard.check_support_language(
            _brief(tldr=text),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        self.assertEqual(violations[0].suppressed_by, support_guard.SUPPRESSED_BY_CONDITIONAL)

    def test_a_same_segment_negation_still_suppresses(self):
        self.assertEqual(self._fired("The event does not show that QUBT benefits."), 0)


class TheSubjectOfTheBenefitMustBeThisCompany(unittest.TestCase):
    """A benefit verb about RIVALS is honest risk prose, not a violation.

    ``bear_summary`` is MANDATORY and its own instruction asks for competitor,
    momentum and valuation risks, so competitor-benefit sentences are the
    expected shape there. Firing on them withheld exactly the honest prose this
    increment exists to keep visible — and the withholding was perfectly
    correlated with the epistemic status the design forbids gating on.
    """

    def _violations(self, **fields):
        return support_guard.check_support_language(
            _brief(**fields),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
            company_name="Quantum Computing Inc",
        )

    def _fired(self, **fields) -> int:
        return len([v for v in self._violations(**fields) if v.suppressed_by is None])

    def test_a_bear_case_about_rivals_does_not_fire(self):
        self.assertEqual(
            self._fired(
                bear_summary=(
                    "Larger rivals with existing contracts benefit more from any "
                    "category spend. Incumbents capture share in this category, "
                    "and any category tailwind for peers is already priced."
                )
            ),
            0,
        )

    def test_the_third_party_match_is_still_reported_as_suppressed(self):
        violations = self._violations(
            bear_summary="Larger rivals with existing contracts benefit more from category spend."
        )
        self.assertTrue(violations)
        self.assertEqual(violations[0].suppressed_by, support_guard.SUPPRESSED_BY_NO_SUBJECT)

    def test_the_ticker_as_subject_still_fires(self):
        """Anti-inertness control: the subject rule must not silence the guard."""
        self.assertEqual(self._fired(tldr="QUBT benefits from the theme."), 1)

    def test_the_company_name_as_subject_still_fires(self):
        self.assertEqual(self._fired(tldr="Quantum Computing benefits from the theme."), 1)

    def test_a_generic_self_reference_still_fires(self):
        self.assertEqual(self._fired(tldr="The company benefits from the theme."), 1)


class EveryLexiconEntryMustBeAbleToFire(unittest.TestCase):
    """Structural anti-rot: an entry that can never match reads as covered.

    Two entries were inert on every possible input — one because ``_normalise``
    stripped hyphens from the TEXT but not from the compiled phrase, one
    because it contains its own conditional cue. Both were formulations a model
    reaches for on a weak link, so this is not a random gap. Enumerating the
    lexicon is the only check that catches the class permanently.
    """

    #: Subject + economic anchor + no negation and no conditional cue, so a
    #: failure here is the ENTRY's inability to fire and nothing else.
    CARRIER = "QUBT {phrase} revenue at scale."

    def test_each_tier1_phrase_fires_on_a_minimal_carrier_sentence(self):
        for phrase in support_guard.BANNED_BENEFIT_PHRASES:
            with self.subTest(phrase=phrase):
                violations = support_guard.check_support_language(
                    _brief(tldr=self.CARRIER.format(phrase=phrase)),
                    causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
                    grounding=channel_assessor.GROUNDING_GROUNDED,
                    ticker="QUBT",
                )
                self.assertEqual(
                    len([v for v in violations if v.suppressed_by is None]),
                    1,
                    f"{phrase!r} can never fire",
                )

    def test_the_hyphenated_entry_fires_in_both_spellings(self):
        for text in (
            "QUBT is a second-order beneficiary of the datacenter buildout.",
            "QUBT is a second order beneficiary of the datacenter buildout.",
        ):
            with self.subTest(text=text):
                violations = support_guard.check_support_language(
                    _brief(tldr=text),
                    causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
                    grounding=channel_assessor.GROUNDING_GROUNDED,
                    ticker="QUBT",
                )
                self.assertEqual(len([v for v in violations if v.suppressed_by is None]), 1)

    def test_the_modal_bearing_entry_fires(self):
        violations = support_guard.check_support_language(
            _brief(tldr="QUBT should see demand from hyperscalers."),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        self.assertEqual([v.matched_phrase for v in violations], ["should see demand"])
        self.assertIsNone(violations[0].suppressed_by)

    def test_the_near_neighbour_paraphrases_are_covered(self):
        # The list was built by enumeration, so the obvious neighbours of
        # entries that ARE present were missing. These are not exhaustive and
        # the docstring says so — recall is measured, not assumed.
        for text in (
            "QUBT stands to profit from the datacenter cycle.",
            "QUBT is levered to hyperscaler capex.",
            "The award is a windfall for QUBT.",
            "QUBT takes share in optics.",
            "QUBT has pricing power in optics.",
            "The cycle drives margin expansion at QUBT.",
        ):
            with self.subTest(text=text):
                violations = support_guard.check_support_language(
                    _brief(tldr=text),
                    causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
                    grounding=channel_assessor.GROUNDING_GROUNDED,
                    ticker="QUBT",
                )
                self.assertEqual(len([v for v in violations if v.suppressed_by is None]), 1)


class PolysemousStemsStayOnTheAnchoredPath(unittest.TestCase):
    """Duplicated stems bypassed the anchor requirement and over-fired.

    ``captures`` / ``lifts`` / ``gains`` sat in BOTH tiers; tier 1 is scanned
    first and returns on the first fired match, so the documented anchor
    requirement was dead for exactly the three stems that most need it. A false
    fire costs a draw and can withhold honest prose from a ``not_established``
    row — the opposite of the design intent.
    """

    def _fired(self, text: str) -> int:
        violations = support_guard.check_support_language(
            _brief(supply_chain_reasoning=text),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        return len([v for v in violations if v.suppressed_by is None])

    def test_a_sensor_capturing_images_is_not_a_benefit_claim(self):
        self.assertEqual(self._fired("The QUBT sensor captures images at 40 Hz."), 0)

    def test_a_crane_lifting_a_module_is_not_a_benefit_claim(self):
        self.assertEqual(self._fired("The QUBT crane lifts the module into place."), 0)

    def test_a_price_move_is_not_a_benefit_claim(self):
        self.assertEqual(self._fired("QUBT gains 3% on the day."), 0)

    def test_no_stem_sits_in_both_tiers(self):
        """The structural form of the same defect, so it cannot come back.

        Tier 1 is scanned first and returns on the first FIRED match, so a stem
        present in both tiers permanently bypasses the anchor requirement.
        """
        overlap = set(support_guard.BANNED_BENEFIT_PHRASES) & set(support_guard._TIER2_TOKENS)
        self.assertEqual(overlap, set())

    def test_the_lexicon_carries_no_hyphens(self):
        """`_normalise` maps a hyphen to a space on both sides of the compare.

        A hyphenated ENTRY therefore matches neither spelling of the text. The
        per-entry carrier test above catches it too; this states the rule.
        """
        self.assertEqual([p for p in support_guard.BANNED_BENEFIT_PHRASES if "-" in p], [])

    def test_the_same_stems_fire_when_anchored(self):
        """Anti-inertness control for the demotion to tier 2."""
        for text in (
            "The award captures revenue for QUBT.",
            "The change lifts margin at QUBT.",
            "QUBT gains share in optics.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._fired(text), 1)


class TheGuardReportsItsOwnVersion(unittest.TestCase):
    def test_the_version_token_is_stamped_and_stable(self):
        self.assertTrue(support_guard.SUPPORT_GUARD_VERSION.startswith("support-guard-"))
        self.assertEqual(support_guard.SUPPORT_GUARD_VERSION, support_guard.SUPPORT_GUARD_VERSION)

    def test_the_banned_lexicon_is_not_empty(self):
        # Belt on the positive control above: an empty tuple would silently
        # disable every scan.
        self.assertGreater(len(support_guard.BANNED_BENEFIT_PHRASES), 20)

    def test_the_not_a_forecast_sentence_is_single_sourced_from_the_assessor(self):
        # One source of wording for the instrument, the prompt and the prose.
        self.assertEqual(
            support_guard.CAUSAL_SUPPORT_NOT_A_FORECAST,
            channel_assessor.CAUSAL_SUPPORT_NOT_A_FORECAST,
        )


if __name__ == "__main__":
    unittest.main()
