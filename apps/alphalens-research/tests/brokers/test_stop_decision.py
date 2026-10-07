"""The stop decision copied into the contract (intent-replay PR 2, #1573).

``broker_contract.stop_decision`` IS the daemon's two post-fill stop-move
arms since #1581: ``position_manager._maybe_trail`` and ``_maybe_reanchor``
call in here for the level, taking the nine-field view of spec section 3.2.
These tests pin it on its own, without importing the daemon, one guard per
test with a positive control beside it, plus the four golden cases of spec
section 6.2. What pins it against the daemon's RECORDED answers is the golden
corpus in ``tests/golden/``, which was captured before the arms delegated.

Three different meanings of ``None`` get three different test names, because
they are three different facts: a document that declared nothing (the
migration rule), a declared policy whose guard refused, and a primitive the
registry cannot honour, which the resolver DEGRADES to the inert policy. The
last one is the daemon's behaviour and the copy keeps it; the replay must
refuse such a document before it ever calls this function (spec section
4.3.1, the interpreter of PR 5).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import math
import textwrap
import unittest

from broker_contract.stop_decision import (
    TRAIL_STEP_EPS,
    StopDecision,
    StopDecisionView,
    _reanchor,
    _trail,
    compose_ratchet_floor,
    decide_reanchor_detail,
    decide_stop,
    decide_trail_detail,
)
from broker_contract.trade_intent.schema import ModelPush, ReanchorOnFill, TrailingStop

_TRAIL = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)
_REANCHOR = ReanchorOnFill(k_atr=1.5, atr=1.20)

_DEGENERATE = (0.0, -0.0, -1.0, float("nan"), float("inf"))


def _trail_view(**overrides: object) -> StopDecisionView:
    """avg 100, brief floor 90 (1R = 10), peak = last = 105 (+0.5R, the arming
    instant), a clean sole stop, no backoff, no trail history, no latch."""
    base: dict[str, object] = {
        "avg_price": 100.0,
        "peak": 105.0,
        "last_price": 105.0,
        "plan_stop": 90.0,
        "reaction": _TRAIL,
        "has_sole_standalone_stop": True,
        "amend_in_backoff": False,
        "ratchet_floor": None,
        "already_reanchored": False,
    }
    base.update(overrides)
    return StopDecisionView(**base)  # type: ignore[arg-type]


def _reanchor_view(**overrides: object) -> StopDecisionView:
    """Spec section 6.2 row 2: anchor 68.00, ATR 1.20, average fill 68.50,
    brief floor 66.20 (the planned ``68.00 - 1.5 * 1.20``)."""
    base: dict[str, object] = {
        "avg_price": 68.50,
        "peak": None,
        "last_price": None,
        "plan_stop": 66.20,
        "reaction": _REANCHOR,
        "has_sole_standalone_stop": True,
        "amend_in_backoff": False,
        "ratchet_floor": None,
        "already_reanchored": False,
    }
    base.update(overrides)
    return StopDecisionView(**base)  # type: ignore[arg-type]


class GoldenCasesTest(unittest.TestCase):
    """Spec section 6.2, computed with the real functions on 2026-09-23."""

    def test_trail_peak_at_half_r_places_entry_plus_0p3r(self) -> None:
        self.assertAlmostEqual(decide_stop(_trail_view()), 103.0, places=9)

    def test_trail_peak_a_tenth_below_activation_is_dark(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(peak=104.9, last_price=104.9)))

    def test_reanchor_moves_the_stop_to_avg_minus_k_atr(self) -> None:
        self.assertAlmostEqual(decide_stop(_reanchor_view()), 66.70, places=9)

    def test_reanchor_after_a_fill_better_than_the_anchor_is_refused_by_the_clamp(self) -> None:
        # 67.50 - 1.5 * 1.20 = 65.70, below the 66.20 brief floor.
        self.assertIsNone(decide_stop(_reanchor_view(avg_price=67.50)))


class DeclarationTest(unittest.TestCase):
    """What the document declared decides which arm runs, and whether any does."""

    def test_a_document_that_declared_nothing_never_moves_the_stop(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(reaction=None)))

    def test_an_unhonourable_reaction_degrades_to_the_inert_policy_like_the_daemon(self) -> None:
        """``resolve_declared_policy`` answers the inert policy for ``ModelPush``
        and for anything it does not recognise. The copy keeps that because the
        daemon has it; a replay must refuse such a document BEFORE calling here."""
        for reaction in (ModelPush(), "trailing_stop", {"kind": "trailing_stop"}, 42):
            with self.subTest(reaction=reaction):
                self.assertIsNone(decide_stop(_trail_view(reaction=reaction)))

    def test_declared_trail_parameters_are_honoured(self) -> None:
        strict = TrailingStop(arm_trigger_r=1.0, trail_frac=1.0)
        self.assertIsNone(decide_stop(_trail_view(reaction=strict)))  # +0.5R < +1R
        level = decide_stop(_trail_view(reaction=strict, peak=110.0, last_price=120.0))
        self.assertAlmostEqual(level, 110.0, places=9)  # keeps the whole gain

    def test_declared_reanchor_parameters_are_honoured(self) -> None:
        level = decide_stop(_reanchor_view(reaction=ReanchorOnFill(k_atr=1.0, atr=1.0)))
        self.assertAlmostEqual(level, 67.50, places=9)


class SharedGuardsTest(unittest.TestCase):
    """The four guards both arms share, in the daemon's order."""

    def test_a_degenerate_average_price_vetoes_both_arms(self) -> None:
        for avg in _DEGENERATE:
            with self.subTest(arm="trail", avg=avg):
                self.assertIsNone(decide_stop(_trail_view(avg_price=avg)))
            with self.subTest(arm="reanchor", avg=avg):
                self.assertIsNone(decide_stop(_reanchor_view(avg_price=avg)))

    def test_no_sole_standalone_stop_vetoes_both_arms(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(has_sole_standalone_stop=False)))
        self.assertIsNone(decide_stop(_reanchor_view(has_sole_standalone_stop=False)))

    def test_an_amend_in_backoff_vetoes_both_arms(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(amend_in_backoff=True)))
        self.assertIsNone(decide_stop(_reanchor_view(amend_in_backoff=True)))

    def test_positive_control_the_bases_do_move(self) -> None:
        self.assertIsNotNone(decide_stop(_trail_view()))
        self.assertIsNotNone(decide_stop(_reanchor_view()))


class TrailFeedVetoesTest(unittest.TestCase):
    def test_a_missing_or_degenerate_peak_is_a_feed_veto(self) -> None:
        for peak in (None, *_DEGENERATE):
            with self.subTest(peak=peak):
                self.assertIsNone(decide_stop(_trail_view(peak=peak)))

    def test_a_missing_or_degenerate_last_price_is_a_feed_veto(self) -> None:
        for last in (None, *_DEGENERATE):
            with self.subTest(last=last):
                self.assertIsNone(decide_stop(_trail_view(last_price=last)))

    def test_a_trail_needs_no_atr(self) -> None:
        # The declared trail carries none; the base view already trails.
        self.assertFalse(hasattr(_TRAIL, "atr"))
        self.assertIsNotNone(decide_stop(_trail_view()))


class TrailClampTest(unittest.TestCase):
    """The never-below-brief-floor envelope, anchored on the LIVE price."""

    def test_the_clamp_anchors_on_the_last_price_not_the_entry(self) -> None:
        # peak 110 -> proposed 106.0; with last 110 the 0.2% floor (109.78) is slack.
        self.assertAlmostEqual(
            decide_stop(_trail_view(peak=110.0, last_price=110.0)), 106.0, places=9
        )

    def test_the_stop_can_lock_profit_above_the_entry(self) -> None:
        self.assertGreater(decide_stop(_trail_view(peak=110.0, last_price=110.0)), 100.0)

    def test_a_close_last_price_binds_the_clamp(self) -> None:
        # proposed 106.0 but 104 * 0.998 = 103.792 caps it.
        self.assertAlmostEqual(
            decide_stop(_trail_view(peak=110.0, last_price=104.0)), 103.792, places=9
        )

    def test_the_clamp_refuses_when_the_live_anchor_falls_below_the_brief_floor(self) -> None:
        """Reachable on the trail arm ONLY through the live-price anchor:
        ``0.998 * last_price < plan_stop``. The proposal itself never sits below
        the entry, so ``plan_stop > proposed`` is not how this refusal happens."""
        self.assertIsNone(decide_stop(_trail_view(peak=110.0, last_price=90.0)))

    def test_positive_control_a_live_anchor_just_above_the_floor_is_allowed(self) -> None:
        self.assertAlmostEqual(
            decide_stop(_trail_view(peak=110.0, last_price=90.2)), 90.0196, places=9
        )

    def test_zero_or_negative_risk_is_dark(self) -> None:
        for plan_stop in (100.0, 110.0):
            with self.subTest(plan_stop=plan_stop):
                self.assertIsNone(
                    decide_stop(_trail_view(plan_stop=plan_stop, peak=150.0, last_price=150.0))
                )

    def test_a_degenerate_brief_floor_vetoes_both_arms(self) -> None:
        for plan_stop in _DEGENERATE:
            with self.subTest(arm="trail", plan_stop=plan_stop):
                self.assertIsNone(decide_stop(_trail_view(plan_stop=plan_stop)))
            with self.subTest(arm="reanchor", plan_stop=plan_stop):
                self.assertIsNone(decide_stop(_reanchor_view(plan_stop=plan_stop)))


class TrailRatchetTest(unittest.TestCase):
    """Never down versus the trail history: the CLAMPED level must clear the last
    trailed level by ``TRAIL_STEP_EPS``. The base view places 103.0."""

    def test_inside_the_step_below_the_level_is_vetoed(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(ratchet_floor=102.985)))

    def test_inside_the_step_above_the_level_is_vetoed(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(ratchet_floor=103.01)))

    def test_a_floor_far_above_the_level_is_vetoed(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(ratchet_floor=200.0)))

    def test_exactly_one_step_below_is_still_vetoed(self) -> None:
        """The daemon compares with ``<=``; ``(h - 0.02) + 0.02 == h`` is exact
        for every level at or above 1.0."""
        self.assertIsNone(decide_stop(_trail_view(ratchet_floor=103.0 - TRAIL_STEP_EPS)))

    def test_clearing_the_step_fires(self) -> None:
        self.assertAlmostEqual(decide_stop(_trail_view(ratchet_floor=102.975)), 103.0, places=9)

    def test_a_floor_far_below_fires(self) -> None:
        self.assertAlmostEqual(decide_stop(_trail_view(ratchet_floor=50.0)), 103.0, places=9)

    def test_the_ratchet_gates_on_the_clamped_level_not_the_proposal(self) -> None:
        # proposed 106.0 clears 103.78 + eps; the clamped 103.792 does not.
        self.assertIsNone(
            decide_stop(_trail_view(peak=110.0, last_price=104.0, ratchet_floor=103.78))
        )

    def test_a_nan_floor_vetoes(self) -> None:
        """#1673. This test read the other way until the hardening its own
        comment called for landed: the copied daemon compared against the floor
        with no ``isfinite`` guard, and ``x <= nan + eps`` is False, so a NaN
        floor let every proposal through.

        A non-finite floor now REFUSES rather than being ignored, and the
        direction is the safe one. This function cannot recover the real floor
        from a composed value, so the two readings are "a floor was stated and
        its value is nonsense, do not move the stop" and "move the stop as
        though no floor existed". Only the first is safe, and it already is
        what an infinite floor did."""
        self.assertIsNone(decide_stop(_trail_view(ratchet_floor=float("nan"))))

    def test_an_infinite_floor_vetoes(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(ratchet_floor=float("inf"))))
        self.assertIsNone(decide_stop(_trail_view(ratchet_floor=float("-inf"))))

    def test_a_finite_floor_far_below_still_fires(self) -> None:
        """Positive control for the two vetoes above: the refusal is about the
        value being non-finite, not about this view refusing to move."""
        self.assertAlmostEqual(decide_stop(_trail_view(ratchet_floor=1.0)), 103.0, places=9)


class ComposeRatchetFloorTest(unittest.TestCase):
    """``compose_ratchet_floor`` is where the two floors meet (#1673).

    It exists because the composition used to be written out at each call site
    and the rule it had to follow was subtle enough to get wrong: the daemon
    ratchets against the HIGHER of the level a trail last moved the stop to and
    the level the stop is RESTING at, and one of those two arrives from a
    journal fold that can carry a corrupt number. Composing with a bare ``max``
    propagates it, and ``max`` is not even symmetric on a NaN input, so the two
    orders of the same two floors disagreed.
    """

    def test_the_higher_of_two_levels_wins(self) -> None:
        self.assertEqual(compose_ratchet_floor(55.0, 56.0), 56.0)
        self.assertEqual(compose_ratchet_floor(56.0, 55.0), 56.0)

    def test_no_levels_at_all_is_no_floor(self) -> None:
        self.assertIsNone(compose_ratchet_floor())
        self.assertIsNone(compose_ratchet_floor(None, None))

    def test_a_missing_level_does_not_hide_the_one_that_is_there(self) -> None:
        self.assertEqual(compose_ratchet_floor(None, 56.0), 56.0)
        self.assertEqual(compose_ratchet_floor(56.0, None), 56.0)

    def test_a_level_of_exactly_zero_is_a_floor_not_an_absence(self) -> None:
        """Filtered on ``is not None`` and on finiteness, never on truthiness:
        ``0.0`` is falsy and is a real level. ``tests/brokers/automanager/
        test_kept_stop_level.py`` is where that matters observably, at penny
        prices, where a zero floor can veto."""
        self.assertEqual(compose_ratchet_floor(0.0), 0.0)
        self.assertEqual(compose_ratchet_floor(0.0, None), 0.0)

    def test_a_non_finite_level_is_dropped(self) -> None:
        for degenerate in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(level=degenerate):
                self.assertEqual(compose_ratchet_floor(degenerate, 56.0), 56.0)
                self.assertIsNone(compose_ratchet_floor(degenerate))

    def test_the_result_does_not_depend_on_the_argument_order(self) -> None:
        """The property that replaces the one golden case able to kill a
        reversed composition. ``max(nan, 56.0)`` is ``nan`` while
        ``max(56.0, nan)`` is ``56.0``; after the filter both are ``56.0``."""
        for pair in ((float("nan"), 56.0), (float("inf"), 56.0), (None, 56.0), (55.0, 56.0)):
            with self.subTest(pair=pair):
                self.assertEqual(
                    compose_ratchet_floor(*pair), compose_ratchet_floor(*reversed(pair))
                )

    def test_a_negative_level_is_kept(self) -> None:
        """Only non-finite values are dropped. A negative floor is a finite
        number this function has no business judging: the daemon's resting-price
        input is already filtered for positivity by its own caller, and the
        envelope refuses a non-positive brief floor before the ratchet is ever
        reached."""
        self.assertEqual(compose_ratchet_floor(-1.0), -1.0)


class ReanchorArmTest(unittest.TestCase):
    def test_a_latched_fill_is_not_reanchored_twice(self) -> None:
        self.assertIsNone(decide_stop(_reanchor_view(already_reanchored=True)))

    def test_a_degenerate_atr_is_refused_by_the_policy(self) -> None:
        for atr in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(atr=atr):
                view = _reanchor_view(reaction=ReanchorOnFill(k_atr=1.5, atr=atr))
                self.assertIsNone(decide_stop(view))

    def test_positive_control_a_finite_atr_on_the_same_shape_does_reanchor(self) -> None:
        view = _reanchor_view(reaction=ReanchorOnFill(k_atr=1.5, atr=1.0))
        self.assertAlmostEqual(decide_stop(view), 67.0, places=9)

    def test_the_clamp_anchors_on_the_average_fill(self) -> None:
        # A tiny ATR proposes 68.49, above the 0.2% floor 68.363 -> capped there.
        view = _reanchor_view(reaction=ReanchorOnFill(k_atr=1.0, atr=0.01))
        self.assertAlmostEqual(decide_stop(view), 68.50 * 0.998, places=9)

    def test_the_reanchor_arm_ignores_the_trail_inputs(self) -> None:
        base = decide_stop(_reanchor_view())
        for overrides in (
            {"peak": 999.0, "last_price": 999.0},
            {"peak": float("nan"), "last_price": 0.0},
            {"ratchet_floor": 999.0},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(decide_stop(_reanchor_view(**overrides)), base)


class ShapeTest(unittest.TestCase):
    def test_the_view_is_frozen_and_slotted(self) -> None:
        view = _trail_view()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.avg_price = 1.0  # type: ignore[misc]
        self.assertFalse(hasattr(view, "__dict__"))

    def test_the_view_has_exactly_the_nine_spec_fields(self) -> None:
        self.assertEqual(
            [f.name for f in dataclasses.fields(StopDecisionView)],
            [
                "avg_price",
                "peak",
                "last_price",
                "plan_stop",
                "reaction",
                "has_sole_standalone_stop",
                "amend_in_backoff",
                "ratchet_floor",
                "already_reanchored",
            ],
        )

    def test_the_ratchet_step_is_two_cents(self) -> None:
        self.assertEqual(TRAIL_STEP_EPS, 0.02)

    def test_the_answer_is_a_finite_float_never_an_action(self) -> None:
        for view in (_trail_view(), _reanchor_view()):
            level = decide_stop(view)
            self.assertIsInstance(level, float)
            self.assertTrue(math.isfinite(level))

    def test_the_same_view_gives_the_same_answer(self) -> None:
        view = _trail_view()
        self.assertEqual(decide_stop(view), decide_stop(view))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheDetailReportsTheLevelsTheDecisionUsedTest(unittest.TestCase):
    """#1581 step 2: the daemon needs more than the price it places.

    Its re-anchor arm compares the CLAMPED level against the RAW PROPOSAL and,
    when they differ, carries that divergence to the journal (#1015); both arms
    log a refusal that prints the proposal. A float answer cannot supply any of
    it, and recomputing the proposal beside the decision cannot say WHICH guard
    refused: measured on the trail arm, 394 of 458 post-proposal ``None``
    answers are RATCHET refusals rather than clamp refusals, so a
    recompute-and-guess log line would be wrong 86 per cent of the time.

    So the leaf reports its own two levels. ``decide_stop`` keeps its exact
    signature, because a second program calls it and renders its answer into a
    schema-versioned published result.
    """

    def test_a_moved_stop_reports_the_proposal_and_the_level_it_placed(self) -> None:
        detail = decide_trail_detail(_trail_view())
        self.assertIsInstance(detail, StopDecision)
        self.assertEqual(detail.proposed, 103.0)
        self.assertEqual(detail.clamped, 103.0)
        self.assertEqual(detail.level, 103.0)

    def test_an_envelope_that_MOVED_the_target_reports_both_levels(self) -> None:
        """The #1015 case, and the only one where ``proposed`` and ``clamped``
        both exist and disagree. The daemon's re-anchor arm compares them with
        ``math.isclose`` and, when they differ, carries the divergence to the
        append-only journal. Every other case here has them equal or has one of
        them absent, so without this case a record that reported ``proposed``
        as a copy of ``clamped`` would pass.

        Built from the arithmetic: a tiny ATR distance puts the target at 68.488,
        inside the policy's 0.002 min-distance envelope around the 68.50 average
        fill, so the envelope pulls it to 68.363 without refusing it."""
        detail = decide_reanchor_detail(
            _reanchor_view(reaction=ReanchorOnFill(k_atr=0.01, atr=1.20))
        )
        self.assertEqual(detail.proposed, 68.488)
        self.assertEqual(detail.clamped, 68.363)
        self.assertEqual(detail.level, 68.363)
        self.assertNotEqual(detail.proposed, detail.clamped)

    def test_the_trail_envelope_also_moves_targets_without_refusing_them(self) -> None:
        """The same divergence on the TRAIL arm, where nothing records it today.

        A pullback puts the live-price anchor below the policy's target: peak
        110.00 with a live price of 101.00 proposes 106.00 and the 0.002
        min-distance envelope places 100.798 instead. The daemon stamps no
        journal record here and logs nothing -- that asymmetry with the
        re-anchor arm is #1674 -- so this field is the only thing that reports
        it, and without this case a record copying ``clamped`` into
        ``proposed`` passes on this arm."""
        detail = decide_trail_detail(_trail_view(peak=110.0, last_price=101.0))
        self.assertEqual(detail.proposed, 106.0)
        self.assertEqual(detail.clamped, 100.798)
        self.assertEqual(detail.level, 100.798)

    def test_a_clamp_refusal_reports_a_proposal_and_no_clamped_level(self) -> None:
        """The condition the daemon's refusal log fires on: a proposal exists
        and the never-below-brief-floor envelope refused it."""
        detail = decide_trail_detail(_trail_view(plan_stop=99.0, peak=110.0, last_price=96.0))
        self.assertEqual(detail.proposed, 106.0)
        self.assertIsNone(detail.clamped)
        self.assertIsNone(detail.level)

    def test_a_ratchet_refusal_reports_a_clamped_level_and_still_no_answer(self) -> None:
        """The case that makes the refusal predicate EXACT, and the reason the
        leaf reports two levels instead of one.

        The ratchet is the LAST gate, after the clamp, and it refuses in
        SILENCE -- the daemon logs nothing here. So a refusal is a clamp refusal
        when ``clamped is None and proposed is not None``, and a ratchet refusal
        when ``clamped`` survives and ``level`` does not. Collapse the two into
        one field and the daemon cannot tell them apart."""
        detail = decide_trail_detail(_trail_view(ratchet_floor=102.99))
        self.assertEqual(detail.proposed, 103.0)
        self.assertEqual(detail.clamped, 103.0)
        self.assertIsNone(detail.level)

    def test_a_policy_that_is_dark_reports_no_proposal_at_all(self) -> None:
        """Before activation the policy returns no target, so there is nothing
        to log and nothing to compare: ``proposed`` is absent, not refused."""
        detail = decide_trail_detail(_trail_view(peak=100.1, last_price=100.1))
        self.assertIsNone(detail.proposed)
        self.assertIsNone(detail.clamped)
        self.assertIsNone(detail.level)

    def test_a_guard_veto_reports_nothing(self) -> None:
        detail = decide_trail_detail(_trail_view(has_sole_standalone_stop=False))
        self.assertEqual((detail.proposed, detail.clamped, detail.level), (None, None, None))

    def test_the_reanchor_detail_never_runs_the_trail_logic(self) -> None:
        """TWO detail functions, not one routed on ``policy.trails``.

        ``breakeven_trail`` carries ``trails=True`` AND
        ``requires_amend_stop=True``, so a trailing declaration passes the
        re-anchor arm's own guard. A single routed entry point would then run
        the TRAIL branch where the daemon runs the re-anchor branch, which
        answers ``None`` because the policy refuses without a peak. That is a
        behaviour change, and it is reachable: four test modules call the arms
        directly rather than through the router."""
        # The view must carry a peak and a live price, or this test is VACUOUS:
        # without them the trail branch vetoes on the feed and answers None too,
        # so a routed entry point would give the same answer and the mutation
        # would survive. Measured -- it did, until this view grew a peak.
        view = _reanchor_view(reaction=_TRAIL, peak=105.0, last_price=105.0)
        self.assertIsNone(decide_reanchor_detail(view).level)
        # ... while the trail branch on that SAME view answers 90.4. That gap is
        # what a single function routed on ``policy.trails`` would place where
        # the daemon's re-anchor arm places nothing.
        self.assertEqual(decide_trail_detail(view).level, 90.4)

    def test_the_trail_detail_never_runs_the_reanchor_logic(self) -> None:
        """The dual of the test above, and the reason the trails guard lives
        INSIDE the leaf's trail branch rather than only in ``decide_stop``'s
        routing. ``decide_trail_detail`` is called by the daemon's
        ``_maybe_trail``, which has that guard, and four test modules call that
        arm directly with whatever declaration they like. A trail entry point
        that trusted its caller to have routed would answer for a declaration
        the arm refuses."""
        self.assertIsNone(decide_trail_detail(_trail_view(reaction=_REANCHOR)).level)
        # Positive control: the re-anchor branch on that same declaration answers.
        self.assertIsNotNone(decide_reanchor_detail(_reanchor_view()).level)

    def test_decide_stop_answers_exactly_the_detail_it_routes_to(self) -> None:
        """The wrapper cannot drift from the two functions, because it IS them."""
        for name, view, detail in (
            ("trail", _trail_view(), decide_trail_detail(_trail_view())),
            ("reanchor", _reanchor_view(), decide_reanchor_detail(_reanchor_view())),
        ):
            with self.subTest(arm=name):
                self.assertEqual(decide_stop(view), detail.level)

    def test_the_record_is_frozen(self) -> None:
        detail = decide_trail_detail(_trail_view())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            detail.level = 1.0  # type: ignore[misc]


def _executable_source(fn: object) -> str:
    """The function's source with every docstring removed, so a NAME mentioned
    in prose does not read as a name the code uses. Same device as
    ``tests/brokers/automanager/test_no_exit_policy_sentinel.py``."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))  # type: ignore[arg-type]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class EachBranchReadsOnlyTheFieldsItsCallerProjectsTest(unittest.TestCase):
    """Two of the view's nine fields are passed as CONSTANTS by the daemon.

    ``_maybe_trail`` passes ``already_reanchored=False`` because the trail arm
    has no idempotence latch, and ``_maybe_reanchor`` passes
    ``ratchet_floor=None`` because the re-anchor arm has no ratchet. Both are
    correct, and a comment saying so would go stale in silence: the day a latch
    is added to the trail branch, the daemon would keep passing ``False`` and
    the latch would never fire, on a live money path.

    So the claim is a predicate instead of a comment. If either assertion goes
    red, the branch gained a field its caller hard-codes, and the caller in
    ``position_manager`` is what has to change.
    """

    def test_the_trail_branch_does_not_read_the_reanchor_latch(self) -> None:
        self.assertNotIn(
            "already_reanchored",
            _executable_source(_trail),
            msg=(
                "_trail now reads already_reanchored, which _maybe_trail passes "
                "as a hard-coded False. Project the real value there first."
            ),
        )

    def test_the_reanchor_branch_does_not_read_the_ratchet_floor(self) -> None:
        self.assertNotIn(
            "ratchet_floor",
            _executable_source(_reanchor),
            msg=(
                "_reanchor now reads ratchet_floor, which _maybe_reanchor passes "
                "as a hard-coded None. Compose the real floor there first."
            ),
        )

    def test_each_branch_does_read_the_field_the_other_ignores(self) -> None:
        """Existence control. Without it both assertions above would still pass
        against a pair of branches that read no view fields at all."""
        self.assertIn("ratchet_floor", _executable_source(_trail))
        self.assertIn("already_reanchored", _executable_source(_reanchor))
