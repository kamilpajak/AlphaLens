"""Property-based tests for the PURE reconcile decision function.

Target under test: ``reconcile_protection(view)`` and ``_reconcile_long(uic,
pos, view)`` in ``alphalens_pipeline.brokers.automanager.position_manager`` — a
deterministic ``ProtectionView -> list[Action]`` map with NO I/O.

The example-based suite lives in ``test_position_manager.py`` (fixtures
``_pos``/``_leg``/``_plan``/``_pview``). This module adds hypothesis-driven
coverage of the SPEC invariants, following the PBT-series (#858-863) lessons:

  * ORACLE INDEPENDENCE — no property re-implements ``_reconcile_long`` or calls
    it twice to compare. Every invariant is an accounting identity / structural
    membership check derived from the SPEC over INPUT view fields joined to the
    returned Action dataclass fields. The one meta-property (determinism) asserts
    ``f(x) == f(x)`` and rides alongside the substantive invariants.
  * MEASURE NON-VACUOUSNESS — constructive per-arm builders guarantee every arm
    is reached BY CONSTRUCTION; a companion test collects the arm label of every
    drawn component and FAILS loudly if any arm-coverage target was never emitted.
  * ``_QTY_EPS`` tolerance on every float compare (never bare ==/</<=).
  * All tests subclass ``unittest.TestCase`` (pytest-style @given functions are
    SILENTLY skipped in this repo's CI); @given decorates methods.

FINAL-sell removal set (spec disagreement resolved in favour of derivation 1):
removal = union(PlaceStop.supersede_ids) ∪ union(PlaceStop.cancel_conflicting) ∪
union(CancelSellLegs.order_ids). CancelSellLegs.order_ids MUST be included — in
arm A the trailing CancelSellLegs drops only the NON-stop legs (TP / Market
noise), while the STOP legs leave EXCLUSIVELY via PlaceStop.supersede_ids after a
successful place (never a naked window). The removal union is unchanged: non-stop
ids via CancelSellLegs.order_ids, stop ids via supersede — only the partition
between the two moves. Omitting the cancel would count the TP amount as surviving
and yield a false over-commit.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from alphalens_pipeline.brokers.automanager import position_manager as pm
from alphalens_pipeline.brokers.automanager.position_manager import (
    STOP_TYPES,
    TP_TYPES,
    AlertOnly,
    AmendStop,
    CancelSellLegs,
    NoOp,
    PlaceStop,
    PlannedExit,
    ProtectionView,
    UpgradeToOco,
    _is_oco_leg,
    reconcile_protection,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    _reconcile_long as reconcile_long,
)
from alphalens_pipeline.brokers.contract import (
    _QTY_EPS,
    InstrumentRef,
    OrderState,
    OrderStatus,
    Position,
)
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

_UIC = 43070


# --------------------------------------------------------------------------
# View construction helpers (mirror the example-suite _leg/_pos/_plan style).
# --------------------------------------------------------------------------


def _instrument(uic: int) -> InstrumentRef:
    return InstrumentRef(
        ticker="BIO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id=str(uic),
        broker_symbol="BIO:xnys",
    )


def _mk_pos(qty: float, uic: int) -> Position:
    return Position(
        instrument=_instrument(uic),
        quantity=qty,
        avg_price=296.0,
        market_value=None,
        unrealized_pnl=None,
        position_id=f"pos-{uic}",
    )


def _mk_leg(
    order_id: str,
    order_type: str,
    amount: float | None,
    uic: int,
    *,
    filled: float = 0.0,
) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=filled,
        raw_status="Working",
        uic=uic,
        side="SELL",
        order_type=order_type,
        amount=amount,
        external_reference=order_id,
    )


def _mk_plan(
    uic: int,
    *,
    tp_price: float | None = None,
    conflicting: bool = False,
    n_plans: int = 1,
    entry_crid: str = "crid",
    stop_price: float = 216.48,
) -> PlannedExit:
    # next_gen deliberately left at the module default (_default_next_gen -> 0)
    # so PlaceStop.request_id is deterministic and the determinism property is real.
    return PlannedExit(
        uic=uic,
        entry_crid=entry_crid,
        side="SELL",
        stop_price=stop_price,
        tp_price=tp_price,
        conflicting=conflicting,
        n_plans=n_plans,
    )


# --------------------------------------------------------------------------
# SPEC primitives — recomputed INDEPENDENTLY from the view (never by calling
# the reconciler). Mirror the module's explicit-None guard exactly.
# --------------------------------------------------------------------------


def _leg_amt(leg: OrderState) -> float:
    return leg.amount if leg.amount is not None else 0.0


def _stop_qty(legs: tuple[OrderState, ...]) -> float:
    return sum(_leg_amt(leg) for leg in legs if leg.order_type in STOP_TYPES)


def _tp_qty(legs: tuple[OrderState, ...]) -> float:
    return sum(_leg_amt(leg) for leg in legs if leg.order_type in TP_TYPES)


def _stop_leg_ids(legs: tuple[OrderState, ...]) -> set[str]:
    return {leg.order_id for leg in legs if leg.order_type in STOP_TYPES}


def _tp_leg_ids(legs: tuple[OrderState, ...]) -> set[str]:
    return {leg.order_id for leg in legs if leg.order_type in TP_TYPES}


def _le(a: float, b: float) -> bool:
    """a <= b within tolerance."""
    return a <= b + _QTY_EPS


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _QTY_EPS


def _placestops(actions: list, uic: int | None = None) -> list[PlaceStop]:
    return [a for a in actions if isinstance(a, PlaceStop) and (uic is None or a.uic == uic)]


def _cancels(actions: list, uic: int | None = None) -> list[CancelSellLegs]:
    return [a for a in actions if isinstance(a, CancelSellLegs) and (uic is None or a.uic == uic)]


def _removal_ids(actions: list, uic: int) -> set[str]:
    """Full FINAL-sell removal set for one uic: superseded ∪ cancel_conflicting ∪
    CancelSellLegs.order_ids (derivation 1 — MUST include the trailing cancel)."""
    ids: set[str] = set()
    for a in _placestops(actions, uic):
        ids |= set(a.supersede_ids)
        ids |= set(a.cancel_conflicting)
    for a in _cancels(actions, uic):
        ids |= set(a.order_ids)
    return ids


# --------------------------------------------------------------------------
# Component records + constructive per-arm builders. Each long builder returns a
# _LongComp whose ``label`` is exactly one arm-coverage target; the region
# predicate holds BY CONSTRUCTION so no assume()-starvation of the narrow arms.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _LongComp:
    uic: int
    position: Position
    legs: tuple[OrderState, ...]
    plan: PlannedExit | None
    oco_unsupported: bool
    label: str


def _view_from_long(c: _LongComp) -> ProtectionView:
    legs_map = {c.uic: c.legs} if c.legs else {}
    plan_map = {c.uic: c.plan} if c.plan is not None else {}
    oco = frozenset({c.uic}) if c.oco_unsupported else frozenset()
    return ProtectionView(
        long_positions={c.uic: c.position},
        all_positions={c.uic: c.position},
        sell_legs_by_uic=legs_map,
        planned_by_uic=plan_map,
        oco_unsupported=oco,
    )


_SIMPLE_OWNED = st.one_of(
    st.integers(2, 5).map(float),
    st.integers(2, 500).map(float),
    st.sampled_from([5000.0, 9999.0]),
)
_BAND_OWNED = st.integers(6, 500).map(float)  # room for the additive (2eps, owned-2eps) band
_ALERT_OWNED = _SIMPLE_OWNED


@st.composite
def _build_plan_none(draw, uic: int) -> _LongComp:
    owned = draw(_ALERT_OWNED)
    event("arm:plan_none_alert")
    return _LongComp(uic, _mk_pos(owned, uic), (), None, False, "plan_none_alert")


@st.composite
def _build_conflicting(draw, uic: int) -> _LongComp:
    owned = draw(_ALERT_OWNED)
    n = draw(st.integers(2, 4))
    plan = _mk_plan(uic, conflicting=True, n_plans=n)
    event("arm:plan_conflicting_alert")
    return _LongComp(uic, _mk_pos(owned, uic), (), plan, False, "plan_conflicting_alert")


@st.composite
def _build_over_hedge_partial(draw, uic: int) -> _LongComp:
    owned = draw(_SIMPLE_OWNED)
    t = draw(st.integers(1, 500).map(float))
    f = draw(st.floats(0.6, 50.0))  # a leg partially filled -> _group_with_partial_fill
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", owned, uic)
    tp = _mk_leg(f"{uic}-tp", "Limit", t, uic, filled=f)
    plan = _mk_plan(uic, tp_price=306.72)
    event("arm:arm_A_over_hedge_partial_fill")
    return _LongComp(
        uic, _mk_pos(owned, uic), (stop, tp), plan, False, "arm_A_over_hedge_partial_fill"
    )


@st.composite
def _build_over_hedge_deficit(draw, uic: int) -> _LongComp:
    # Arm A wins over B while the stop side is a genuine deficit (naked-ish stop +
    # a huge TP). Exercises INV2 "deficit_never_yields_noop even when A fires".
    owned = draw(st.integers(3, 500).map(float))
    s = draw(st.integers(1, int(owned) - 2).map(float))  # s < owned - eps -> deficit
    f = draw(st.floats(0.6, 50.0))
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", s, uic)
    tp = _mk_leg(f"{uic}-tp", "Limit", owned, uic, filled=f)  # total = s + owned > owned + eps
    plan = _mk_plan(uic, tp_price=306.72)
    event("arm:arm_A_over_hedge_deficit_fill")
    return _LongComp(
        uic, _mk_pos(owned, uic), (stop, tp), plan, False, "arm_A_over_hedge_deficit_fill"
    )


@st.composite
def _build_over_hedge_newest(draw, uic: int) -> _LongComp:
    owned = draw(_SIMPLE_OWNED)
    d = draw(st.integers(1, 200).map(float))  # resting stop > owned, no fill -> _newest_group
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", owned + d, uic, filled=0.0)
    plan = _mk_plan(uic)
    event("arm:arm_A_over_hedge_newest_group")
    return _LongComp(
        uic, _mk_pos(owned, uic), (stop,), plan, False, "arm_A_over_hedge_newest_group"
    )


@st.composite
def _build_over_hedge_with_noise(draw, uic: int) -> _LongComp:
    # A non-protective 'Market' SELL leg: counted by neither stop_qty nor tp_qty,
    # yet swept in arm A (bad.order_ids). Documented separate arm.
    owned = draw(_SIMPLE_OWNED)
    t = draw(st.integers(1, 500).map(float))
    m = draw(st.integers(1, 100).map(float))
    f = draw(st.floats(0.6, 50.0))
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", owned, uic)
    tp = _mk_leg(f"{uic}-tp", "Limit", t, uic, filled=f)
    noise = _mk_leg(f"{uic}-mkt", "Market", m, uic, filled=0.0)
    plan = _mk_plan(uic, tp_price=306.72)
    event("arm:leg_type_noise_non_stop_non_tp")
    return _LongComp(
        uic, _mk_pos(owned, uic), (stop, tp, noise), plan, False, "leg_type_noise_non_stop_non_tp"
    )


@st.composite
def _build_additive_grow_plain(draw, uic: int) -> _LongComp:
    owned = draw(_BAND_OWNED)
    s = draw(st.integers(2, int(owned) - 2).map(float))  # (2eps, owned-2eps) -> B1 delta
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", s, uic)
    plan = _mk_plan(uic)
    event("arm:arm_B1_additive_grow_plain")
    return _LongComp(uic, _mk_pos(owned, uic), (stop,), plan, False, "arm_B1_additive_grow_plain")


@st.composite
def _build_additive_grow_lone_tp(draw, uic: int) -> _LongComp:
    owned = draw(st.integers(7, 500).map(float))
    s = draw(st.integers(2, int(owned) - 3).map(float))
    t = draw(st.integers(1, int(owned) - int(s)).map(float))  # s + t <= owned -> stays in B (not A)
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", s, uic)
    tp = _mk_leg(f"{uic}-tp", "Limit", t, uic)
    plan = _mk_plan(uic, tp_price=306.72)
    event("arm:arm_B1_additive_grow_with_lone_tp")
    return _LongComp(
        uic, _mk_pos(owned, uic), (stop, tp), plan, False, "arm_B1_additive_grow_with_lone_tp"
    )


@st.composite
def _build_cancel_replace_naked(draw, uic: int) -> _LongComp:
    owned = draw(_SIMPLE_OWNED)
    plan = _mk_plan(uic)
    event("arm:arm_B2_cancel_replace_naked")
    return _LongComp(uic, _mk_pos(owned, uic), (), plan, False, "arm_B2_cancel_replace_naked")


@st.composite
def _build_cancel_replace_zero_amount(draw, uic: int) -> _LongComp:
    owned = draw(_SIMPLE_OWNED)
    amt = draw(st.sampled_from([0.0, None]))  # exercises the explicit-None guard both ways
    stop = _mk_leg(f"{uic}-stop", "Stop", amt, uic)
    plan = _mk_plan(uic)
    event("arm:arm_B2_cancel_replace_zero_amount_stop")
    return _LongComp(
        uic, _mk_pos(owned, uic), (stop,), plan, False, "arm_B2_cancel_replace_zero_amount_stop"
    )


@st.composite
def _build_cancel_replace_lone_tp(draw, uic: int) -> _LongComp:
    owned = draw(_SIMPLE_OWNED)
    t = draw(st.integers(1, int(owned)).map(float))  # t <= owned -> stays in B (not A over-hedge)
    tp = _mk_leg(f"{uic}-tp", "Limit", t, uic)
    plan = _mk_plan(uic, tp_price=306.72)
    event("arm:arm_B2_cancel_replace_lone_tp")
    return _LongComp(uic, _mk_pos(owned, uic), (tp,), plan, False, "arm_B2_cancel_replace_lone_tp")


@st.composite
def _build_cancel_replace_oco_unsupported(draw, uic: int) -> _LongComp:
    owned = draw(_BAND_OWNED)
    s = draw(
        st.integers(2, int(owned) - 2).map(float)
    )  # B1-eligible band, but oco_unsupported -> B2
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", s, uic)
    plan = _mk_plan(uic)
    event("arm:arm_B2_cancel_replace_oco_unsupported")
    return _LongComp(
        uic, _mk_pos(owned, uic), (stop,), plan, True, "arm_B2_cancel_replace_oco_unsupported"
    )


@st.composite
def _build_covered_stop_only(draw, uic: int) -> _LongComp:
    owned = draw(_SIMPLE_OWNED)
    amt = owned - draw(st.sampled_from([0.0, 0.1, 0.4, 0.49]))  # covered (stop_qty + eps >= owned)
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", amt, uic)
    plan = _mk_plan(uic)
    event("arm:arm_C_covered_stop_only")
    return _LongComp(uic, _mk_pos(owned, uic), (stop,), plan, False, "arm_C_covered_stop_only")


@st.composite
def _build_covered_with_tp_price(draw, uic: int) -> _LongComp:
    owned = draw(_SIMPLE_OWNED)
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", owned, uic)
    plan = _mk_plan(uic, tp_price=306.72)  # STOP-ONLY: still NoOp, never UpgradeToOco
    event("arm:arm_C_covered_with_tp_price")
    return _LongComp(uic, _mk_pos(owned, uic), (stop,), plan, False, "arm_C_covered_with_tp_price")


@st.composite
def _build_covered_flicker(draw, uic: int) -> _LongComp:
    owned = draw(_SIMPLE_OWNED)
    amt = owned - draw(st.sampled_from([1e-7, 1e-5, 1e-4, 0.25, 0.49]))  # within eps of owned
    stop = _mk_leg(f"{uic}-stop", "StopIfTraded", amt, uic)
    plan = _mk_plan(uic)
    event("arm:arm_C_covered_float_tolerance_flicker")
    return _LongComp(
        uic, _mk_pos(owned, uic), (stop,), plan, False, "arm_C_covered_float_tolerance_flicker"
    )


@st.composite
def _build_covered_exact_multi(draw, uic: int) -> _LongComp:
    # Covered EXACTLY: one or more STOP legs summing to owned, no TP -> not
    # over-hedged, not a deficit. Dedicated anchor for "covered is [NoOp()]".
    owned = draw(st.integers(4, 500))
    if draw(st.booleans()):
        s1 = draw(st.integers(1, owned - 1))
        legs = (
            _mk_leg(f"{uic}-stopA", "StopIfTraded", float(s1), uic),
            _mk_leg(f"{uic}-stopB", "Stop", float(owned - s1), uic),
        )
    else:
        legs = (_mk_leg(f"{uic}-stop", "StopIfTraded", float(owned), uic),)
    plan = _mk_plan(uic)
    event("arm:anchor_covered_exact")
    return _LongComp(uic, _mk_pos(float(owned), uic), legs, plan, False, "anchor_covered_exact")


# Builder groups.
_ALERT_BUILDERS = [_build_plan_none, _build_conflicting]
_ARM_A_BUILDERS = [
    _build_over_hedge_partial,
    _build_over_hedge_deficit,
    _build_over_hedge_newest,
    _build_over_hedge_with_noise,
]
_B1_BUILDERS = [_build_additive_grow_plain, _build_additive_grow_lone_tp]
_B2_BUILDERS = [
    _build_cancel_replace_naked,
    _build_cancel_replace_zero_amount,
    _build_cancel_replace_lone_tp,
    _build_cancel_replace_oco_unsupported,
]
_C_BUILDERS = [_build_covered_stop_only, _build_covered_with_tp_price, _build_covered_flicker]
_PLACESTOP_BUILDERS = _ARM_A_BUILDERS + _B1_BUILDERS + _B2_BUILDERS
_NONALERT_BUILDERS = _PLACESTOP_BUILDERS + _C_BUILDERS
_ALL_LONG_BUILDERS = _ALERT_BUILDERS + _NONALERT_BUILDERS
# TP-bearing placestop builders (>=1 Limit leg) for the "every TP cleared" property.
_TP_BEARING_PLACESTOP_BUILDERS = [
    _build_over_hedge_partial,
    _build_over_hedge_deficit,
    _build_over_hedge_with_noise,
    _build_additive_grow_lone_tp,
    _build_cancel_replace_lone_tp,
]
_DEFICIT_BUILDERS = _B1_BUILDERS + _B2_BUILDERS + [_build_over_hedge_deficit]


def _one_of(builders, uic: int = _UIC):
    return st.one_of([b(uic) for b in builders])


# --------------------------------------------------------------------------
# Composite multi-uic view (reconcile_protection-level: orphan / short / multi).
# Unique uics partitioned DISJOINTLY into long / orphan / short pools.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Mixed:
    view: ProtectionView
    longs: tuple[_LongComp, ...]
    orphans: tuple[tuple[int, tuple[OrderState, ...]], ...]
    shorts: tuple[tuple[int, Position], ...]

    def labels(self) -> set[str]:
        out = {c.label for c in self.longs}
        if self.orphans:
            out.add("protection_orphan_sweep")
        if self.shorts:
            out.add("protection_short_alert")
        if len(self.longs) >= 2 and self.orphans and self.shorts:
            out.add("protection_multi_uic_concatenation")
        return out


@st.composite
def _build_orphan(draw, uic: int) -> tuple[int, tuple[OrderState, ...]]:
    n = draw(st.integers(1, 3))
    types = draw(
        st.lists(
            st.sampled_from(["StopIfTraded", "Limit", "Market", "Stop"]), min_size=n, max_size=n
        )
    )
    legs = []
    for i, t in enumerate(types):
        amt = draw(st.one_of(st.none(), st.just(0.0), st.integers(1, 100).map(float)))
        legs.append(_mk_leg(f"{uic}-orph{i}", t, amt, uic))
    event("arm:protection_orphan_sweep")
    return uic, tuple(legs)


@st.composite
def _build_short_pos(draw, uic: int) -> tuple[int, Position]:
    q = draw(st.one_of(st.just(-0.6), st.integers(1, 200).map(lambda n: -float(n))))
    event("arm:protection_short_alert")
    return uic, _mk_pos(q, uic)


@st.composite
def _mixed_view(
    draw,
    *,
    require_multi=False,
    require_orphan=False,
    require_short=False,
    require_all_longs=False,
) -> _Mixed:
    # require_all_longs makes the non-vacuousness gate DETERMINISTIC: instead of
    # sampling n_long random builders (coverage of every arm would then be only
    # probabilistic), include exactly one long from EACH builder, so a single
    # example already emits every long-arm label.
    n_long = (
        len(_ALL_LONG_BUILDERS)
        if require_all_longs
        else draw(st.integers(2 if require_multi else 1, 3))
    )
    n_orphan = draw(st.integers(1 if require_orphan else 0, 2))
    n_short = draw(st.integers(1 if require_short else 0, 2))
    total = n_long + n_orphan + n_short
    uics = draw(st.lists(st.integers(1000, 99999), unique=True, min_size=total, max_size=total))
    long_uics = uics[:n_long]
    orphan_uics = uics[n_long : n_long + n_orphan]
    short_uics = uics[n_long + n_orphan :]

    if require_all_longs:
        longs = tuple(
            draw(builder(u)) for builder, u in zip(_ALL_LONG_BUILDERS, long_uics, strict=True)
        )
    else:
        longs = tuple(draw(_one_of(_ALL_LONG_BUILDERS, u)) for u in long_uics)
    orphans = tuple(draw(_build_orphan(u)) for u in orphan_uics)
    shorts = tuple(draw(_build_short_pos(u)) for u in short_uics)

    long_positions: dict[int, Position] = {}
    all_positions: dict[int, Position] = {}
    sell_legs: dict[int, tuple[OrderState, ...]] = {}
    planned: dict[int, PlannedExit] = {}
    oco: set[int] = set()
    for c in longs:
        long_positions[c.uic] = c.position
        all_positions[c.uic] = c.position
        if c.legs:
            sell_legs[c.uic] = c.legs
        if c.plan is not None:
            planned[c.uic] = c.plan
        if c.oco_unsupported:
            oco.add(c.uic)
    for uic, legs in orphans:
        sell_legs[uic] = legs  # NOT in long_positions/all_positions -> orphan sweep
    for uic, pos in shorts:
        all_positions[uic] = pos  # NOT in long_positions -> short alert

    view = ProtectionView(
        long_positions=long_positions,
        all_positions=all_positions,
        sell_legs_by_uic=sell_legs,
        planned_by_uic=planned,
        oco_unsupported=frozenset(oco),
    )
    return _Mixed(view, longs, orphans, shorts)


_LONG_SETTINGS = settings(deadline=None, max_examples=300)
_COMPOSITE_SETTINGS = settings(
    deadline=None, max_examples=200, suppress_health_check=[HealthCheck.too_slow]
)


# --------------------------------------------------------------------------
# INVARIANTS (one property per invariant).
# --------------------------------------------------------------------------


class TestFinalSellInvariants(unittest.TestCase):
    """Accounting identities over FINAL intended sell state (INV1/INV3/INV5)."""

    @given(_one_of(_NONALERT_BUILDERS))
    @_LONG_SETTINGS
    def test_final_sell_le_owned(self, c: _LongComp) -> None:
        # INV1 — exercises A, B1, B2, C (incl. the Market-noise arm-A variant).
        view = _view_from_long(c)
        actions = reconcile_long(c.uic, c.position, view)
        owned = c.position.quantity
        removal = _removal_ids(actions, c.uic)
        surviving = sum(_leg_amt(leg) for leg in c.legs if leg.order_id not in removal)
        placed = sum(a.qty for a in _placestops(actions, c.uic))
        final = surviving + placed
        self.assertTrue(
            _le(final, owned),
            f"FINAL sell {final} exceeds owned {owned} (label={c.label}, removal={removal})",
        )

    @given(_one_of(_PLACESTOP_BUILDERS))
    @_LONG_SETTINGS
    def test_final_stop_coverage_lands_on_owned(self, c: _LongComp) -> None:
        # INV3 — whenever a stop is placed, surviving-stop + placed == owned.
        view = _view_from_long(c)
        actions = reconcile_long(c.uic, c.position, view)
        stops = _placestops(actions, c.uic)
        self.assertTrue(stops, f"expected a PlaceStop for label={c.label}")
        owned = c.position.quantity
        removal = _removal_ids(actions, c.uic)
        surviving_stop = sum(
            _leg_amt(leg)
            for leg in c.legs
            if leg.order_type in STOP_TYPES and leg.order_id not in removal
        )
        placed = sum(a.qty for a in stops)
        self.assertTrue(
            _close(surviving_stop + placed, owned),
            f"coverage {surviving_stop + placed} != owned {owned} (label={c.label})",
        )

    @given(_one_of(_PLACESTOP_BUILDERS))
    @_LONG_SETTINGS
    def test_placed_qty_strictly_positive(self, c: _LongComp) -> None:
        # INV5 — every PlaceStop.qty > _QTY_EPS.
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        for a in _placestops(actions, c.uic):
            self.assertGreater(
                a.qty, _QTY_EPS, f"non-positive placed qty {a.qty} (label={c.label})"
            )


class TestDeficitAndSizing(unittest.TestCase):
    @given(_one_of(_DEFICIT_BUILDERS))
    @_LONG_SETTINGS
    def test_deficit_never_yields_noop(self, c: _LongComp) -> None:
        # INV2 — a genuine downside deficit (stop_qty + eps < owned) with a valid
        # plan MUST place a stop and MUST NOT NoOp/alert. Holds even when arm A wins.
        owned = c.position.quantity
        stop_qty = _stop_qty(c.legs)
        self.assertLess(
            stop_qty + _QTY_EPS, owned, "builder must construct a deficit"
        )  # antecedent
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        self.assertTrue(
            _placestops(actions, c.uic), f"deficit produced no PlaceStop (label={c.label})"
        )
        self.assertFalse(
            any(isinstance(a, (NoOp, AlertOnly)) for a in actions),
            f"deficit read as protected/alert (label={c.label})",
        )

    @given(_one_of(_PLACESTOP_BUILDERS))
    @_LONG_SETTINGS
    def test_delta_stop_never_supersedes(self, c: _LongComp) -> None:
        # INV4 — a delta-sized stop (qty < owned - eps, arm B1) must not supersede.
        owned = c.position.quantity
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        for a in _placestops(actions, c.uic):
            if a.qty < owned - _QTY_EPS:  # a delta stop
                self.assertEqual(
                    a.supersede_ids, (), f"delta stop superseded (label={c.label}, qty={a.qty})"
                )


class TestOverHedgeShape(unittest.TestCase):
    @given(_one_of(_ARM_A_BUILDERS))
    @_LONG_SETTINGS
    def test_over_hedge_shape_and_ordering(self, c: _LongComp) -> None:
        # INV6 — arm A places a residual stop FIRST, superseding EXACTLY the input
        # STOP legs (they leave only via supersede-after-success, never a naked
        # window). A trailing CancelSellLegs names the NON-stop legs ONLY, and is
        # emitted ONLY when such legs exist:
        #   * with non-stop legs (TP / Market noise) -> [PlaceStop, CancelSellLegs]
        #   * stop-only over-committed group          -> [PlaceStop] (no cancel)
        # The teeth of the fix: NO stop id ever appears in an unconditional cancel.
        owned = c.position.quantity
        # confirm the builder is genuinely over-hedged (independent predicate)
        self.assertGreater(_stop_qty(c.legs) + _tp_qty(c.legs), owned + _QTY_EPS)
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))

        # First action is always the residual place, sized to netted owned.
        self.assertIsInstance(actions[0], PlaceStop)
        place = actions[0]
        assert isinstance(place, PlaceStop)
        self.assertTrue(_close(place.qty, owned), f"residual {place.qty} != owned {owned}")

        # EQUALITY (not subset): supersede_ids is EXACTLY the input STOP-leg subset,
        # sourced from the legs (NOT from any cancel list) — the stop legs leave
        # only via supersede-after-a-successful-place.
        stop_ids = _stop_leg_ids(c.legs)
        self.assertEqual(
            set(place.supersede_ids),
            stop_ids,
            "supersede_ids != the input stop-leg subset",
        )

        non_stop_ids = {leg.order_id for leg in c.legs if leg.order_type not in STOP_TYPES}
        cancels = _cancels(actions, c.uic)
        if non_stop_ids:
            # Non-stop legs present -> exactly [PlaceStop, CancelSellLegs(non-stop)].
            self.assertEqual(len(actions), 2, f"expected 2 actions (label={c.label})")
            self.assertIsInstance(actions[1], CancelSellLegs)
            cancel = actions[1]
            assert isinstance(cancel, CancelSellLegs)
            self.assertEqual(
                set(cancel.order_ids),
                non_stop_ids,
                "CancelSellLegs must name exactly the NON-stop legs",
            )
            self.assertEqual(
                set(cancel.order_ids) & stop_ids,
                set(),
                "no STOP leg may sit in an unconditional cancel (naked-window guard)",
            )
        else:
            # Stop-only over-committed group -> [PlaceStop] alone, no cancel at all.
            self.assertEqual(len(actions), 1, f"stop-only arm A must be 1 action (label={c.label})")
            self.assertEqual(cancels, [], "stop-only arm A must emit NO CancelSellLegs")


class TestCancelFields(unittest.TestCase):
    @given(_one_of(_PLACESTOP_BUILDERS))
    @_LONG_SETTINGS
    def test_cancel_conflicting_names_only_tp_legs(self, c: _LongComp) -> None:
        # INV7 — cancel_conflicting ⊆ TP legs; no STOP id ever pre-cancelled.
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        tp_ids = _tp_leg_ids(c.legs)
        stop_ids = _stop_leg_ids(c.legs)
        for a in _placestops(actions, c.uic):
            for oid in a.cancel_conflicting:
                self.assertIn(oid, tp_ids, f"cancel_conflicting id {oid} is not a TP leg")
                self.assertNotIn(oid, stop_ids, f"a STOP leg {oid} was pre-cancelled")

    @given(_one_of(_TP_BEARING_PLACESTOP_BUILDERS))
    @_LONG_SETTINGS
    def test_every_tp_leg_cleared_when_placestop(self, c: _LongComp) -> None:
        # INV8 — if a stop is placed, every Limit leg is cancelled (cancel_conflicting
        # for B1/B2, CancelSellLegs.order_ids for A). No TP survives alongside a stop.
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        self.assertTrue(_placestops(actions, c.uic), f"expected a PlaceStop (label={c.label})")
        cleared: set[str] = set()
        for a in _placestops(actions, c.uic):
            cleared |= set(a.cancel_conflicting)
        for a in _cancels(actions, c.uic):
            cleared |= set(a.order_ids)
        for tp_id in _tp_leg_ids(c.legs):
            self.assertIn(tp_id, cleared, f"TP leg {tp_id} survived a place (label={c.label})")

    @given(_one_of(_ALL_LONG_BUILDERS))
    @_LONG_SETTINGS
    def test_no_fabricated_cancel_ids(self, c: _LongComp) -> None:
        # INV9 — every superseded/cancelled/conflicting id is an input leg id.
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        input_ids = {leg.order_id for leg in c.legs}
        self.assertTrue(
            _removal_ids(actions, c.uic) <= input_ids,
            f"fabricated cancel id (label={c.label})",
        )


class TestAlertArms(unittest.TestCase):
    @given(_one_of(_ALERT_BUILDERS))
    @_LONG_SETTINGS
    def test_no_plan_or_conflicting_single_alert(self, c: _LongComp) -> None:
        # INV10 — plan None or conflicting -> exactly [AlertOnly], no order action.
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        self.assertEqual(len(actions), 1, f"alert arm must be length-1 (label={c.label})")
        self.assertIsInstance(actions[0], AlertOnly)
        self.assertFalse(
            any(isinstance(a, (PlaceStop, CancelSellLegs, UpgradeToOco, NoOp)) for a in actions)
        )
        if c.label == "plan_conflicting_alert":
            a = actions[0]
            assert isinstance(a, AlertOnly)
            self.assertIn("refusing to merge", a.reason)


class TestProtectionLevel(unittest.TestCase):
    @given(_mixed_view(require_orphan=True))
    @_COMPOSITE_SETTINGS
    def test_orphan_sweep_completeness(self, m: _Mixed) -> None:
        # INV11 — each orphan uic gets exactly one CancelSellLegs naming ALL its
        # leg ids (any order_type), and no PlaceStop.
        actions = reconcile_protection(m.view)
        for uic, legs in m.orphans:
            sweeps = _cancels(actions, uic)
            self.assertEqual(len(sweeps), 1, f"orphan uic {uic} not swept exactly once")
            self.assertEqual(
                set(sweeps[0].order_ids), {leg.order_id for leg in legs}, "orphan sweep incomplete"
            )
            self.assertEqual(_placestops(actions, uic), [], "orphan uic got a PlaceStop")

    @given(_mixed_view(require_short=True))
    @_COMPOSITE_SETTINGS
    def test_short_position_alerts(self, m: _Mixed) -> None:
        # INV12 — every short uic raises a SHORT AlertOnly; no long uic does.
        actions = reconcile_protection(m.view)
        short_alerts = [a for a in actions if isinstance(a, AlertOnly) and "SHORT" in a.reason]
        self.assertEqual(len(short_alerts), len(m.shorts))
        for uic, _ in m.shorts:
            self.assertTrue(
                any(f"uic {uic}:" in a.reason for a in short_alerts),
                f"short uic {uic} not alerted",
            )
        for c in m.longs:
            self.assertFalse(
                any(f"uic {c.uic}: unexpected SHORT" in a.reason for a in short_alerts),
                f"long uic {c.uic} raised a SHORT alert",
            )

    @given(_mixed_view())
    @_COMPOSITE_SETTINGS
    def test_determinism_purity(self, m: _Mixed) -> None:
        # INV13 — pure function of its argument: f(x) == f(x) element-wise (frozen
        # dataclass __eq__, incl. request_id, over the stateless _default_next_gen).
        self.assertEqual(reconcile_protection(m.view), reconcile_protection(m.view))
        # also at the per-long level
        for c in m.longs:
            self.assertEqual(
                reconcile_long(c.uic, c.position, m.view),
                reconcile_long(c.uic, c.position, m.view),
            )


class TestNearEpsBoundary(unittest.TestCase):
    """FP knife-edge: near-owned stop amounts. Assert ONLY arm-agnostic invariants
    (never an arm identity at the tie); exclude the exact 0.5 tie."""

    @given(
        owned=st.integers(2, 500).map(float),
        delta=st.sampled_from([0.0, 0.4, 0.49, 0.51, 0.6, 1.0]),  # excludes the exact 0.5 tie
    )
    @_LONG_SETTINGS
    def test_boundary_arm_agnostic(self, owned: float, delta: float) -> None:
        pos = _mk_pos(owned, _UIC)
        stop = _mk_leg(f"{_UIC}-stop", "StopIfTraded", owned - delta, _UIC)
        view = ProtectionView(
            long_positions={_UIC: pos},
            all_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: (stop,)},
            planned_by_uic={_UIC: _mk_plan(_UIC)},
            oco_unsupported=frozenset(),
        )
        actions = reconcile_long(_UIC, pos, view)
        removal = _removal_ids(actions, _UIC)
        surviving = sum(_leg_amt(leg) for leg in (stop,) if leg.order_id not in removal)
        placed = sum(a.qty for a in _placestops(actions, _UIC))
        self.assertTrue(_le(surviving + placed, owned), "boundary over-commit")
        stops = _placestops(actions, _UIC)
        if stops:  # a deficit existed -> coverage lands on owned, placed > eps
            surviving_stop = sum(
                _leg_amt(leg)
                for leg in (stop,)
                if leg.order_type in STOP_TYPES and leg.order_id not in removal
            )
            self.assertTrue(_close(surviving_stop + placed, owned), "boundary coverage off owned")
            for a in stops:
                self.assertGreater(a.qty, _QTY_EPS)


class TestKillSwitchAdditiveOff(unittest.TestCase):
    """ADDITIVE_STOPS_CONFIRMED is a MODULE GLOBAL (cannot toggle per-example), so
    the B2-via-kill-switch arm rides a separate @patch'd test over a B1-eligible
    shape: it must now cancel-replace (qty == owned, supersede = stop ids)."""

    @patch.object(pm, "ADDITIVE_STOPS_CONFIRMED", False)
    @given(_build_additive_grow_plain(_UIC))
    @_LONG_SETTINGS
    def test_additive_off_forces_cancel_replace(self, c: _LongComp) -> None:
        owned = c.position.quantity
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        stops = _placestops(actions, c.uic)
        self.assertEqual(len(stops), 1)
        self.assertTrue(_close(stops[0].qty, owned), "kill-switch off must place full owned")
        self.assertEqual(set(stops[0].supersede_ids), _stop_leg_ids(c.legs))


class TestArmIdentityAnchors(unittest.TestCase):
    """AFFIRMATIVE arm-identity anchors. Each independently recomputes the arm's
    INPUT predicate from the ProtectionView (never by calling the reconciler
    twice), asserts the antecedent holds BY CONSTRUCTION, then asserts the SPEC
    OUTPUT shape unique to that arm. These are the anti-vacuous teeth: they FAIL
    if an arm collapses into a neighbour (B1->B2, C->PlaceStop, oco guard drop)."""

    @given(_build_additive_grow_plain(_UIC))
    @_LONG_SETTINGS
    def test_b1_fires_delta_when_eligible(self, c: _LongComp) -> None:
        # ANCHOR 1 — unambiguously B1-eligible: ADDITIVE on (module default), a
        # single covering stop in (eps, owned-eps), NOT oco_unsupported, not
        # over-hedged. SPEC: a DELTA stop for owned-stop_qty, no supersede.
        owned = c.position.quantity
        stop_qty = _stop_qty(c.legs)
        tp_qty = _tp_qty(c.legs)
        self.assertTrue(pm.ADDITIVE_STOPS_CONFIRMED, "B1 antecedent: additive on")
        self.assertFalse(c.oco_unsupported, "B1 antecedent: not oco_unsupported")
        self.assertGreater(stop_qty, _QTY_EPS, "B1 antecedent: covering stop present")
        self.assertLess(stop_qty + _QTY_EPS, owned, "B1 antecedent: genuine deficit")
        self.assertFalse(stop_qty + tp_qty > owned + _QTY_EPS, "B1 antecedent: not over-hedged")
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        stops = _placestops(actions, c.uic)
        self.assertEqual(len(stops), 1, "B1 must place exactly one stop")
        place = stops[0]
        self.assertTrue(
            _close(place.qty, owned - stop_qty),
            f"B1 qty {place.qty} != deficit {owned - stop_qty}",
        )
        self.assertLess(place.qty, owned - _QTY_EPS, "B1 must place a DELTA, not full owned")
        self.assertEqual(place.supersede_ids, (), "B1 delta must NOT supersede")

    @given(_build_covered_exact_multi(_UIC))
    @_LONG_SETTINGS
    def test_covered_is_exactly_noop(self, c: _LongComp) -> None:
        # ANCHOR 2 — covered (stop_qty within eps of owned) and NOT over-hedged.
        # SPEC: the uic's action list is EXACTLY [NoOp()] — no PlaceStop, no cancel.
        owned = c.position.quantity
        stop_qty = _stop_qty(c.legs)
        tp_qty = _tp_qty(c.legs)
        self.assertFalse(stop_qty + _QTY_EPS < owned, "covered antecedent: not a deficit")
        self.assertFalse(
            stop_qty + tp_qty > owned + _QTY_EPS, "covered antecedent: not over-hedged"
        )
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        self.assertEqual(actions, [NoOp()], f"covered must be exactly [NoOp()] (got {actions})")

    @given(_build_cancel_replace_oco_unsupported(_UIC))
    @_LONG_SETTINGS
    def test_oco_unsupported_forces_cancel_replace(self, c: _LongComp) -> None:
        # ANCHOR 3 — a B1-eligible SHAPE (deficit, covering stop in band) but the
        # uic is oco_unsupported. SPEC: B2 full-owned place + supersede the input
        # STOP legs — NEVER a delta.
        owned = c.position.quantity
        stop_qty = _stop_qty(c.legs)
        self.assertTrue(c.oco_unsupported, "B2 antecedent: oco_unsupported")
        self.assertGreater(stop_qty, _QTY_EPS, "shape antecedent: covering stop present")
        self.assertLess(stop_qty + _QTY_EPS, owned, "shape antecedent: genuine deficit")
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        stops = _placestops(actions, c.uic)
        self.assertEqual(len(stops), 1)
        place = stops[0]
        self.assertTrue(
            _close(place.qty, owned), f"oco_unsupported must place full owned, got {place.qty}"
        )
        self.assertEqual(
            set(place.supersede_ids),
            _stop_leg_ids(c.legs),
            "B2 must supersede the input STOP legs",
        )
        self.assertNotEqual(place.supersede_ids, (), "B2 must supersede (never a bare delta)")

    @given(_build_cancel_replace_zero_amount(_UIC))
    @_LONG_SETTINGS
    def test_zero_amount_stale_stop_routes_b2(self, c: _LongComp) -> None:
        # ANCHOR 4 — a present-but-zero-amount stale STOP leg (stop_qty ~= 0). It
        # is NOT a covering stop, so the arm is B2, not B1: full-owned place with
        # the stale leg id superseded.
        owned = c.position.quantity
        stop_qty = _stop_qty(c.legs)
        stop_ids = _stop_leg_ids(c.legs)
        self.assertTrue(_close(stop_qty, 0.0), "antecedent: stale zero-amount stop")
        self.assertTrue(stop_ids, "antecedent: a STOP leg is present (id must be superseded)")
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        stops = _placestops(actions, c.uic)
        self.assertEqual(len(stops), 1)
        place = stops[0]
        self.assertTrue(
            _close(place.qty, owned), "zero-amount stale stop must place full owned (B2)"
        )
        self.assertEqual(
            set(place.supersede_ids), stop_ids, "the zero-amount stale stop id must be superseded"
        )


# --------------------------------------------------------------------------
# NON-VACUOUSNESS COMPANION (mandatory PBT-series gate): every arm-coverage
# target MUST be emitted at least once, else fail loudly.
# --------------------------------------------------------------------------

_ARM_COVERAGE_TARGETS = frozenset(
    {
        "plan_none_alert",
        "plan_conflicting_alert",
        "arm_A_over_hedge_partial_fill",
        "arm_A_over_hedge_deficit_fill",
        "arm_A_over_hedge_newest_group",
        "arm_B1_additive_grow_plain",
        "arm_B1_additive_grow_with_lone_tp",
        "arm_B2_cancel_replace_naked",
        "arm_B2_cancel_replace_zero_amount_stop",
        "arm_B2_cancel_replace_lone_tp",
        "arm_B2_cancel_replace_oco_unsupported",
        "arm_B2_cancel_replace_additive_off",
        "arm_C_covered_stop_only",
        "arm_C_covered_with_tp_price",
        "arm_C_covered_float_tolerance_flicker",
        "protection_orphan_sweep",
        "protection_short_alert",
        "protection_multi_uic_concatenation",
        "leg_type_noise_non_stop_non_tp",
    }
)


class TestArmCoverageNonVacuousness(unittest.TestCase):
    """Runs the generator and FAILS if any arm was never produced. This is the
    anti-vacuous gate: a PBT that passes because it never reached an arm is a
    defect (PBT-series #858-863 lesson)."""

    seen: set[str] = set()

    @given(_mixed_view(require_all_longs=True, require_orphan=True, require_short=True))
    @settings(deadline=None, max_examples=25, suppress_health_check=[HealthCheck.too_slow])
    def test_generate_all_arms(self, m: _Mixed) -> None:
        # require_all_longs guarantees every long-arm label in EVERY example
        # (one long per builder) + orphan/short/multi, so coverage is deterministic
        # rather than dependent on random sampling reaching a rare builder.
        type(self).seen |= m.labels()

    @patch.object(pm, "ADDITIVE_STOPS_CONFIRMED", False)
    @given(_build_additive_grow_plain(_UIC))
    @settings(deadline=None, max_examples=10)
    def test_additive_off_arm_covered(self, c: _LongComp) -> None:
        # This arm cannot appear in the module-global run above; cover it here.
        actions = reconcile_long(c.uic, c.position, _view_from_long(c))
        self.assertTrue(_placestops(actions, c.uic))
        type(self).seen.add("arm_B2_cancel_replace_additive_off")

    @classmethod
    def tearDownClass(cls) -> None:
        missing = _ARM_COVERAGE_TARGETS - cls.seen
        assert not missing, f"arms never generated (vacuous coverage): {sorted(missing)}"


# --------------------------------------------------------------------------
# Stage 3 properties (B0 OCO-direct-on-fill + AmendStop resize). These run with
# the dark flags forced ON (the default-off arms above never emit them). Each
# sanitizes BOTH env flags first so an ambient flag cannot leak into the arm.
# --------------------------------------------------------------------------


def _stage3_env(**flags: str) -> dict[str, str]:
    base = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ALPHALENS_BROKER_OCO_ENABLED", "ALPHALENS_BROKER_AMEND_ENABLED")
    }
    base.update(flags)
    return base


def _one_uic_view(
    owned: float,
    legs: tuple[OrderState, ...],
    *,
    tp_price: float | None = 306.72,
    oco_recently_placed: frozenset[int] = frozenset(),
) -> tuple[Position, ProtectionView]:
    pos = _mk_pos(owned, _UIC)
    view = ProtectionView(
        long_positions={_UIC: pos},
        all_positions={_UIC: pos},
        sell_legs_by_uic={_UIC: legs} if legs else {},
        planned_by_uic={_UIC: _mk_plan(_UIC, tp_price=tp_price)},
        oco_unsupported=frozenset(),
        oco_recently_placed=oco_recently_placed,
    )
    return pos, view


def _final_sell_including_amend(
    actions: list, legs: tuple[OrderState, ...], uic: int, owned: float
) -> float:
    """FINAL intended sell commitment on a uic, extended for Stage 3 arms.

    An ``AmendStop`` RESIZES its target resting stop in place, so that leg's final
    resting amount is ``target_qty`` (grow/downsize emit exactly one AmendStop and
    no other order action — the sole clean stop is the only leg). A B0
    ``UpgradeToOco`` fires only on a naked uic and rests a mutually-exclusive OCO
    pair committing ``qty`` ONCE. Otherwise fall back to the PlaceStop accounting
    (removal ∪ surviving + placed) used by the Stage-1/2 invariants."""
    amends = [a for a in actions if isinstance(a, AmendStop)]
    if amends:
        return sum(a.target_qty for a in amends)
    upgrades = [a for a in actions if isinstance(a, UpgradeToOco)]
    if upgrades:
        return sum(a.qty for a in upgrades)
    removal = _removal_ids(actions, uic)
    surviving = sum(_leg_amt(leg) for leg in legs if leg.order_id not in removal)
    placed = sum(a.qty for a in _placestops(actions, uic))
    return surviving + placed


class TestStage3AmendAndB0Properties(unittest.TestCase):
    @given(owned=st.integers(4, 5000).map(float))
    @_LONG_SETTINGS
    def test_property_amendstop_target_never_exceeds_owned(self, owned: float) -> None:
        # INV — every AmendStop resizes to the ABSOLUTE live-owned target, never a
        # sum, so target_qty <= owned in BOTH grow (stop under owned) and downsize
        # (stop over owned). Non-vacuous: both cases DO emit an AmendStop.
        with patch.dict(os.environ, _stage3_env(ALPHALENS_BROKER_AMEND_ENABLED="1"), clear=True):
            for stop_amt, direction in ((owned / 2.0, "grow"), (owned * 2.0, "downsize")):
                stop = _mk_leg(f"{_UIC}-s", "StopIfTraded", stop_amt, _UIC)
                pos, view = _one_uic_view(owned, (stop,))
                actions = reconcile_long(_UIC, pos, view)
                amends = [a for a in actions if isinstance(a, AmendStop)]
                self.assertEqual(len(amends), 1, f"{direction} must emit one AmendStop")
                self.assertTrue(
                    _le(amends[0].target_qty, owned),
                    f"{direction} target {amends[0].target_qty} exceeds owned {owned}",
                )
                self.assertTrue(_close(amends[0].target_qty, owned), "target must equal live owned")

    @given(owned=st.integers(3, 5000).map(float), has_leg=st.booleans())
    @_LONG_SETTINGS
    def test_property_b0_emitted_only_on_truly_naked_uic(self, owned: float, has_leg: bool) -> None:
        # INV — B0 (UpgradeToOco with empty supersede) fires IFF the uic is truly
        # naked (no resting legs). A deficit WITH a resting leg never takes B0.
        legs = (_mk_leg(f"{_UIC}-s", "StopIfTraded", 1.0, _UIC),) if has_leg else ()
        with patch.dict(os.environ, _stage3_env(ALPHALENS_BROKER_OCO_ENABLED="1"), clear=True):
            pos, view = _one_uic_view(owned, legs)
            actions = reconcile_long(_UIC, pos, view)
        emitted_b0 = any(isinstance(a, UpgradeToOco) for a in actions)
        self.assertEqual(emitted_b0, not has_leg, f"B0 must fire iff naked (has_leg={has_leg})")
        if emitted_b0:
            up = next(a for a in actions if isinstance(a, UpgradeToOco))
            self.assertEqual(up.supersede_ids, ())

    @given(owned=st.integers(4, 5000).map(float))
    @_LONG_SETTINGS
    def test_property_b0_suppressed_by_recent_marker_noops_never_places_stop(
        self, owned: float
    ) -> None:
        # INV — a naked-looking uic (empty legs) that is in oco_recently_placed
        # emits [NoOp()], NEVER a PlaceStop. This is the list-lag window: a just-
        # placed OCO pair rests at the broker but is invisible in the view, so any
        # stop placed here would be a SECOND owned SELL on top of it (2x owned).
        # The no-double-commit property above is structurally BLIND to this case —
        # _final_sell_including_amend only counts legs present in the view, and the
        # offending OCO is hidden — so this targeted anchor pins the NoOp directly.
        with patch.dict(os.environ, _stage3_env(ALPHALENS_BROKER_OCO_ENABLED="1"), clear=True):
            pos, view = _one_uic_view(owned, (), oco_recently_placed=frozenset({_UIC}))
            actions = reconcile_long(_UIC, pos, view)
        self.assertEqual(actions, [NoOp()], "recent-marker suppression must NoOp, never PlaceStop")

    @given(
        owned=st.integers(4, 5000).map(float),
        case=st.sampled_from(["grow", "downsize", "b0", "naked_stop_only"]),
    )
    @_LONG_SETTINGS
    def test_property_no_double_commit_after_reconcile_including_b0_and_amend(
        self, owned: float, case: str
    ) -> None:
        # INV — the FINAL intended sell commitment on the uic never exceeds owned,
        # across the Stage-3 arms (AmendStop absolute-target, B0 OCO count-once) as
        # well as the plain stop-only fallback.
        flags: dict[str, str] = {}
        legs: tuple[OrderState, ...]
        if case == "grow":
            flags = {"ALPHALENS_BROKER_AMEND_ENABLED": "1"}
            legs = (_mk_leg(f"{_UIC}-s", "StopIfTraded", owned / 2.0, _UIC),)
        elif case == "downsize":
            flags = {"ALPHALENS_BROKER_AMEND_ENABLED": "1"}
            legs = (_mk_leg(f"{_UIC}-s", "StopIfTraded", owned * 2.0, _UIC),)
        elif case == "b0":
            flags = {"ALPHALENS_BROKER_OCO_ENABLED": "1"}
            legs = ()
        else:  # naked_stop_only — both flags OFF
            legs = ()
        with patch.dict(os.environ, _stage3_env(**flags), clear=True):
            pos, view = _one_uic_view(owned, legs)
            actions = reconcile_long(_UIC, pos, view)
        final = _final_sell_including_amend(actions, legs, _UIC, owned)
        self.assertTrue(_le(final, owned), f"double-commit: final {final} > owned {owned} ({case})")


# --------------------------------------------------------------------------
# Stage 3.5 — in-place OCO-pair resize (OCO-leg PATCH amend) invariants. A
# resting OCO exit pair {StopIfTraded, Limit} resizes via a PATCH on its child
# stop leg (Q9 propagates symmetrically to the Limit sibling). These run with the
# amend flag forced ON; the fallback modes (amend skipped) pin the never-naked
# guarantee that NO OCO leg is ever pre-cancelled.
# --------------------------------------------------------------------------

_OCO_MODES = (
    "grow_amend",
    "downsize_amend",
    "lag_noop",
    "grow_fallback_recently_failed",
    "grow_fallback_unsupported",
    "downsize_fallback_recently_failed",
)
_OCO_AMEND_MODES = frozenset({"grow_amend", "downsize_amend"})


def _mk_oco_leg(
    order_id: str,
    order_type: str,
    amount: float,
    uic: int,
    *,
    base: str = "crid-oco-0",
    filled: float = 0.0,
) -> OrderState:
    """A resting OCO exit leg (``OrderRelation='Oco'`` + shared base ref with a
    ``-stop`` / ``-tp`` suffix, what ``_build_oco_exit_body`` stamps)."""
    suffix = "-stop" if order_type in STOP_TYPES else "-tp"
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=filled,
        raw_status="Working",
        uic=uic,
        side="SELL",
        order_type=order_type,
        amount=amount,
        external_reference=f"{base}{suffix}",
        order_relation="Oco",
    )


def _build_oco_case(mode: str, owned: float, uic: int) -> tuple[Position, ProtectionView]:
    """Canonical resting-OCO-pair state for one mode (amend flag ON at call site).

    grow modes rest a clean pair UNDER owned (owned grew); downsize/lag modes rest
    a pair AT/OVER owned. The two fallback modes degrade the uic (amend_recently_
    failed / oco_unsupported) so the OCO-amend arm is skipped and the reconciler
    falls to B1 additive / B2 place-first / the M1 NoOp hold."""
    pos = _mk_pos(owned, uic)
    grow_amt = max(2.0, owned - 2.0)  # clean pair under owned (a genuine deficit)
    over_amt = owned + 3.0  # clean pair over owned (over-hedge / lag)
    amend_recently_failed: frozenset[int] = frozenset()
    oco_unsupported: frozenset[int] = frozenset()
    if mode in ("grow_amend", "grow_fallback_recently_failed", "grow_fallback_unsupported"):
        stop_amt = tp_amt = grow_amt
    elif mode in ("downsize_amend", "downsize_fallback_recently_failed"):
        stop_amt = tp_amt = over_amt
    else:  # lag_noop — stop already == owned, tp lags at the OLD larger amount
        stop_amt, tp_amt = owned, over_amt
    if mode in ("grow_fallback_recently_failed", "downsize_fallback_recently_failed"):
        amend_recently_failed = frozenset({uic})
    if mode == "grow_fallback_unsupported":
        oco_unsupported = frozenset({uic})
    legs = (
        _mk_oco_leg(f"{uic}-oco-stop", "StopIfTraded", stop_amt, uic),
        _mk_oco_leg(f"{uic}-oco-tp", "Limit", tp_amt, uic),
    )
    view = ProtectionView(
        long_positions={uic: pos},
        all_positions={uic: pos},
        sell_legs_by_uic={uic: legs},
        planned_by_uic={uic: _mk_plan(uic, tp_price=306.72)},
        oco_unsupported=oco_unsupported,
        amend_recently_failed=amend_recently_failed,
    )
    return pos, view


def _oco_leg_ids(view: ProtectionView, uic: int) -> set[str]:
    return {leg.order_id for leg in view.sell_legs_by_uic.get(uic, ()) if _is_oco_leg(leg)}


class TestOcoAmendInvariantsPBT(unittest.TestCase):
    """Stage 3.5 OCO-leg PATCH-amend invariants (money-adjacent). The teeth: no OCO
    leg is EVER pre-cancelled (cancel_conflicting / CancelSellLegs), so the pair is
    never torn down before a replacement lands — the never-naked guarantee across
    the amend arms AND every fallback (amend skipped)."""

    @given(
        owned=st.integers(4, 5000).map(float),
        mode=st.sampled_from(_OCO_MODES),
    )
    @_LONG_SETTINGS
    def test_pbt_oco_amend_never_naked_never_double_commit_never_oversell(
        self, owned: float, mode: str
    ) -> None:
        event(f"oco_mode:{mode}")
        with patch.dict(os.environ, _stage3_env(ALPHALENS_BROKER_AMEND_ENABLED="1"), clear=True):
            pos, view = _build_oco_case(mode, owned, _UIC)
            actions = reconcile_long(_UIC, pos, view)
        oco_ids = _oco_leg_ids(view, _UIC)
        self.assertEqual(len(oco_ids), 2, "canonical case must rest a 2-leg OCO pair")

        # NEVER-NAKED: no OCO leg id may sit in an unconditional PRE-cancel
        # (cancel_conflicting is cancelled BEFORE the place; cancelling one OCO leg
        # cascade-cancels its covering sibling -> a naked window). OCO legs leave
        # ONLY via supersede_ids (cancel AFTER a successful place).
        for a in _placestops(actions, _UIC):
            self.assertEqual(
                set(a.cancel_conflicting) & oco_ids,
                set(),
                f"OCO leg pre-cancelled in cancel_conflicting (mode={mode})",
            )
        for a in _cancels(actions, _UIC):
            self.assertEqual(
                set(a.order_ids) & oco_ids,
                set(),
                f"OCO leg in an unconditional CancelSellLegs (mode={mode})",
            )

        # ABSOLUTE-TARGET: an OCO-amend resizes to live owned (never a sum), and is
        # the WHOLE action (the pair commits owned ONCE after Q9 propagation).
        amends = [a for a in actions if isinstance(a, AmendStop)]
        for a in amends:
            self.assertTrue(_close(a.target_qty, owned), f"amend target {a.target_qty} != owned")
            self.assertIn(a.order_id, oco_ids, "amend must target the OCO stop leg")
        if mode in _OCO_AMEND_MODES:
            self.assertEqual(len(actions), 1, f"amend arm must emit exactly one action ({mode})")
            self.assertEqual(len(amends), 1, f"amend arm must emit an AmendStop ({mode})")

        # NEVER-OVERSELL on a placed fallback stop: B1 delta covers owned-stop_qty;
        # B2 places full owned. A placed stop never exceeds owned.
        for a in _placestops(actions, _UIC):
            self.assertTrue(
                _le(a.qty, owned), f"placed stop {a.qty} exceeds owned {owned} ({mode})"
            )

    @given(owned=st.integers(4, 5000).map(float))
    @_LONG_SETTINGS
    def test_anchor_grow_downsize_lag_arms_execute(self, owned: float) -> None:
        # AFFIRMATIVE non-vacuousness anchors: each mode emits its SPECIFIC action on
        # its canonical state. Fails loudly if an arm collapses into a neighbour.
        with patch.dict(os.environ, _stage3_env(ALPHALENS_BROKER_AMEND_ENABLED="1"), clear=True):
            # grow amend -> one AmendStop up to owned, reason 'grow-after-OCO'.
            pos, view = _build_oco_case("grow_amend", owned, _UIC)
            grow = reconcile_long(_UIC, pos, view)
            self.assertEqual(len(grow), 1)
            self.assertIsInstance(grow[0], AmendStop)
            assert isinstance(grow[0], AmendStop)
            self.assertTrue(grow[0].reason.startswith("grow-after-OCO"))
            self.assertTrue(_close(grow[0].target_qty, owned))

            # downsize amend -> one AmendStop down to owned, reason 'OCO downsize'.
            pos, view = _build_oco_case("downsize_amend", owned, _UIC)
            down = reconcile_long(_UIC, pos, view)
            self.assertEqual(len(down), 1)
            self.assertIsInstance(down[0], AmendStop)
            assert isinstance(down[0], AmendStop)
            self.assertTrue(down[0].reason.startswith("OCO downsize"))
            self.assertTrue(_close(down[0].target_qty, owned))

            # lag -> M1 NoOp (stop already == owned, tp read lags), never a teardown.
            # The hold stamps uic + reason so the control loop can count consecutive
            # holds and page a genuinely-stuck lag (issue #5); still a no-op action.
            pos, view = _build_oco_case("lag_noop", owned, _UIC)
            self.assertEqual(
                reconcile_long(_UIC, pos, view), [NoOp(uic=_UIC, reason="oco-lag-hold")]
            )

            # grow fallback (recently failed) -> B1 additive delta, NO OCO pre-cancel.
            pos, view = _build_oco_case("grow_fallback_recently_failed", owned, _UIC)
            fb = reconcile_long(_UIC, pos, view)
            self.assertEqual(len(fb), 1)
            self.assertIsInstance(fb[0], PlaceStop)
            assert isinstance(fb[0], PlaceStop)
            self.assertEqual(fb[0].cancel_conflicting, ())
            self.assertTrue(_close(fb[0].qty, owned - max(2.0, owned - 2.0)))


if __name__ == "__main__":
    unittest.main()
