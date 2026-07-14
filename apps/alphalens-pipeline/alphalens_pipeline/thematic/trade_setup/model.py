"""Dataclasses + JSON contract for a deterministic Trade Setup.

The ``TradeSetup.to_dict()`` shape is the persisted ``brief_trade_setup``
JSON (Django ``JSONField`` / parquet JSON string). Consumers MUST check
``schema_version`` and reject unknown versions rather than parse-and-fail
(zen review §5, 2026-05-27).
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = "1.1.0"  # 1.1.0: adds builder_config_version (ADR 0013)

STATUS_OK = "OK"
STATUS_NO_STRUCTURE = "NO_STRUCTURE"


@dataclass(frozen=True)
class EntryTier:
    """One limit-entry tier. ``atr_distance`` is (close − limit)/ATR (>0 = below close)."""

    limit: float
    alloc_pct: float
    atr_distance: float
    tag: str

    def to_dict(self) -> dict:
        # Full-precision floats — the frontend owns display rounding (toFixed).
        # Rounding here too would double-round / drift vs the presentation layer.
        return {
            "limit": self.limit,
            "alloc_pct": self.alloc_pct,
            "atr_distance": self.atr_distance,
            "tag": self.tag,
        }


@dataclass(frozen=True)
class TpTranche:
    """One take-profit tranche. ``r_multiple`` = (target − blended_entry)/(blended_entry − stop)."""

    target: float
    tranche_pct: float
    r_multiple: float
    tag: str

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "tranche_pct": self.tranche_pct,
            "r_multiple": self.r_multiple,
            "tag": self.tag,
        }


@dataclass(frozen=True)
class TradeSetup:
    """Full deterministic setup for one candidate. Reference levels, not a forecast."""

    schema_version: str
    status: str
    asof_close: float
    atr: float
    disaster_stop: float | None
    suggested_size_pct: float | None
    order_ttl_days: int
    entry_tiers: tuple[EntryTier, ...]
    tp_tranches: tuple[TpTranche, ...]
    # Geometry poolability key (ADR 0013): canonical JSON of the builder/ladder/
    # levels/sizing constants that produced this setup. Defaulted for old-format
    # dicts; both construction paths stamp the live token.
    builder_config_version: str = ""

    @classmethod
    def no_structure(cls, *, asof_close: float, atr: float, order_ttl_days: int) -> TradeSetup:
        """Emit when there is no usable structure (illiquid / no supports below)."""
        from alphalens_pipeline.thematic.trade_setup.config_version import (
            setup_builder_config_version,
        )

        return cls(
            schema_version=SCHEMA_VERSION,
            status=STATUS_NO_STRUCTURE,
            asof_close=asof_close,
            atr=atr,
            disaster_stop=None,
            suggested_size_pct=None,
            order_ttl_days=order_ttl_days,
            entry_tiers=(),
            tp_tranches=(),
            builder_config_version=setup_builder_config_version(),
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "asof_close": self.asof_close,
            "atr": self.atr,
            "disaster_stop": self.disaster_stop,
            "suggested_size_pct": self.suggested_size_pct,
            "order_ttl_days": self.order_ttl_days,
            "entry_tiers": [t.to_dict() for t in self.entry_tiers],
            "tp_tranches": [t.to_dict() for t in self.tp_tranches],
            "builder_config_version": self.builder_config_version,
        }


__all__ = [
    "SCHEMA_VERSION",
    "STATUS_NO_STRUCTURE",
    "STATUS_OK",
    "EntryTier",
    "TpTranche",
    "TradeSetup",
]
