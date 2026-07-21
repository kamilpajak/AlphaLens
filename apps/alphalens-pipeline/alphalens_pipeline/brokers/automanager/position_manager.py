"""Position-manager — the "act" half of the auto-manager loop.

Pure decision function: one reconcile verdict + a per-tick BrokerView (the
control loop assembles it from the broker + the standalone-stop journal) -> the
single Action to take. No I/O — the control loop executes the returned Action.

MVP action set (design memo Components §9):
  entry FILLED, position open, no protective stop yet
      -> PlaceStandaloneStop(realized filled qty, journaled disaster stop)
  round-trip closed / CANCELLED / REJECTED / EXPIRED -> CancelRemaining
  PAST-TTL / divergence / UNRESOLVED -> AlertOnly(reason) (never auto-cancel)
  else (still WORKING, or already protected) -> NoOp

Realized-qty rule (Risk 2): the stop MUST size to the REALIZED entry fill
(verdict.details['filled_quantity']), NEVER planned verdict.qty — a planned-qty
stop over-hedges and can flip short after a partial fill.

Broker-state-truth protection (saxo-oco memo §6): ``reconcile_protection`` /
``_reconcile_long`` are a SECOND, pure decision layer that derives protection
from a live-broker snapshot (``ProtectionView``) instead of any journal line —
this kills Bug A (a failed stop POST leaving a permanently-naked position) and
Bug B (a lone-TP double-sell). Keyed per-uic (the unit Saxo nets to), sized to
netted owned qty. STAGE 1 IS STOP-ONLY: ``_oco_enabled()`` returns ``False`` so
the rung 1 -> 2 OCO upgrade (``UpgradeToOco``) is defined but never emitted.
The control loop (Task 6) assembles the ``ProtectionView`` and executes the
returned Actions; this module performs no I/O.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class DisasterStop:
    uic: int
    side: str  # "SELL" for a long position's protective stop
    stop_price: float


def _default_next_gen(_qty: float) -> int:
    """Fallback resize counter for a hand-built ``PlannedExit`` (pure tests): no
    persistence, always generation 0. The control loop injects the real
    journal-backed callable via ``_fold_planned_exits`` (saxo-oco memo §4.5)."""
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


@dataclass(frozen=True)
class BrokerView:
    protected_request_ids: frozenset[str]  # entries already carrying a live standalone stop
    disaster_stops: Mapping[
        str, DisasterStop
    ]  # journaled disaster stop per entry client_request_id
    working_children: Mapping[str, tuple[str, ...]]  # request_id -> still-working exit order ids


@dataclass(frozen=True)
class PlaceStandaloneStop:
    qty: float
    stop_price: float


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
    """Rung 1 -> 2 TP-capture upgrade. DEFINED for Stage 2; NEVER emitted in
    Stage 1 (``_oco_enabled()`` returns ``False`` so ``_reconcile_long`` degrades
    the covered branch to ``NoOp``). Kept here so Task 6's executor can type-guard
    it out without a forward reference."""

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
    """Cancel the named SELL legs on a uic (orphan sweep, or the over-committed
    group in an over-hedge repair). Idempotent + cascade-safe at the executor."""

    uic: int
    order_ids: tuple[str, ...]
    reason: str


Action = (
    PlaceStop
    | UpgradeToOco
    | CancelSellLegs
    | PlaceStandaloneStop
    | CancelRemaining
    | AlertOnly
    | NoOp
)

# A SELL leg that PROTECTS the downside vs one that is UPSIDE only (memo §6).
STOP_TYPES = frozenset({"StopIfTraded", "Stop", "TrailingStopIfTraded"})
TP_TYPES = frozenset({"Limit"})

# Q5 (same-uic stops sum cleanly) is UNCONFIRMED, so Stage 1 never places an
# additive delta stop — the deficit arm always cancel-replaces with the
# place-full-owned-stop-first ordering. Flipped on only after a green SIM probe.
ADDITIVE_STOPS_CONFIRMED = False


def _oco_enabled() -> bool:
    """Stage 1 is STOP-ONLY: the rung 1 -> 2 OCO upgrade stays dark. Always
    ``False`` here so ``_reconcile_long`` never emits ``UpgradeToOco``."""
    return False


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

    Emits, in order: (1) per netted LONG, the downside-cover arm; (2) an orphan
    sweep for SELL legs on a uic with no long (else they can fire into a naked
    short); (3) a negative-position alert. STOP-ONLY: no ``UpgradeToOco`` is ever
    returned in Stage 1."""
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

    stop_qty = sum(leg.amount or 0.0 for leg in legs if leg.order_type in STOP_TYPES)
    tp_qty = sum(leg.amount or 0.0 for leg in legs if leg.order_type in TP_TYPES)
    total = stop_qty + tp_qty

    # (A) OVER-HEDGE: an exit leg partially filled (netted owned shrank) or the
    #     position shrank -> total sell > owned. Place a residual-sized stop FIRST
    #     (never a naked repair window), then cancel the over-committed group.
    if total > owned + _QTY_EPS:
        bad = _group_with_partial_fill(legs) or _newest_group(legs)
        gen = plan.next_gen(owned)
        return [
            PlaceStop(
                uic,
                _SIDE,
                owned,
                plan.stop_price,
                _exit_stop_ref(plan.entry_crid, gen),
                supersede_ids=bad.stop_leg_ids,  # keep old stop until the residual is confirmed
            ),
            CancelSellLegs(uic, bad.order_ids, reason="over-hedge repair (post-place)"),
        ]

    # (B) DOWNSIDE DEFICIT: naked, grew past the covering stop, a lone-TP Bug-B
    #     shape, or a stale/partial stop. Cancel-replace, place-full-owned-first
    #     (Stage 1: additive-on-growth stays dark until Q5 is green). A lone TP is
    #     cancelled BEFORE the place (it holds the conflicting sell commitment); a
    #     stale stop is superseded AFTER (no naked window on the covered shares).
    if stop_qty + _QTY_EPS < owned:
        gen = plan.next_gen(owned)
        return [
            PlaceStop(
                uic,
                _SIDE,
                owned,
                plan.stop_price,
                _exit_stop_ref(plan.entry_crid, gen),
                supersede_ids=_stop_leg_ids(legs),  # stale stop -> cancel AFTER
                cancel_conflicting=_tp_only_leg_ids(legs),  # lone TP -> cancel BEFORE (Bug B)
            )
        ]

    # (C) DOWNSIDE COVERED. The rung 1 -> 2 TP-capture upgrade is Stage 2 only.
    if tp_qty + _QTY_EPS >= owned:  # pragma: no cover — Stage 1: a full TP + a covering stop
        return [NoOp()]  # trips arm (A) over-hedge first; reachable only at rung 2
    if plan.tp_price is None or uic in view.oco_unsupported or not _oco_enabled():
        return [NoOp()]  # STOP-ONLY: stop-only is the accepted terminal rung
    return [  # pragma: no cover — Stage 1 keeps _oco_enabled() False; guarded for Stage 2
        UpgradeToOco(
            uic,
            _SIDE,
            owned,
            plan.stop_price,
            plan.tp_price,
            plan.entry_crid,
            plan.next_gen(owned),
            supersede_ids=_stop_leg_ids(legs),
        )
    ]


def advance(verdict: ReconcileVerdict, broker_view: BrokerView) -> Action:
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
    "BrokerView",
    "CancelRemaining",
    "CancelSellLegs",
    "DisasterStop",
    "NoOp",
    "PlaceStandaloneStop",
    "PlaceStop",
    "PlannedExit",
    "ProtectionView",
    "UpgradeToOco",
    "advance",
    "reconcile_protection",
]
