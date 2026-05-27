"""Dataclasses + JSON contract for a deterministic Trade Setup.

The ``TradeSetup.to_dict()`` shape is the persisted ``brief_trade_setup``
JSON (Django ``JSONField`` / parquet JSON string). Consumers MUST check
``schema_version`` and reject unknown versions rather than parse-and-fail
(zen review §5, 2026-05-27).
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = "1.0.0"

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
        return {
            "limit": round(self.limit, 2),
            "alloc_pct": round(self.alloc_pct, 1),
            "atr_distance": round(self.atr_distance, 2),
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
            "target": round(self.target, 2),
            "tranche_pct": round(self.tranche_pct, 1),
            "r_multiple": round(self.r_multiple, 2),
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

    @classmethod
    def no_structure(cls, *, asof_close: float, atr: float, order_ttl_days: int) -> TradeSetup:
        """Emit when there is no usable structure (illiquid / no supports below)."""
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
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "asof_close": round(self.asof_close, 2),
            "atr": round(self.atr, 2),
            "disaster_stop": None if self.disaster_stop is None else round(self.disaster_stop, 2),
            "suggested_size_pct": (
                None if self.suggested_size_pct is None else round(self.suggested_size_pct, 2)
            ),
            "order_ttl_days": self.order_ttl_days,
            "entry_tiers": [t.to_dict() for t in self.entry_tiers],
            "tp_tranches": [t.to_dict() for t in self.tp_tranches],
        }


__all__ = [
    "SCHEMA_VERSION",
    "STATUS_NO_STRUCTURE",
    "STATUS_OK",
    "EntryTier",
    "TpTranche",
    "TradeSetup",
]
