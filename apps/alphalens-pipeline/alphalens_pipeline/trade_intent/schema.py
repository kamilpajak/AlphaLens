"""Pure value types for the Boundary-2 wire schema (client <-> broker-manager).

Formalizes the shape that today crosses as an armed-pick dict plus the
brief-side setup dict computed by ``compute_setup_plan`` (memo section 2.3):
an unsized :class:`TradeSpec` (entry ladder + disaster stop + TP ladder +
suggested sizing), paired with an :class:`ExitGeometrySpec` describing the
client-precomputed initial stop/TP levels and an optional, bounded-vocabulary
reaction plan the executor evaluates after fill (memo revision R3). These are
pure data: no validation, no parsing, no I/O. The client-side parse/validate
step is deferred to a later PR in the broker-manager extraction sequence (see
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS

# Wire schema version stamped on every TradeSpec / IntentMeta instance so a
# future broker-manager can detect + reject stale client payloads.
SCHEMA_VERSION = "1"

# Reserved multi-tenant dimension. A single account is live today; the field
# exists so a future multi-account broker-manager does not need a schema
# migration to add it.
DEFAULT_ACCOUNT_ID = "default"


@dataclass(frozen=True)
class InstrumentHint:
    """Identifies the tradable instrument (memo section 2.3 Boundary-2 contract)."""

    ticker: str
    mic: str


@dataclass(frozen=True)
class EntryTierSpec:
    """One rung of the client-computed entry ladder."""

    limit_price: float
    alloc_pct: float


@dataclass(frozen=True)
class TpTrancheSpec:
    """One take-profit tranche of the client-computed TP ladder."""

    price: float
    alloc_pct: float


@dataclass(frozen=True)
class TradeSpec:
    """Formalizes ``compute_setup_plan``'s UNSIZED dict input (memo section 2.3)."""

    entry_tiers: tuple[EntryTierSpec, ...]
    disaster_stop: float
    tp_tranches: tuple[TpTrancheSpec, ...]
    suggested_size_pct: float
    order_ttl_days: int = DEFAULT_ORDER_TTL_DAYS
    # Guards the "Sell"->Buy client-side footgun: today only long entries are
    # armed, so the field is pinned to a single literal rather than left open.
    side: Literal["long"] = "long"
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True)
class InitialLevels:
    """Client-precomputed stop/TP pair (via the ``exit_geometry`` leaf)."""

    stop: float
    tp: float


@dataclass(frozen=True)
class ReanchorOnFill:
    """bezpazery + the memo section 4.3 P0 fix: re-anchor stop/TP on fill-complete.

    On fill-complete the executor sets stop/TP to ``avg_price +/- k_atr *
    atr`` (capped at ``ceiling_price`` when given). Carries the absolute
    ``atr`` snapshot (not just the multiplier) so the executor can recompute
    the new levels without a second data fetch.
    """

    k_atr: float
    atr: float
    ceiling_price: float | None = None
    kind: Literal["reanchor_on_fill"] = "reanchor_on_fill"


@dataclass(frozen=True)
class TrailingStop:
    """be_0p5r_trail0p6: arm break-even at ``arm_trigger_r`` MFE, then trail.

    Once favorable excursion reaches ``arm_trigger_r`` multiples of initial
    risk, the executor arms a trailing stop that follows ``trail_frac`` of
    the peak favorable excursion.
    """

    arm_trigger_r: float
    trail_frac: float
    kind: Literal["trailing_stop"] = "trailing_stop"


@dataclass(frozen=True)
class ModelPush:
    """Reserved escape hatch for a model/ML-pushed level (memo revision R3).

    Levels for this primitive arrive via a later ``amend_exit`` call, not via
    fields on this dataclass — it exists only to reserve the ``kind`` tag in
    the discriminated union.
    """

    kind: Literal["model"] = "model"


# Discriminated union of the reaction-plan primitives the executor evaluates
# post-fill. Bounded vocabulary by design (memo revision R3): the executor
# dispatches on the ``kind`` literal, never on an open-ended callable.
ReactionPrimitive = ReanchorOnFill | TrailingStop | ModelPush


@dataclass(frozen=True)
class ExitGeometrySpec:
    """Initial levels plus an optional reaction plan (memo revision R3)."""

    initial_levels: InitialLevels
    reaction_plan: tuple[ReactionPrimitive, ...] = ()


@dataclass(frozen=True)
class IntentMeta:
    """Wire-friendly provenance for a :class:`TradeIntent` (no datetime dep)."""

    # ISO-8601 timestamp string.
    armed_ts: str
    # YYYY-MM-DD string.
    brief_date: str
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True)
class TradeIntent:
    """The Boundary-2 wire type: client-armed pick handed to the broker-manager.

    See ``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
    section 2.3 for the contract this formalizes.
    """

    # Client-authored idempotency key.
    intent_id: str
    instrument: InstrumentHint
    spec: TradeSpec
    # Field name "exit" per the memo contract.
    exit: ExitGeometrySpec
    meta: IntentMeta
    # Reserved tenant dimension; single value today.
    account_id: str = DEFAULT_ACCOUNT_ID
