"""Reconcile ledger order state with Alpaca paper.

Polls Alpaca for the current state of every open ledger order, appends
fills as Alpaca reports new filled quantity, and transitions
``orders.status`` through the canonical lifecycle (SUBMITTED ->
PARTIALLY_FILLED -> FILLED / CANCELED / REJECTED / EXPIRED).

Phase A scope (this step 4/8):
- Poll-based fills tracker only.
- TP/SL/time-stop exit choreography lives in :mod:`exit_manager` (step 5+).
- Live-gross WARNING lives in :mod:`gross_guard` (step 7).

Idempotency: each observed filled_qty value produces ONE fill row keyed
by ``f"{alpaca_order_id}-{filled_qty}"`` so re-polling with identical
state is a no-op (UNIQUE constraint on ``fills.alpaca_fill_id``).
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphalens_pipeline.paper.ledger import (
    fetch_fills_for_order,
    fetch_open_orders,
    insert_fill,
    open_ledger,
    update_order_status,
)

logger = logging.getLogger(__name__)


# Mapping from Alpaca SDK OrderStatus enum (string form) to our local
# ledger status. The SDK returns lower-case identifiers; we uppercase for
# consistency with our schema CHECK constraint.
_ALPACA_STATUS_MAP: dict[str, str] = {
    "new": "SUBMITTED",
    "accepted": "SUBMITTED",
    "pending_new": "SUBMITTED",
    "accepted_for_bidding": "SUBMITTED",
    "partially_filled": "PARTIALLY_FILLED",
    "filled": "FILLED",
    "done_for_day": "FILLED",  # treated as terminal for our analysis
    "canceled": "CANCELED",
    "expired": "EXPIRED",
    "rejected": "REJECTED",
    "stopped": "CANCELED",
    "suspended": "SUBMITTED",
    "pending_cancel": "PARTIALLY_FILLED",  # still has fills if any
    "pending_replace": "PARTIALLY_FILLED",
    "replaced": "PARTIALLY_FILLED",
    "calculated": "PARTIALLY_FILLED",
    "held": "SUBMITTED",
}


def _map_alpaca_status(status_raw: Any) -> str:
    """Translate an Alpaca status (enum OR string) to our local status.

    Alpaca-py's OrderStatus is an Enum; calling ``str(status)`` typically
    yields ``"OrderStatus.NEW"``. We extract the suffix + lowercase to
    look up the mapping. Unknown statuses fall through to SUBMITTED with
    a warning so a new SDK status doesn't silently corrupt the ledger.
    """
    text = str(status_raw).split(".")[-1].lower()
    mapped = _ALPACA_STATUS_MAP.get(text)
    if mapped is None:
        logger.warning("unknown Alpaca status %r; treating as SUBMITTED", status_raw)
        return "SUBMITTED"
    return mapped


@dataclass(frozen=True)
class OrderReconcileOutcome:
    """One ledger order's outcome from a reconcile pass."""

    order_id: int
    alpaca_order_id: str
    prev_status: str
    new_status: str
    n_new_fills: int


@dataclass(frozen=True)
class ReconcileReport:
    """Aggregate report of one ``alphalens paper reconcile`` invocation."""

    n_orders_checked: int
    n_orders_transitioned: int
    n_fills_appended: int
    n_exits_attached: int
    n_outcomes_written: int
    n_time_stops_fired: int
    gross_ratio: float
    gross_warning_emitted: bool
    outcomes: tuple[OrderReconcileOutcome, ...]


def _existing_fill_ids(conn: sqlite3.Connection, order_id: int) -> set[str]:
    return {row["alpaca_fill_id"] for row in fetch_fills_for_order(conn, order_id)}


def _summed_filled_qty(conn: sqlite3.Connection, order_id: int) -> int:
    cur = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM fills WHERE order_id = ?", (order_id,))
    return int(cur.fetchone()[0])


def _process_one_order(
    conn: sqlite3.Connection,
    *,
    ledger_row: sqlite3.Row,
    alpaca_order: Any,
    observed_at: dt.datetime,
) -> OrderReconcileOutcome:
    """Reconcile one ledger order against its Alpaca counterpart."""
    order_id = int(ledger_row["order_id"])
    alpaca_order_id = ledger_row["alpaca_order_id"]
    prev_status = ledger_row["status"]
    new_status = _map_alpaca_status(alpaca_order.status)

    # Alpaca tracks filled_qty as a cumulative count (string) and
    # filled_avg_price as the volume-weighted blended price (None until
    # any fill). Convert defensively.
    alpaca_filled_qty = int(float(getattr(alpaca_order, "filled_qty", 0) or 0))
    alpaca_avg_price_raw = getattr(alpaca_order, "filled_avg_price", None)
    alpaca_avg_price = (
        float(alpaca_avg_price_raw)
        if alpaca_avg_price_raw is not None and str(alpaca_avg_price_raw) != "None"
        else None
    )

    locally_filled = _summed_filled_qty(conn, order_id)
    delta = alpaca_filled_qty - locally_filled
    n_new_fills = 0

    if delta > 0 and alpaca_avg_price is not None:
        # Synthesize one fill row capturing the new chunk. The fill_id is
        # derived from the cumulative filled_qty so repeated polls with
        # identical state hit UNIQUE and silently no-op.
        synthetic_fill_id = f"{alpaca_order_id}-cum-{alpaca_filled_qty}"
        if synthetic_fill_id not in _existing_fill_ids(conn, order_id):
            insert_fill(
                conn,
                order_id=order_id,
                alpaca_fill_id=synthetic_fill_id,
                qty=delta,
                price=alpaca_avg_price,
                filled_at=observed_at,
            )
            n_new_fills = 1
            logger.info(
                "paper reconcile fill alpaca=%s order_id=%d +qty=%d @%.2f -> %d/%d",
                alpaca_order_id,
                order_id,
                delta,
                alpaca_avg_price,
                alpaca_filled_qty,
                int(ledger_row["qty"]),
            )

    if new_status != prev_status:
        update_order_status(
            conn,
            order_id=order_id,
            status=new_status,
            last_updated_at=observed_at,
        )
        logger.info(
            "paper reconcile status alpaca=%s order_id=%d %s -> %s",
            alpaca_order_id,
            order_id,
            prev_status,
            new_status,
        )

    return OrderReconcileOutcome(
        order_id=order_id,
        alpaca_order_id=alpaca_order_id,
        prev_status=prev_status,
        new_status=new_status,
        n_new_fills=n_new_fills,
    )


def reconcile_orders(
    *,
    ledger_path: Path,
    alpaca_client: Any,
) -> ReconcileReport:
    """Walk every open ledger order, pull its Alpaca counterpart, append
    fills + transition status as needed.

    "Open" = ledger status in ``('SUBMITTED', 'PARTIALLY_FILLED')``. Terminal
    statuses (FILLED / CANCELED / REJECTED / EXPIRED) are skipped — the
    reconciler does not revive them. The TP/SL/time-stop attacher in
    step 5 watches FILLED ENTRY orders to know when to submit exits.
    """
    from alphalens_pipeline.paper.exit_manager import process_plan_exit

    observed_at = dt.datetime.now(dt.UTC)
    outcomes: list[OrderReconcileOutcome] = []
    transitioned = 0
    appended = 0
    plans_touched: set[int] = set()

    with open_ledger(ledger_path) as conn:
        open_rows = fetch_open_orders(conn)
        for ledger_row in open_rows:
            alpaca_order_id = ledger_row["alpaca_order_id"]
            try:
                alpaca_order = alpaca_client.get_order(alpaca_order_id)
            except Exception as exc:
                logger.warning(
                    "reconcile failed to fetch Alpaca order %s: %s; will retry next cycle",
                    alpaca_order_id,
                    exc,
                )
                continue
            outcome = _process_one_order(
                conn,
                ledger_row=ledger_row,
                alpaca_order=alpaca_order,
                observed_at=observed_at,
            )
            outcomes.append(outcome)
            if outcome.new_status != outcome.prev_status:
                transitioned += 1
            appended += outcome.n_new_fills
            plans_touched.add(int(ledger_row["plan_id"]))

        # Plus every plan that has open exit orders or no outcome yet —
        # the exit_manager may need to attach exits / write outcomes for
        # plans whose orders are all already terminal (e.g. all entry
        # orders CANCELED by TTL with zero fills).
        cur = conn.execute(
            """SELECT DISTINCT p.plan_id FROM plans p
               LEFT JOIN plan_outcomes po ON po.plan_id = p.plan_id
               WHERE p.status = 'PLANNED' AND po.outcome_id IS NULL"""
        )
        for row in cur.fetchall():
            plans_touched.add(int(row[0]))

        # Drive each touched plan through the exit-phase state machine.
        n_attached = 0
        n_outcomes = 0
        n_time_stops = 0
        for plan_id in sorted(plans_touched):
            exit_outcome = process_plan_exit(
                conn,
                plan_id=plan_id,
                alpaca_client=alpaca_client,
                observed_at=observed_at,
            )
            if exit_outcome.action == "ATTACHED":
                n_attached += exit_outcome.n_exits_submitted
            elif exit_outcome.action in ("CLOSED", "UNFILLED"):
                n_outcomes += 1
            elif exit_outcome.action == "TIME_STOP":
                n_time_stops += 1

    # Memo §6.1 Path B — closed-loop live-gross check post-reconcile.
    from alphalens_pipeline.paper.gross_guard import check_live_gross

    try:
        guard = check_live_gross(alpaca_client)
        gross_ratio = guard.gross_ratio
        gross_warning = guard.warning_emitted
    except Exception as exc:
        logger.warning("gross guard check failed (will retry next cycle): %s", exc)
        gross_ratio = 0.0
        gross_warning = False

    logger.info(
        "paper reconcile: %d orders checked, %d transitioned, %d fills appended, "
        "%d exits attached, %d outcomes written, %d time-stops, gross=%.2f",
        len(outcomes),
        transitioned,
        appended,
        n_attached,
        n_outcomes,
        n_time_stops,
        gross_ratio,
    )
    return ReconcileReport(
        n_orders_checked=len(outcomes),
        n_orders_transitioned=transitioned,
        n_fills_appended=appended,
        n_exits_attached=n_attached,
        n_outcomes_written=n_outcomes,
        n_time_stops_fired=n_time_stops,
        gross_ratio=gross_ratio,
        gross_warning_emitted=gross_warning,
        outcomes=tuple(outcomes),
    )


__all__ = [
    "OrderReconcileOutcome",
    "ReconcileReport",
    "reconcile_orders",
]
