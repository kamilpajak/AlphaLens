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

from broker_contract.exit_geometry.registry import ExitGeometryPolicy


@runtime_checkable
class ExitPolicy(Protocol):
    name: str
    version: int
    applies_geometry: bool
    requires_amend_stop: bool
    min_stop_distance_frac: float

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None: ...

    def decide_reanchor(self, avg_price: float, atr: float) -> float | None: ...


@dataclass(frozen=True)
class SetupStaticPolicy:
    name: str = "setup_static"
    version: int = 1
    applies_geometry: bool = False
    requires_amend_stop: bool = False
    min_stop_distance_frac: float = 0.0

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None:
        return None

    def decide_reanchor(self, avg_price: float, atr: float) -> float | None:
        return None


@dataclass(frozen=True)
class AtrBracketPolicy:
    geom: ExitGeometryPolicy
    applies_geometry: bool = True
    requires_amend_stop: bool = True
    min_stop_distance_frac: float = 0.002  # hair-trigger floor; never binds 1.5x ATR

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

    def decide_reanchor(self, avg_price: float, atr: float) -> float | None:
        if not math.isfinite(avg_price) or avg_price <= 0:
            return None
        if not math.isfinite(atr) or atr <= 0:
            return None
        target = avg_price - self.geom.stop_atr_mult * atr
        if not math.isfinite(target) or target <= 0:
            return None
        return target
