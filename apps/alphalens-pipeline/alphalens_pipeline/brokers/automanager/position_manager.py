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

import logging
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeIs

from broker_contract.contract import (
    _QTY_EPS,
    OrderState,
    OrderStatus,
    Position,
)
from broker_contract.exit_geometry import (
    ExitPolicy,
    SetupStaticPolicy,
    clamp_reanchor_target,
)

from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

logger = logging.getLogger(__name__)

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
class ReanchorFacts:
    """The fill-complete STOP re-anchor facts (PR-6b, broker-manager extraction
    memo §4.3), folded from the geometry shadow stamp journaled at placement
    (PR-6a's ``control_loop._geometry_shadow_stamp``). Minimal — TP reanchor is OUT OF SCOPE, only
    the disaster stop moves with the realized fill blend."""

    k_atr: float
    atr: float


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
    # PR-6b: the fill-complete reanchor facts (k_atr/atr), folded ONLY when the
    # governing planned line carries a "geometry" shadow stamp (PR-6a). ``None``
    # for every pre-PR-6a journal line and for every hand-built PlannedExit that
    # omits it — the default keeps every existing construction byte-identical.
    # Deliberately kept OUT of the deterministic-ref governing logic (next_gen /
    # next_amend_seq) — a reanchor arm never bumps those counters itself.
    reanchor: ReanchorFacts | None = None


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


# Reason tag the M1 OCO-lag guard stamps on its hold NoOp (issue #5). The control
# loop keys a daemon-lifetime per-uic consecutive counter on this exact string to
# surface a genuinely-stuck Q9 propagation lag; a wording change here fails the
# control-loop tests loudly rather than silently blinding the monitor.
_OCO_LAG_HOLD_REASON = "oco-lag-hold"


@dataclass(frozen=True)
class NoOp:
    """Do nothing this tick. The executor treats every NoOp as a no-op.

    ``uic`` / ``reason`` are OPTIONAL diagnostics (default ``None`` / ``""``) so
    every bare ``NoOp()`` construction is byte-identical to before. The M1 OCO-lag
    guard stamps ``NoOp(uic=uic, reason=_OCO_LAG_HOLD_REASON)`` so the control loop
    can track a persistently-stuck lag (issue #5); a healthy-covered NoOp stays
    bare (``reason == ""``)."""

    uic: int | None = None
    reason: str = ""


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
    # PR-6b: the avg_price this amend reanchors the stop to, so the executor can
    # latch it (ProtectionView.reanchored_by_uic) ONLY on confirmed PATCH success.
    # ``None`` for every non-reanchor AmendStop (over-hedge downsize / plain grow)
    # — additive, so every existing construction stays byte-identical.
    reanchor_avg_price: float | None = None
    # Task 4: the high-water ``peak`` / live ``last_price`` this TRAIL amend was
    # computed from, carried on the action so the executor can stamp them on the
    # confirmed ``trailed`` journal marker (the telemetry substrate for the future
    # /edge trailing lens — the placed ``level`` alone cannot show how far the stop
    # trailed the market). ``None`` for every non-trail AmendStop — additive, so
    # every existing construction (over-hedge downsize / plain grow / reanchor)
    # stays byte-identical.
    trail_peak: float | None = None
    trail_last_price: float | None = None


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


# Env flag selecting the placement-time exit-geometry policy (PR-6a, broker-
# manager extraction memo §2.5 / §4.1). DEFAULTS to the brief's static
# disaster_stop/tp (geometry INERT — byte-identical to pre-PR-6 placement).
# Flipping to "atr_bracket_1p5" is now safe to enable live: the PR-6b
# fill-complete avg_price reanchor (``_maybe_reanchor`` below) ships alongside
# this flag, so ``build_default_deps`` no longer fail-fasts on it.
_EXIT_POLICY_ENV = "ALPHALENS_BROKER_EXIT_POLICY"
_DEFAULT_EXIT_POLICY = "setup_static"


def _exit_policy() -> str:
    """Active exit-geometry policy name (read at call time, restart-consistent
    and hermetically testable — same pattern as ``_oco_enabled``/``_amend_enabled``).

    Default ``"setup_static"`` = the brief's static disaster_stop/tp (geometry
    INERT, byte-identical to pre-PR-6 placement). Flip to ``"atr_bracket_1p5"``
    to activate both the placement-time ATR-bracket geometry (PR-6a) AND the
    fill-complete avg_price reanchor (PR-6b, ``_maybe_reanchor``) together —
    the two ship as one flag so a live flip is never geometry-without-reanchor."""
    value = os.environ.get(_EXIT_POLICY_ENV, "").strip()
    return value or _DEFAULT_EXIT_POLICY


# PR-6b idempotence-latch tolerance: avg_price only changes on a NEW fill (a
# qty-weighted blend re-averages), so a near-exact match against the last
# reanchored value means the reanchor already fired for this blend — never a
# genuine drift worth re-firing over. Same order of magnitude as _QTY_EPS.
_REANCHOR_AVG_PRICE_EPS = 1e-6

# Task 2 trailing-stop ratchet step [in_sample]: the coarse price increment a new
# trailing target must clear ABOVE the last live trailed level before ``_maybe_trail``
# re-fires. Sized well above tick noise so a resting stop is not re-PATCHed every
# tick for a sub-cent peak wiggle (each amend is a request-id + a broker round-trip);
# it bounds trail chatter, NOT correctness (the never-below-brief-floor clamp is the
# capital guard). Deliberately much coarser than _REANCHOR_AVG_PRICE_EPS.
_TRAIL_STEP_EPS = 0.02


@dataclass(frozen=True)
class ProtectionView:
    """The ONE per-tick snapshot the pure reconciler diffs (assembled by
    ``control_loop.build_protection_view``; saxo-oco memo §6). Protection is a
    function of live broker state ONLY — no journal line asserts it.

    ``planned_by_uic`` supplies the plan PRICES the broker cannot know, joined by
    uic. ``oco_unsupported`` is the persisted per-instrument capability flag
    (Stage 2) UNIONED with unexpired transient ``oco_too_far`` markers (a
    TooFarFromMarket reject degrades the uic only for a TTL — see
    ``control_loop.build_protection_view``)."""

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
    # PR-6b: the idempotence latch for the fill-complete reanchor — uic -> the
    # avg_price it was last reanchored at (folded from the ``reanchored``
    # journal marker, written ONLY on a confirmed amend success). A PERMANENT
    # latch per blend (no TTL, unlike the two sets above): avg_price only
    # changes on a new fill, so a near-exact match means the reanchor already
    # fired for THIS blend. The latch is JOURNAL-lifetime, not position-lifetime
    # (latest-by-ts per uic across all history): if a uic reanchors at price X,
    # fully exits, and a brand-new position on the same uic later realizes a blend
    # within _REANCHOR_AVG_PRICE_EPS of X, the stale latch suppresses that new
    # reanchor — the stop then simply stays at its placement-time planned distance
    # (benign: never a naked window or a bad stop, and an exact 1e-6 collision is
    # negligible). Default empty dict so pure tests + a second broker stay
    # source-compatible.
    reanchored_by_uic: Mapping[int, float] = field(default_factory=dict)
    # The behavioral exit policy for this tick, threaded from ``deps.exit_policy``
    # by ``control_loop.build_protection_view`` (resolved ONCE at startup). Default =
    # the inert ``setup_static`` so any ProtectionView built without it behaves like
    # today's dark path (pure tests + a second broker stay source-compatible).
    exit_policy: ExitPolicy = field(default_factory=SetupStaticPolicy)
    # Task 2 trailing-stop inputs, read ONLY by ``_maybe_trail`` (the trailing arm).
    # ``peak_by_uic`` is the high-water mark since entry (the Chandelier anchor);
    # ``last_price_by_uic`` the latest observed price (a future policy may use it).
    # Both are populated by ``control_loop.build_protection_view`` from a real
    # price feed in a LATER task; the empty defaults keep the arm dark (a missing
    # peak is a feed veto -> None), so pure tests + a second broker stay
    # source-compatible. ``trailed_stop_by_uic`` is the never-DOWN ratchet: uic ->
    # the level the stop was last CONFIRMED trailed to (folded from the ``trailed``
    # journal marker, latest-by-ts), the live-history floor a new proposal must
    # clear by ``_TRAIL_STEP_EPS``. Default empty = no prior trail on record.
    # Like ``reanchored_by_uic`` above, the fold is JOURNAL-lifetime, not
    # position-lifetime (latest-by-ts per uic across all history): if a uic
    # trails to level X, fully exits, and a brand-new position on the same uic
    # is later re-picked, the stale ``trailed`` marker still folds in as that
    # uic's floor. This is benign — the ratchet in ``_maybe_trail`` only ever
    # gates a NEW proposal against the inherited floor, so the worst case is
    # the placed stop simply stays at its placement-time planned distance
    # (never loosened relative to the stale floor, never left naked).
    peak_by_uic: Mapping[int, float] = field(default_factory=dict)
    last_price_by_uic: Mapping[int, float] = field(default_factory=dict)
    trailed_stop_by_uic: Mapping[int, float] = field(default_factory=dict)


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
    """Order ids of STANDALONE (non-OCO) TP legs — the Bug-B lone-TP shape that
    holds an INDEPENDENT sell commitment and must be cancelled BEFORE a stop place.

    OCO Limit legs are EXCLUDED: an OCO TP is mutually exclusive with its sibling
    stop (the pair commits owned ONCE), and cancelling one OCO leg cascade-cancels
    the covering stop (control_loop ``_idempotent_cancel`` / the arm-A note). Since
    ``cancel_conflicting`` is cancelled BEFORE the place (``_execute_place_stop``),
    naming an OCO leg here would tear the pair down and open a fully-naked window
    before the replacement/delta stop lands. When a grown OCO pair falls to the B1
    additive / B2 place-first fallback (OCO-amend skipped: amend off, uic degraded,
    or a recent amend-fail), the OCO stop leaves ONLY via ``supersede_ids`` (cancel
    AFTER a successful place); its TP sibling is left in place (Q3a caps oversell).
    Mirrors the arm-A ``oco_ids`` exclusion so no OCO leg ever sits in an
    unconditional pre-cancel (never-naked invariant)."""
    return tuple(
        leg.order_id for leg in legs if leg.order_type in TP_TYPES and not _is_oco_leg(leg)
    )


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


def _finite_positive(value: float | None) -> TypeIs[float]:
    """Shared amend-arm guard: a usable price/ATR/peak — present, finite, > 0.

    ``None``, NaN, ±inf and the SIM NoAccess ``<= 0`` sentinel all read as
    "veto" (the caller returns ``None`` and the resting stop stays put).

    Returns ``TypeIs[float]`` rather than plain ``bool`` so the "present" half
    of the guard is visible to the type checker: every caller writes
    ``if not _finite_positive(x): return None`` and then uses ``x`` as a real
    price, which only reads as correct once this narrows."""
    return value is not None and math.isfinite(value) and value > 0


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


def _maybe_reanchor(
    uic: int,
    pos: Position,
    plan: PlannedExit,
    legs: tuple[OrderState, ...],
    view: ProtectionView,
) -> AmendStop | None:
    """PR-6b: the fill-complete STOP re-anchor arm (broker-manager extraction
    memo §4.3). A covered position's resting standalone stop was sized to the
    PLANNED blend at placement time; a gapped / deep fill realizes a DIFFERENT
    avg_price, so the live risk distance (``avg_price - stop_price``) drifts
    from the intended ``k_atr * atr``. This arm PATCHes the stop back to
    ``avg_price - k_atr * atr`` once the fill is complete, restoring the
    invariant — never on placement, never mid-fill (Q10 TOCTOU, the amend
    executor's own re-check).

    Fires ONLY when ALL hold:
      - ``view.exit_policy.decide_reanchor`` returns a non-None target — the
        cached behavioral policy (resolved ONCE at startup by
        ``build_protection_view``) is an active geometry policy. The inert
        ``setup_static`` policy always returns ``None`` here, so the arm stays
        dark by default; no env sentinel is read in this pass.
      - ``plan.reanchor is not None`` — the governing planned line carried a
        geometry shadow stamp (PR-6a); a pre-PR-6a plan never reanchors.
      - ``pos.avg_price`` is finite and > 0 — never anchor on the SIM
        NoAccess ``<= 0`` sentinel or a NaN/inf blend.
      - ``plan.reanchor.atr`` is finite and > 0 — never divide the risk
        distance by a degenerate ATR.
      - ``_sole_standalone_stop(legs)`` is not ``None`` — SCOPE: a clean
        resting standalone stop only. An OCO pair or a multi-stop shape is
        left to its own arms; this never touches them.
      - ``uic`` is not in ``view.amend_recently_failed`` — same retry
        backoff as every other amend arm.
      - the uic has NEVER been reanchored at THIS avg_price
        (``view.reanchored_by_uic``, ``_REANCHOR_AVG_PRICE_EPS`` tolerance) —
        the idempotence latch: a confirmed reanchor for a blend never re-fires
        for that same blend, only for a NEW one (the position grew/shrank via
        another fill).

    Never-below-brief-floor envelope (Decision 1): the proposed target passes
    through ``clamp_reanchor_target`` against ``plan.stop_price`` (the brief
    disaster floor). A reanchor may only ever tighten toward the floor — it
    NEVER moves the stop below ``plan.stop_price``. When the clamp would push
    the stop below the floor (a deep gap-down fill) the arm returns ``None`` and
    the resting stop stays put. Returns ``None`` (never a bad stop) whenever the
    policy or the envelope refuses (non-finite / ``<= 0`` / below-floor)."""
    policy = view.exit_policy
    if plan.reanchor is None:
        return None
    avg_price = pos.avg_price
    if not _finite_positive(avg_price):
        return None
    atr = plan.reanchor.atr
    if not _finite_positive(atr):
        return None
    sole = _sole_standalone_stop(legs)
    if sole is None:
        return None
    if uic in view.amend_recently_failed:
        return None
    latched = view.reanchored_by_uic.get(uic)
    if latched is not None and abs(latched - avg_price) <= _REANCHOR_AVG_PRICE_EPS:
        return None
    proposed = policy.decide_reanchor(avg_price, atr)
    if proposed is None:
        return None  # setup_static inert, or a degenerate the policy refuses
    clamped = clamp_reanchor_target(
        plan.stop_price,
        proposed,
        anchor_price=avg_price,
        min_distance_frac=policy.min_stop_distance_frac,
    )
    if clamped is None:
        logger.info(
            "reanchor refused (below brief floor): policy=%s proposed=%.4f prior_stop=%.4f avg_price=%.4f",
            policy.name,
            proposed,
            plan.stop_price,
            avg_price,
        )
        return None  # never-below-brief-floor (Decision 1) or degenerate -> keep the resting stop
    if not math.isclose(clamped, proposed):
        logger.info(
            "reanchor envelope clamped: policy=%s proposed=%.4f clamped=%.4f prior_stop=%.4f",
            policy.name,
            proposed,
            clamped,
            plan.stop_price,
        )
        # Deferred (#1015): persist this divergence to the append-only journal
        # (INC-2 memo section 7), not log-only.
    target = clamped
    owned = pos.quantity
    return AmendStop(
        uic,
        _SIDE,
        sole.order_id,
        sole.order_type or "StopIfTraded",
        owned,
        target,
        _exit_amend_ref(plan.entry_crid, plan.next_amend_seq()),
        reason="reanchor-on-fill",
        reanchor_avg_price=avg_price,
    )


def _maybe_trail(
    uic: int,
    pos: Position,
    plan: PlannedExit,
    legs: tuple[OrderState, ...],
    view: ProtectionView,
) -> AmendStop | None:
    """Task 2: the bot-amend trailing-stop arm (sibling of ``_maybe_reanchor``). A
    covered position's resting standalone stop is PATCHed UP to the Chandelier
    target ``peak - k_atr * atr`` as the injected high-water mark rises, once the
    ``trailing_atr`` policy is armed (``activation_r`` R-multiples in profit). The
    stop moves UP ONLY — two independent guards enforce it: (1) the RATCHET vs the
    last CONFIRMED live trailed level (``view.trailed_stop_by_uic``) — a new
    proposal must clear a coarse ``_TRAIL_STEP_EPS`` step above it, so a peak
    wiggle never re-PATCHes and the level never drops vs the live trail history;
    (2) the never-below-brief-floor ``clamp_reanchor_target`` vs ``plan.stop_price``
    (the brief disaster floor), anchored on the LIVE PRICE
    (``view.last_price_by_uic``) so the min-distance floor caps the stop just below
    the current MARKET and the stop CAN ratchet above entry to lock profit — unlike
    ``_maybe_reanchor``, which anchors on ``avg_price`` for its one-shot fill
    reanchor (correct there, wrong here — an avg_price anchor forbids locking profit).

    PURE oracle: reads the peak from the ``ProtectionView`` snapshot only — never
    fetches a feed, never holds state. Fires ONLY when ALL hold (same guard order
    as ``_maybe_reanchor`` for the shared preconditions):
      - ``view.exit_policy.trails`` — a trailing policy is cached (resolved ONCE
        at startup). ``setup_static`` / ``atr_bracket_1p5`` have ``trails=False``
        and are routed to ``_maybe_reanchor`` by ``_reconcile_long`` instead, so
        this arm never touches them.
      - ``plan.reanchor is not None`` — the governing planned line carried the
        geometry shadow stamp (its ``atr``); a pre-stamp plan never trails.
      - ``pos.avg_price`` finite and > 0 — never anchor on the SIM NoAccess
        ``<= 0`` sentinel or a NaN/inf blend.
      - ``plan.reanchor.atr`` finite and > 0 — never a degenerate ATR.
      - ``_sole_standalone_stop(legs)`` is not ``None`` — a clean resting
        standalone stop only; an OCO pair / multi-stop shape is left to its arms.
      - ``uic`` not in ``view.amend_recently_failed`` — the shared amend backoff.
      - a finite, ``> 0`` ``view.peak_by_uic[uic]`` exists — a missing / degenerate
        peak is a feed veto (``None``), never a trail on a bad high-water mark.
      - a finite, ``> 0`` ``view.last_price_by_uic[uic]`` exists — the clamp
        anchor; a missing / degenerate live price is a feed veto (``None``), same
        discipline as the peak veto.
      - the policy returns a non-None target — dark before activation.
      - the never-below-brief-floor clamp allows the tighten.
      - the CLAMPED level (the level actually placed) clears ``_TRAIL_STEP_EPS``
        above the last trailed level — the ratchet gates on the post-clamp level,
        not the raw proposal, so a pullback can never place a stop below the trail
        history (Task 4 CARRYOVER-1).

    Returns ``None`` (never a bad stop) whenever any guard, the ratchet, or the
    envelope refuses."""
    policy = view.exit_policy
    if not policy.trails:
        return None
    if plan.reanchor is None:
        return None
    avg_price = pos.avg_price
    if not _finite_positive(avg_price):
        return None
    atr = plan.reanchor.atr
    if not _finite_positive(atr):
        return None
    sole = _sole_standalone_stop(legs)
    if sole is None:
        return None
    if uic in view.amend_recently_failed:
        return None
    peak = view.peak_by_uic.get(uic)
    if not _finite_positive(peak):
        return None  # feed veto / no peak yet
    last_price = view.last_price_by_uic.get(uic)
    if not _finite_positive(last_price):
        return None  # feed veto / no live price yet (same discipline as the peak veto)
    proposed = policy.decide_reanchor(avg_price, atr, peak=peak, last_price=last_price)
    if proposed is None:
        return None  # dark before activation (or a degenerate the policy refuses)
    # Anchor the min-distance floor to the LIVE PRICE, not the entry: for a
    # trailing stop the floor caps how close the stop may sit to the current
    # MARKET (never at/above market -> OnWrongSideOfMarket), while
    # ``prior_stop=plan.stop_price`` still enforces never-below-brief-floor.
    # Anchoring on ``avg_price`` (as the one-shot ``_maybe_reanchor`` correctly
    # does) would be wrong here: it caps the stop at ~0.998*avg_price, so an armed
    # trailing stop could never ratchet above breakeven to lock in profit.
    clamped = clamp_reanchor_target(
        plan.stop_price,
        proposed,
        anchor_price=last_price,
        min_distance_frac=policy.min_stop_distance_frac,
    )
    if clamped is None:
        logger.info(
            "trail refused (below brief floor): policy=%s proposed=%.4f prior_stop=%.4f avg_price=%.4f",
            policy.name,
            proposed,
            plan.stop_price,
            avg_price,
        )
        return None  # never-below-brief-floor or degenerate -> keep the resting stop
    # RATCHET (never-DOWN vs the live trail history) — gate on the CLAMPED level,
    # NOT the raw ``proposed`` (Task 4 CARRYOVER-1). Once ``trailed_stop_by_uic``
    # is non-empty (the marker writer landed in Task 4), gating the pre-clamp
    # proposal would be a reachable loosen: on a pullback ``proposed`` can clear
    # the floor while the live-price clamp pulls the PLACED level BELOW the prior
    # trailed level. Gating the post-clamp ``clamped`` keeps the level actually
    # placed strictly monotone-up vs the trail history — a new placed level must
    # clear a coarse _TRAIL_STEP_EPS step above the last CONFIRMED trailed level,
    # else the resting stop stays put (also bounds re-PATCH chatter on a sub-step
    # peak wiggle).
    floor = view.trailed_stop_by_uic.get(uic)
    if floor is not None and clamped <= floor + _TRAIL_STEP_EPS:
        return None
    target = clamped
    owned = pos.quantity
    return AmendStop(
        uic,
        _SIDE,
        sole.order_id,
        sole.order_type or "StopIfTraded",
        owned,
        target,
        _exit_amend_ref(plan.entry_crid, plan.next_amend_seq()),
        reason="trail",
        reanchor_avg_price=avg_price,
        trail_peak=peak,
        trail_last_price=last_price,
    )


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

    if total > owned + _QTY_EPS:
        return _reconcile_over_hedge(uic, owned, plan, legs, view)
    if stop_qty + _QTY_EPS < owned:
        return _reconcile_deficit(uic, owned, stop_qty, plan, legs, view)
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
    # (PR-6b / Task 2) POST-FILL STOP MOVE on a covered standalone stop. The two
    # arms are MUTUALLY EXCLUSIVE by the cached policy's ``trails`` flag so they
    # never both fire: a trailing policy (``trailing_atr``) trails the stop UP to
    # ``peak - k*atr`` (``_maybe_trail``); every other policy reanchors the stop
    # onto the realized fill blend (``_maybe_reanchor``). Both are dark by default
    # (the inert ``setup_static`` returns None) and never fire on the OCO-healthy
    # branch above (standalone stop only, by construction).
    if view.exit_policy.trails:
        action = _maybe_trail(uic, pos, plan, legs, view)
    else:
        action = _maybe_reanchor(uic, pos, plan, legs, view)
    if action is not None:
        return [action]
    return [NoOp()]


def _reconcile_over_hedge(
    uic: int, owned: float, plan: PlannedExit, legs: tuple[OrderState, ...], view: ProtectionView
) -> list[Action]:
    """(A) OVER-HEDGE arm of ``_reconcile_long``: the sell commitment exceeds netted
    owned (an exit partially filled or the position shrank). Places a residual-sized
    stop FIRST (never a naked repair window) — see the inline notes."""
    # (A) OVER-HEDGE: an exit leg partially filled (netted owned shrank) or the
    #     position shrank -> total sell > owned. Place a residual-sized stop FIRST
    #     (never a naked repair window). The old STOP legs leave EXCLUSIVELY via
    #     PlaceStop.supersede_ids — i.e. ONLY after the residual place succeeds — so
    #     a deferred place (Saxo SellOrdersAlreadyExist) leaves the old over-sized
    #     stop resting = over-covered, NEVER naked (the Bug-A cardinal sin). The
    #     separate, unconditional CancelSellLegs names ONLY the NON-stop legs
    #     (TP / Market noise); when there are none (Stage-1 stop-only) it is not
    #     emitted at all. Stops must never sit in an unconditional cancel.
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
    # (M1) OCO NoOp guard: reaching here with a clean unfilled OCO pair
    # (oco_stop is not None) yet commitment > owned is one of two SAFE states,
    # both NoOp'd one tick rather than torn down:
    #   (a) PROPAGATION LAG — the downsize amend already resized the stop to
    #       <= owned last tick, but list-orders lags Q9's symmetric propagation
    #       so the TP leg still shows the OLD larger Amount. NoOp until the TP
    #       read catches up (then total == owned -> arm C NoOp).
    #   (b) AMEND SKIPPED — the downsize amend above was skipped because the uic
    #       is in amend_recently_failed (or oco_unsupported), so the stop leg may
    #       still be > owned (a genuine over-hedge, not merely a lag). NoOp is
    #       the deliberate hold: the OCO stop keeps covering the downside (excess
    #       sell NotOwned-capped, cash account cannot short), the TTL self-clears,
    #       and the next tick's downsize amend resizes to owned. Place-residual-
    #       first here would POST a doomed 3rd sell (SellOrdersAlreadyExist while
    #       the OCO rests) -> alert spam, ending equally over-covered.
    # Either way the downside is fully covered and the pair is never torn down.
    # Gated on _amend_enabled() so arm A stays byte-identical when the flag off.
    # The NoOp carries uic + reason so the control loop can count consecutive
    # holds and page once if a lag genuinely stalls (issue #5); the executor
    # still treats it as nothing.
    if _amend_enabled() and oco_stop is not None:
        return [NoOp(uic=uic, reason=_OCO_LAG_HOLD_REASON)]
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
    non_stop_ids = tuple(oid for oid in bad.order_ids if oid not in stop_ids and oid not in oco_ids)
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


def _reconcile_deficit(
    uic: int,
    owned: float,
    stop_qty: float,
    plan: PlannedExit,
    legs: tuple[OrderState, ...],
    view: ProtectionView,
) -> list[Action]:
    """(B) DOWNSIDE-DEFICIT arm of ``_reconcile_long``: the resting stop qty is below
    netted owned (naked, grew past the covering stop, a lone-TP Bug-B shape, or a
    stale/partial stop). Covers the deficit without ever opening a naked window."""
    # (B) DOWNSIDE DEFICIT: naked, grew past the covering stop, a lone-TP Bug-B
    #     shape, or a stale/partial stop. A lone TP is always cancelled BEFORE the
    #     place (it holds the conflicting sell commitment — Bug B).
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
    #      Skipped only when Q5 is off. DELIBERATELY NOT gated on
    #      ``oco_unsupported`` (VRNS incident 2026-07-29): that marker means the
    #      OCO PAIR feature failed — often transiently, e.g. TooFarFromMarket at
    #      a volatile open — and says nothing about plain additive stops, which
    #      the broker accepts on such a uic. Gating B1 on it dropped a
    #      multi-tier gap fill into B2, whose place-FIRST full-owned stop can
    #      never be accepted while the partial stop rests (sum > owned ->
    #      SellOrdersAlreadyExist) -> deferred every tick -> the grown delta
    #      stayed naked until a manual cover.
    #      Edge: ``next_gen`` keys the ref on qty, so two grow steps of the SAME
    #      delta within Saxo's 15 s request-id dedup window share a ref and the
    #      2nd is deduped away — that slice is under-covered for < 15 s and
    #      self-heals on the next tick once the window passes (the disaster stop
    #      is deep OTM, so the transient gap is immaterial).
    if ADDITIVE_STOPS_CONFIRMED and stop_qty > _QTY_EPS:
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
    "_OCO_LAG_HOLD_REASON",
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
    "ReanchorFacts",
    "UpgradeToOco",
    "advance",
    "reconcile_protection",
]
