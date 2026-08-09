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
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from broker_contract.exit_geometry.levels import chandelier_target
from broker_contract.exit_geometry.registry import ExitGeometryPolicy


@runtime_checkable
class ExitPolicy(Protocol):
    name: str
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
    applies_geometry: bool = True
    requires_amend_stop: bool = True
    min_stop_distance_frac: float = 0.002  # hair-trigger floor; never binds 1.5x ATR
    trails: bool = False

    @property
    def name(self) -> str:
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
    two differ ONLY in ``decide_reanchor``."""

    geom: ExitGeometryPolicy
    activation_r: float
    k_atr: float
    applies_geometry: bool = True
    requires_amend_stop: bool = True
    min_stop_distance_frac: float = 0.002  # hair-trigger floor; never binds 1.5x ATR
    trails: bool = True

    @property
    def name(self) -> str:
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
