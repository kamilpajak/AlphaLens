"""Offline execution-quality reconciler (build-seq 1b-ii).

The fire path already stamps a decision-side ``tranche_fired`` telemetry line
(``live_exit_engine._fire_telemetry`` / ``mark_tranche_fired``) carrying the
provider bid/ask/mid at the decision instant plus a ``sell_order_id`` JOIN KEY.
This module joins the broker's ACTUAL fill to that line — OFFLINE, after the
fact — by resolving each ``sell_order_id`` through the vendor
``SupportsOrderResolution`` capability (Saxo: one audit-trail read) and computes
the implementation shortfall.

STRICTLY READ-ONLY against the broker: :func:`reconcile_fills` is a PURE
function (no I/O, no clock) of the journal lines + the resolver; nothing here
places, cancels, or amends an order. :func:`write_exec_quality_parquet` is the
only side effect — it rebuilds a single overwrite parquet from the records.

Sign convention (see :class:`ExecQualityRecord`): for SELLING a long the
decision reference is the BID, so a fill BELOW the decision bid is an adverse
cost and reads as POSITIVE slippage.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from broker_contract.contract import OrderStatus

from alphalens_pipeline.brokers.reconcile import SupportsOrderResolution

# Fill-status vocabulary (kept as named constants — never bare string literals
# at the call sites).
FILL_STATUS_FILLED = "filled"
FILL_STATUS_PENDING = "pending"
FILL_STATUS_UNRESOLVED = "unresolved"

# Basis-points scale for the slippage-in-bps conversion.
_BPS_SCALE = 1e4

# The runtime data root ($HOME/.alphalens) — same base the automanager / broker
# paths use (see ``control_loop._ALPHALENS_HOME``). The write function ALWAYS
# takes an explicit ``out_path``, so this constant is only the operator default
# and is never read implicitly by the tests.
_ALPHALENS_HOME = Path.home() / ".alphalens"
EXEC_QUALITY_PARQUET = _ALPHALENS_HOME / "exec_quality" / "tranche_fills.parquet"


@dataclass(frozen=True)
class ExecQualityRecord:
    """One reconciled tranche fire: decision-side telemetry joined to the fill.

    Sign convention — for selling a long the decision reference is the BID:

    - ``slippage_abs = decision_bid - fill_price`` — POSITIVE means the tranche
      filled BELOW the decision bid (an adverse execution cost); NEGATIVE means
      it filled above the bid (favorable).
    - ``slippage_bps = slippage_abs / decision_bid * 1e4`` — guarded to ``None``
      when ``decision_bid <= 0`` (no meaningful denominator).

    ``fill_status`` is one of ``"filled"`` / ``"pending"`` / ``"unresolved"``.
    Price / quantity / slippage fields are ``None`` (HONEST — never fabricated)
    whenever the fill is not a verified priced fill.
    """

    uic: int
    tag: str
    sell_order_id: str
    decision_bid: float
    decision_mid: float
    target_price: float
    planned_qty: int
    event_time: str | None
    fill_status: str
    fill_price: float | None
    filled_qty: float | None
    slippage_abs: float | None
    slippage_bps: float | None


# Stable parquet column order (the record's field order). A downstream reader
# keys off these verbatim; adding a column is strictly-additive only.
EXEC_QUALITY_COLUMNS: tuple[str, ...] = tuple(ExecQualityRecord.__dataclass_fields__)

# Pinned pyarrow schema so EVERY rebuilt parquet (empty, all-unresolved, or
# priced) has an IDENTICAL arrow schema — a downstream reader unioning periodic
# snapshots never sees dtype drift. Field order mirrors EXEC_QUALITY_COLUMNS;
# ``None`` values land as arrow nulls.
_ARROW_SCHEMA = pa.schema(
    [
        ("uic", pa.int64()),
        ("tag", pa.string()),
        ("sell_order_id", pa.string()),
        ("decision_bid", pa.float64()),
        ("decision_mid", pa.float64()),
        ("target_price", pa.float64()),
        ("planned_qty", pa.int64()),
        ("event_time", pa.string()),
        ("fill_status", pa.string()),
        ("fill_price", pa.float64()),
        ("filled_qty", pa.float64()),
        ("slippage_abs", pa.float64()),
        ("slippage_bps", pa.float64()),
    ]
)


def _compute_slippage(
    decision_bid: float, fill_price: float | None
) -> tuple[float | None, float | None]:
    """(slippage_abs, slippage_bps) for a priced fill; nulls when unpriced.

    ``bps`` is guarded on ``decision_bid > 0`` — a zero/negative reference has
    no meaningful denominator, so bps is ``None`` while abs stays the raw diff.
    """
    if fill_price is None:
        return None, None
    slippage_abs = decision_bid - fill_price
    slippage_bps = slippage_abs / decision_bid * _BPS_SCALE if decision_bid > 0 else None
    return slippage_abs, slippage_bps


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """int() via float() so a numeric-string / float uic still parses; None on
    a non-numeric value (a malformed journal line is skipped, never a crash)."""
    parsed = _as_float(value)
    return int(parsed) if parsed is not None else None


def _record_for(
    line: Mapping[str, Any],
    telemetry: Mapping[str, Any],
    resolver: SupportsOrderResolution,
    *,
    uic: int,
) -> ExecQualityRecord:
    sell_order_id = str(telemetry["sell_order_id"])
    decision_bid = float(telemetry.get("decision_bid") or 0.0)
    planned_qty_raw = _as_float(telemetry.get("qty"))
    planned_qty = int(planned_qty_raw) if planned_qty_raw is not None else 0

    state = resolver.resolve_order_outcome(sell_order_id)

    fill_status = FILL_STATUS_PENDING
    fill_price: float | None = None
    filled_qty: float | None = None
    slippage_abs: float | None = None
    slippage_bps: float | None = None

    if state.status is OrderStatus.FILLED:
        # Filled either way; the PRICE may still be unverified (avg_fill_price
        # None) — record the fill honestly and leave slippage null in that case.
        fill_status = FILL_STATUS_FILLED
        filled_qty = state.filled_quantity
        fill_price = state.avg_fill_price
        slippage_abs, slippage_bps = _compute_slippage(decision_bid, fill_price)
    elif state.status is OrderStatus.UNKNOWN:
        # The resolver could not determine a terminal outcome (retention gap /
        # inconsistent audit row) — surface as unresolved, never guessed.
        fill_status = FILL_STATUS_UNRESOLVED
    # Any other terminal (CANCELLED / REJECTED / EXPIRED) or a not-yet-final
    # order (WORKING / PARTIALLY_FILLED) stays pending with nulls.

    return ExecQualityRecord(
        uic=uic,
        tag=str(line["tag"]),
        sell_order_id=sell_order_id,
        decision_bid=decision_bid,
        decision_mid=float(telemetry.get("decision_mid") or 0.0),
        target_price=float(telemetry.get("target_price") or 0.0),
        planned_qty=planned_qty,
        event_time=telemetry.get("event_time"),
        fill_status=fill_status,
        fill_price=fill_price,
        filled_qty=filled_qty,
        slippage_abs=slippage_abs,
        slippage_bps=slippage_bps,
    )


def reconcile_fills(
    journal_lines: Iterable[Mapping[str, Any]], resolver: SupportsOrderResolution
) -> list[ExecQualityRecord]:
    """Join each ``tranche_fired`` telemetry line to its broker fill (PURE).

    For every line with ``kind == "tranche_fired"`` carrying a ``telemetry``
    mapping with a truthy ``sell_order_id``, ``resolver.resolve_order_outcome``
    is called and the (Status, avg_fill_price) pair is routed to a
    :class:`ExecQualityRecord`. Lines that are not ``tranche_fired``, or that
    lack a ``telemetry.sell_order_id``, are skipped. Deterministic given the
    same inputs — no I/O, no ``datetime.now``.
    """
    records: list[ExecQualityRecord] = []
    for line in journal_lines:
        if line.get("kind") != "tranche_fired":
            continue
        telemetry = line.get("telemetry")
        if not isinstance(telemetry, Mapping):
            continue
        if not telemetry.get("sell_order_id"):
            continue
        uic = _as_int(line.get("uic"))
        if uic is None or not line.get("tag"):
            continue  # malformed / non-numeric uic — skip, never crash
        records.append(_record_for(line, telemetry, resolver, uic=uic))
    return records


def write_exec_quality_parquet(records: list[ExecQualityRecord], out_path: Path) -> Path:
    """Rebuild (overwrite) the exec-quality parquet from ``records``.

    Parents are created; the write is atomic (tmp + ``os.replace``). Records are
    written through the pinned :data:`_ARROW_SCHEMA`, so EVERY file — empty,
    all-unresolved, or priced — has an IDENTICAL schema (``None`` -> null).
    Returns ``out_path``.
    """
    out_path = Path(out_path)
    table = pa.Table.from_pylist([asdict(r) for r in records], schema=_ARROW_SCHEMA)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    pq.write_table(table, tmp_path)
    os.replace(tmp_path, out_path)
    return out_path


__all__ = [
    "EXEC_QUALITY_COLUMNS",
    "EXEC_QUALITY_PARQUET",
    "FILL_STATUS_FILLED",
    "FILL_STATUS_PENDING",
    "FILL_STATUS_UNRESOLVED",
    "ExecQualityRecord",
    "reconcile_fills",
    "write_exec_quality_parquet",
]
