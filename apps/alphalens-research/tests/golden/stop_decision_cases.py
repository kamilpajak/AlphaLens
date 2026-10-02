"""The frozen case table for the stop-arm golden corpus, and the record builder.

ONE module, imported by BOTH ``scripts/record_golden_stop_decision.py`` and
``tests/golden/test_golden_stop_decision_replay.py``, so the recording and the
assertion cannot describe different cases (the convention
``tests/golden/projection.py`` already follows for the pipeline stages).

WHY A CORPUS AT ALL (#1581). The live daemon's two post-fill stop arms,
``position_manager._maybe_trail`` and ``_maybe_reanchor``, are about to delegate
their DECISION to ``broker_contract.stop_decision``, which is a guard-for-guard
copy of them. The claim that makes that safe is that the live answers do not
move. This corpus is that claim as a runnable artifact: it is recorded from the
UNMODIFIED arms, committed, and replayed afterwards.

WHY A HAND-BUILT TABLE AND NOT HYPOTHESIS DRAWS. A hit rate is a property of the
generator, not of the code — the same divergence measured 17.4 per cent of draws
under one generator and 1 in 60 000 under another. A frozen table states on its
face which answers it sampled, and the bucket gate in the replay test fails when
a named bucket goes empty. Two cases here could NOT be sampled and had to be
built from the arithmetic; each says so in its own comment.

FLOATS. Recorded as JSON numbers with ``allow_nan`` left on, the way
``scripts/record_golden_score.py`` does it and for the reason its comment gives:
NaN and the infinities round-trip natively rather than being coerced to null.
Equality is then NOT enough on the replay side, because ``nan != nan`` and
``-0.0 == 0.0``; ``same_float`` below compares the bits.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from alphalens_pipeline.brokers.automanager import position_manager as pm
from alphalens_pipeline.brokers.automanager.position_manager import (
    PlannedExit,
    ProtectionView,
    _maybe_reanchor,
    _maybe_trail,
)
from broker_contract.contract import InstrumentRef, OrderState, OrderStatus, Position
from broker_contract.trade_intent.schema import ReanchorOnFill, TrailingStop

UIC = 43070

# The declarations the two arms route on. ``TrailingStop`` resolves to a policy
# whose ``trails`` is True (the trail arm); ``ReanchorOnFill`` to one whose
# ``requires_amend_stop`` is True and ``trails`` False (the re-anchor arm).
DECLARED_TRAIL = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)
DECLARED_REANCHOR = ReanchorOnFill(k_atr=1.5, atr=4.0, ceiling_price=None)
# A re-anchor whose target lands just UNDER the min-distance envelope: at
# avg_price 50.00 the proposal is 49.95 and the policy's 0.002 distance floor
# pulls it to 49.90, which is the only way to reach the #1015 envelope telemetry.
# Derived by running the policy and the clamp, not guessed.
DECLARED_REANCHOR_TIGHT = ReanchorOnFill(k_atr=0.0125, atr=4.0, ceiling_price=None)


# ---------------------------------------------------------------------------
# Daemon-side factories, deliberately SELF-CONTAINED. The richest set used to
# live in tests/property/test_stop_decision_parity.py, which the PR that wired
# the daemon to the leaf deleted -- importing from it would have made this
# corpus die with it.
# ---------------------------------------------------------------------------


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        ticker="BIO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id=str(UIC),
        broker_symbol="BIO:xnys",
    )


def mk_pos(avg_price: float, *, quantity: float = 7.0) -> Position:
    return Position(
        instrument=_instrument(),
        quantity=quantity,
        avg_price=avg_price,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-1",
    )


def mk_leg(
    order_id: str = "stop-1",
    order_type: str | None = "StopIfTraded",
    *,
    amount: float = 7.0,
    resting_price: float | None = None,
) -> OrderState:
    """``resting_price`` is where the broker reports the order SITTING, which is
    the daemon's SECOND ratchet floor (``_resting_stop_price``, #1514).
    ``amount`` is the RESTING quantity and is varied independently of the
    position's, because a corpus whose every case had them equal could not kill
    a mutant that sized the amend off the leg instead of the position."""
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=0.0,
        raw_status="Working",
        uic=UIC,
        side="SELL",
        order_type=order_type,
        amount=amount,
        external_reference=order_id,
        order_relation=None,
        resting_price=resting_price,
    )


def mk_plan(stop_price: float, reaction: Any) -> PlannedExit:
    return PlannedExit(
        uic=UIC,
        entry_crid="crid",
        side="SELL",
        stop_price=stop_price,
        tp_price=None,
        conflicting=False,
        n_plans=1,
        reaction=reaction,
    )


# ---------------------------------------------------------------------------
# One case.
# ---------------------------------------------------------------------------

_ABSENT = object()


@dataclass(frozen=True, slots=True)
class Case:
    """One call into ONE arm. ``bucket`` is the outcome class the replay's
    non-vacuity gate counts; ``why`` says what this case is here to witness."""

    name: str
    arm: str  # "trail" or "reanchor"
    bucket: str
    why: str
    avg_price: float = 50.0
    quantity: float = 7.0
    plan_stop: float = 45.0
    reaction: Any = DECLARED_TRAIL
    legs: tuple[OrderState, ...] = field(default_factory=lambda: (mk_leg(),))
    peak: Any = _ABSENT
    last_price: Any = _ABSENT
    journaled_floor: Any = _ABSENT
    latched_avg: Any = _ABSENT
    in_backoff: bool = False


def _per_uic(value: Any) -> dict[int, Any]:
    return {} if value is _ABSENT else {UIC: value}


def build_view(case: Case) -> tuple[ProtectionView, Position, PlannedExit]:
    pos = mk_pos(case.avg_price, quantity=case.quantity)
    plan = mk_plan(case.plan_stop, case.reaction)
    view = ProtectionView(
        long_positions={UIC: pos},
        all_positions={UIC: pos},
        sell_legs_by_uic={UIC: case.legs},
        planned_by_uic={UIC: plan},
        oco_unsupported=frozenset(),
        amend_recently_failed=frozenset({UIC}) if case.in_backoff else frozenset(),
        reanchored_by_uic=_per_uic(case.latched_avg),
        peak_by_uic=_per_uic(case.peak),
        last_price_by_uic=_per_uic(case.last_price),
        trailed_stop_by_uic=_per_uic(case.journaled_floor),
    )
    return view, pos, plan


def composed_ratchet_floor(case: Case) -> float | None:
    """What the daemon's trail arm combines its two floors into, reproduced here
    so the record carries the floor the arm actually used rather than the raw
    journaled level. Trailed level FIRST, resting price second, because ``max``
    keeps its first argument when the comparison is False and the two orders
    therefore disagree on a NaN input."""
    sole = pm._sole_standalone_stop(case.legs)
    resting = None if sole is None else pm._resting_stop_price(sole)
    journaled = None if case.journaled_floor is _ABSENT else case.journaled_floor
    floors = [level for level in (journaled, resting) if level is not None]
    return max(floors) if floors else None


class _Collect(logging.Handler):
    """Collects the LEVEL beside the rendered message. The level is not
    decoration: this daemon is quiet on a happy tick and logs only alerts and
    actions, so a refusal demoted from ``info`` to ``debug`` disappears from the
    operator's journal while every answer stays identical. A corpus that
    recorded the message alone did not see that mutation at all -- measured,
    which is why the level is here.

    Capturing at DEBUG rather than INFO is deliberate, and it has a cost worth
    stating: a newly added ``logger.debug`` line in either arm turns the corpus
    red although no deployment at INFO would print it. That is accepted, because
    a new log line IS a change to the arm, and because capturing the demotion
    gives the far better diagnostic -- the record says the level moved, instead
    of saying a line vanished."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append({"level": record.levelname, "message": record.getMessage()})


def run_case(case: Case) -> dict[str, Any]:
    """Drive ONE arm and return the record. No seam is needed: neither arm
    writes a file, opens a socket, starts a subprocess or reads an environment
    variable, so the only observable besides the return value is the log."""
    view, pos, plan = build_view(case)
    handler = _Collect()
    pm.logger.addHandler(handler)
    previous = pm.logger.level
    pm.logger.setLevel(logging.DEBUG)
    try:
        arm = _maybe_trail if case.arm == "trail" else _maybe_reanchor
        action = arm(UIC, pos, plan, case.legs, view)
    finally:
        pm.logger.setLevel(previous)
        pm.logger.removeHandler(handler)
    return {
        "name": case.name,
        "arm": case.arm,
        "bucket": case.bucket,
        "why": case.why,
        "input": input_echo(case),
        "composed_ratchet_floor": composed_ratchet_floor(case),
        "answer": None if action is None else _amend(action),
        "logs": handler.messages,
    }


def input_echo(case: Case) -> dict[str, Any]:
    """The inputs as recorded. PUBLIC because the replay test compares the
    recorded echo against the live table: without that comparison a case whose
    inputs drift WITHOUT changing the answer passes silently and the recorded
    echo becomes a lie. Demonstrated -- moving ``avg_price`` from 50.00 to 51.00
    on a case that vetoes for a missing peak left the suite green while the
    corpus still claimed 50.00, and the echo is what a reader of a future
    failure reads first."""

    def absent(value: Any) -> Any:
        return None if value is _ABSENT else value

    return {
        "avg_price": case.avg_price,
        "quantity": case.quantity,
        "plan_stop": case.plan_stop,
        "reaction": None if case.reaction is None else repr(case.reaction),
        "legs": [
            {
                "order_id": leg.order_id,
                "order_type": leg.order_type,
                "amount": leg.amount,
                "resting_price": leg.resting_price,
            }
            for leg in case.legs
        ],
        "peak": absent(case.peak),
        "last_price": absent(case.last_price),
        "journaled_floor": absent(case.journaled_floor),
        "latched_avg": absent(case.latched_avg),
        "in_backoff": case.in_backoff,
        "peak_stated": case.peak is not _ABSENT,
        "last_price_stated": case.last_price is not _ABSENT,
        "journaled_floor_stated": case.journaled_floor is not _ABSENT,
        "latched_avg_stated": case.latched_avg is not _ABSENT,
    }


def _amend(action: Any) -> dict[str, Any]:
    """Every field of the AmendStop, by name off the dataclass, so a field added
    later lands in the record instead of being silently dropped."""
    return {f.name: getattr(action, f.name) for f in dataclasses.fields(action)}


def same_record(left: Any, right: Any) -> bool:
    """Structural equality that reaches every float leaf through ``same_float``.

    Plain ``==`` fails in BOTH directions on these records. It calls two NaNs
    different, so a dict carrying a NaN input compares unequal to itself -- the
    first version of the input-echo assertion failed on exactly one case, the
    NaN one, for that reason and no other. And it calls ``-0.0`` equal to
    ``0.0``, so a sign flip hides. Recurse instead."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            same_record(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same_record(a, b) for a, b in zip(left, right, strict=True)
        )
    return same_float(left, right)


def same_float(left: Any, right: Any) -> bool:
    """Bit equality for the floats a record can hold. ``==`` is wrong twice
    over: it calls two NaNs different and it calls ``-0.0`` and ``0.0`` the
    same, and both shapes are in the table on purpose."""
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    return bool(left == right)


# ---------------------------------------------------------------------------
# The table. One axis off its clean arm at a time, then the cases that had to be
# CONSTRUCTED because no single-axis sweep reaches them.
#
# The clean trail arm is: avg 50.00, plan_stop 45.00, peak 59.17, last 59.00, a
# sole standalone stop resting at nothing in particular, no journaled floor, no
# latch, no backoff. The clean re-anchor arm is the same minus the peak and the
# last price, which that arm never reads, with a ReanchorOnFill declaration.
# ---------------------------------------------------------------------------

_CLEAN_TRAIL: dict[str, Any] = {
    "arm": "trail",
    "reaction": DECLARED_TRAIL,
    "peak": 59.17,
    "last_price": 59.00,
}
# plan_stop is 40.00 here, not the trail arm's 45.00, and the difference is
# load-bearing: at avg_price 50.00 the declaration proposes 44.00, so a brief
# floor of 45.00 makes the envelope REFUSE. Every re-anchor case was first
# written with 45.00 and every one of them answered None for that reason -- the
# latch cases included, which measured the refusal and not the latch.
_CLEAN_REANCHOR: dict[str, Any] = {
    "arm": "reanchor",
    "reaction": DECLARED_REANCHOR,
    "plan_stop": 40.0,
}

CASES: tuple[Case, ...] = (
    # --- the trail arm, placing and refusing -------------------------------
    Case(
        name="trail_clean_places",
        bucket="trail_places",
        why="the baseline: every guard satisfied, the stop moves up",
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_journaled_floor_below_proposal",
        bucket="trail_places",
        why="a journaled floor the proposal clears by more than the step",
        journaled_floor=40.0,
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_zero_journaled_floor",
        bucket="trail_places",
        why="a floor of exactly 0.0 on the normal price scale. It has NO power "
        "over the is-not-None filter and is not the witness for it: with "
        "the floor at 0.0 and the clamped level at 55.502 the ratchet lets "
        "the move through, and dropping the floor lets it through too, so "
        "a truthiness filter produces the same answer. Measured, not "
        "assumed. The witness is the penny-price case below",
        journaled_floor=0.0,
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_zero_floor_vetoes_at_penny_prices",
        bucket="trail_ratchet_refusal",
        why="CONSTRUCTED, and the ONLY witness that the floor filter must test "
        "'is not None' and never truthiness. A 0.0 floor is observable only "
        "where it can VETO, which needs a clamped level inside "
        "TRAIL_STEP_EPS of zero: avg 0.0100, floor 0.0090, peak 0.0120, "
        "last 0.0115 give a clamped level of 0.0112, and 0.0112 <= 0.0 + "
        "0.02 refuses. Drop the 0.0 with a truthiness filter and the same "
        "inputs PLACE 0.0112 instead",
        avg_price=0.01,
        plan_stop=0.009,
        peak=0.012,
        last_price=0.0115,
        journaled_floor=0.0,
        arm="trail",
        reaction=DECLARED_TRAIL,
    ),
    Case(
        name="trail_ratchet_refuses_inside_the_step",
        bucket="trail_ratchet_refusal",
        why="a journaled floor within TRAIL_STEP_EPS of the clamped level; "
        "refused WITHOUT a log line, which is what makes the refusal "
        "reason unrecoverable from the answer alone",
        journaled_floor=55.50,
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_resting_stop_is_a_floor_too",
        bucket="trail_ratchet_refusal",
        why="#1514 itself: the journaled level lags the stop that RESTS, and a "
        "proposal between the two must not patch the resting stop down",
        journaled_floor=55.0,
        legs=(mk_leg(resting_price=56.00),),
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_non_finite_resting_price_is_no_floor",
        bucket="trail_places",
        why="an infinite resting price must be filtered BEFORE the floor is "
        "combined, or it vetoes every move for ever",
        legs=(mk_leg(resting_price=float("inf")),),
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_clamp_refuses_below_the_brief_floor",
        bucket="trail_clamp_refusal",
        why="CONSTRUCTED by measurement, because the obvious construction does "
        "not reach it: raising the brief floor above avg_price makes the "
        "POLICY go dark before the clamp is ever called, so the arm answers "
        "None with no log. The refusal needs a PULLBACK instead -- peak "
        "55.00, last price 48.00, a 13 per cent giveback -- where the "
        "min-distance anchor on the LIVE price caps the level at 47.904, "
        "below the 49.00 floor. Proposal 53.00. Reachable on 282 of 720 "
        "grid points, so this is not a corner. The arm LOGS here, and the "
        "log prints the raw proposal, which no float answer carries",
        arm="trail",
        reaction=DECLARED_TRAIL,
        plan_stop=49.0,
        peak=55.0,
        last_price=48.0,
    ),
    Case(
        name="trail_dark_before_activation",
        bucket="trail_policy_dark",
        why="the policy itself returns no target: not in profit far enough yet",
        peak=50.10,
        last_price=50.05,
        arm="trail",
        reaction=DECLARED_TRAIL,
    ),
    Case(
        name="trail_no_peak_is_a_feed_veto",
        bucket="trail_feed_veto",
        why="an absent high-water mark is a feed veto, never a trail on a missing peak",
        arm="trail",
        reaction=DECLARED_TRAIL,
        last_price=59.00,
    ),
    Case(
        name="trail_no_last_price_is_a_feed_veto",
        bucket="trail_feed_veto",
        why="the clamp anchors on the LIVE price; absent, the arm refuses",
        arm="trail",
        reaction=DECLARED_TRAIL,
        peak=59.17,
    ),
    Case(
        name="trail_non_positive_avg_price",
        bucket="trail_guard_veto",
        why="the SIM NoAccess sentinel: never anchor on a <= 0 blend",
        avg_price=0.0,
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_amend_in_backoff",
        bucket="trail_guard_veto",
        why="the shared amend backoff after a PATCH reject",
        in_backoff=True,
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_no_sole_standalone_stop",
        bucket="trail_guard_veto",
        why="a stray second sell leg disqualifies the sole-stop shape, so the "
        "arm leaves the position to its own arms",
        legs=(mk_leg(), mk_leg("tp-1", "Limit")),
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_declaration_is_not_a_trail",
        bucket="trail_non_trailing_declaration",
        why="a ReanchorOnFill reaching the TRAIL arm by a direct call: the "
        "inner trails guard is the only thing that refuses it, and the two "
        "tests named for this case answer None through a feed veto instead",
        arm="trail",
        reaction=DECLARED_REANCHOR,
        peak=59.17,
        last_price=59.00,
    ),
    Case(
        name="trail_leg_amount_differs_from_quantity",
        bucket="trail_places",
        why="CONSTRUCTED, not sampled: the amend is sized off the POSITION, "
        "and a corpus whose every case had leg amount == quantity could "
        "not kill a mutant that sized it off the leg",
        legs=(mk_leg(amount=3.0),),
        **_CLEAN_TRAIL,
    ),
    Case(
        name="trail_floor_between_proposal_and_clamped",
        bucket="trail_ratchet_refusal",
        why="CONSTRUCTED from the arithmetic: peak 62 and last 56 put the raw "
        "proposal at 57.2 and the clamped level at 55.888, so a floor of "
        "56.0 sits BETWEEN them. Gating the ratchet on the raw proposal "
        "instead of the clamped level survives any corpus without this",
        peak=62.0,
        last_price=56.0,
        journaled_floor=56.0,
        arm="trail",
        reaction=DECLARED_TRAIL,
    ),
    Case(
        name="trail_nan_journaled_floor_KNOWN_RESIDUAL",
        bucket="known_residual",
        why="KNOWN RESIDUAL, issue #1673, NOT a statement of correct behaviour: "
        "max(nan, 56.0) is nan and 'clamped <= nan' is False, so the "
        "ratchet refuses nothing and the arm patches a stop resting at "
        "56.00 DOWN. Frozen here so the delegation is provably neutral; "
        "resolving #1673 turns the test beside this row red on purpose. "
        "ONE MORE THING THE FIX MUST CARRY: measured over eleven mutations "
        "of the arms, this row is the ONLY witness that kills a reversed "
        "floor composition (max keeps its first argument when the "
        "comparison is False, so the order only matters on a NaN input). "
        "Whoever resolves #1673 therefore owes the order a new witness, or "
        "that mutation goes unobserved",
        journaled_floor=float("nan"),
        legs=(mk_leg(resting_price=56.00),),
        **_CLEAN_TRAIL,
    ),
    # --- the re-anchor arm --------------------------------------------------
    Case(
        name="reanchor_clean_places",
        bucket="reanchor_places",
        why="the baseline: the stop is patched back to avg_price - k_atr*atr",
        **_CLEAN_REANCHOR,
    ),
    Case(
        name="reanchor_envelope_clamps_the_proposal",
        bucket="reanchor_envelope_clamped",
        why="CONSTRUCTED: the envelope MOVED the proposal without refusing it "
        "(49.95 -> 49.90), so the #1015 triple rides on the AmendStop and "
        "the arm logs it. The raw proposal appears in both, and it is the "
        "one value a float answer cannot carry",
        arm="reanchor",
        reaction=DECLARED_REANCHOR_TIGHT,
        plan_stop=45.0,
    ),
    Case(
        name="reanchor_clamp_refuses_below_the_brief_floor",
        bucket="reanchor_clamp_refusal",
        why="the envelope refuses outright and the arm logs the raw proposal: "
        "at a brief floor of 45.00 the proposal of 44.00 is below it",
        arm="reanchor",
        reaction=DECLARED_REANCHOR,
        plan_stop=45.0,
    ),
    Case(
        name="reanchor_latch_suppresses_the_same_blend",
        bucket="reanchor_latch_suppressed",
        why="the idempotence latch: one confirmed re-anchor per blend",
        latched_avg=50.0,
        **_CLEAN_REANCHOR,
    ),
    Case(
        name="reanchor_latch_inside_the_epsilon",
        bucket="reanchor_latch_suppressed",
        why="CONSTRUCTED: a latch half an epsilon away still suppresses, "
        "because the daemon compares with a 1e-6 tolerance and not with "
        "equality. A caller that passes exact equality diverges here",
        latched_avg=50.0 + 5e-7,
        **_CLEAN_REANCHOR,
    ),
    Case(
        name="reanchor_latch_outside_the_epsilon",
        bucket="reanchor_places",
        why="the other side of the same boundary: 1e-3 away is a NEW blend",
        latched_avg=50.0 + 1e-3,
        **_CLEAN_REANCHOR,
    ),
    Case(
        name="reanchor_no_declaration_is_inert",
        bucket="reanchor_inert_policy",
        why="a document that declared nothing resolves to the inert policy and "
        "the stop never moves: what makes a hand-written pick immune",
        arm="reanchor",
        reaction=None,
    ),
    Case(
        name="reanchor_non_positive_avg_price",
        bucket="reanchor_guard_veto",
        why="the SIM NoAccess sentinel on the re-anchor arm",
        avg_price=0.0,
        **_CLEAN_REANCHOR,
    ),
    Case(
        name="reanchor_amend_in_backoff",
        bucket="reanchor_guard_veto",
        why="the shared amend backoff, on the other arm",
        in_backoff=True,
        **_CLEAN_REANCHOR,
    ),
    Case(
        name="reanchor_no_sole_standalone_stop",
        bucket="reanchor_guard_veto",
        why="the sole-stop shape guard, on the other arm",
        legs=(mk_leg(), mk_leg("tp-1", "Limit")),
        **_CLEAN_REANCHOR,
    ),
    Case(
        name="reanchor_leg_amount_differs_from_quantity",
        bucket="reanchor_places",
        why="CONSTRUCTED: the re-anchor arm has the identical 'size off the "
        "position' line and had no witness anywhere",
        legs=(mk_leg(amount=3.0),),
        **_CLEAN_REANCHOR,
    ),
)

# ---------------------------------------------------------------------------
# What each bucket MEANS, as a predicate over the recorded answer.
#
# A row count per bucket is not a gate. The first version of this corpus counted
# rows, and every one of the ten re-anchor cases answered None -- including the
# one named ``reanchor_clean_places`` -- because their brief floor sat above
# their own proposal. All fourteen buckets were "non-empty" and the gate passed.
# A property can be true and empty; the fix is to assert the SHAPE the bucket's
# name claims.
#
# ``logs`` is the sharpest of the three. The trail arm's RATCHET refusal is
# SILENT while its CLAMP refusal logs, and nothing in the answer distinguishes
# them -- which is precisely why the refusal reason cannot be recomputed from a
# float and why the delegation has to report the clamped level. Pinning
# ``logs=False`` on ``trail_ratchet_refusal`` is what makes that property a test.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BucketShape:
    places: bool  # the arm returned an AmendStop rather than None
    logs: bool  # the arm emitted at least one log record
    envelope: bool = False  # the #1015 envelope triple is stamped


BUCKET_SHAPES: dict[str, BucketShape] = {
    "trail_places": BucketShape(places=True, logs=False),
    "trail_clamp_refusal": BucketShape(places=False, logs=True),
    "trail_ratchet_refusal": BucketShape(places=False, logs=False),
    "trail_policy_dark": BucketShape(places=False, logs=False),
    "trail_feed_veto": BucketShape(places=False, logs=False),
    "trail_guard_veto": BucketShape(places=False, logs=False),
    "trail_non_trailing_declaration": BucketShape(places=False, logs=False),
    "reanchor_places": BucketShape(places=True, logs=False),
    "reanchor_envelope_clamped": BucketShape(places=True, logs=True, envelope=True),
    "reanchor_clamp_refusal": BucketShape(places=False, logs=True),
    "reanchor_latch_suppressed": BucketShape(places=False, logs=False),
    "reanchor_guard_veto": BucketShape(places=False, logs=False),
    "reanchor_inert_policy": BucketShape(places=False, logs=False),
    # Not a statement of correct behaviour: this bucket PLACES a level, and that
    # is the defect (#1673). See the case's own comment.
    "known_residual": BucketShape(places=True, logs=False),
}

BUCKETS: tuple[str, ...] = tuple(sorted(BUCKET_SHAPES))


def shape_violations(records: list[dict[str, Any]]) -> list[str]:
    """Every way the recorded answers disagree with their bucket's declared
    shape, plus any bucket with no case and any case naming an unknown bucket.
    Shared by the recorder (which refuses to write) and the replay test."""
    problems: list[str] = []
    for bucket in BUCKETS:
        if not any(record["bucket"] == bucket for record in records):
            problems.append(f"bucket {bucket} has no case")
    for record in records:
        shape = BUCKET_SHAPES.get(record["bucket"])
        if shape is None:
            problems.append(f"{record['name']}: unknown bucket {record['bucket']}")
            continue
        answer = record["answer"]
        if (answer is not None) != shape.places:
            problems.append(
                f"{record['name']}: bucket {record['bucket']} says "
                f"places={shape.places} but the answer "
                f"{'placed a level' if answer is not None else 'was None'}"
            )
        if bool(record["logs"]) != shape.logs:
            problems.append(
                f"{record['name']}: bucket {record['bucket']} says logs="
                f"{shape.logs} but the arm emitted {len(record['logs'])} record(s)"
            )
        stamped = answer is not None and answer.get("envelope_proposed") is not None
        if stamped != shape.envelope:
            problems.append(
                f"{record['name']}: bucket {record['bucket']} says envelope="
                f"{shape.envelope} but the triple was "
                f"{'stamped' if stamped else 'absent'}"
            )
    return problems
