"""Behavioral exit-policy abstraction (name -> placement + reanchor decisions).

An ``ExitPolicy`` is a PURE price oracle: it proposes exit levels and a reanchor
target and NEVER emits a broker Action. The daemon resolves ONE by name at
startup and routes placement + reanchor through it, so adding a policy (a future
ML policy included) is a new registry entry, not a new call-site.
``SetupStaticPolicy`` is the inert/null policy that keeps the brief's static
disaster stop/TP and never reanchors; ``AtrBracketPolicy`` wraps a numeric
``ExitGeometryPolicy``. ``min_stop_distance_frac`` lets the reanchor envelope
stay policy-agnostic (a future close-stop policy sets its own floor).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from broker_contract.exit_geometry.levels import chandelier_target, fractional_giveback_target
from broker_contract.exit_geometry.registry import ExitGeometryPolicy


@runtime_checkable
class ExitPolicy(Protocol):
    # Every member below is READ-ONLY (a property, not a bare annotation)
    # because these are the policy's IDENTITY, never its state — nothing
    # writes to them. The distinction is not cosmetic: a bare annotation
    # declares a WRITABLE member, which demands an assignable attribute of
    # exactly that type, while every implementation is a frozen dataclass whose
    # fields are read-only. So no implementation actually satisfied this
    # protocol, and the registry could not be typed as returning one. Nobody
    # saw it because this package sat outside the type gate (issue #1140).
    # Read-only also makes the member types covariant, which is what lets a
    # policy that always has a geometry narrow ``geometry_name`` to ``str``.
    #
    # The implementations below are NOT expected to mirror this shape: a plain
    # frozen-dataclass FIELD satisfies a read-only property member, because
    # read-only asks only that the attribute be readable. So ``SetupStaticPolicy``
    # declaring ``name`` as a field and ``AtrBracketPolicy`` declaring
    # ``geometry_name`` as a property are BOTH valid, and neither needs changing
    # to match the other. ``isinstance`` is unaffected either way — a
    # runtime_checkable Protocol checks that the attribute is PRESENT, not that
    # it is a descriptor.

    # The BEHAVIORAL identity — the key ALPHALENS_BROKER_EXIT_POLICY selects and
    # the one an operator reads in a log line. It must never be derived from a
    # wrapped geometry: two policies can share a geometry and differ in whether
    # they trail, which is the defect issue #1138 records.
    @property
    def name(self) -> str: ...

    # The geometry the policy places against, or None when it places none.
    # A separate, equally real fact — a reader needs both.
    @property
    def geometry_name(self) -> str | None: ...

    @property
    def version(self) -> int: ...

    @property
    def applies_geometry(self) -> bool: ...

    @property
    def requires_amend_stop(self) -> bool: ...

    @property
    def min_stop_distance_frac(self) -> float: ...

    @property
    def trails(self) -> bool: ...

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None: ...

    # ``plan_stop`` is the journaled brief disaster floor (``plan.stop_price``)
    # — the 1R denominator for policies whose risk unit is the BRIEF geometry
    # (entry minus disaster stop) rather than an ATR multiple. Policies that
    # define risk off their wrapped geometry ignore it.
    def decide_reanchor(
        self,
        avg_price: float,
        atr: float,
        *,
        peak: float | None = None,
        last_price: float | None = None,
        plan_stop: float | None = None,
    ) -> float | None: ...


@dataclass(frozen=True)
class SetupStaticPolicy:
    name: str = "setup_static"
    geometry_name: str | None = None  # places no geometry at all
    version: int = 1
    applies_geometry: bool = False
    requires_amend_stop: bool = False
    min_stop_distance_frac: float = 0.0
    trails: bool = False

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None:
        return None

    def decide_reanchor(
        self,
        avg_price: float,
        atr: float,
        *,
        peak: float | None = None,
        last_price: float | None = None,
        plan_stop: float | None = None,
    ) -> float | None:
        return None


@dataclass(frozen=True)
class AtrBracketPolicy:
    geom: ExitGeometryPolicy
    # Required and keyword-only ON PURPOSE (issue #1138): a default here is what
    # let this class report its geometry's name for months. The registry key is
    # the only honest source, so the construction site must state it.
    name: str = field(kw_only=True)
    applies_geometry: bool = True
    requires_amend_stop: bool = True
    min_stop_distance_frac: float = 0.002  # hair-trigger floor; never binds 1.5x ATR
    trails: bool = False

    @property
    def geometry_name(self) -> str:
        return self.geom.name

    @property
    def version(self) -> int:
        return self.geom.version

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None:
        return self.geom.levels(blended, atr, ceiling_price=ceiling_price)

    def decide_reanchor(
        self,
        avg_price: float,
        atr: float,
        *,
        peak: float | None = None,
        last_price: float | None = None,
        plan_stop: float | None = None,
    ) -> float | None:
        if not math.isfinite(avg_price) or avg_price <= 0:
            return None
        if not math.isfinite(atr) or atr <= 0:
            return None
        target = avg_price - self.geom.stop_atr_mult * atr
        if not math.isfinite(target) or target <= 0:
            return None
        return target


@dataclass(frozen=True)
class TrailingAtrPolicy:
    """Bot-amend Chandelier trailing stop: ``peak - k_atr*atr``, armed only
    once the position is ``activation_r`` R-multiples in profit (R = the
    wrapped geometry's ``stop_atr_mult * atr`` initial risk distance).
    Placement geometry (initial disaster stop + TP) is delegated to the
    wrapped ``ExitGeometryPolicy``, identical to ``AtrBracketPolicy`` — the
    two share a geometry and differ in ``decide_reanchor``, ``trails`` and
    ``name``. They used to share the name too, which is issue #1138."""

    geom: ExitGeometryPolicy
    activation_r: float
    k_atr: float
    # Required and keyword-only — see AtrBracketPolicy.name.
    name: str = field(kw_only=True)
    applies_geometry: bool = True
    requires_amend_stop: bool = True
    min_stop_distance_frac: float = 0.002  # hair-trigger floor; never binds 1.5x ATR
    trails: bool = True

    @property
    def geometry_name(self) -> str:
        return self.geom.name

    @property
    def version(self) -> int:
        return self.geom.version

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None:
        return self.geom.levels(blended, atr, ceiling_price=ceiling_price)

    def decide_reanchor(
        self,
        avg_price: float,
        atr: float,
        *,
        peak: float | None = None,
        last_price: float | None = None,
        plan_stop: float | None = None,
    ) -> float | None:
        if not math.isfinite(avg_price) or avg_price <= 0:
            return None
        if not math.isfinite(atr) or atr <= 0:
            return None
        if peak is None or not math.isfinite(peak) or peak <= 0:
            return None
        risk = self.geom.stop_atr_mult * atr
        if peak < avg_price + self.activation_r * risk:
            return None
        return chandelier_target(peak, atr, k=self.k_atr)


@dataclass(frozen=True)
class BreakevenTrailPolicy:
    """Bot-amend break-even + fractional-giveback trailing stop — the live port
    of the ``be_0p5r_trail0p6`` what-if lens. Places NO geometry
    (``applies_geometry=False``): the brief's TP tranche ladder and the brief
    disaster stop are journaled verbatim, so profit realizes through the
    research TP levels while this policy manages ONLY the stop. 1R is the
    LENS risk unit — ``avg_price - plan_stop`` (filled blend minus the brief
    disaster floor), NOT an ATR multiple — and ``atr`` is ignored entirely
    HERE. Read that narrowly: it means this method never reads ``atr``, NOT
    that the value is irrelevant to whether this policy runs. The caller
    (``position_manager._maybe_trail``) still refuses on a missing or
    degenerate ``plan.reanchor.atr`` before it ever calls in, which is why a
    pick armed without a geometry stamp never trails under this policy. That
    mismatch is deliberate and load-bearing — see issue #1325.
    Dark until the peak reaches ``avg_price + activation_r*R``; once armed the
    target is ``max(avg_price, avg_price + trail_frac*(peak - avg_price))``,
    which at the arming instant already sits at
    ``avg_price + activation_r*trail_frac*R`` (entry+0.3R for 0.5/0.6), same
    as the lens. The caller's ratchet + clamp keep the placed stop monotone."""

    activation_r: float
    trail_frac: float
    # Required and keyword-only — see AtrBracketPolicy.name.
    name: str = field(kw_only=True)
    geometry_name: str | None = None  # places no geometry at all
    version: int = 1
    applies_geometry: bool = False
    requires_amend_stop: bool = True
    min_stop_distance_frac: float = 0.002
    trails: bool = True

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None:
        return None

    def decide_reanchor(
        self,
        avg_price: float,
        atr: float,
        *,
        peak: float | None = None,
        last_price: float | None = None,
        plan_stop: float | None = None,
    ) -> float | None:
        if not math.isfinite(avg_price) or avg_price <= 0:
            return None
        if peak is None or not math.isfinite(peak) or peak <= 0:
            return None
        if plan_stop is None or not math.isfinite(plan_stop) or plan_stop <= 0:
            return None
        risk = avg_price - plan_stop
        if risk <= 0:
            return None
        if peak < avg_price + self.activation_r * risk:
            return None
        return fractional_giveback_target(avg_price, peak, frac=self.trail_frac)
