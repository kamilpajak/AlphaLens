"""Parity between the daemon's stop decision and its copy in the contract
(intent-replay design section 6.1; PR 2 of #1571).

Step 1 of the design leaves TWO implementations of the post-fill stop decision
standing: ``position_manager._maybe_trail`` / ``_maybe_reanchor`` in the live
daemon and ``broker_contract.stop_decision.decide_stop``, copied from them.
Their agreement is a claim, so it is tested: one strategy draws a covered long
and builds BOTH the daemon's inputs (``Position``, ``PlannedExit``, the resting
sell legs, a ``ProtectionView`` for one uic) and the nine-field
``StopDecisionView`` from the same draws; the property requires the two to
answer the same level or both ``None``.

This is a DIFFERENTIAL test of two production paths, on purpose and against
the "oracle independence" rule the other property modules state. The design
mandates it (section 6.1: "it must import the live daemon composition, not a
copy"), and ``test_vol_target_properties.TestScaleSeriesVsScaleFactor`` is the
precedent. Nothing here recomputes a stop level: contract leaves are called
only to PLACE draws near interesting values and to LABEL outcomes, never on
the assertion path.

Retirement predicate (#1581): this module is DELETED in the PR in which
``position_manager`` calls ``broker_contract.stop_decision``. From then on it
would compare the leaf with itself.

What it does not compare (section 6.1): the native entry trail (no local
implementation to disagree with), the three predicate results the view carries
as booleans (they are INPUTS here; a replay passes "no broker obstacle" and
reports the optimism as a divergence), and the intra-bar tie convention.

Every property is a method on a ``TestCase`` (``unittest discover`` skips
bare functions), and the profile is loaded at import by ``base``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.brokers.automanager import position_manager as pm
from alphalens_pipeline.brokers.automanager.position_manager import (
    AmendStop,
    PlannedExit,
    ProtectionView,
    _maybe_reanchor,
    _maybe_trail,
    _sole_standalone_stop,
)
from broker_contract.contract import _QTY_EPS, InstrumentRef, OrderState, OrderStatus, Position
from broker_contract.exit_geometry.levels import clamp_reanchor_target, fractional_giveback_target
from broker_contract.exit_geometry.registry import resolve_declared_policy
from broker_contract.stop_decision import TRAIL_STEP_EPS, StopDecisionView, decide_stop
from broker_contract.trade_intent.schema import ModelPush, ReanchorOnFill, TrailingStop
from hypothesis import event, given, settings
from hypothesis import strategies as st

from .base import PropertyTestCase

_UIC = 43070
_OTHER_UIC = _UIC + 1
_ABSENT = object()  # "no entry for this uic in the per-uic map"

_NON_FINITE_OR_ZERO = (0.0, -1.0, float("nan"), float("inf"))


# ---------------------------------------------------------------------------
# Daemon-side factories (the shapes tests/brokers/automanager/test_maybe_trail.py uses).
# ---------------------------------------------------------------------------


def _instrument(uic: int) -> InstrumentRef:
    return InstrumentRef(
        ticker="BIO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id=str(uic),
        broker_symbol="BIO:xnys",
    )


def _mk_pos(avg_price: float) -> Position:
    return Position(
        instrument=_instrument(_UIC),
        quantity=7.0,
        avg_price=avg_price,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-1",
    )


def _mk_leg(
    order_id: str,
    order_type: str | None,
    *,
    filled: float = 0.0,
    ref: str | None = None,
    relation: str | None = None,
) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=filled,
        raw_status="Working",
        uic=_UIC,
        side="SELL",
        order_type=order_type,
        amount=7.0,
        external_reference=order_id if ref is None else ref,
        order_relation=relation,
    )


def _mk_plan(stop_price: float, reaction: Any) -> PlannedExit:
    return PlannedExit(
        uic=_UIC,
        entry_crid="crid",
        side="SELL",
        stop_price=stop_price,
        tp_price=None,
        conflicting=False,
        n_plans=1,
        reaction=reaction,
    )


# ---------------------------------------------------------------------------
# The strategy: nine axes, each with one CLEAN arm and a FULL arm list.
# ---------------------------------------------------------------------------

_ARMS: dict[str, tuple[str, ...]] = {
    "reaction": (
        "trailing",
        "none",
        "reanchor_shallow",
        "reanchor_deep",
        "reanchor_free",
        "reanchor_degenerate_atr",
        "model_push",
        "trailing_degenerate_params",
    ),
    "avg": ("clean", "degenerate"),
    "plan_stop": ("below_avg", "equal_avg", "above_avg", "degenerate"),
    "peak": (
        "above_activation",
        "far_above",
        "exact_activation",
        "just_below_activation",
        "below_activation",
        "absent",
        "other_uic",
        "degenerate",
    ),
    "last": (
        "at_or_below_peak",
        "above_peak",
        "at_or_below_avg",
        "below_floor",
        "absent",
        "other_uic",
        "degenerate",
    ),
    "legs": (
        "sole_clean",
        "none",
        "two_stops",
        "stop_plus_tp",
        "oco_relation",
        "oco_ref_infix",
        "partial_fill",
        "stop_plus_stray",
    ),
    "backoff": ("off", "on", "other_uic"),
    "floor": (
        "absent",
        "far_below",
        "band_below",
        "band_above",
        "boundary",
        "clears_step",
        "above_level",
        "other_uic",
        "degenerate",
    ),
    "latch": ("absent", "hit", "hit_within_tolerance", "miss", "other_uic"),
}
_AXES = tuple(_ARMS)
# The first arm of every axis is the one that passes its own guard.
_CLEAN = {axis: arms[0] for axis, arms in _ARMS.items()}


@dataclass(frozen=True)
class _Case:
    uic: int
    pos: Position
    plan: PlannedExit
    legs: tuple[OrderState, ...]
    view: ProtectionView
    sdv: StopDecisionView
    labels: frozenset[str]


def _usable(value: Any) -> bool:
    return isinstance(value, float) and pm._finite_positive(value)


@st.composite
def _case(draw: st.DrawFn, *, focus: str, force_arm: str | None = None) -> _Case:
    """One covered long. ``focus`` names the axis that draws from its FULL arm
    list (or every axis, for ``"all"``); the others stay on their clean arm.
    ``force_arm`` pins the focused axis to one arm, for the coverage suite."""
    labels: set[str] = set()

    def arm_of(axis: str) -> str:
        if axis == focus and force_arm is not None:
            arm = force_arm
        elif focus in (axis, "all"):
            arm = draw(st.sampled_from(_ARMS[axis]))
        elif axis == "reaction" and focus == "latch":
            # The latch is read by the re-anchor arm only; a trailing document
            # would leave every latch arm unobserved (a mutant that dropped the
            # guard survived 300 examples before this branch existed).
            arm = "reanchor_shallow"
        else:
            arm = _CLEAN[axis]
        labels.add(f"{axis}:{arm}")
        return arm

    def floats(lo: float, hi: float) -> float:
        return draw(st.floats(min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False))

    # --- avg ---------------------------------------------------------------
    avg_arm = arm_of("avg")
    avg = (
        floats(1.0, 1e4)
        if avg_arm == "clean"
        else draw(st.sampled_from([0.0, -0.0, -5.0, *_NON_FINITE_OR_ZERO[2:], float("-inf")]))
    )
    anchor = avg if _usable(avg) else 100.0  # placement anchor when avg is degenerate

    # --- plan_stop ---------------------------------------------------------
    plan_arm = arm_of("plan_stop")
    if plan_arm == "below_avg":
        plan_stop = anchor * floats(0.5, 0.95)
    elif plan_arm == "equal_avg":
        plan_stop = avg
    elif plan_arm == "above_avg":
        plan_stop = anchor * floats(1.0001, 1.5)
    else:
        plan_stop = draw(st.sampled_from(_NON_FINITE_OR_ZERO))
    floor_hint = plan_stop if _usable(plan_stop) else 0.9 * anchor
    risk = anchor - floor_hint

    # --- reaction ----------------------------------------------------------
    reaction_arm = arm_of("reaction")
    reaction: Any
    if reaction_arm == "trailing":
        reaction = TrailingStop(
            arm_trigger_r=draw(st.one_of(st.just(0.5), st.floats(0.1, 2.0))),
            trail_frac=draw(st.one_of(st.just(0.6), st.floats(0.05, 1.0))),
        )
    elif reaction_arm == "none":
        reaction = None
    elif reaction_arm == "reanchor_shallow":
        # k*atr = a fraction of the brief risk: the target lands above the floor
        # and below the 0.2% envelope, so the arm places a level by construction.
        k_atr = draw(st.one_of(st.just(1.5), st.floats(0.5, 3.0)))
        reaction = ReanchorOnFill(k_atr=k_atr, atr=abs(risk) * floats(0.05, 0.9) / k_atr)
    elif reaction_arm == "reanchor_deep":
        # k*atr beyond the brief risk: the clamp refuses (section 6.2 row 1).
        k_atr = draw(st.one_of(st.just(1.5), st.floats(0.5, 3.0)))
        reaction = ReanchorOnFill(k_atr=k_atr, atr=abs(risk) * floats(1.0, 3.0) / k_atr + 1e-3)
    elif reaction_arm == "reanchor_free":
        reaction = ReanchorOnFill(k_atr=floats(0.5, 3.0), atr=floats(0.1, 50.0))
    elif reaction_arm == "reanchor_degenerate_atr":
        reaction = ReanchorOnFill(k_atr=1.5, atr=draw(st.sampled_from(_NON_FINITE_OR_ZERO)))
    elif reaction_arm == "model_push":
        reaction = ModelPush()
    else:
        reaction = TrailingStop(
            arm_trigger_r=draw(st.sampled_from([float("nan"), -1.0, 0.0])),
            trail_frac=draw(st.sampled_from([0.0, 1.5, float("nan")])),
        )

    # --- peak (placed off the activation threshold, the policy's own expression) ---
    r = (
        reaction.arm_trigger_r
        if isinstance(reaction, TrailingStop) and _usable(reaction.arm_trigger_r)
        else 0.5
    )
    activation = anchor + r * risk
    if not _usable(activation):
        activation = anchor
    peak_arm = arm_of("peak")
    peak: Any = _ABSENT
    if peak_arm == "above_activation":
        peak = activation + floats(0.0, 0.5 * anchor)
    elif peak_arm == "far_above":
        peak = activation * floats(1.5, 5.0)
    elif peak_arm == "exact_activation":
        peak = activation
    elif peak_arm == "just_below_activation":
        peak = activation - floats(1e-9, 1e-3)
    elif peak_arm == "below_activation":
        peak = activation * floats(0.5, 0.999)
    elif peak_arm == "degenerate":
        peak = draw(st.sampled_from(_NON_FINITE_OR_ZERO))
    peak_map: dict[int, float] = {}
    if peak_arm == "other_uic":
        peak_map[_OTHER_UIC] = anchor
    elif peak is not _ABSENT:
        peak_map[_UIC] = peak
    peak_hint = peak if _usable(peak) else anchor

    # --- last price --------------------------------------------------------
    last_arm = arm_of("last")
    last: Any = _ABSENT
    if last_arm == "at_or_below_peak":
        last = peak_hint * floats(0.995, 1.0)
    elif last_arm == "above_peak":
        last = peak_hint * floats(1.0, 1.5)
    elif last_arm == "at_or_below_avg":
        last = anchor * floats(0.5, 1.0)
    elif last_arm == "below_floor":
        last = floor_hint * floats(0.5, 1.0)
    elif last_arm == "degenerate":
        last = draw(st.sampled_from(_NON_FINITE_OR_ZERO))
    last_map: dict[int, float] = {}
    if last_arm == "other_uic":
        last_map[_OTHER_UIC] = anchor
    elif last is not _ABSENT:
        last_map[_UIC] = last

    # --- legs (the sole-stop predicate's answer is fixed by the shape) --------
    legs_arm = arm_of("legs")
    stop_type = draw(st.sampled_from(sorted(pm.STOP_TYPES)))
    if legs_arm == "sole_clean":
        legs = (_mk_leg("stop-1", stop_type, filled=draw(st.sampled_from([0.0, 0.1, _QTY_EPS]))),)
    elif legs_arm == "none":
        legs = ()
    elif legs_arm == "two_stops":
        legs = (_mk_leg("stop-1", stop_type), _mk_leg("stop-2", stop_type))
    elif legs_arm == "stop_plus_tp":
        legs = (_mk_leg("stop-1", stop_type), _mk_leg("tp-1", "Limit"))
    elif legs_arm == "oco_relation":
        legs = (_mk_leg("stop-1", stop_type, relation=pm._OCO_RELATION),)
    elif legs_arm == "oco_ref_infix":
        legs = (_mk_leg("stop-1", stop_type, ref=f"crid{pm._OCO_REF_INFIX}0-stop"),)
    elif legs_arm == "partial_fill":
        legs = (_mk_leg("stop-1", stop_type, filled=floats(_QTY_EPS * 1.01, 7.0)),)
    else:
        stray_type = draw(st.sampled_from(["Market", "TrailingStop", None]))
        legs = (_mk_leg("stop-1", stop_type), _mk_leg("stray-1", stray_type))
    has_sole = legs_arm == "sole_clean"

    # --- backoff -----------------------------------------------------------
    backoff_arm = arm_of("backoff")
    backoff = {"off": frozenset(), "on": frozenset({_UIC}), "other_uic": frozenset({_OTHER_UIC})}[
        backoff_arm
    ]

    # --- trail floor (placed off the level the trail would reach) ------------
    floor_arm = arm_of("floor")
    level_hint: float | None = None
    if (
        isinstance(reaction, TrailingStop)
        and _usable(avg)
        and _usable(peak)
        and _usable(last)
        and _usable(plan_stop)
        and _usable(reaction.trail_frac)
    ):
        proposed_hint = fractional_giveback_target(avg, peak, kept_gain_frac=reaction.trail_frac)
        if proposed_hint is not None:
            level_hint = clamp_reanchor_target(
                plan_stop,
                proposed_hint,
                anchor_price=last,
                min_distance_frac=resolve_declared_policy(reaction).min_stop_distance_frac,
            )
    floor: Any = _ABSENT
    if floor_arm == "far_below":
        floor = floor_hint * floats(0.1, 0.9)
    elif floor_arm == "degenerate":
        floor = draw(st.sampled_from([float("nan"), float("inf"), float("-inf"), 0.0, -1.0]))
    elif floor_arm in ("band_below", "band_above", "boundary", "clears_step", "above_level"):
        if level_hint is None:
            labels.add("floor:unplaceable")
            floor = anchor
        elif floor_arm == "band_below":
            floor = level_hint - floats(0.0, 0.019)
        elif floor_arm == "band_above":
            floor = level_hint + floats(0.0, 0.019)
        elif floor_arm == "boundary":
            floor = level_hint - TRAIL_STEP_EPS
        elif floor_arm == "clears_step":
            floor = level_hint - floats(0.03, 5.0)
        else:
            floor = level_hint + floats(1.0, 50.0)
    floor_map: dict[int, float] = {}
    if floor_arm == "other_uic":
        floor_map[_OTHER_UIC] = anchor
    elif floor is not _ABSENT:
        floor_map[_UIC] = floor

    # --- reanchor latch ----------------------------------------------------
    latch_arm = arm_of("latch")
    latch_map: dict[int, float] = {}
    if latch_arm == "hit":
        latch_map[_UIC] = avg
    elif latch_arm == "hit_within_tolerance":
        latch_map[_UIC] = avg + pm._REANCHOR_AVG_PRICE_EPS / 2.0
    elif latch_arm == "miss":
        latch_map[_UIC] = anchor - 1.0
    elif latch_arm == "other_uic":
        latch_map[_OTHER_UIC] = avg
    latched = latch_map.get(_UIC)
    # The daemon's predicate, position_manager.py:727-728, as the field's definition.
    already_reanchored = latched is not None and abs(latched - avg) <= pm._REANCHOR_AVG_PRICE_EPS

    for label in sorted(labels):
        event(label)

    pos = _mk_pos(avg)
    plan = _mk_plan(plan_stop, reaction)
    view = ProtectionView(
        long_positions={_UIC: pos},
        all_positions={_UIC: pos},
        sell_legs_by_uic={_UIC: legs},
        planned_by_uic={_UIC: plan},
        oco_unsupported=frozenset(),
        amend_recently_failed=backoff,
        reanchored_by_uic=latch_map,
        peak_by_uic=peak_map,
        last_price_by_uic=last_map,
        trailed_stop_by_uic=floor_map,
    )
    sdv = StopDecisionView(
        avg_price=avg,
        peak=peak_map.get(_UIC),
        last_price=last_map.get(_UIC),
        plan_stop=plan_stop,
        reaction=reaction,
        has_sole_standalone_stop=has_sole,
        amend_in_backoff=_UIC in backoff,
        last_trailed_level=floor_map.get(_UIC),
        already_reanchored=already_reanchored,
    )
    return _Case(_UIC, pos, plan, legs, view, sdv, frozenset(labels))


_CASES = st.one_of([_case(focus=axis) for axis in _AXES] + [_case(focus="all")])


def _daemon_decides(c: _Case) -> AmendStop | None:
    """The daemon's routing, ``_reconcile_long``: one arm or the other by the
    declared policy's ``trails`` flag."""
    if resolve_declared_policy(c.plan.reaction).trails:
        return _maybe_trail(c.uic, c.pos, c.plan, c.legs, c.view)
    return _maybe_reanchor(c.uic, c.pos, c.plan, c.legs, c.view)


class TestStopDecisionParity(PropertyTestCase):
    """The leaf answers exactly what the daemon would place, or ``None`` when
    the daemon would not move the stop."""

    @given(_CASES)
    def test_daemon_and_leaf_agree(self, c: _Case) -> None:
        # Antecedents: the three booleans really are the daemon predicates' results.
        self.assertEqual(
            c.sdv.has_sole_standalone_stop, _sole_standalone_stop(c.legs) is not None, c.labels
        )
        self.assertEqual(c.sdv.amend_in_backoff, c.uic in c.view.amend_recently_failed, c.labels)
        latched = c.view.reanchored_by_uic.get(c.uic)
        self.assertEqual(
            c.sdv.already_reanchored,
            latched is not None and abs(latched - c.pos.avg_price) <= pm._REANCHOR_AVG_PRICE_EPS,
            c.labels,
        )

        action = _daemon_decides(c)
        level = decide_stop(c.sdv)
        if action is None:
            event("outcome:none")
            self.assertIsNone(level, c.labels)
            return
        self.assertIsInstance(action, AmendStop, c.labels)
        event(f"outcome:{action.reason}")
        self.assertIsNotNone(level, c.labels)
        # Exact: both sides call the same contract leaves on the same floats.
        self.assertEqual(action.stop_price, level, c.labels)

    def test_the_ratchet_step_is_the_daemons(self) -> None:
        """The one constant the copy carries; the property may never land in the
        two-cent band in one run, so the equality is pinned on its own."""
        self.assertEqual(TRAIL_STEP_EPS, pm._TRAIL_STEP_EPS)


# ---------------------------------------------------------------------------
# Non-vacuity: every arm and every outcome is actually generated.
# ---------------------------------------------------------------------------


def _outcome_labels(c: _Case) -> set[str]:
    """Labels read off the DAEMON's answers (never off the leaf): which arm
    fired, whether the ratchet vetoed a level the trail would otherwise have
    placed, and whether the envelope bound, was slack, or refused."""
    labels: set[str] = set()
    action = _daemon_decides(c)
    labels.add("outcome:none" if action is None else f"outcome:{action.reason}")
    policy = resolve_declared_policy(c.plan.reaction)
    if not policy.trails:
        return labels
    if c.uic in c.view.trailed_stop_by_uic:
        unratcheted = _maybe_trail(
            c.uic, c.pos, c.plan, c.legs, dataclasses.replace(c.view, trailed_stop_by_uic={})
        )
        if unratcheted is not None:
            labels.add("ratchet:cleared" if action is not None else "ratchet:vetoed")
            if action is None and "floor:boundary" in c.labels:
                labels.add("ratchet:boundary")
    peak, last = c.sdv.peak, c.sdv.last_price
    if _usable(c.sdv.avg_price) and _usable(peak) and _usable(last):
        proposed = policy.decide_reanchor(
            c.sdv.avg_price, None, peak=peak, last_price=last, plan_stop=c.sdv.plan_stop
        )
        if proposed is not None:
            clamped = clamp_reanchor_target(
                c.sdv.plan_stop,
                proposed,
                anchor_price=last,
                min_distance_frac=policy.min_stop_distance_frac,
            )
            if clamped is None:
                labels.add("clamp:refused")
            elif action is not None:
                labels.add("clamp:binds" if action.stop_price < proposed else "clamp:slack")
    return labels


_OUTCOME_TARGETS = frozenset(
    {
        "outcome:none",
        "outcome:trail",
        "outcome:reanchor-on-fill",
        "ratchet:vetoed",
        "ratchet:cleared",
        "ratchet:boundary",
        "clamp:binds",
        "clamp:slack",
        "clamp:refused",
    }
)
_ARM_TARGETS = frozenset(f"{axis}:{arm}" for axis, arms in _ARMS.items() for arm in arms)


@st.composite
def _every_arm_of(draw: st.DrawFn, axis: str) -> tuple[_Case, ...]:
    """One case per arm of ONE axis, the other axes clean. Kept per axis so the
    base example stays far under Hypothesis's ``large_base_example`` limit."""
    return tuple(draw(_case(focus=axis, force_arm=arm)) for arm in _ARMS[axis])


class TestArmCoverageNonVacuousness(PropertyTestCase):
    """Fails if any arm, or any outcome, was never produced. One method per axis
    (written out, not generated: the bare-test gate walks module-level defs
    only, and no test in this tree is built with ``setattr``); every method
    feeds one class-level set, checked once at the end."""

    seen: set[str] = set()

    def _observe(self, cases: tuple[_Case, ...]) -> None:
        for c in cases:
            type(self).seen |= c.labels | _outcome_labels(c)

    @given(_every_arm_of("reaction"))
    @settings(max_examples=25)
    def test_reaction_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @given(_every_arm_of("avg"))
    @settings(max_examples=25)
    def test_avg_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @given(_every_arm_of("plan_stop"))
    @settings(max_examples=25)
    def test_plan_stop_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @given(_every_arm_of("peak"))
    @settings(max_examples=25)
    def test_peak_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @given(_every_arm_of("last"))
    @settings(max_examples=25)
    def test_last_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @given(_every_arm_of("legs"))
    @settings(max_examples=25)
    def test_legs_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @given(_every_arm_of("backoff"))
    @settings(max_examples=25)
    def test_backoff_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @given(_every_arm_of("floor"))
    @settings(max_examples=25)
    def test_floor_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @given(_every_arm_of("latch"))
    @settings(max_examples=25)
    def test_latch_arms(self, cases: tuple[_Case, ...]) -> None:
        self._observe(cases)

    @classmethod
    def tearDownClass(cls) -> None:
        missing = (_ARM_TARGETS | _OUTCOME_TARGETS) - cls.seen
        assert not missing, f"arms never generated (vacuous coverage): {sorted(missing)}"


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
