"""The post-fill stop decision: position state, declared policy and market view
in; a new stop level or ``None`` out (intent-replay design, section 3.2).

A COPY of the daemon's two stop-move arms, ``position_manager._maybe_trail``
and ``position_manager._maybe_reanchor``, guard for guard and in the daemon's
order. Copy, not extraction: until step 2 (#1581) the daemon keeps its own,
and ``tests/property/test_stop_decision_parity.py`` holds the two together by
running both on generated views. Every guard below therefore mirrors a line of
the daemon on purpose, including the ones a reader would want to harden:

* the ratchet floor is compared raw, without ``isfinite`` (a NaN floor lets a
  trail through, an infinite one vetoes), because the daemon compares it raw;
* ``atr`` is read the daemon's way although no trailing declaration carries
  one, so the line is dead on the trail arm and no test can kill a mutant that
  drops it;
* the policy is resolved through ``resolve_declared_policy``, which DEGRADES a
  primitive it cannot honour (``ModelPush``, a foreign object) to the inert
  policy. That is the daemon's rule, and the reason a replay must refuse such a
  document before it reaches this function (design section 4.3.1).

Hardening any of these is step 2's job, once one implementation remains.

The view carries nine primitives and nothing broker-shaped. Three of them are
the RESULTS of predicates the daemon evaluates over things a replay does not
have: ``has_sole_standalone_stop`` over the resting sell legs,
``amend_in_backoff`` over the amend failure history, ``already_reanchored``
over the ``reanchored`` journal marker (the daemon computes
``abs(latched - avg_price) <= _REANCHOR_AVG_PRICE_EPS`` itself and passes the
bool; a replay knows the answer from its own trace). A replay passes the
values that mean "no broker obstacle" for the first two and reports the
optimism as a named divergence; it never invents order legs.

The answer is a PRICE. Never an action, never a journal write, never the
envelope telemetry the daemon attaches to its ``AmendStop`` when the clamp
moved the proposal; those stay with the executor.

Dependencies: stdlib and this package only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, TypeIs

from broker_contract.exit_geometry.levels import clamp_reanchor_target
from broker_contract.exit_geometry.policy import ExitPolicy
from broker_contract.exit_geometry.registry import resolve_declared_policy
from broker_contract.trade_intent.schema import ReactionPrimitive, ReanchorOnFill

# The coarse price step a new trailing level must clear ABOVE the last confirmed
# trailed level before the stop moves again. A parameter of the decision, not a
# fact of any deployment: the daemon carries the same value as
# ``position_manager._TRAIL_STEP_EPS`` and the parity suite pins the two equal.
# It bounds re-amend chatter on a sub-step peak wiggle; the never-below-floor
# clamp is the capital guard.
TRAIL_STEP_EPS: Final = 0.02


@dataclass(frozen=True, slots=True)
class StopDecisionView:
    """The minimal view of one covered long, design section 3.2, field by field.

    ``avg_price`` is the anchor and the 1R numerator; ``plan_stop`` the brief
    disaster floor, the never-below level and the 1R denominator; ``peak`` the
    high-water mark since entry and ``last_price`` the latest observed price,
    either absent when the feed has not supplied one; ``reaction`` what the
    document declared; ``ratchet_floor`` the level a new proposal must clear.
    The three booleans are predicate results, never the things the predicates
    read (see the module docstring).

    ``ratchet_floor`` is COMPOSED BY THE CALLER and was called
    ``last_trailed_level`` until 2026-10-02. The rename is the field catching up
    with what it has to carry: the live daemon ratchets against the HIGHER of the
    level a trail last moved the stop to and the level the stop is RESTING at,
    because the journaled level can lag the resting one after a lost marker or an
    owner-raised stop (#1514). A field named for one of the two would be read as
    carrying only that one, and a caller that passed only the trailed level would
    let a proposal inside ``TRAIL_STEP_EPS`` of the resting stop through -- which
    is how a stop resting at 56.00 could be patched down to 55.502.

    Composing it is the caller's job rather than this module's because both
    inputs are the caller's own state: one is a journal fold and the other is the
    price on an order leg, and section 3.2 keeps order-shaped data out of this
    view. Compose it in that order -- trailed first, resting second -- because
    ``max`` keeps its first argument when the comparison is False, so the two
    orders disagree on a NaN input.
    """

    avg_price: float
    peak: float | None
    last_price: float | None
    plan_stop: float
    reaction: ReactionPrimitive | None
    has_sole_standalone_stop: bool
    amend_in_backoff: bool
    ratchet_floor: float | None
    already_reanchored: bool


@dataclass(frozen=True, slots=True)
class StopDecision:
    """One arm's answer, with the two intermediate levels its CALLER needs.

    ``level`` is what the arm decided: the price to move the stop to, or
    ``None``. The other two exist because the live daemon does more with a
    decision than place it, and none of it is recoverable from the price:

    * ``proposed`` is the policy's raw target, before the never-below-brief-floor
      envelope. The daemon prints it in both refusal log lines and carries it to
      the journal as ``envelope_proposed`` when the envelope moved the target
      (#1015). Recomputing it beside the decision is bit-exact but says nothing
      about WHICH guard refused.
    * ``clamped`` is the envelope's output. It is what separates the two refusals
      the daemon reports differently: a CLAMP refusal (``clamped is None`` with a
      ``proposed``) is logged, and a RATCHET refusal (``clamped`` survives,
      ``level`` does not) is SILENT. Measured on the trail arm, 394 of 458
      post-proposal ``None`` answers are ratchet refusals, so a caller that
      could not tell them apart would log a false line 86 per cent of the time.

    There is deliberately no ``policy_name``: every caller resolves the policy
    itself, because it needs ``trails`` or ``requires_amend_stop`` for its own
    guards, and a second source for one fact is a drift waiting to happen.
    """

    level: float | None
    proposed: float | None
    clamped: float | None


# Nothing was proposed: a guard vetoed, or the policy is dark before activation.
_NO_DECISION: Final = StopDecision(level=None, proposed=None, clamped=None)


def _finite_positive(value: float | None) -> TypeIs[float]:
    """A usable price, peak or ATR: present, finite, strictly positive. ``None``,
    NaN, either infinity, zero (``-0.0`` included) and the SIM ``<= 0`` sentinel
    all read as a veto. Same three clauses as the daemon's guard."""
    return value is not None and math.isfinite(value) and value > 0


def _declared_atr(reaction: ReactionPrimitive | None) -> float | None:
    """The ATR the document declared, or ``None``: only ``ReanchorOnFill``
    carries one. A trailing declaration does not, by design, and its policy
    never reads it; whether an absent ATR is fatal is the policy's answer."""
    return reaction.atr if isinstance(reaction, ReanchorOnFill) else None


def decide_trail_detail(view: StopDecisionView) -> StopDecision:
    """The TRAIL arm's decision with its levels: the daemon's ``_maybe_trail``,
    guard for guard, INCLUDING the ``policy.trails`` guard. That guard is inside
    this branch and not only in ``decide_stop``'s routing, because this function
    is called directly by an arm whose callers do not all route first."""
    return _trail(view, resolve_declared_policy(view.reaction))


def decide_reanchor_detail(view: StopDecisionView) -> StopDecision:
    """The RE-ANCHOR arm's decision with its levels: the daemon's
    ``_maybe_reanchor``, guard for guard.

    Separate from ``decide_trail_detail`` rather than one function routed on
    ``policy.trails``, because ``breakeven_trail`` carries ``trails`` AND
    ``requires_amend_stop``: a trailing declaration reaching the re-anchor arm
    passes its guard, and a routed entry point would run the TRAIL branch where
    the daemon runs this one. The daemon answers ``None`` there, because the
    policy refuses without a peak."""
    return _reanchor(view, resolve_declared_policy(view.reaction))


def decide_stop(view: StopDecisionView) -> float | None:
    """The level the daemon's protection pass would move the stop to on this
    view, or ``None`` when no guard, the policy, the envelope or the ratchet lets
    it move. Routed on the declared policy's ``trails`` flag exactly as
    ``_reconcile_long`` routes between its two arms."""
    policy = resolve_declared_policy(view.reaction)
    if policy.trails:
        return _trail(view, policy).level
    return _reanchor(view, policy).level


def _trail(view: StopDecisionView, policy: ExitPolicy) -> StopDecision:
    """Copy of ``_maybe_trail``: the stop moves UP only, to the policy's target,
    clamped never below the brief floor with the min-distance envelope anchored
    on the LIVE price (so the stop can sit above the entry and lock profit),
    then ratcheted against the trail history on the CLAMPED level.

    The ``policy.trails`` guard is HERE as well as in ``decide_stop``'s routing.
    The routing alone was enough while this branch had no public entry point; it
    is not enough now that ``decide_trail_detail`` exists, because the arm that
    calls it is itself called directly by tests that pass any declaration."""
    if not policy.trails:
        return _NO_DECISION
    avg_price = view.avg_price
    if not _finite_positive(avg_price):
        return _NO_DECISION
    # Dead on this arm (no trailing declaration carries an ATR); kept so the
    # arm reads line for line like the daemon's.
    atr = _declared_atr(view.reaction)
    if not view.has_sole_standalone_stop:
        return _NO_DECISION
    if view.amend_in_backoff:
        return _NO_DECISION
    peak = view.peak
    if not _finite_positive(peak):
        return _NO_DECISION  # feed veto / no peak yet
    last_price = view.last_price
    if not _finite_positive(last_price):
        return _NO_DECISION  # feed veto / no live price yet
    proposed = policy.decide_reanchor(
        avg_price, atr, peak=peak, last_price=last_price, plan_stop=view.plan_stop
    )
    if proposed is None:
        return _NO_DECISION  # dark before activation, or a degenerate the policy refuses
    clamped = clamp_reanchor_target(
        view.plan_stop,
        proposed,
        anchor_price=last_price,
        min_distance_frac=policy.min_stop_distance_frac,
    )
    if clamped is None:
        # never-below-brief-floor, or a degenerate input. The caller LOGS this
        # one, and the proposal is what it prints.
        return StopDecision(level=None, proposed=proposed, clamped=None)
    # RATCHET on the clamped level, compared raw like the daemon does. The
    # caller stays SILENT here, which is why ``clamped`` survives into the
    # record: it is the only thing separating this refusal from the one above.
    floor = view.ratchet_floor
    if floor is not None and clamped <= floor + TRAIL_STEP_EPS:
        return StopDecision(level=None, proposed=proposed, clamped=clamped)
    return StopDecision(level=clamped, proposed=proposed, clamped=clamped)


def _reanchor(view: StopDecisionView, policy: ExitPolicy) -> StopDecision:
    """Copy of ``_maybe_reanchor``: once per fill, the stop is moved to the
    policy's target off the realized average fill, clamped never below the
    brief floor with the envelope anchored on the AVERAGE fill.

    This arm has NO ratchet, so ``level`` and ``clamped`` never disagree. Both
    are reported anyway, so one record shape serves both arms and the caller's
    envelope condition reads the same on either."""
    if not policy.requires_amend_stop:
        return _NO_DECISION  # nothing declared -> the inert policy -> the stop never moves
    avg_price = view.avg_price
    if not _finite_positive(avg_price):
        return _NO_DECISION
    atr = _declared_atr(view.reaction)
    if not view.has_sole_standalone_stop:
        return _NO_DECISION
    if view.amend_in_backoff:
        return _NO_DECISION
    if view.already_reanchored:
        return _NO_DECISION  # the idempotence latch: one confirmed re-anchor per fill
    proposed = policy.decide_reanchor(avg_price, atr)
    if proposed is None:
        return _NO_DECISION  # setup_static inert, or a degenerate the policy refuses
    clamped = clamp_reanchor_target(
        view.plan_stop,
        proposed,
        anchor_price=avg_price,
        min_distance_frac=policy.min_stop_distance_frac,
    )
    return StopDecision(level=clamped, proposed=proposed, clamped=clamped)


__all__ = [
    "TRAIL_STEP_EPS",
    "StopDecision",
    "StopDecisionView",
    "decide_reanchor_detail",
    "decide_stop",
    "decide_trail_detail",
]
