"""Submit entry-tier orders for already-planned candidates.

Reads PLANNED rows from the ledger for a given brief_date and routes each
candidate's entry-ladder tiers to Alpaca paper as limit-GTC BUY orders.
Idempotent at the (plan_id, tier_index) key — re-running after a partial
submit (mid-batch crash, network blip) only submits the tiers that don't
already have an ENTRY row in ``orders``.

What this module does NOT do:
- Plan candidates. That's :mod:`alphalens_pipeline.paper.planner`.
- Reconcile fills or attach TP/SL exits. That's
  :mod:`alphalens_pipeline.paper.reconciler` (next step in PR 3).
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphalens_pipeline.paper.ledger import (
    fetch_orders_for_plan,
    insert_order,
    open_ledger,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitOutcome:
    """One candidate's outcome from a submit run."""

    plan_id: int
    ticker: str
    n_tiers_submitted: int
    n_tiers_skipped_existing: int
    n_tiers_skipped_zero_qty: int


@dataclass(frozen=True)
class SubmitReport:
    """Aggregate report of one ``alphalens paper submit`` invocation."""

    brief_date: dt.date
    n_plans_processed: int
    n_orders_submitted: int
    outcomes: tuple[SubmitOutcome, ...]


def _existing_entry_tier_indices(conn: sqlite3.Connection, plan_id: int) -> set[int]:
    """Build the set of ``tier_index`` values that already have an ENTRY
    order row for this plan. The submitter skips those on re-runs to
    avoid double-submission after a crash mid-batch.
    """
    return {
        row["tier_index"]
        for row in fetch_orders_for_plan(conn, plan_id)
        if row["order_kind"] == "ENTRY" and row["tier_index"] is not None
    }


def _fetch_tiers_for_plan(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM plan_entries WHERE plan_id = ? ORDER BY tier_index",
        (plan_id,),
    )
    return list(cur.fetchall())


def _fetch_planned_for_date(
    conn: sqlite3.Connection, brief_date: dt.date, account: str
) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM plans WHERE brief_date = ? AND status = 'PLANNED' AND account = ? "
        "ORDER BY plan_id",
        (brief_date.isoformat(), account),
    )
    return list(cur.fetchall())


def submit_for_date(
    *,
    brief_date: dt.date,
    ledger_path: Path,
    alpaca_client: Any,
    account: str = "main",
) -> SubmitReport:
    """Submit entry-tier limit orders for every PLANNED candidate on
    ``brief_date`` that hasn't been submitted yet.

    Args:
        brief_date: ISO date of the brief whose plans to submit against.
        ledger_path: SQLite ledger location.
        alpaca_client: a wired :class:`AlpacaClient` (main OR test profile).

    For each PLANNED plan:
      For each tier with qty > 0 that doesn't already have an ENTRY
      order row:
        - submit_limit_order(BUY, GTC) via the AlpacaClient
        - persist the returned alpaca_order_id to ``orders`` with
          order_kind='ENTRY', status='SUBMITTED'

    Returns a :class:`SubmitReport` summarising the per-plan outcome —
    how many tiers each plan actually pushed to Alpaca vs how many were
    already there (idempotent skip) vs how many had qty=0 (audit-visible
    in the plan but not real orders).
    """
    submitted_at = dt.datetime.now(dt.UTC)
    outcomes: list[SubmitOutcome] = []
    total_submitted = 0

    with open_ledger(ledger_path) as conn:
        plans = _fetch_planned_for_date(conn, brief_date, account)

        for plan in plans:
            plan_id = int(plan["plan_id"])
            ticker = plan["ticker"]
            tiers = _fetch_tiers_for_plan(conn, plan_id)
            existing = _existing_entry_tier_indices(conn, plan_id)

            n_submitted = 0
            n_skipped_existing = 0
            n_skipped_zero_qty = 0

            for tier in tiers:
                tier_index = int(tier["tier_index"])
                qty = int(tier["qty"])
                limit_price = float(tier["limit_price"])

                if qty <= 0:
                    n_skipped_zero_qty += 1
                    continue
                if tier_index in existing:
                    n_skipped_existing += 1
                    continue

                order = alpaca_client.submit_limit_order(
                    symbol=ticker,
                    qty=qty,
                    limit_price=limit_price,
                    side="buy",
                    time_in_force="gtc",
                )
                alpaca_order_id = str(order.id)
                insert_order(
                    conn,
                    plan_id=plan_id,
                    alpaca_order_id=alpaca_order_id,
                    side="BUY",
                    order_kind="ENTRY",
                    tier_index=tier_index,
                    order_type="LIMIT",
                    qty=qty,
                    limit_price=limit_price,
                    time_in_force="gtc",
                    submitted_at=submitted_at,
                    account=account,
                )
                n_submitted += 1
                total_submitted += 1
                logger.info(
                    "paper submit %s plan_id=%d tier=%d qty=%d limit=%.2f alpaca_id=%s",
                    ticker,
                    plan_id,
                    tier_index,
                    qty,
                    limit_price,
                    alpaca_order_id,
                )

            outcomes.append(
                SubmitOutcome(
                    plan_id=plan_id,
                    ticker=ticker,
                    n_tiers_submitted=n_submitted,
                    n_tiers_skipped_existing=n_skipped_existing,
                    n_tiers_skipped_zero_qty=n_skipped_zero_qty,
                )
            )

    logger.info(
        "paper submit %s: %d plans processed, %d orders submitted",
        brief_date.isoformat(),
        len(plans),
        total_submitted,
    )

    return SubmitReport(
        brief_date=brief_date,
        n_plans_processed=len(plans),
        n_orders_submitted=total_submitted,
        outcomes=tuple(outcomes),
    )


__all__ = [
    "SubmitOutcome",
    "SubmitReport",
    "submit_for_date",
]
