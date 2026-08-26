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

from broker_contract.exit_geometry.levels import chandelier_target
from broker_contract.exit_geometry.registry import ExitGeometryPolicy


@runtime_checkable
class ExitPolicy(Protocol):
    # The BEHAVIORAL identity — the key ALPHALENS_BROKER_EXIT_POLICY selects and
    # the one an operator reads in a log line. It must never be derived from a
    # wrapped geometry: two policies can share a geometry and differ in whether
    # they trail, which is the defect issue #1138 records.
    name: str
    # The geometry the policy places against, or None when it places none.
    # A separate, equally real fact — a reader needs both.
    geometry_name: str | None
    version: int
    applies_geometry: bool
    requires_amend_stop: bool
    min_stop_distance_frac: float
    trails: bool

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None: ...

    def decide_reanchor(
        self,
        avg_price: float,
        atr: float,
        *,
        peak: float | None = None,
        last_price: float | None = None,
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
