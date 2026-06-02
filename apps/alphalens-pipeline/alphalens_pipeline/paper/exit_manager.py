"""Attach TP / SL exits after entry phase settles + write plan_outcome.

State machine per plan:

  ENTRY phase (multi-tier ladder)
    -> ALL entry orders terminal (FILLED / CANCELED / EXPIRED / REJECTED)
       AND total filled qty > 0:
         -> ATTACH exits (multi-tranche TP limit-sells + single SL stop)
       AND total filled qty == 0:
         -> WRITE plan_outcome(UNFILLED), no exits

  EXIT phase (TPs + SL open)
    -> TPs / SL flow through reconciler.reconcile_orders() like any other
       order (statuses + fills synthesized from Alpaca polls).
    -> When all SELL orders reach terminal state:
         -> WRITE plan_outcome(TP_HIT / SL_HIT / PARTIAL_TP / TIME_STOP_HIT)
            with blended_entry/exit + realized R-multiple.
    -> Time-stop: if first fill > TIME_STOP_DAYS ago and exit phase still
       open, cancel pending exits + submit market-sell for remaining qty.

Phase A simplification: a partial TP fill does NOT shrink the SL qty.
The SL stays sized for the full filled quantity, so if SL fires after
partial TP execution the stop sells slightly less than intended (only
remaining position size). This is a small directional bias — flagged as
Known Issue in the design memo follow-up — and acceptable in exchange
for not needing a cancel-and-resize loop on every TP fill.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from alphalens_pipeline.paper.broker import BrokerClient
from alphalens_pipeline.paper.constants import TIME_STOP_DAYS
from alphalens_pipeline.paper.ledger import (
    fetch_orders_for_plan,
    insert_order,
    insert_plan_outcome,
)

logger = logging.getLogger(__name__)


_TERMINAL_ENTRY_STATUSES = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})


@runtime_checkable
class _RowProto(Protocol):
    """Structural type satisfied by ``sqlite3.Row`` and ``_RowLike``.

    Both ``sqlite3.Row`` (raw fetchone()) and the augmented ``_RowLike``
    adapter (raw row + observed-status overlay) flow through the snapshot,
    so the dataclass field types are widened to the common protocol.
    """

    def __getitem__(self, k: str) -> Any: ...
    def __contains__(self, k: object) -> bool: ...
    def keys(self) -> Any: ...


@dataclass(frozen=True)
class _PlanSnapshot:
    """Cached projection of one plan's lifecycle state for the exit manager."""

    plan_id: int
    ticker: str
    disaster_stop: float
    tp_tranches: tuple[dict, ...]
    first_entry_fill_at: dt.datetime | None
    entry_orders: tuple[_RowProto, ...]
    exit_orders: tuple[_RowProto, ...]
    has_outcome: bool
    account: str  # v4: 'main' or 'test' — used when inserting exit orders

    @property
    def entry_phase_settled(self) -> bool:
        if not self.entry_orders:
            return False
        return all(o["status"] in _TERMINAL_ENTRY_STATUSES for o in self.entry_orders)

    @property
    def total_entry_filled_qty(self) -> int:
        return sum(
            int(o["filled_qty_observed"] or 0) if "filled_qty_observed" in o else 0
            for o in self.entry_orders
        )


@dataclass(frozen=True)
class ExitOutcome:
    """One plan's exit-phase outcome from a reconcile pass."""

    plan_id: int
    action: str  # 'ATTACHED' | 'CLOSED' | 'TIME_STOP' | 'UNFILLED' | 'NOOP'
    n_exits_submitted: int = 0
    exit_kind: str | None = None


def _fetch_plan_meta(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,))
    return cur.fetchone()


def _fetch_tp_tranches(conn: sqlite3.Connection, plan_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM plan_exits WHERE plan_id = ? ORDER BY tranche_index",
        (plan_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_outcome_exists(conn: sqlite3.Connection, plan_id: int) -> bool:
    cur = conn.execute("SELECT 1 FROM plan_outcomes WHERE plan_id = ? LIMIT 1", (plan_id,))
    return cur.fetchone() is not None


def _filled_qty_for_order(conn: sqlite3.Connection, order_id: int) -> int:
    cur = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM fills WHERE order_id = ?", (order_id,))
    return int(cur.fetchone()[0])


def _first_fill_at_for_plan(conn: sqlite3.Connection, plan_id: int) -> dt.datetime | None:
    cur = conn.execute(
        """SELECT MIN(f.filled_at)
           FROM fills f JOIN orders o ON f.order_id = o.order_id
           WHERE o.plan_id = ? AND o.order_kind = 'ENTRY'""",
        (plan_id,),
    )
    row = cur.fetchone()
    ts_str = row[0] if row else None
    if ts_str is None:
        return None
    ts = dt.datetime.fromisoformat(ts_str)
    # SQLite has no native tz support; the writer (reconciler) stores
    # UTC-aware ISO strings, but an operator who manually patches a row
    # via raw SQL could leave a naive timestamp. Coerce to UTC so the
    # subsequent ``observed_at - first_entry_fill_at`` arithmetic doesn't
    # TypeError on tz-naive vs tz-aware mismatch.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return ts


def _blended_price(
    conn: sqlite3.Connection, plan_id: int, kinds: tuple[str, ...]
) -> tuple[float | None, int]:
    """Volume-weighted average fill price over orders of the given kinds.
    Returns (price_or_None, total_qty)."""
    # ``placeholders`` is a fixed-length ``"?,?,?"`` string derived from the
    # arity of ``kinds`` — no user input flows into the SQL text.
    placeholders = ",".join("?" * len(kinds))
    cur = conn.execute(
        f"""SELECT f.qty, f.price
            FROM fills f JOIN orders o ON f.order_id = o.order_id
            WHERE o.plan_id = ? AND o.order_kind IN ({placeholders})""",  # nosec B608
        (plan_id, *kinds),
    )
    rows = cur.fetchall()
    total_qty = 0
    total_value = 0.0
    for r in rows:
        q = int(r["qty"])
        p = float(r["price"])
        total_qty += q
        total_value += q * p
    if total_qty == 0:
        return None, 0
    return total_value / total_qty, total_qty


def _snapshot(conn: sqlite3.Connection, plan_id: int) -> _PlanSnapshot | None:
    """Build a plan snapshot from current ledger state. Returns None if the
    plan doesn't exist (cleared by --force, deleted, …)."""
    plan = _fetch_plan_meta(conn, plan_id)
    if plan is None:
        return None
    all_orders = fetch_orders_for_plan(conn, plan_id)
    entry_orders = []
    exit_orders = []
    for o in all_orders:
        # Decorate each order row with locally observed filled qty.
        filled = _filled_qty_for_order(conn, int(o["order_id"]))
        decorated = dict(o)
        decorated["filled_qty_observed"] = filled
        if o["order_kind"] == "ENTRY":
            entry_orders.append(decorated)
        else:
            exit_orders.append(decorated)
    return _PlanSnapshot(
        plan_id=plan_id,
        ticker=plan["ticker"],
        disaster_stop=float(plan["disaster_stop"]),
        tp_tranches=tuple(_fetch_tp_tranches(conn, plan_id)),
        first_entry_fill_at=_first_fill_at_for_plan(conn, plan_id),
        entry_orders=tuple(_RowLike(d) for d in entry_orders),
        exit_orders=tuple(_RowLike(d) for d in exit_orders),
        has_outcome=_fetch_outcome_exists(conn, plan_id),
        account=plan["account"],
    )


class _RowLike:
    """Adapter so a decorated dict acts like a ``sqlite3.Row`` for our
    indexing patterns (``row["key"]`` + ``"key" in row`` + ``row.keys()``)."""

    __slots__ = ("_d",)

    def __init__(self, d: dict) -> None:
        self._d = d

    def __getitem__(self, k: str) -> Any:
        return self._d[k]

    def __contains__(self, k: object) -> bool:
        return k in self._d

    def keys(self) -> list[str]:
        return list(self._d.keys())


# ----- exit attacher -----


def _attach_exits(
    conn: sqlite3.Connection,
    *,
    snapshot: _PlanSnapshot,
    broker: BrokerClient,
    submitted_at: dt.datetime,
) -> int:
    """Submit TPs + SL for the just-settled entry phase. Returns the number
    of exit orders successfully submitted (typically 1 SL + N TP tranches)."""
    total_filled = snapshot.total_entry_filled_qty
    if total_filled <= 0:
        return 0

    submitted = 0

    # Submit one stop-sell for the full entry-filled position.
    sl_order = broker.submit_stop_order(
        symbol=snapshot.ticker,
        qty=total_filled,
        stop_price=snapshot.disaster_stop,
        side="sell",
        time_in_force="gtc",
    )
    insert_order(
        conn,
        plan_id=snapshot.plan_id,
        alpaca_order_id=str(sl_order.id),
        side="SELL",
        order_kind="SL",
        order_type="STOP",
        qty=total_filled,
        stop_price=snapshot.disaster_stop,
        time_in_force="gtc",
        submitted_at=submitted_at,
        account=snapshot.account,
    )
    submitted += 1
    logger.info(
        "exit_manager attach SL plan_id=%d ticker=%s qty=%d stop=%.2f alpaca=%s",
        snapshot.plan_id,
        snapshot.ticker,
        total_filled,
        snapshot.disaster_stop,
        sl_order.id,
    )

    # Submit one limit-sell per TP tranche. qty proportional to tranche_pct.
    remaining_qty = total_filled
    for idx, tranche in enumerate(snapshot.tp_tranches):
        # Last tranche absorbs any rounding residue so SUM(tranche_qty) == total_filled.
        if idx == len(snapshot.tp_tranches) - 1:
            qty = remaining_qty
        else:
            qty = int(total_filled * float(tranche["tranche_pct"]) / 100.0)
        if qty <= 0:
            continue
        target_price = float(tranche["target_price"])
        tp_order = broker.submit_limit_order(
            symbol=snapshot.ticker,
            qty=qty,
            limit_price=target_price,
            side="sell",
            time_in_force="gtc",
        )
        insert_order(
            conn,
            plan_id=snapshot.plan_id,
            alpaca_order_id=str(tp_order.id),
            side="SELL",
            order_kind="TP",
            tranche_index=int(tranche["tranche_index"]),
            order_type="LIMIT",
            qty=qty,
            limit_price=target_price,
            time_in_force="gtc",
            submitted_at=submitted_at,
            account=snapshot.account,
        )
        remaining_qty -= qty
        submitted += 1
        logger.info(
            "exit_manager attach TP plan_id=%d tranche=%d qty=%d limit=%.2f alpaca=%s",
            snapshot.plan_id,
            int(tranche["tranche_index"]),
            qty,
            target_price,
            tp_order.id,
        )

    return submitted


# ----- exit lifecycle (write outcome when exits settle) -----


def _classify_exit_kind(snapshot: _PlanSnapshot) -> str:
    """Pick the canonical exit_kind from how the exit orders ended.

    Uses ``filled_qty_observed > 0`` rather than ``status == 'FILLED'``
    because the Phase-A SL-not-resized simplification means Alpaca may
    PARTIAL-fill the SL up to remaining inventory (when partial TPs
    already executed) and then transition the order to CANCELED rather
    than FILLED. Looking at the observed fill count avoids that lockup.
    """
    sl_orders = [o for o in snapshot.exit_orders if o["order_kind"] == "SL"]
    tp_orders = [o for o in snapshot.exit_orders if o["order_kind"] == "TP"]
    time_stop_orders = [o for o in snapshot.exit_orders if o["order_kind"] == "TIME_STOP"]

    if any(int(o["filled_qty_observed"] or 0) > 0 for o in time_stop_orders):
        return "TIME_STOP_HIT"
    if any(int(o["filled_qty_observed"] or 0) > 0 for o in sl_orders):
        return "SL_HIT"
    if tp_orders and all(o["status"] == "FILLED" for o in tp_orders):
        return "TP_HIT"
    return "PARTIAL_TP"


def _all_exits_terminal(snapshot: _PlanSnapshot) -> bool:
    if not snapshot.exit_orders:
        return False
    terminal = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})
    return all(o["status"] in terminal for o in snapshot.exit_orders)


def _cancel_open_exits(
    *,
    snapshot: _PlanSnapshot,
    broker: BrokerClient,
) -> int:
    """Cancel every still-open exit (TP / SL / TIME_STOP) for the plan.
    Used both when an exit triggers (cancel the others) and on time-stop.

    Does NOT update the local order status to CANCELED — that's the
    reconciler's job on the next poll. Marking it CANCELED locally
    immediately would drop the order out of ``fetch_open_orders`` and
    the reconciler would never observe any final partial fills that
    landed at Alpaca between our cancel request and the broker
    processing it.

    Returns the number of orders for which the cancel REQUEST was
    successfully accepted by Alpaca (the ledger status transition
    lands on the next reconcile pass)."""
    n = 0
    for o in snapshot.exit_orders:
        if o["status"] in ("SUBMITTED", "PARTIALLY_FILLED"):
            try:
                broker.cancel_order(o["alpaca_order_id"])
            except Exception as exc:
                logger.warning(
                    "exit_manager cancel failed alpaca=%s: %s; will retry next cycle",
                    o["alpaca_order_id"],
                    exc,
                )
                continue
            n += 1
    return n


def _write_outcome(
    conn: sqlite3.Connection,
    *,
    snapshot: _PlanSnapshot,
    exit_kind: str,
    closed_at: dt.datetime,
) -> None:
    blended_entry, _ = _blended_price(conn, snapshot.plan_id, ("ENTRY",))
    blended_exit, _ = _blended_price(conn, snapshot.plan_id, ("TP", "SL", "TIME_STOP"))
    r_multiple: float | None = None
    if blended_entry is not None and blended_exit is not None:
        risk_per_share = blended_entry - snapshot.disaster_stop
        if risk_per_share > 0:
            r_multiple = (blended_exit - blended_entry) / risk_per_share

    insert_plan_outcome(
        conn,
        plan_id=snapshot.plan_id,
        exit_kind=exit_kind,
        first_fill_at=snapshot.first_entry_fill_at,
        last_exit_at=closed_at,
        blended_entry_price=blended_entry,
        blended_exit_price=blended_exit,
        realized_r_multiple=r_multiple,
        closed_at=closed_at,
    )
    logger.info(
        "exit_manager outcome plan_id=%d kind=%s entry=%s exit=%s R=%s",
        snapshot.plan_id,
        exit_kind,
        f"{blended_entry:.2f}" if blended_entry else "—",
        f"{blended_exit:.2f}" if blended_exit else "—",
        f"{r_multiple:.2f}" if r_multiple is not None else "—",
    )


def _time_stop_should_fire(snapshot: _PlanSnapshot, now: dt.datetime) -> bool:
    """True when the position is old enough for time-stop AND we haven't
    already submitted a TIME_STOP order. The existence check prevents an
    infinite re-fire loop when the time-stop market order doesn't fill
    immediately (e.g. submitted while market is closed) and the next
    reconcile pass would otherwise cancel-and-resubmit it endlessly.

    The age metric is the number of XNYS trading days elapsed between
    the entry fill and ``now`` (half-open, end-inclusive). The PR-B
    switch from calendar-day arithmetic restores parity with the
    trade-setup memo's "N trading days hold" intent — a position opened
    Fri 16:00 ET is 1 trading day old at Mon close, 2 at Tue close, etc.
    Weekends and US public holidays do not tick the clock.
    """
    from alphalens_pipeline.paper.calendar import trading_days_elapsed

    if snapshot.first_entry_fill_at is None:
        return False
    if any(o["order_kind"] == "TIME_STOP" for o in snapshot.exit_orders):
        return False
    age = trading_days_elapsed(snapshot.first_entry_fill_at, now)
    return age >= TIME_STOP_DAYS


def _submit_time_stop(
    conn: sqlite3.Connection,
    *,
    snapshot: _PlanSnapshot,
    broker: BrokerClient,
    observed_at: dt.datetime,
) -> int:
    """Cancel open exits + submit a market sell for the remaining open
    position. Returns 1 if a market sell was submitted, else 0.

    Computes the remaining-to-sell quantity by querying Alpaca for the
    LIVE position rather than reading entry_filled - exit_filled from
    the ledger. The same-ticker policy guarantees one active plan per
    ticker so the broker-side position is the authoritative count.
    Reading locally risks over-selling when an exit fill landed at
    Alpaca but the reconciler hasn't observed it yet (we'd compute a
    too-large remaining and submit a market-sell larger than our
    inventory, flipping the paper account short).
    """
    _cancel_open_exits(snapshot=snapshot, broker=broker)

    try:
        position = broker.get_position(snapshot.ticker)
    except Exception as exc:
        logger.warning(
            "exit_manager time-stop failed to fetch position for %s: %s; will retry",
            snapshot.ticker,
            exc,
        )
        return 0
    remaining = int(float(getattr(position, "qty", 0) or 0)) if position is not None else 0
    if remaining <= 0:
        return 0

    mkt_order = broker.submit_market_order(
        symbol=snapshot.ticker,
        qty=remaining,
        side="sell",
        time_in_force="day",
    )
    insert_order(
        conn,
        plan_id=snapshot.plan_id,
        alpaca_order_id=str(mkt_order.id),
        side="SELL",
        order_kind="TIME_STOP",
        order_type="MARKET",
        qty=remaining,
        time_in_force="day",
        submitted_at=observed_at,
        account=snapshot.account,
    )
    logger.info(
        "exit_manager time-stop plan_id=%d ticker=%s qty=%d alpaca=%s",
        snapshot.plan_id,
        snapshot.ticker,
        remaining,
        mkt_order.id,
    )
    return 1


# ----- public driver -----


def process_plan_exit(
    conn: sqlite3.Connection,
    *,
    plan_id: int,
    broker: BrokerClient,
    observed_at: dt.datetime | None = None,
) -> ExitOutcome:
    """Drive one plan through its exit-phase state machine.

    Called by the reconciler after the per-order pass. Idempotent: a plan
    in a stable state (entry not settled yet, or outcome already written)
    produces ExitOutcome(action='NOOP').
    """
    observed_at = observed_at or dt.datetime.now(dt.UTC)
    snap = _snapshot(conn, plan_id)
    if snap is None:
        return ExitOutcome(plan_id=plan_id, action="NOOP")

    if snap.has_outcome:
        return ExitOutcome(plan_id=plan_id, action="NOOP")

    # Entry phase still in progress — wait.
    if not snap.entry_phase_settled:
        return ExitOutcome(plan_id=plan_id, action="NOOP")

    # Entry settled with zero fills → UNFILLED outcome, no exits.
    if snap.total_entry_filled_qty == 0:
        insert_plan_outcome(
            conn,
            plan_id=plan_id,
            exit_kind="UNFILLED",
            first_fill_at=None,
            last_exit_at=None,
            blended_entry_price=None,
            blended_exit_price=None,
            realized_r_multiple=None,
            closed_at=observed_at,
        )
        return ExitOutcome(plan_id=plan_id, action="UNFILLED", exit_kind="UNFILLED")

    # Entry settled with fills — exits not yet attached: attach now.
    if not snap.exit_orders:
        n = _attach_exits(
            conn,
            snapshot=snap,
            broker=broker,
            submitted_at=observed_at,
        )
        return ExitOutcome(plan_id=plan_id, action="ATTACHED", n_exits_submitted=n)

    # Exit phase active. Check for time-stop first.
    if _time_stop_should_fire(snap, observed_at):
        _submit_time_stop(conn, snapshot=snap, broker=broker, observed_at=observed_at)
        # Re-snapshot so the outcome write sees the just-canceled exits + new TIME_STOP order.
        snap = _snapshot(conn, plan_id) or snap
        # Don't write outcome yet — wait for the market-sell to fill in a
        # subsequent reconcile pass. ExitOutcome reports the time-stop.
        return ExitOutcome(plan_id=plan_id, action="TIME_STOP")

    # If the stop-loss fired (FULL FILL or PARTIAL FILL before Alpaca
    # short-circuit cancels the unfillable remainder), cancel remaining
    # open TPs. Check ``filled_qty_observed > 0`` not ``status == FILLED``
    # so the partial-then-canceled case caused by the Phase-A SL-not-
    # resized simplification is caught.
    sl_fired = any(
        o["order_kind"] == "SL" and int(o["filled_qty_observed"] or 0) > 0 for o in snap.exit_orders
    )
    if sl_fired:
        _cancel_open_exits(snapshot=snap, broker=broker)
        snap = _snapshot(conn, plan_id) or snap

    if not _all_exits_terminal(snap):
        return ExitOutcome(plan_id=plan_id, action="NOOP")

    # All exits terminal — write outcome + cancel anything still open (should be none).
    kind = _classify_exit_kind(snap)
    _write_outcome(conn, snapshot=snap, exit_kind=kind, closed_at=observed_at)
    return ExitOutcome(plan_id=plan_id, action="CLOSED", exit_kind=kind)


__all__ = [
    "ExitOutcome",
    "process_plan_exit",
]
