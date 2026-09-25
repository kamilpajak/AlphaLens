"""The stop decision copied into the contract (intent-replay PR 2, #1573).

``broker_contract.stop_decision.decide_stop`` is a COPY of the daemon's two
post-fill stop-move arms (``position_manager._maybe_trail`` and
``_maybe_reanchor``), guard for guard, taking the nine-field view of spec
section 3.2 and answering a price or ``None``. It is a copy, not an
extraction: until step 2 (#1581) two implementations stand and the parity
property in ``tests/property/test_stop_decision_parity.py`` holds them
together. These tests pin the copy on its own, without importing the daemon,
one guard per test with a positive control beside it, plus the four golden
cases of spec section 6.2.

Three different meanings of ``None`` get three different test names, because
they are three different facts: a document that declared nothing (the
migration rule), a declared policy whose guard refused, and a primitive the
registry cannot honour, which the resolver DEGRADES to the inert policy. The
last one is the daemon's behaviour and the copy keeps it; the replay must
refuse such a document before it ever calls this function (spec section
4.3.1, the interpreter of PR 5).
"""

from __future__ import annotations

import dataclasses
import math
import unittest

from broker_contract.stop_decision import TRAIL_STEP_EPS, StopDecisionView, decide_stop
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
        "last_trailed_level": None,
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
        "last_trailed_level": None,
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
        self.assertIsNone(decide_stop(_trail_view(last_trailed_level=102.985)))

    def test_inside_the_step_above_the_level_is_vetoed(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(last_trailed_level=103.01)))

    def test_a_floor_far_above_the_level_is_vetoed(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(last_trailed_level=200.0)))

    def test_exactly_one_step_below_is_still_vetoed(self) -> None:
        """The daemon compares with ``<=``; ``(h - 0.02) + 0.02 == h`` is exact
        for every level at or above 1.0."""
        self.assertIsNone(decide_stop(_trail_view(last_trailed_level=103.0 - TRAIL_STEP_EPS)))

    def test_clearing_the_step_fires(self) -> None:
        self.assertAlmostEqual(
            decide_stop(_trail_view(last_trailed_level=102.975)), 103.0, places=9
        )

    def test_a_floor_far_below_fires(self) -> None:
        self.assertAlmostEqual(decide_stop(_trail_view(last_trailed_level=50.0)), 103.0, places=9)

    def test_the_ratchet_gates_on_the_clamped_level_not_the_proposal(self) -> None:
        # proposed 106.0 clears 103.78 + eps; the clamped 103.792 does not.
        self.assertIsNone(
            decide_stop(_trail_view(peak=110.0, last_price=104.0, last_trailed_level=103.78))
        )

    def test_a_nan_floor_does_not_ratchet(self) -> None:
        """Copied as is: the daemon compares against the floor without an
        ``isfinite`` guard, and ``x <= nan + eps`` is False. Hardening belongs
        to step 2, when one implementation remains."""
        self.assertAlmostEqual(
            decide_stop(_trail_view(last_trailed_level=float("nan"))), 103.0, places=9
        )

    def test_an_infinite_floor_vetoes(self) -> None:
        self.assertIsNone(decide_stop(_trail_view(last_trailed_level=float("inf"))))


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
            {"last_trailed_level": 999.0},
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
                "last_trailed_level",
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
