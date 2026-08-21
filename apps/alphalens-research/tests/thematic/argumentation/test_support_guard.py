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
A guard that also policed prose AGREEING with a well-evidenced, benefit-direction
record would start rewriting it and would drift into an editorial filter, which
is a different (and unauthorised) thing.

Failing to support a benefit claim has four shapes, not three. The support LEVEL
answers "how well is this chain evidenced", never "which way does it point" — so
a `suggestive` + `grounded` record reading "potential negative impact on revenue"
is a well-formed record of an ADVERSE mechanism, and benefit prose beside it
contradicts the row's own record (issue #1070).
`AHarmDirectionChannelPutsTheRowInScope` and
`ABenefitDirectionChannelIsNeverPutInScopeByDirection` are the two halves of
that: the first proves the arm can fire, the second proves it discriminates
rather than simply widening scope to everything.

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

# The two harm-direction channel records from issue #1070, verbatim. They are
# the ONLY observed harm strings: the exploratory slot that produced them was
# overwritten by a later slot, and the committed golden fixtures carry none, so
# the true-positive evidence for the harm lexicon is N=2 and is quoted here
# rather than paraphrased.
_HARM_GO = (
    "Softer retail sales and consumer sentiment data -> concerns about weakening "
    "consumer spending -> potential negative impact on revenue for small-cap "
    "retailers like Grocery Outlet."
)
_HARM_OLLI = (
    "As a small-cap retailer, Ollie's could see reduced customer demand, leading "
    "to lower revenue in the near term."
)

# Subject for the LEXICON-level tests. Any ticker/name works: subject_terms
# always includes the generic self-references ("this company", "the company",
# "its"), and the carriers below use them — so a failure in those tests is the
# ENTRY's inability to fire, never a subject mismatch. The subject test itself
# has its own class with a real, named candidate.
_ANY_TICKER = "XYZ"
_ANY_NAME = "Example Corp"

# REAL benefit-direction records, quoted from the committed golden fixtures and
# from the live store. They are the false-positive corpus, and they are chosen
# for the shapes that break a naive harm lexicon rather than for variety:
#   * GO / SVV  — a defensive-retail chain whose OPENING arm is harm to the
#                 consumer and whose last arm is revenue growth. This is the
#                 same macro sentence the GO HARM record above opens with.
#   * Z         — a unicode arrow, and "lower mortgage rates" mid-chain.
#   * MTH       — four arrows, and "rates stay lower for longer" mid-chain.
#   * ABUS      — no arrows at all: the comma-tail fallback path.
#   * HIVE      — a margin arm, in the direction the margin entries invert.
#   * OLLI      — the SAME ticker as the harm record, pointing the other way.
_REAL_BENEFIT_CHANNELS: tuple[str, ...] = (
    "Softer retail sales and consumer sentiment indicate consumers are pulling "
    "back on spending -> Budget-conscious shoppers trade down from traditional "
    "grocers to discount options -> Grocery Outlet's revenue increases from "
    "higher customer traffic and basket size in the coming quarters.",
    "Softer retail sales indicate consumers are reducing spending on new goods "
    "-> consumers trade down to cheaper secondhand alternatives -> demand for "
    "Savers Value Village's thrift stores increases, boosting revenue in the "
    "near term.",
    "Reduced rate hike expectations (from event implication) → lower mortgage "
    "rates → increased housing market activity → higher demand for Zillow's "
    "real estate marketplace and iBuying services, boosting revenue.",
    "Cooler US inflation reinforces Fed pause expectations -> interest rates "
    "stay lower for longer -> mortgage rates remain supportive -> homebuyer "
    "demand for entry-level homes increases -> Meritage Homes' new home "
    "orders/revenue improve over subsequent quarters.",
    "The success of Moderna and Merck's mRNA cancer vaccine validates the mRNA "
    "platform, potentially accelerating development of other mRNA vaccines. "
    "This could increase demand for Arbutus Biopharma's lipid nanoparticle "
    "(LNP) delivery technology, which is critical for mRNA therapeutics, "
    "leading to higher royalty revenue over the medium term.",
    "Bitcoin price jumps 6-7% -> higher market price for mined Bitcoin -> "
    "HIVE's mining revenue per Bitcoin rises, improving margins and earnings "
    "over the following days/weeks.",
    "The event notes that discount retailers could benefit if inflation drives "
    "trade-down behavior -> consumers shift purchases from higher-priced "
    "retailers toward discounters -> Ollie's Bargain Outlet, as a "
    "closeout/discount retailer, sees increased customer traffic and revenue "
    "from trade-down demand over subsequent quarters.",
)

# A REAL falsifier, quoted from the committed golden fixture. Falsifiers are
# written in non-occurrence language by construction, and the prompt instructs
# the model to render them into `bear_summary` and `catalyst_failure_exit`.
_REAL_FALSIFIER = (
    "If Maravai's revenue from mRNA-related products does not increase in the "
    "following quarters, or if the trial success does not lead to increased "
    "mRNA manufacturing demand."
)


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
        # With no record text to read, the direction arm cannot fire, so the
        # level and grounding arms are the whole test — the pre-#1070 behaviour.
        self.assertFalse(
            support_guard.guard_applies(
                causal_support=channel_assessor.SUPPORT_SUGGESTIVE,
                grounding=channel_assessor.GROUNDING_GROUNDED,
                channel_text="",
                ticker=_ANY_TICKER,
                company_name=_ANY_NAME,
            )
        )

    def test_a_non_grounded_row_is_in_scope_at_any_support_level(self):
        # The overlay: an event that is not about the theme cannot support a
        # benefit claim however confident the chain reads.
        self.assertTrue(
            support_guard.guard_applies(
                causal_support=channel_assessor.SUPPORT_ESTABLISHED,
                grounding=channel_assessor.GROUNDING_THEME_MISROUTE,
                channel_text="",
                ticker=_ANY_TICKER,
                company_name=_ANY_NAME,
            )
        )

    def test_no_record_is_in_scope(self):
        # An assessor outage carries no judgement at all, so the prose may not
        # borrow one in either direction.
        self.assertTrue(
            support_guard.guard_applies(
                causal_support=support_guard.NO_RECORD,
                grounding=channel_assessor.GROUNDING_UNKNOWN,
                channel_text="",
                ticker=_ANY_TICKER,
                company_name=_ANY_NAME,
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

    def test_a_blank_or_non_string_field_is_skipped(self):
        violations = support_guard.check_support_language(
            {**_brief(), "bear_summary": "   ", "catalyst_failure_exit": None},
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        self.assertEqual(violations, [])

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

    def test_a_quoted_headline_naming_the_candidate_still_suppresses(self):
        """The quotation suppressor must stay reachable past the subject rule.

        The pre-existing quotation case used a headline about "quantum firms",
        which the subject rule now suppresses first — so without a case whose
        quoted text NAMES the candidate, the quotation arm would be shadowed
        and could rot to inert unnoticed.
        """
        text = 'The headline reads "QUBT benefits from the AI build-out".'
        self.assertEqual(self._fired(text), 0)
        violations = support_guard.check_support_language(
            _brief(tldr=text),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="QUBT",
        )
        self.assertEqual(violations[0].suppressed_by, support_guard.SUPPRESSED_BY_QUOTED)

    def test_a_field_with_no_trailing_punctuation_is_still_scanned(self):
        # The clause scan has to terminate at end-of-text, not only at a
        # boundary character; the model does drop the final full stop.
        self.assertEqual(self._fired("QUBT benefits from the theme"), 1)


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


class AHarmDirectionChannelPutsTheRowInScope(unittest.TestCase):
    """A record that describes HARM cannot support a benefit claim either.

    The support LEVEL says how well the chain is evidenced, never which way it
    points. So a `suggestive` + `grounded` row whose own channel text ends in
    "potential negative impact on revenue" was out of scope, and prose asserting
    a benefit shipped beside a record saying the opposite. Issue #1070.
    """

    def test_a_suggestive_grounded_harm_channel_is_in_scope(self):
        # The REAL subject, not the generic one: this record names Ollie's, and
        # the direction arm asks who is harmed. Passing a stranger's ticker here
        # would assert that the arm is inert, which is the opposite claim.
        self.assertTrue(
            support_guard.guard_applies(
                causal_support=channel_assessor.SUPPORT_SUGGESTIVE,
                grounding=channel_assessor.GROUNDING_GROUNDED,
                channel_text=_HARM_OLLI,
                ticker="OLLI",
                company_name="Ollie's Bargain Outlet",
            )
        )

    def test_benefit_prose_against_a_harm_channel_fires(self):
        violations = support_guard.check_support_language(
            _brief(tldr="Ollie's benefits from the theme and is positioned to win share."),
            causal_support=channel_assessor.SUPPORT_SUGGESTIVE,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="OLLI",
            company_name="Ollie's Bargain Outlet Holdings",
            channel_text=_HARM_OLLI,
        )
        fired = [v for v in violations if v.suppressed_by is None]
        self.assertEqual([v.field for v in fired], ["tldr"])

    def test_the_other_observed_harm_record_is_in_scope(self):
        # The arrow-chain shape, where the harm sits in the final arm and the
        # first two arms are about the macro economy.
        self.assertTrue(
            support_guard.channel_describes_harm(
                _HARM_GO, ticker="GO", company_name="Grocery Outlet"
            )
        )

    def test_a_hedged_adverse_chain_is_still_adverse(self):
        """The prose escape hatch has NO counterpart on the direction test.

        "could see reduced customer demand" carries a conditional cue, and the
        prose scan would suppress it. 21 of the 32 real records carry such a
        cue, because the assessor writes `suggestive` chains in modal language
        by construction — so reusing the suppressor would disarm the check on
        one of the only two known true positives. A hedged adverse mechanism is
        still an adverse mechanism.
        """
        self.assertTrue(
            support_guard.channel_describes_harm(
                _HARM_OLLI, ticker="OLLI", company_name="Ollie's Bargain Outlet"
            )
        )
        self.assertTrue(
            any(cue.search(_HARM_OLLI.lower()) for cue in support_guard._CONDITIONAL_RES),
            "the fixture must actually carry a conditional cue, or this proves nothing",
        )


class ABenefitDirectionChannelIsNeverPutInScopeByDirection(unittest.TestCase):
    """FALSE POSITIVES ARE THE WORSE FAILURE, and here they are measurable.

    Withholding prose from a row whose record genuinely supports it is the
    direction :func:`_suppressor` already calls the worse one. Unlike the
    true-positive side (N=2), this side has a real corpus: 32 distinct real
    benefit records, of which the seven quoted above are the shapes that break a
    naive lexicon.
    """

    def test_no_real_benefit_channel_reads_as_harm(self):
        for text in _REAL_BENEFIT_CHANNELS:
            with self.subTest(text=text[:60]):
                self.assertFalse(
                    support_guard.channel_describes_harm(
                        text, ticker=_ANY_TICKER, company_name=_ANY_NAME
                    )
                )

    def test_no_real_benefit_channel_puts_a_row_in_scope_at_any_support_level(self):
        for level in (
            channel_assessor.SUPPORT_ESTABLISHED,
            channel_assessor.SUPPORT_SUGGESTIVE,
        ):
            for text in _REAL_BENEFIT_CHANNELS:
                with self.subTest(level=level, text=text[:60]):
                    self.assertFalse(
                        support_guard.guard_applies(
                            causal_support=level,
                            grounding=channel_assessor.GROUNDING_GROUNDED,
                            channel_text=text,
                            ticker=_ANY_TICKER,
                            company_name=_ANY_NAME,
                        )
                    )

    def test_a_harm_arm_before_the_last_link_does_not_fire(self):
        """The discriminator, stated on its own.

        This is the real Savers Value Village record with its middle arm
        rewritten into the lexicon's own vocabulary. The chain still ENDS in
        revenue growth, so it is a benefit record — a scan over the whole text
        would invert it.
        """
        text = (
            "Softer retail sales -> reduced demand for new goods pushes shoppers "
            "to cheaper secondhand alternatives -> demand for Savers Value "
            "Village's thrift stores increases, boosting revenue in the near term."
        )
        self.assertTrue(
            support_guard._HARM_RE.search(text.lower()),
            "the fixture must carry a harm phrase somewhere, or this proves nothing",
        )
        self.assertFalse(
            support_guard.channel_describes_harm(text, ticker=_ANY_TICKER, company_name=_ANY_NAME)
        )

    def test_a_real_falsifier_does_not_read_as_harm(self):
        """The falsifier is outside the scanned text, and must stay outside.

        All 32 real falsifiers are written in non-occurrence language and 11 of
        them literally contain "not increase". Feeding one to the direction test
        must not fire, or a future refactor that widened the read would turn the
        check into an always-fire.
        """
        self.assertFalse(
            support_guard.channel_describes_harm(
                _REAL_FALSIFIER, ticker=_ANY_TICKER, company_name=_ANY_NAME
            )
        )

    def test_the_harm_lexicon_is_never_applied_to_the_prose(self):
        """The prompt TEACHES harm vocabulary into two of the guarded fields.

        "The bear case may cite the falsifier. The exit line is the falsifier
        rendered as an observable." So an exit line reading "if revenue does not
        increase" is the contract working as designed, and a row with a
        benefit-direction record must stay out of scope however its prose reads.
        """
        violations = support_guard.check_support_language(
            _brief(
                bear_summary="A weaker consumer could mean lower revenue and margin compression.",
                catalyst_failure_exit=(
                    "If Maravai's revenue from mRNA-related products does not "
                    "increase in the following quarters, the channel is invalidated."
                ),
            ),
            causal_support=channel_assessor.SUPPORT_SUGGESTIVE,
            grounding=channel_assessor.GROUNDING_GROUNDED,
            ticker="MRVI",
            company_name="Maravai LifeSciences Holdings",
            channel_text=_REAL_BENEFIT_CHANNELS[4],
        )
        self.assertEqual(violations, [])

    def test_an_absent_record_is_not_read_as_harm(self):
        """Absent evidence, not evidence of absence.

        The assessor blanks `channel_text` at `not_established`, and the
        `no_record` projection carries "". Both rows are already in scope on the
        level / grounding arms, so the empty default may only ever make the
        direction arm inert — never wrong.
        """
        self.assertFalse(
            support_guard.channel_describes_harm("", ticker=_ANY_TICKER, company_name=_ANY_NAME)
        )
        self.assertFalse(
            support_guard.guard_applies(
                causal_support=channel_assessor.SUPPORT_SUGGESTIVE,
                grounding=channel_assessor.GROUNDING_GROUNDED,
                channel_text="",
                ticker=_ANY_TICKER,
                company_name=_ANY_NAME,
            )
        )

    def test_the_pre_existing_scope_is_unchanged_by_a_benefit_channel(self):
        """Anti-regression: the direction arm WIDENS scope, it never narrows it."""
        for level, grounding in (
            (channel_assessor.SUPPORT_NOT_ESTABLISHED, channel_assessor.GROUNDING_GROUNDED),
            (support_guard.NO_RECORD, channel_assessor.GROUNDING_UNKNOWN),
            (channel_assessor.SUPPORT_ESTABLISHED, channel_assessor.GROUNDING_THEME_MISROUTE),
        ):
            with self.subTest(level=level, grounding=grounding):
                self.assertTrue(
                    support_guard.guard_applies(
                        causal_support=level,
                        grounding=grounding,
                        channel_text=_REAL_BENEFIT_CHANNELS[0],
                        ticker=_ANY_TICKER,
                        company_name=_ANY_NAME,
                    )
                )


class EveryHarmEntryMustBeAbleToFire(unittest.TestCase):
    """Structural anti-rot, mirroring ``EveryLexiconEntryMustBeAbleToFire``.

    An entry that can never match reads as covered. The benefit list had two
    such entries, for two different mechanical reasons, so this class is not a
    hypothetical.
    """

    #: An arrow chain whose FINAL arm hosts the phrase, so a failure here is the
    #: ENTRY's inability to fire and not the terminal-link rule.
    CARRIER = "The event -> weaker end markets -> {phrase} at this company."

    def test_each_harm_phrase_fires_on_a_minimal_carrier_chain(self):
        for phrase in support_guard.HARM_DIRECTION_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    support_guard.channel_describes_harm(
                        self.CARRIER.format(phrase=phrase),
                        ticker=_ANY_TICKER,
                        company_name=_ANY_NAME,
                    ),
                    f"{phrase!r} can never fire",
                )

    def test_a_carrier_without_the_phrase_does_not_fire(self):
        """POSITIVE CONTROL for the carrier itself: it must be inert on its own."""
        self.assertFalse(
            support_guard.channel_describes_harm(
                self.CARRIER.format(phrase="higher revenue and expanding margins"),
                ticker=_ANY_TICKER,
                company_name=_ANY_NAME,
            )
        )

    def test_the_harm_lexicon_carries_no_hyphens(self):
        # `_normalise` maps a hyphen to a space on both sides of the compare, so
        # a hyphenated ENTRY matches neither spelling of the text.
        self.assertEqual([p for p in support_guard.HARM_DIRECTION_PHRASES if "-" in p], [])

    def test_the_harm_lexicon_is_not_empty(self):
        # An empty tuple would compile to a regex that matches everything or
        # nothing depending on the join, and would silently disable the arm.
        self.assertGreater(len(support_guard.HARM_DIRECTION_PHRASES), 10)

    def test_a_hyphenated_record_still_fires_in_both_spellings(self):
        for text in (
            "Weak demand -> margin-compression at this company.",
            "Weak demand -> margin compression at this company.",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    support_guard.channel_describes_harm(
                        text, ticker=_ANY_TICKER, company_name=_ANY_NAME
                    )
                )


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


class TheDirectionTestAsksWhoIsHarmedNotWhereTheHarmSits(unittest.TestCase):
    """Regression suite for three defects the adversarial review demonstrated.

    The first implementation answered "is the harm to this company?" by POSITION
    — it scanned only the chain's last link. Position is a proxy for subject, and
    the review showed it is the wrong one in both directions on real wordings.

    The fix asks the question directly, reusing the prose arm's own machinery:
    a harm phrase counts only when the clause carrying it NAMES THE CANDIDATE and
    is not negated. Scope is then the whole record, because there is no longer
    anything for the scoping to protect against.
    """

    TICKER = "OLLI"
    NAME = "Ollie's Bargain Outlet"

    def _harm(self, text: str) -> bool:
        return support_guard.channel_describes_harm(
            text, ticker=self.TICKER, company_name=self.NAME
        )

    def test_a_denied_harm_does_not_fire(self):
        # "sees NO negative impact ... and gains share" is a benefit chain. The
        # prose arm has owned _NEGATION_RES since day one; the direction arm
        # shipped without it, so an explicit denial of harm read AS harm.
        self.assertFalse(
            self._harm(
                "Softer retail sales -> consumers pull back -> Ollie's Bargain Outlet "
                "sees no negative impact on revenue and gains share."
            )
        )

    def test_harm_to_a_rival_in_the_final_arm_does_not_fire(self):
        # The substitution / trade-down family: the mechanism IS a rival losing,
        # and the assessor prompt teaches exactly that shape. Whether the loser
        # lands in the middle arm or the final one is a wording coin flip, so
        # position cannot separate them — the subject can.
        self.assertFalse(
            self._harm(
                "Inflation drives trade-down -> Ollie's Bargain Outlet gains share "
                "while full-price retailers lose share."
            )
        )

    def test_harm_to_the_candidate_fires_wherever_it_sits(self):
        # The #1070 record, reworded so the harm clause is no longer the last
        # comma segment. The record still says harm to this company, so the
        # verdict must not depend on where the sentence ends.
        self.assertTrue(
            self._harm(
                "Softer retail sales suggest consumers are pulling back. This could "
                "reduce customer demand at Ollie's Bargain Outlet, though the timing "
                "is unclear."
            )
        )

    def test_the_original_1070_records_still_fire(self):
        # Both real harm records from the issue. Losing either would mean the
        # rewrite fixed the false positives by disarming the check.
        self.assertTrue(
            self._harm(
                "Softer retail sales and consumer sentiment data -> concerns about "
                "weakening consumer spending -> potential negative impact on revenue "
                "for small-cap retailers like Ollie's Bargain Outlet."
            )
        )
        self.assertTrue(
            self._harm(
                "As a small-cap retailer, Ollie's Bargain Outlet could see reduced "
                "customer demand, leading to lower revenue in the near term."
            )
        )

    def test_a_benefit_chain_opening_on_macro_harm_does_not_fire(self):
        # The shape the old scoping existed to protect: a real defensive-retail
        # chain OPENS on harm to the world and ends in the candidate gaining.
        # The subject test covers it without any scoping at all.
        self.assertFalse(
            self._harm(
                "Softer retail sales indicate consumers are reducing spending on new "
                "goods -> consumers trade down to cheaper alternatives -> demand at "
                "Ollie's Bargain Outlet increases."
            )
        )

    def test_an_unnamed_candidate_cannot_be_the_harmed_party(self):
        # No name anywhere: the record does not say harm to THIS company, so the
        # least-claiming answer is False. The support and grounding conditions
        # are what cover a row with no usable record.
        self.assertFalse(
            self._harm("Tariffs raise component prices, reducing demand across the sector.")
        )
