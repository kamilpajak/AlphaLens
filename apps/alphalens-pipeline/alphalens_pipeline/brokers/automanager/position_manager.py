"""Position-manager — the "act" half of the auto-manager loop.

Pure decision function: one reconcile verdict + a per-tick BrokerView (the
control loop assembles it from the broker + the standalone-stop journal) -> the
single Action to take. No I/O — the control loop executes the returned Action.

MVP action set (design memo Components §9). Stop PLACEMENT is owned by the
broker-state protection pass (``reconcile_protection``); ``advance`` only routes
the verdict-level terminal/alert cases:
  round-trip closed / CANCELLED / REJECTED / EXPIRED -> CancelRemaining
  PAST-TTL / divergence / UNRESOLVED -> AlertOnly(reason) (never auto-cancel)
  else (still WORKING) -> NoOp

Realized-qty rule (Risk 2): the stop MUST size to the REALIZED entry fill
(verdict.details['filled_quantity']), NEVER planned verdict.qty — a planned-qty
stop over-hedges and can flip short after a partial fill.

Broker-state-truth protection (saxo-oco memo §6): ``reconcile_protection`` /
``_reconcile_long`` are a SECOND, pure decision layer that derives protection
from a live-broker snapshot (``ProtectionView``) instead of any journal line —
this kills Bug A (a failed stop POST leaving a permanently-naked position) and
Bug B (a lone-TP double-sell). Keyed per-uic (the unit Saxo nets to), sized to
netted owned qty.

Stage 3 (saxo Stage-3 memo) adds three write paths behind two dark env flags,
all default OFF: (B0) a TRULY NAKED fresh fill goes straight to a resting OCO
pair via ``UpgradeToOco(supersede_ids=())`` when ``_oco_enabled()``; (AmendStop)
an in-place PATCH resize of a SINGLE clean standalone stop grows it UP (composes
with the B1 additive fallback) or converges an over-hedge DOWN to owned when
``_amend_enabled()``; (rung-1 REFUSE) a position that already has a resting
rung-1 stop stays stop-only for its whole life — arm C never upgrades a resting
stop to OCO (PATCH cannot add a TP leg, cancel-then-OCO is naked, OCO-then-cancel
is 2x-owned rejected live). The control loop assembles the ``ProtectionView`` and
executes the returned Actions; this module performs no I/O.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from alphalens_pipeline.brokers.contract import (
    _QTY_EPS,
    OrderState,
    OrderStatus,
    Position,
)
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

# Exact reconcile note string this module keys on (brokers/reconcile.py
# _reconcile_filled). Pinned as a constant so a reconcile-side wording change
# fails these tests loudly rather than silently mis-classifying a live position.
_NOTE_ROUND_TRIP_CLOSED = "round trip closed (FIFO pair)"

_TERMINAL_NON_FILLED = frozenset(
    {OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value}
)

# Exit side for a long position's protective legs.
_SIDE = "SELL"


def _default_next_gen(_qty: float) -> int:
    """Fallback resize counter for a hand-built ``PlannedExit`` (pure tests): no
    persistence, always generation 0. The control loop injects the real
    journal-backed callable via ``_fold_planned_exits`` (saxo-oco memo §4.5)."""
    return 0


def _default_next_amend_seq() -> int:
    """Fallback monotonic amend-sequence for a hand-built ``PlannedExit`` (pure
    tests): no persistence, always 0. The control loop injects the real
    journal-backed callable (``_make_next_amend_seq``, ALWAYS max+1) so a
    cross-tick re-resize never dedup-collides (saxo Stage-3 memo, mitigation A3)."""
    return 0


def _exit_stop_ref(entry_crid: str, gen: int) -> str:
    """Deterministic gen-stamped x-request-id for a protective STOP leg (memo §4.5).

    One home for both consumers: the pure reconciler stamps it on ``PlaceStop``
    and the control-loop executor derives the same ref for an OCO stop leg. A
    same-size retry reuses the ref (Saxo 15 s dedup); a resize bumps ``gen`` to a
    distinct ref (never falsely deduped to the stale, smaller order)."""
    return f"{entry_crid}-stop-{gen}"


def _exit_tp_ref(entry_crid: str, gen: int) -> str:
    """Deterministic gen-stamped x-request-id for a take-profit leg (rung 2, memo §4.5)."""
    return f"{entry_crid}-tp-{gen}"


def _exit_oco_ref(entry_crid: str, gen: int) -> str:
    """Deterministic gen-stamped BASE x-request-id for an OCO exit pair (rung 2).

    The executor passes this to ``SupportsOcoExit.place_oco_exit`` as the POST
    x-request-id (a same-size crash-retry hits Saxo's 15 s dedup instead of
    resting a second OCO); the adapter derives the two per-leg
    ``ExternalReference`` values from it (``<ref>-stop`` / ``<ref>-tp``). The
    ``-oco-`` infix keeps it distinct from the standalone-stop ref
    (``<crid>-stop-<gen>``) so the two rails never collide on one uic."""
    return f"{entry_crid}-oco-{gen}"


def _exit_amend_ref(entry_crid: str, seq: int) -> str:
    """Deterministic MONOTONIC-seq PATCH x-request-id for an ``AmendStop`` resize.

    The distinct ``-amend-`` namespace NEVER shares with ``-stop-``/``-oco-``
    (mitigation H5): the amend PATCH and a standalone-stop POST for the same uic
    must never collide on Saxo's 15 s request-id dedup. ``seq`` is per-uic and
    ALWAYS max+1 (never qty-keyed), so a genuine re-resize to a previously-seen
    target qty is never dedup-swallowed while a single write stays never-blind-
    retry (mitigation A3/H3)."""
    return f"{entry_crid}-amend-{seq}"


@dataclass(frozen=True)
class PlannedExit:
    """The plan PRICES the broker cannot know, folded per NETTED uic from the
    append-only ``planned`` journal lines (saxo-oco memo §7). Carries NO
    protection flag — protection is derived from live broker state every tick.

    ``next_gen(qty)`` reads/increments the persisted per-uic resize counter: it
    returns the SAME generation for a same-size crash-retry (so Saxo's request-id
    dedup catches it) and a DISTINCT generation when the intended sell qty changes
    (a resize is a distinct order, never falsely deduped to the stale smaller
    one). Excluded from equality/repr so two folds compare on data alone."""

    uic: int
    entry_crid: str  # governing (shallowest-filled) tier crid, for the deterministic ref
    side: str  # "SELL"
    stop_price: float
    tp_price: float | None
    conflicting: bool  # True if >1 distinct active plan folded to this uic (refuse-to-merge)
    n_plans: int
    next_gen: Callable[[float], int] = field(default=_default_next_gen, compare=False, repr=False)
    # Per-uic MONOTONIC amend sequence (saxo Stage-3 memo): returns max+1 ALWAYS
    # (never qty-keyed) so an AmendStop resize to a previously-seen target qty is
    # never dedup-swallowed. Journal-backed callable injected by the control loop;
    # excluded from equality/repr so two folds compare on data alone.
    next_amend_seq: Callable[[], int] = field(
        default=_default_next_amend_seq, compare=False, repr=False
    )


@dataclass(frozen=True)
class BrokerView:
    """The verdict-level view ``advance`` routes over. Protection is NO LONGER
    journal-derived (saxo-oco memo §10 kills Bug A), so the ``protected_request_ids``
    / ``disaster_stops`` fields are gone; only ``working_children`` remains, for the
    terminal / round-trip ``CancelRemaining`` sweep of leftover exit legs."""

    working_children: Mapping[str, tuple[str, ...]]  # request_id -> still-working exit order ids


@dataclass(frozen=True)
class CancelRemaining:
    pass


@dataclass(frozen=True)
class AlertOnly:
    reason: str


@dataclass(frozen=True)
class NoOp:
    pass


@dataclass(frozen=True)
class PlaceStop:
    """Place a standalone protective ``StopIfTraded`` sized to netted owned qty.

    ``cancel_conflicting`` legs are cancelled BEFORE the place (a lone TP holds
    the conflicting sell commitment — Bug B — so it must clear first).
    ``supersede_ids`` legs are cancelled AFTER the place succeeds (a stale/smaller
    stop, superseded so there is never a naked window on the covered shares).
    The two orderings are opposite on purpose (saxo-oco memo §6/§8)."""

    uic: int
    side: str  # "SELL" for a long
    qty: float  # NETTED realized owned qty — never a planned tier qty
    stop_price: float
    request_id: str  # gen-stamped deterministic ref (_exit_stop_ref)
    supersede_ids: tuple[str, ...] = ()  # cancelled AFTER a successful place
    cancel_conflicting: tuple[str, ...] = ()  # cancelled BEFORE the place (lone TP)


@dataclass(frozen=True)
class UpgradeToOco:
    """OCO-direct-on-fill (Stage 3 arm B0): emitted for a TRULY NAKED fresh fill
    (``not legs``) when ``_oco_enabled()`` is true, the plan carries a TP price,
    and the uic is not ``oco_unsupported`` / ``oco_recently_placed`` — the position
    goes straight to a resting OCO pair instead of a stop-only rung 1. ``entry_crid``
    + ``gen`` derive the deterministic OCO base ref (``_exit_oco_ref``).
    ``supersede_ids`` is ALWAYS empty ``()`` in Stage 3 (a naked fill has no stop to
    supersede — the old rung 1 -> 2 upgrade emission was deleted, see arm C)."""

    uic: int
    side: str
    qty: float
    stop_price: float
    tp_price: float
    entry_crid: str
    gen: int
    supersede_ids: tuple[str, ...]


@dataclass(frozen=True)
class CancelSellLegs:
    """Cancel the named SELL legs on a uic (orphan sweep, or the NON-stop legs of
    an over-committed group in an over-hedge repair — the stop legs of that group
    leave only via ``PlaceStop.supersede_ids`` after a successful place, never an
    unconditional cancel). Idempotent + cascade-safe at the executor."""

    uic: int
    order_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class AmendStop:
    """In-place PATCH resize of a resting protective stop. Two callers, one shape:
    a SINGLE clean resting standalone stop (Stage 3, saxo Stage-3 memo) OR the
    child stop leg of a CLEAN unfilled resting OCO pair (Stage 3.5, saxo Stage-3.5
    memo — amending the OCO stop's Amount resizes BOTH legs symmetrically Saxo-side
    per Q9, so the mutually-exclusive pair still commits owned ONCE). ``target_qty``
    is the ABSOLUTE live-owned amount to set (NOT a delta): re-applying it is
    idempotent-in-effect (two sets = owned, never 2x), so a cross-tick re-emit is
    safe and the ref is MONOTONIC. Used to grow a stop UP (owned grew) and to
    converge an over-hedge stop DOWN to owned (#884 gap) — the order never leaves
    the book, so neither direction opens a naked window. ``request_id`` is the
    ``-amend-`` PATCH x-request-id (dedup key in the header, NOT the body — the
    amend preserves the resting order's own ref)."""

    uic: int
    side: str  # "SELL" for a long
    order_id: str  # the resting stop being resized in place
    order_type: str  # preserved from the resting stop (Saxo Q8 body requires it)
    target_qty: float  # ABSOLUTE new Amount == live netted owned, never a delta
    stop_price: float
    request_id: str  # gen-stamped deterministic -amend- ref (_exit_amend_ref)
    reason: str


Action = PlaceStop | UpgradeToOco | AmendStop | CancelSellLegs | CancelRemaining | AlertOnly | NoOp

# A SELL leg that PROTECTS the downside vs one that is UPSIDE only (memo §6).
STOP_TYPES = frozenset({"StopIfTraded", "Stop", "TrailingStopIfTraded"})
TP_TYPES = frozenset({"Limit"})

# OCO-group discrimination (saxo-oco memo, Stage 2). A resting OCO exit pair
# {near Limit take-profit, far StopIfTraded disaster} is MUTUALLY EXCLUSIVE — only
# one leg can ever fill — so Saxo commits the sell side ONCE for the whole pair, not
# once per leg. Two independent signals identify an OCO leg (either suffices, so
# detection survives the unverified Q7 per-leg ExternalReference echo): Saxo's
# ``OrderRelation == "Oco"`` and the ``-oco-`` infix in the gen-stamped base ref
# (``<crid>-oco-<gen>-stop`` / ``-tp``, stamped by ``_build_oco_exit_body`` — the
# standalone-stop ref ``<crid>-stop-<gen>`` has no such infix).
_OCO_RELATION = "Oco"
_OCO_REF_INFIX = "-oco-"
_OCO_REF_SUFFIXES = ("-stop", "-tp")


def _leg_amount(leg: OrderState) -> float:
    """The RESTING sell quantity a leg commits. A genuine ``0.0`` contributes 0.0;
    an absent (``None``) amount is treated as 0.0 (never misread as a live qty)."""
    return leg.amount if leg.amount is not None else 0.0


def _is_oco_leg(leg: OrderState) -> bool:
    """Whether a SELL leg belongs to a resting OCO exit pair (Stage 2). True on
    EITHER signal — the echoed ``OrderRelation`` OR the ``-oco-`` infix in the
    per-leg ref — so a healthy pair is still recognised if Saxo honours only one."""
    if leg.order_relation == _OCO_RELATION:
        return True
    ref = leg.external_reference
    return ref is not None and _OCO_REF_INFIX in ref


def _oco_group_key(leg: OrderState) -> str:
    """The base ref shared by the two legs of one OCO pair (``-stop`` / ``-tp``
    stripped). Falls back to the empty string when the per-leg ref is absent /
    unsuffixed (Q7): only one OCO pair can rest per uic (a second is rejected
    ``SellOrdersAlreadyExist``), so collapsing to one group per uic is correct."""
    ref = leg.external_reference
    if not ref:
        return ""
    for suffix in _OCO_REF_SUFFIXES:
        if ref.endswith(suffix):
            return ref[: -len(suffix)]
    return ref


def _sell_commitment(legs: tuple[OrderState, ...]) -> float:
    """Total sell-side quantity committed on a uic for the over-hedge test,
    counting each OCO group's commitment ONCE (saxo-oco memo, Stage 2).

    Saxo counts a mutually-exclusive OCO pair as a SINGLE commitment (only one leg
    fills), so a healthy resting exit OCO {StopIfTraded=owned, Limit=owned} commits
    ``owned``, NOT ``2*owned``. Summing every leg would double-count the pair and
    trip the over-hedge arm on the terminal rung-2 steady state (which would then
    cascade-cancel a leg and open a naked window — recurring churn). Non-OCO legs
    each count in full."""
    total = sum(_leg_amount(leg) for leg in legs if not _is_oco_leg(leg))
    oco_groups: dict[str, float] = {}
    for leg in legs:
        if _is_oco_leg(leg):
            key = _oco_group_key(leg)
            oco_groups[key] = max(oco_groups.get(key, 0.0), _leg_amount(leg))
    return total + sum(oco_groups.values())


# Q5 (same-uic stops sum cleanly against owned) CONFIRMED live on SIM 2026-07-21:
# a 2nd standalone StopIfTraded for the delta on an already-stopped uic was
# ACCEPTED (200) when stop_qty + delta == owned. So the grow arm places an
# ADDITIVE delta stop (no cancel, no naked window) instead of cancel-replacing.
# Kept as a module kill-switch: flip False to revert every uic to the shipped
# Stage-1 cancel-replace path (or per-uic via oco_unsupported).
ADDITIVE_STOPS_CONFIRMED = True


# Env flag gating the rung 1 -> 2 OCO upgrade. DEFAULTS OFF (ship dark): the
# machinery lands unenabled and is turned on only after the SIM upgrade-ordering
# probe closes the open enablement questions (saxo-oco memo §11 / §2).
_OCO_ENABLED_ENV = "ALPHALENS_BROKER_OCO_ENABLED"


def _oco_enabled() -> bool:
    """Whether the OCO path is enabled (read at call time).

    The SINGLE source gating both the pure B0 emission (``_reconcile_long``
    OCO-direct-on-fill arm) and the control-loop executor. Reads the env flag
    every call so it is restart-consistent and hermetically testable (no
    import-time snapshot). Defaults OFF — this PR ships the OCO path DARK."""
    return os.environ.get(_OCO_ENABLED_ENV) == "1"


# Env flag gating the Stage-3 PATCH-amend resize (both AmendStop arms + executor).
# DEFAULTS OFF (ship dark): the machinery lands unenabled and is turned on only
# after the SIM amend live probe passes (saxo Stage-3 memo §"Env gates").
_AMEND_ENABLED_ENV = "ALPHALENS_BROKER_AMEND_ENABLED"


def _amend_enabled() -> bool:
    """Whether the Stage-3 in-place PATCH-amend resize is enabled (read at call
    time). The SINGLE source gating both pure AmendStop emissions (grow + over-
    hedge downsize) and the control-loop executor. Reads the env flag every call
    so it is restart-consistent and hermetically testable (no import-time
    snapshot). Defaults OFF — this PR ships the amend path DARK."""
    return os.environ.get(_AMEND_ENABLED_ENV) == "1"


@dataclass(frozen=True)
class ProtectionView:
    """The ONE per-tick snapshot the pure reconciler diffs (assembled by
    ``control_loop.build_protection_view``; saxo-oco memo §6). Protection is a
    function of live broker state ONLY — no journal line asserts it.

    ``planned_by_uic`` supplies the plan PRICES the broker cannot know, joined by
    uic. ``oco_unsupported`` is the persisted per-instrument capability flag
    (Stage 2)."""

    long_positions: Mapping[int, Position]  # uic -> netted long, quantity > _QTY_EPS
    all_positions: Mapping[int, Position]  # includes flats/shorts (orphan + short arms)
    sell_legs_by_uic: Mapping[int, tuple[OrderState, ...]]
    planned_by_uic: Mapping[int, PlannedExit]
    oco_unsupported: frozenset[int]
    # Stage-3 TTL folds (saxo Stage-3 memo), populated by the control-loop view
    # builder from journal markers; default empty so pure tests + a second broker
    # stay source-compatible. ``oco_recently_placed`` suppresses a B0 re-fire while
    # a just-placed OCO rests but list-orders lags (H1b/A1). ``amend_recently_
    # failed`` skips the amend arms for one TTL after a PATCH reject so B1 additive
    # / place-first covers the delta by a proven primitive (verdict-2-finding-2).
    oco_recently_placed: frozenset[int] = frozenset()
    amend_recently_failed: frozenset[int] = frozenset()


@dataclass(frozen=True)
class _LegGroup:
    """A set of SELL legs on one uic to cancel in an over-hedge repair, plus its
    stop-leg subset (superseded after the residual place) and the largest leg
    ``filled_quantity`` (the partial-fill discriminator, memo B-S5)."""

    order_ids: tuple[str, ...]
    stop_leg_ids: tuple[str, ...]
    filled_quantity: float


def _stop_leg_ids(legs: tuple[OrderState, ...]) -> tuple[str, ...]:
    return tuple(leg.order_id for leg in legs if leg.order_type in STOP_TYPES)


def _tp_only_leg_ids(legs: tuple[OrderState, ...]) -> tuple[str, ...]:
    return tuple(leg.order_id for leg in legs if leg.order_type in TP_TYPES)


def _sole_standalone_stop(legs: tuple[OrderState, ...]) -> OrderState | None:
    """The lone amendable standalone stop on a uic, or ``None`` (saxo Stage-3
    memo). Returns the leg IFF ALL hold: (1) EXACTLY ONE leg is in STOP_TYPES;
    (2) it is NOT an OCO leg (``_is_oco_leg`` false — an OCO stop cannot be resized
    without cascading its sibling, Q9); (3) NO Limit/TP leg is present anywhere in
    ``legs`` (belt for a Q7 hidden-OCO whose relation echo failed — a real OCO
    always carries a TP sibling); (4) the stop has NOT partially triggered
    (``filled_quantity <= _QTY_EPS`` — never amend a stop mid-fill, mitigation H2);
    (5) the stop is the SOLE resting sell leg on the uic — NO other leg of any type
    rests alongside it (a stray non-stop / non-TP SELL leg, e.g. a Market or a
    TrailingStop outside ``STOP_TYPES``, would keep resting after an in-place amend
    and leave a residual over-commit; the amend arm resizes only the stop and
    returns). Any other shape (>1 stop, an OCO leg, a TP present, a partial fill, a
    stray extra leg) returns ``None`` and the caller falls to the always-correct B1
    additive / place-residual-first path (over-covered, never naked)."""
    stops = [leg for leg in legs if leg.order_type in STOP_TYPES]
    if len(stops) != 1:
        return None
    stop = stops[0]
    if _is_oco_leg(stop):
        return None
    if any(leg.order_type in TP_TYPES for leg in legs):
        return None
    if (stop.filled_quantity or 0.0) > _QTY_EPS:
        return None
    if any(leg.order_id != stop.order_id for leg in legs):
        return None
    return stop


def _oco_stop_leg(legs: tuple[OrderState, ...]) -> OrderState | None:
    """The child ``StopIfTraded`` leg of a CLEAN unfilled resting OCO pair, or
    ``None`` (saxo Stage-3.5 memo). The OCO-REQUIRING inverse of
    ``_sole_standalone_stop``: returns the OCO stop leg IFF ALL hold: (1) EXACTLY
    TWO legs, both OCO (``_is_oco_leg`` true — recognises a Q7-asymmetric pair via
    ``OrderRelation`` OR the ``-oco-`` ref infix; ``len == 2`` auto-rejects any
    stray leg alongside the pair, since ``SellOrdersAlreadyExist`` guarantees at
    most one pair per uic); (2) the pair is well-formed {exactly one StopIfTraded,
    exactly one Limit}; (3) NEITHER leg has partially triggered
    (``filled_quantity <= _QTY_EPS``) — a partially-filled pair defers to the
    partial-fill-aware place-residual-first arm because Q9 propagation onto a
    partially-filled leg is UNPROVEN (mitigation for the mid-fill TOCTOU).

    Amending the returned stop leg's Amount resizes BOTH legs symmetrically
    Saxo-side (Q9), so the pair still commits owned ONCE. Any other shape returns
    ``None`` and the caller falls to the always-correct place-residual-first / B1
    additive path (over-covered, never naked). Uses only existing primitives
    (STOP_TYPES, TP_TYPES, ``_is_oco_leg``, ``_QTY_EPS``)."""
    if len(legs) != 2 or not all(_is_oco_leg(leg) for leg in legs):
        return None
    stops = [leg for leg in legs if leg.order_type in STOP_TYPES]
    tps = [leg for leg in legs if leg.order_type in TP_TYPES]
    if len(stops) != 1 or len(tps) != 1:
        return None
    if (stops[0].filled_quantity or 0.0) > _QTY_EPS or (tps[0].filled_quantity or 0.0) > _QTY_EPS:
        return None
    return stops[0]


def _all_legs_group(legs: tuple[OrderState, ...]) -> _LegGroup:
    return _LegGroup(
        order_ids=tuple(leg.order_id for leg in legs),
        stop_leg_ids=_stop_leg_ids(legs),
        filled_quantity=max((leg.filled_quantity or 0.0 for leg in legs), default=0.0),
    )


def _group_with_partial_fill(legs: tuple[OrderState, ...]) -> _LegGroup | None:
    """The over-committed group selected by a leg's ``filled_quantity`` (a TP that
    partially filled dropped netted owned; fixes B-S5). ``None`` when no leg has
    filled — the caller falls back to ``_newest_group``."""
    if any((leg.filled_quantity or 0.0) > _QTY_EPS for leg in legs):
        return _all_legs_group(legs)
    return None


def _newest_group(legs: tuple[OrderState, ...]) -> _LegGroup:
    """Fallback over-hedge group when no leg shows a partial fill (e.g. the netted
    position simply shrank). Stage 1 has no OCO grouping, so the whole leg set on
    the uic is the over-committed group."""
    return _all_legs_group(legs)


def reconcile_protection(view: ProtectionView) -> list[Action]:
    """Pure per-tick desired-vs-actual diff over live broker state (memo §6).

    Emits, in order: (1) per netted LONG, the downside-cover arm (a covered long
    may upgrade to ``UpgradeToOco`` when ``_oco_enabled()`` — else stop-only NoOp);
    (2) an orphan sweep for SELL legs on a uic with no long (else they can fire
    into a naked short); (3) a negative-position alert."""
    actions: list[Action] = []
    for uic, pos in view.long_positions.items():
        actions.extend(_reconcile_long(uic, pos, view))
    for uic, legs in view.sell_legs_by_uic.items():
        if legs and uic not in view.long_positions:
            actions.append(
                CancelSellLegs(
                    uic,
                    tuple(leg.order_id for leg in legs),
                    reason=f"uic {uic}: exit legs on flat/absent position — orphan sweep",
                )
            )
    for uic, pos in view.all_positions.items():
        if pos.quantity < -_QTY_EPS:
            actions.append(
                AlertOnly(f"uic {uic}: unexpected SHORT {pos.quantity} — manual intervention")
            )
    return actions


def _reconcile_long(uic: int, pos: Position, view: ProtectionView) -> list[Action]:
    """The downside-cover arm for ONE netted long (memo §6). Sizes every stop to
    ``pos.quantity`` (netted realized owned) — never a planned tier qty."""
    owned = pos.quantity  # STRUCTURAL netted qty — never planned
    plan = view.planned_by_uic.get(uic)
    legs = view.sell_legs_by_uic.get(uic, ())

    if plan is None:
        return [
            AlertOnly(
                f"uic {uic}: long {owned} open but no journaled disaster-stop plan — cannot protect"
            )
        ]
    if plan.conflicting:  # >1 distinct active plan folded to one netted uic
        return [
            AlertOnly(
                f"uic {uic}: {plan.n_plans} active plans on one netted position — refusing to merge"
            )
        ]

    # Explicit-None guard: ``amount`` is ``float | None`` (the RESTING qty). A
    # genuine ``0.0`` must contribute 0.0 to the sum, not be misread as an absent
    # amount — ``or 0.0`` conflates the two (harmless today, latent misread).
    stop_qty = sum(_leg_amount(leg) for leg in legs if leg.order_type in STOP_TYPES)
    tp_qty = sum(_leg_amount(leg) for leg in legs if leg.order_type in TP_TYPES)
    # Over-hedge is measured on the COMMITMENT, not the raw leg sum: a resting OCO
    # pair {stop=owned, tp=owned} commits owned ONCE (mutually exclusive), so it is
    # the terminal rung-2 state, never a 2*owned over-hedge (saxo-oco memo, Stage 2).
    total = _sell_commitment(legs)

    # (A) OVER-HEDGE: an exit leg partially filled (netted owned shrank) or the
    #     position shrank -> total sell > owned. Place a residual-sized stop FIRST
    #     (never a naked repair window). The old STOP legs leave EXCLUSIVELY via
    #     PlaceStop.supersede_ids — i.e. ONLY after the residual place succeeds — so
    #     a deferred place (Saxo SellOrdersAlreadyExist) leaves the old over-sized
    #     stop resting = over-covered, NEVER naked (the Bug-A cardinal sin). The
    #     separate, unconditional CancelSellLegs names ONLY the NON-stop legs
    #     (TP / Market noise); when there are none (Stage-1 stop-only) it is not
    #     emitted at all. Stops must never sit in an unconditional cancel.
    if total > owned + _QTY_EPS:
        # (DOWNSIZE amend, Stage 3): a SINGLE clean standalone stop over-covers
        # (owned shrank) -> PATCH amend it DOWN to live owned in place (the #884
        # gap), no cancel, no naked window. Absolute-target (Amount = owned) so a
        # cross-tick re-emit is idempotent. Only when amend is enabled and the uic
        # is not degraded (oco_unsupported / amend_recently_failed); a multi-stop
        # or OCO-leg over-hedge keeps the unchanged place-residual-first arm below
        # (over-covered, never naked — Q9 residual).
        sole = _sole_standalone_stop(legs)
        if (
            _amend_enabled()
            and sole is not None
            and uic not in view.oco_unsupported
            and uic not in view.amend_recently_failed
            and _leg_amount(sole) > owned + _QTY_EPS
        ):
            return [
                AmendStop(
                    uic,
                    _SIDE,
                    sole.order_id,
                    sole.order_type or "",
                    owned,
                    plan.stop_price,
                    _exit_amend_ref(plan.entry_crid, plan.next_amend_seq()),
                    reason="over-hedge downsize — PATCH amend in place",
                )
            ]
        # (OCO DOWNSIZE amend, Stage 3.5): a CLEAN unfilled resting OCO pair
        # over-covers (owned shrank) -> PATCH the OCO stop leg DOWN to live owned in
        # place; Q9 propagates symmetrically to the Limit sibling so the pair still
        # commits owned ONCE — no cancel, no naked window. Absolute-target so a
        # cross-tick re-emit is idempotent. Same guards as the standalone downsize;
        # gated on _amend_enabled() ONLY (a resting OCO exists because OCO was
        # enabled at B0-placement, and resizing it in place is strictly safer than
        # a fallback teardown even if OCO was since disabled).
        oco_stop = _oco_stop_leg(legs)
        if (
            _amend_enabled()
            and oco_stop is not None
            and uic not in view.oco_unsupported
            and uic not in view.amend_recently_failed
            and _leg_amount(oco_stop) > owned + _QTY_EPS
        ):
            return [
                AmendStop(
                    uic,
                    _SIDE,
                    oco_stop.order_id,
                    oco_stop.order_type or "StopIfTraded",
                    owned,
                    plan.stop_price,
                    _exit_amend_ref(plan.entry_crid, plan.next_amend_seq()),
                    reason="OCO downsize — PATCH OCO stop leg down in place",
                )
            ]
        # (M1) OCO propagation-lag NoOp guard: reaching here with a clean unfilled
        # OCO pair (oco_stop is not None) means its stop leg is already <= owned
        # (else the downsize amend fired) yet the commitment > owned — i.e. the TP
        # leg still shows the OLD larger Amount because list-orders lags Q9's
        # symmetric propagation. Tearing the pair down via place-residual-first
        # would cancel the pair the previous tick just resized on a transient read
        # lag; the downside is fully covered (stop <= owned), so NoOp one tick until
        # the TP read catches up (then total == owned -> arm C NoOp). Gated on
        # _amend_enabled() so arm A stays byte-identical when the flag is off.
        if _amend_enabled() and oco_stop is not None:
            return [NoOp()]
        bad = _group_with_partial_fill(legs) or _newest_group(legs)
        gen = plan.next_gen(owned)
        stop_ids = set(bad.stop_leg_ids)
        # NEVER name a live OCO leg in an unconditional cancel: cancelling one OCO
        # leg cascade-cancels its sibling, and the replacement PlaceStop can be
        # rejected (SellOrdersAlreadyExist) while the OCO still commits owned -> a
        # naked window. An OCO leg leaves only via supersede-after-a-successful
        # place (its stop leg is in supersede_ids); its TP leg is simply left in
        # place (Q3a caps oversell, the OCO stop keeps covering the downside).
        oco_ids = {leg.order_id for leg in legs if _is_oco_leg(leg)}
        non_stop_ids = tuple(
            oid for oid in bad.order_ids if oid not in stop_ids and oid not in oco_ids
        )
        actions: list[Action] = [
            PlaceStop(
                uic,
                _SIDE,
                owned,
                plan.stop_price,
                _exit_stop_ref(plan.entry_crid, gen),
                supersede_ids=bad.stop_leg_ids,  # keep old stop until the residual is confirmed
            )
        ]
        if non_stop_ids:
            actions.append(
                CancelSellLegs(uic, non_stop_ids, reason="over-hedge repair — non-stop legs")
            )
        return actions

    # (B) DOWNSIDE DEFICIT: naked, grew past the covering stop, a lone-TP Bug-B
    #     shape, or a stale/partial stop. A lone TP is always cancelled BEFORE the
    #     place (it holds the conflicting sell commitment — Bug B).
    if stop_qty + _QTY_EPS < owned:
        # (B0) OCO-DIRECT-ON-FILL (Stage 3): a TRULY NAKED fresh fill (no resting
        #      legs) with OCO wanted goes STRAIGHT to a resting OCO pair via
        #      UpgradeToOco(supersede_ids=()) — never a stop-only rung 1 first (the
        #      system reaches OCO only at the fresh-fill moment; rung-1 stops are
        #      never upgraded, see arm C). Suppressed while a just-placed OCO rests
        #      but list-orders lags (oco_recently_placed) so a 2nd B0 can never
        #      double-commit (H1b/A1). Fires ONLY on `not legs`, so total after
        #      placement == owned once, never 2x.
        if (
            _oco_enabled()
            and plan.tp_price is not None
            and uic not in view.oco_unsupported
            and not legs
        ):
            if uic in view.oco_recently_placed:
                # A just-placed OCO pair rests but list-orders lags (the view
                # shows no legs). Placing ANY stop now would commit a second
                # owned SELL atop the invisible resting OCO pair -> 2x owned,
                # the exact double-commit the marker exists to prevent (H1b/A1).
                # NoOp — the OCO stop leg already covers the downside; next tick
                # the pair becomes visible (arm C -> NoOp) or the TTL expires and
                # B0 re-evaluates against live broker state.
                return [NoOp()]
            return [
                UpgradeToOco(
                    uic,
                    _SIDE,
                    owned,
                    plan.stop_price,
                    plan.tp_price,
                    plan.entry_crid,
                    plan.next_gen(owned),
                    supersede_ids=(),
                )
            ]
        # (GROW amend, Stage 3): a SINGLE clean standalone stop under-covers (owned
        #      grew) -> PATCH amend it UP to live owned in place (absolute-target,
        #      no naked window). Falls through to the B1 additive-delta stop below
        #      when amend is off, >1 stop rests (B1-grown multi-tier), a TP leg is
        #      present, the stop partially filled, or the uic recently failed an
        #      amend — B1 is the always-correct fallback that covers the delta with
        #      a second stop.
        sole = _sole_standalone_stop(legs)
        if (
            _amend_enabled()
            and sole is not None
            and uic not in view.oco_unsupported
            and uic not in view.amend_recently_failed
            and _leg_amount(sole) + _QTY_EPS < owned
        ):
            return [
                AmendStop(
                    uic,
                    _SIDE,
                    sole.order_id,
                    sole.order_type or "",
                    owned,
                    plan.stop_price,
                    _exit_amend_ref(plan.entry_crid, plan.next_amend_seq()),
                    reason="grow — PATCH amend stop up in place",
                )
            ]
        # (GROW-after-OCO amend, Stage 3.5): a CLEAN unfilled resting OCO pair
        #      under-covers (owned grew) -> PATCH the OCO stop leg UP to live owned
        #      in place; Q9 propagates symmetrically to the Limit sibling so both
        #      legs resize and the pair commits owned ONCE, no naked window.
        #      Absolute-target. B0 is skipped for a resting OCO (`not legs` False)
        #      and the standalone grow amend returns None (a TP leg is present), so
        #      this is the first arm that can fire. Falls through to B1 additive
        #      below when amend is off / the uic is degraded (oco_unsupported /
        #      amend_recently_failed) — the always-correct fallback covers the delta.
        oco_stop = _oco_stop_leg(legs)
        if (
            _amend_enabled()
            and oco_stop is not None
            and uic not in view.oco_unsupported
            and uic not in view.amend_recently_failed
            and _leg_amount(oco_stop) + _QTY_EPS < owned
        ):
            return [
                AmendStop(
                    uic,
                    _SIDE,
                    oco_stop.order_id,
                    oco_stop.order_type or "StopIfTraded",
                    owned,
                    plan.stop_price,
                    _exit_amend_ref(plan.entry_crid, plan.next_amend_seq()),
                    reason="grow-after-OCO — PATCH OCO stop leg up in place",
                )
            ]
        # (B1) ADDITIVE-ON-GROWTH (Q5 confirmed live): a covering stop already
        #      holds stop_qty and the position simply GREW (another tier filled).
        #      Place a stop for the DELTA only, KEEPING the existing stop — no
        #      supersede, no naked window, and the sell side sums to exactly owned.
        #      Skipped when Q5 is off or the uic opted out of broker multi-order
        #      features (oco_unsupported), which fall through to cancel-replace.
        #      Edge: ``next_gen`` keys the ref on qty, so two grow steps of the SAME
        #      delta within Saxo's 15 s request-id dedup window share a ref and the
        #      2nd is deduped away — that slice is under-covered for < 15 s and
        #      self-heals on the next tick once the window passes (the disaster stop
        #      is deep OTM, so the transient gap is immaterial).
        if ADDITIVE_STOPS_CONFIRMED and stop_qty > _QTY_EPS and uic not in view.oco_unsupported:
            deficit = owned - stop_qty
            return [
                PlaceStop(
                    uic,
                    _SIDE,
                    deficit,
                    plan.stop_price,
                    _exit_stop_ref(plan.entry_crid, plan.next_gen(deficit)),
                    cancel_conflicting=_tp_only_leg_ids(legs),  # lone TP -> cancel BEFORE (Bug B)
                )
            ]
        # (B2) CANCEL-REPLACE (naked, Q5 off, or oco_unsupported): place the full
        #      owned stop FIRST, supersede any stale stop AFTER (no naked window on
        #      the already-covered shares).
        return [
            PlaceStop(
                uic,
                _SIDE,
                owned,
                plan.stop_price,
                _exit_stop_ref(plan.entry_crid, plan.next_gen(owned)),
                supersede_ids=_stop_leg_ids(legs),  # stale stop -> cancel AFTER
                cancel_conflicting=_tp_only_leg_ids(legs),  # lone TP -> cancel BEFORE (Bug B)
            )
        ]

    # (C) DOWNSIDE COVERED. A resting exit already covers the position.
    if tp_qty + _QTY_EPS >= owned:
        # A full TP + a covering stop == a healthy resting OCO pair (from B0): the
        # terminal rung-2 steady state. Arm (A) no longer trips first (the OCO pair
        # commits owned ONCE), so this is the state a successful B0 settles into.
        return [NoOp()]
    # rung1->2 conversion of a resting standalone stop is unsafe by construction
    # (Stage 3): PATCH cannot add a TP leg, cancel-then-OCO is naked, OCO-then-cancel
    # is 2x-owned rejected live — refuse, stay stop-only; OCO is reached only via B0
    # on a fresh naked fill. A position that already has a resting rung-1 stop (or a
    # covering OCO stop leg without a full TP) therefore stays stop-only for its whole
    # life; the system converges to full OCO coverage purely by turnover.
    return [NoOp()]


def advance(verdict: ReconcileVerdict) -> Action:
    """One verdict -> the single terminal/alert Action (pure; no side effects).

    Stop PLACEMENT is no longer decided here: the broker-state protection pass
    (``reconcile_protection`` / ``_reconcile_long``) owns every open long,
    keyed per-uic and sized to netted owned qty (saxo-oco memo §6/§10). ``advance``
    keeps only the verdict-level routing the protection pass does not cover:
    divergence / unresolved / partial-fill alerts and the terminal round-trip
    / cancelled / rejected / expired ``CancelRemaining`` sweep of leftover
    exit legs."""
    if verdict.divergence:
        return AlertOnly(verdict.reason or f"{verdict.ticker}: divergence — {verdict.verdict}")
    if verdict.unresolved:
        return AlertOnly(verdict.reason or f"{verdict.ticker}: {verdict.verdict}")
    if verdict.status == OrderStatus.FILLED.value:
        return _advance_filled(verdict)
    if verdict.status in _TERMINAL_NON_FILLED:
        return CancelRemaining()
    if verdict.status == OrderStatus.PARTIALLY_FILLED.value:
        # Risk 2: a partial entry fill leaves the position open; the protection
        # pass sizes the stop to whatever netted qty is realized, but surface the
        # partial as an alert too so the operator sees the in-progress fill.
        filled = verdict.details.get("filled_quantity")
        return AlertOnly(
            f"{verdict.ticker}: entry PARTIALLY_FILLED (order {verdict.entry_order_id}, "
            f"filled {filled!r}) — position open, protection sized to netted fill"
        )
    return NoOp()  # still WORKING, not past TTL


def _advance_filled(verdict: ReconcileVerdict) -> Action:
    """A FILLED entry. The terminal round-trip-closed case still cancels leftover
    exit legs; the open-position case is handled entirely by the broker-state
    protection pass, so ``advance`` returns ``NoOp`` (no journal-derived stop)."""
    if verdict.note == _NOTE_ROUND_TRIP_CLOSED:
        return CancelRemaining()
    return NoOp()


__all__ = [
    "Action",
    "AlertOnly",
    "AmendStop",
    "BrokerView",
    "CancelRemaining",
    "CancelSellLegs",
    "NoOp",
    "PlaceStop",
    "PlannedExit",
    "ProtectionView",
    "UpgradeToOco",
    "advance",
    "reconcile_protection",
]
