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

from alphalens_pipeline.paper.broker import BrokerClient
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
    n_entries_ttl_canceled: int  # entry-TTL sweep — see _sweep_expired_entries
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


def _sweep_expired_entries(
    conn: sqlite3.Connection,
    *,
    broker: BrokerClient,
    account: str,
    observed_at: dt.datetime,
) -> int:
    """Cancel ENTRY orders on plans whose ``order_ttl_days`` window elapsed.

    For every PLANNED row scoped to ``account`` with no plan_outcome yet,
    compute the number of XNYS trading days elapsed between ``planned_at``
    and ``observed_at`` (half-open, end-inclusive — same-day plan + same-day
    observed = 0 elapsed; Fri-planned + Mon-observed = 1 elapsed). Once
    the count meets or exceeds the per-plan ``order_ttl_days`` budget,
    request Alpaca to cancel every ENTRY order on the plan still in a
    non-terminal local state (SUBMITTED / PARTIALLY_FILLED).

    The PR-B switch from calendar-day arithmetic (``.days`` on a date
    delta) to ``trading_days_elapsed`` matches the trade-setup memo's
    "N trading days" intent — a Fri plan observed Tue-after-Memorial-Day
    is 1 trading day old, not 4. See
    ``docs/research/paper_trading_non_trading_day_2026_05_29.md`` §5.

    The cancel REQUEST is fire-and-forget — local order.status flips on the
    NEXT reconcile poll when Alpaca reports the canonical 'canceled' state.
    This intentional split means the existing per-order reconciler loop +
    ``process_plan_exit`` chain absorb the transition naturally: a plan
    with zero fills lands on UNFILLED, a plan with partial fills lands on
    ATTACHED (TP+SL sized to the partial position). No new exit_kind is
    introduced — UNFILLED already carries the right semantic ("entry
    settled without enough fills to manage").

    Account scoping is mandatory: a MAIN-account reconciler pass MUST NOT
    cancel TEST-account UUIDs (would 404 against the wrong client and the
    cross-pollute side-effect would silently corrupt the test sandbox).

    Returns the count of cancel requests Alpaca acknowledged. SDK failures
    are warning-logged and the sweep continues so a single bad UUID does
    not abort handling of the rest of the basket on the same cycle.
    """
    from alphalens_pipeline.paper.calendar import trading_days_elapsed

    cur = conn.execute(
        """SELECT p.plan_id, p.planned_at, p.order_ttl_days
           FROM plans p
           LEFT JOIN plan_outcomes po ON po.plan_id = p.plan_id
           WHERE p.status = 'PLANNED' AND po.outcome_id IS NULL
             AND p.account = ?""",
        (account,),
    )
    n_canceled = 0
    for row in cur.fetchall():
        plan_id = int(row["plan_id"])
        planned_at = dt.datetime.fromisoformat(row["planned_at"])
        # SQLite has no tz; align with the rest of the harness (cf. the
        # exit_manager._first_fill_at_for_plan helper) and coerce naive
        # writes to UTC so the date-arithmetic does not TypeError.
        if planned_at.tzinfo is None:
            planned_at = planned_at.replace(tzinfo=dt.UTC)
        ttl_days = int(row["order_ttl_days"])
        # Trading-day delta (XNYS sessions strictly after ``planned_at``
        # up through ``observed_at``). Mirrors how the trade-setup memo
        # specifies "N trading days" of fill window.
        days_since = trading_days_elapsed(planned_at, observed_at)
        if days_since < ttl_days:
            continue
        entry_cur = conn.execute(
            """SELECT alpaca_order_id FROM orders
               WHERE plan_id = ? AND order_kind = 'ENTRY'
                 AND status IN ('SUBMITTED', 'PARTIALLY_FILLED')""",
            (plan_id,),
        )
        for entry_row in entry_cur.fetchall():
            alpaca_id = entry_row["alpaca_order_id"]
            try:
                broker.cancel_order(alpaca_id)
            except Exception as exc:
                logger.warning(
                    "ttl-sweep cancel failed alpaca=%s plan_id=%d: %s; will retry next cycle",
                    alpaca_id,
                    plan_id,
                    exc,
                )
                continue
            n_canceled += 1
            logger.info(
                "ttl-sweep cancel alpaca=%s plan_id=%d days_since=%d ttl=%d",
                alpaca_id,
                plan_id,
                days_since,
                ttl_days,
            )
    return n_canceled


def reconcile_orders(
    *,
    ledger_path: Path,
    broker: BrokerClient,
    account: str = "main",
    observed_at: dt.datetime | None = None,
) -> ReconcileReport:
    """Walk every open ledger order on the given ``account``, pull its
    Alpaca counterpart, append fills + transition status as needed.

    "Open" = ledger status in ``('SUBMITTED', 'PARTIALLY_FILLED')``. Terminal
    statuses (FILLED / CANCELED / REJECTED / EXPIRED) are skipped — the
    reconciler does not revive them. The TP/SL/time-stop attacher in
    step 5 watches FILLED ENTRY orders to know when to submit exits.

    ``account`` (v4): scopes the open-orders sweep so a reconciler pass
    against TEST does NOT try to fetch MAIN-account UUIDs (which would
    404). The broker passed in MUST be the matching profile.

    ``observed_at`` (PR-B): optional override for the timestamp the TTL
    + time-stop sweeps treat as "now". Defaults to UTC wall-clock when
    None. Tests pin both ``planned_at`` and ``observed_at`` to known
    XNYS anchor dates so the trading-day arithmetic is deterministic
    independent of which weekday CI happens to run on.
    """
    from alphalens_pipeline.paper.exit_manager import process_plan_exit

    if observed_at is None:
        observed_at = dt.datetime.now(dt.UTC)
    outcomes: list[OrderReconcileOutcome] = []
    transitioned = 0
    appended = 0
    plans_touched: set[int] = set()

    with open_ledger(ledger_path) as conn:
        # Entry-TTL sweep runs FIRST so any Alpaca cancel-acks Alpaca
        # processes synchronously are observed by the per-order loop below
        # in the SAME reconcile cycle (compresses TTL-cancel → ledger
        # CANCELED → exit_manager UNFILLED into one pass instead of three).
        # Slow Alpaca processing is fine too — the per-order loop just sees
        # SUBMITTED for one more cycle, the next cycle catches it. Note
        # that the same order may appear in BOTH the sweep (cancel sent to
        # Alpaca) and the per-order loop (status re-polled) within one
        # cycle; this is expected, not redundant — the per-order poll is
        # what transitions ``orders.status`` to CANCELED once Alpaca acks.
        n_entries_ttl_canceled = _sweep_expired_entries(
            conn,
            broker=broker,
            account=account,
            observed_at=observed_at,
        )

        open_rows = fetch_open_orders(conn, account=account)
        for ledger_row in open_rows:
            alpaca_order_id = ledger_row["alpaca_order_id"]
            try:
                alpaca_order = broker.get_order(alpaca_order_id)
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
        # orders CANCELED by TTL with zero fills). Scoped to ``account``
        # so a MAIN reconciler pass doesn't try to attach exits to TEST
        # plans (and submit them via the MAIN client).
        cur = conn.execute(
            """SELECT DISTINCT p.plan_id FROM plans p
               LEFT JOIN plan_outcomes po ON po.plan_id = p.plan_id
               WHERE p.status = 'PLANNED' AND po.outcome_id IS NULL
                 AND p.account = ?""",
            (account,),
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
                broker=broker,
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
        guard = check_live_gross(broker)
        gross_ratio = guard.gross_ratio
        gross_warning = guard.warning_emitted
    except Exception as exc:
        logger.warning("gross guard check failed (will retry next cycle): %s", exc)
        gross_ratio = 0.0
        gross_warning = False

    logger.info(
        "paper reconcile: %d orders checked, %d transitioned, %d fills appended, "
        "%d exits attached, %d outcomes written, %d time-stops, %d ttl-cancels, gross=%.2f",
        len(outcomes),
        transitioned,
        appended,
        n_attached,
        n_outcomes,
        n_time_stops,
        n_entries_ttl_canceled,
        gross_ratio,
    )
    return ReconcileReport(
        n_orders_checked=len(outcomes),
        n_orders_transitioned=transitioned,
        n_fills_appended=appended,
        n_exits_attached=n_attached,
        n_outcomes_written=n_outcomes,
        n_time_stops_fired=n_time_stops,
        n_entries_ttl_canceled=n_entries_ttl_canceled,
        gross_ratio=gross_ratio,
        gross_warning_emitted=gross_warning,
        outcomes=tuple(outcomes),
    )


__all__ = [
    "OrderReconcileOutcome",
    "ReconcileReport",
    "reconcile_orders",
]


# Internal hook for the future R3 follow-up (cancel unfilled entries on TP1):
# the same "cancel ENTRY orders for a given plan_id" code path lives inside
# ``_sweep_expired_entries``. When R3 ships, the TP1-fill handler in
# exit_manager will need an analogous cancel-loop scoped by plan_id rather
# than by TTL expiry — promote the inner cancel block to a shared helper at
# that point and call it from both spots. Not extracted today because YAGNI
# until R3 actually starts.
