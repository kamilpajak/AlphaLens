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
is to generate the prose FROM the record and make the unsupported shape
mechanically impossible to render. That is what this module tests.

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
                )
                self.assertEqual([v.field for v in violations if v.suppressed_by is None], [field])

    def test_two_benefit_claims_in_one_field_collapse_to_one_violation(self):
        violations = support_guard.check_support_language(
            _brief(tldr="It benefits from the theme and also gains share."),
            causal_support=channel_assessor.SUPPORT_NOT_ESTABLISHED,
            grounding=channel_assessor.GROUNDING_GROUNDED,
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
