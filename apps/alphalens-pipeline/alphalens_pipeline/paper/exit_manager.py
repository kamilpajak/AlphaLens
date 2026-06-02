"""Attach TP / SL exits + write plan_outcome.

The protective stop-loss (SL) is a SAFETY-CRITICAL leg: any filled position
MUST converge to a LIVE disaster-stop, and it must do so independently of
whether the entry ladder ever settles. This module enforces that as a
self-healing convergence rule rather than a one-shot settlement-gated attach.

State machine per plan:

  CONVERGENCE rule (runs EVERY pass, before the settlement gate):
    -> the position (entry fills minus exit fills) is open AND its live
       disaster-stop coverage is LESS than the current position qty (no SL,
       a canceled-zero-fill SL, or a too-small SL after a gap fill):
         (a) best-effort cancel any still-open ENTRY tiers — frees the
             reserved-share / opposite-side hold that makes Alpaca reject
             the SL with held_for_orders, and collapses the ladder to the
             filled qty (scale-in tiers are intentionally abandoned),
         (b) cancel any UNDER-sized live SL so the re-submit does not stack,
             then
         (c) attempt the disaster-stop SL sized to the CURRENT position qty.
       SL-PRIORITY invariant: the protective disaster-stop must ALWAYS win the
       shares; the opportunistic take-profit is strictly secondary. The TP
       ladder is attached ONLY once the SL is live this pass — never while the
       SL is missing. If the SL submit is rejected because the plan's OWN live
       TP order(s) reserve the shares (the live DLB / S state: a resting TP
       holds the position while no SL covers it), those TPs are CANCELLED
       best-effort and the SL is re-attempted the same pass (Alpaca releases
       the hold immediately on cancel). If the SL submit is rejected for any
       other reason (held_for_orders / wash-trade 403 / any APIError) NOTHING
       SL-shaped is persisted and NO TP is submitted, so the SAME branch
       re-enters next pass and retries. This is NOT gated on
       entry_phase_settled and is NOT skipped just because a TP order
       already exists — a TP-without-SL state is transient, never terminal.
       A partial TP that shrinks the position below an over-covering SL does
       NOT re-converge (the Phase-A SL-not-resized-down simplification stands).

  ENTRY phase (no fills yet)
    -> ALL entry orders terminal AND total filled qty == 0:
         -> WRITE plan_outcome(UNFILLED), no exits
    -> otherwise wait (NOOP).

  EXIT phase (TPs + SL live)
    -> TPs / SL flow through reconciler.reconcile_orders() like any other
       order (statuses + fills synthesized from Alpaca polls).
    -> When all SELL orders reach terminal state:
         -> WRITE plan_outcome(TP_HIT / SL_HIT / PARTIAL_TP / TIME_STOP_HIT)
            with blended_entry/exit + realized R-multiple.
    -> Time-stop: if first fill > TIME_STOP_DAYS ago and exit phase still
       open, cancel pending exits + submit market-sell for the LIVE broker
       position (never the planned qty — a planned-qty time-stop over-sells
       after a partial TP).

Sizing: every protective order (SL, each TP tranche, TIME_STOP) is sized to
the CURRENT observed filled / live broker position qty, never the planned
qty. The TIME_STOP reads the live Alpaca position directly.

Phase A simplification: a partial TP fill does NOT shrink the SL qty.
The SL stays sized for the full filled quantity, so if SL fires after
partial TP execution the stop sells slightly less than intended (only
remaining position size). This is a small directional bias — flagged as
Known Issue in the design memo follow-up — and acceptable in exchange
for not needing a cancel-and-resize loop on every TP fill.

Behaviour notes / Known issues (PR-body markers):
  (a) Cancel-FIRST on a gap-fill SL resize (transient UNDER-coverage window).
      When a cheaper tier fills in the race and the position grows past the
      live under-sized SL, the convergence rule CANCELS the stale under-sized
      SL FIRST, then submits the correctly-sized one. For a brief window
      (until the resized SL is acked) the position may be under-covered; the
      next reconcile pass re-runs convergence and re-submits if the resize did
      not land, so the gap self-heals. We deliberately do NOT protect-first
      (submit the new SL before cancelling the old one): two live stop-sells
      on the same shares would BOTH trigger together and OVER-SELL the position
      to a short — the exact bug class this module guards against. A brief
      under-covered window (self-healed next pass) is the correct trade-off
      against ever over-covering / oversell-to-short.
  (b) The TP ladder is NOT re-sized up after a gap fill. The SL always covers
      the full position, but the TP tranches stay sized to the FIRST observed
      fill (the Phase-A simplification). Some upside shares may sit without a
      take-profit limit until manual / later handling — not a safety risk
      (the SL covers them), only a missed-TP-coverage nicety.
  (c) The account label is a closed enum {main, test}. Anything that turns it
      into a Prometheus / gauge label needs only simple sanitization of those
      two known values — there is no free-form account string to defend
      against.
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
    platform: str  # v5: trading platform (e.g. 'alpaca') — threaded onto exit orders

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

    @property
    def total_exit_filled_qty(self) -> int:
        """Observed filled qty across ALL exit orders (TP / SL / TIME_STOP)."""
        return sum(
            int(o["filled_qty_observed"] or 0) if "filled_qty_observed" in o else 0
            for o in self.exit_orders
        )

    @property
    def net_open_qty(self) -> int:
        """Ledger-side estimate of shares still held: entry fills minus exit
        fills. Used as the convergence GATE — a position whose exits have
        fully sold it (net 0) is closed and must NOT re-arm an SL. The
        protective-order SIZE still comes from the live broker position
        (``_position_qty``), which is authoritative for a not-yet-reconciled
        fill; this is only the gate.
        """
        return self.total_entry_filled_qty - self.total_exit_filled_qty

    @property
    def sl_coverage_qty(self) -> int:
        """Shares currently covered by a disaster-stop: for every LIVE
        (non-terminal) SL order, the REMAINING open qty (``qty`` minus shares
        already filled, clamped at >= 0), plus the observed fills of any SL
        that already FIRED. Counting the original ``qty`` of a partially-filled
        live SL would overstate remaining coverage and delay re-convergence;
        the remaining-open qty is the protection that actually still stands.
        A terminal SL CANCELED with zero fill contributes nothing, so a
        position with only such an SL is unprotected and must re-converge.
        """
        total = 0
        for o in self.exit_orders:
            if o["order_kind"] != "SL":
                continue
            filled = int(o["filled_qty_observed"] or 0) if "filled_qty_observed" in o else 0
            if o["status"] not in _TERMINAL_ENTRY_STATUSES:
                total += max(int(o["qty"] or 0) - filled, 0)
            elif filled > 0:
                total += filled
        return total

    @property
    def live_sl_orders(self) -> tuple[_RowProto, ...]:
        """The non-terminal SL orders (used to cancel an under-sized SL before
        re-submitting a correctly-sized one during a gap-fill re-size)."""
        return tuple(
            o
            for o in self.exit_orders
            if o["order_kind"] == "SL" and o["status"] not in _TERMINAL_ENTRY_STATUSES
        )

    @property
    def has_tp_orders(self) -> bool:
        return any(o["order_kind"] == "TP" for o in self.exit_orders)

    @property
    def live_tp_orders(self) -> tuple[_RowProto, ...]:
        """The non-terminal TP orders. The SL-priority rule cancels these when
        an opportunistic TP is resting on a position that has no live disaster-
        stop, so the protective SL can take the shares (the TP yields)."""
        return tuple(
            o
            for o in self.exit_orders
            if o["order_kind"] == "TP" and o["status"] not in _TERMINAL_ENTRY_STATUSES
        )

    @property
    def has_live_time_stop(self) -> bool:
        """True when a TIME_STOP market-sell is in flight (submitted but not
        yet terminal). A live time-stop IS the liquidation for the whole
        position, so the SL-convergence rule must stand down: re-arming an SL
        (or re-attaching the TP ladder) against a position that is being
        market-liquidated stacks a second live sell on the same shares and
        over-sells to a short when both fill. See the guard in
        ``process_plan_exit`` (TIME_STOP-vs-SL-convergence collision).
        """
        return any(
            o["order_kind"] == "TIME_STOP" and o["status"] not in _TERMINAL_ENTRY_STATUSES
            for o in self.exit_orders
        )


@dataclass(frozen=True)
class ExitOutcome:
    """One plan's exit-phase outcome from a reconcile pass."""

    plan_id: int
    # 'ATTACHED' | 'CLOSED' | 'TIME_STOP' | 'UNFILLED' | 'CONVERGE_SL'
    #   | 'DESYNC_FLAT' | 'NOOP'
    action: str
    n_exits_submitted: int = 0
    exit_kind: str | None = None
    # Count of exit submits that the broker rejected this pass (e.g. Alpaca
    # held_for_orders / insufficient qty). Caught + counted, never raised.
    n_exits_failed: int = 0
    # Count of still-open entry tiers cancelled by the convergence rule.
    n_entries_canceled: int = 0
    # 1 when this plan ends the pass with filled shares but NO live
    # protective SL (a transient unprotected position the next pass retries);
    # 0 otherwise. The dead-man signal the reconciler accumulates + emits.
    n_filled_without_sl: int = 0
    # 1 when the broker CONFIRMED the position flat/absent (definitive
    # no-position, NOT a transient read error) while the ledger believed the
    # plan was filled (net_open_qty > 0, no outcome). A ledger<->broker DESYNC:
    # the harness stops chasing the phantom (submits nothing), writes a
    # terminal RECONCILED_FLAT outcome, and signals here. 0 otherwise.
    n_ledger_broker_desync: int = 0


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
        platform=plan["platform"],
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


# Broker-position read states. The desync guard hinges on distinguishing a
# DEFINITIVE broker-confirmed flat position (``FLAT``) from a TRANSIENT read
# error (``ERROR``): a flat read while the ledger thinks the plan is filled is
# a ledger<->broker desync, whereas a transient error must NOT be treated as a
# desync (that would mask a real position and drop its disaster-stop).
_POS_REAL = "REAL"  # broker reported a live position with qty > 0
_POS_FLAT = "FLAT"  # broker DEFINITIVELY confirmed no position (None, per contract)
_POS_ERROR = "ERROR"  # transient read failure — get_position raised


@dataclass(frozen=True)
class _PositionRead:
    """Outcome of a single ``broker.get_position`` call for sizing + desync.

    ``state`` is one of ``_POS_REAL`` / ``_POS_FLAT`` / ``_POS_ERROR``.
    ``qty`` is the sizing quantity every protective order is sized to:
      * REAL  -> the live broker position qty (authoritative inventory),
      * FLAT  -> 0 (there is nothing to size against — confirmed flat),
      * ERROR -> the net-open LEDGER qty fallback (entry fills minus exit
                 fills), so a not-yet-reconciled real position is still
                 protected without over-counting shares already sold.
    """

    state: str
    qty: int


def _read_position(snapshot: _PlanSnapshot, broker: BrokerClient) -> _PositionRead:
    """Read the live broker position, classifying flat-vs-error per contract.

    The same-ticker policy guarantees one active plan per ticker, so the
    broker-side position is the authoritative count. We PREFER it over the
    ledger total because an exit fill (a partial TP) may have landed at Alpaca
    that the reconciler has not observed yet — sizing the SL to a stale ledger
    total would over-state inventory and risk an insufficient-qty rejection.

    Per the :class:`BrokerClient.get_position` contract, ``None`` is a POSITIVE
    assertion of a flat / absent position (``_POS_FLAT``); only a transient /
    non-definitive failure raises (``_POS_ERROR``). On ``_POS_ERROR`` the
    sizing falls back to ``net_open_qty`` (the true live ledger-side position)
    rather than the GROSS ``total_entry_filled_qty`` — sizing the fallback to
    gross entry fills ignores shares already SOLD by exits, oversizing the
    protective sell -> persistent insufficient_qty rejection AND, if the
    over-sized SL ever lands, oversell-to-short. ``net_open_qty`` never exceeds
    the shares actually held.
    """
    ledger_qty = max(snapshot.net_open_qty, 0)
    try:
        position = broker.get_position(snapshot.ticker)
    except Exception as exc:
        logger.warning(
            "exit_manager position read failed for %s: %s; sizing to net-open ledger qty %d",
            snapshot.ticker,
            exc,
            ledger_qty,
        )
        return _PositionRead(state=_POS_ERROR, qty=ledger_qty)
    if position is None:
        # The broker DEFINITIVELY confirms there is no open position. This is
        # NOT a sizing fallback — it is authoritative truth used by the desync
        # guard. Sizing qty is 0 (nothing to protect).
        return _PositionRead(state=_POS_FLAT, qty=0)
    pos_qty = max(int(float(getattr(position, "qty", 0) or 0)), 0)
    return _PositionRead(state=_POS_REAL, qty=pos_qty)


def _position_qty(snapshot: _PlanSnapshot, broker: BrokerClient) -> int:
    """The qty every protective order is sized to. Thin wrapper over
    :func:`_read_position` for call sites (re-snapshot dead-man recompute,
    time-stop) that only need the sizing number, not the flat-vs-error state.
    """
    return _read_position(snapshot, broker).qty


def _attach_sl(
    conn: sqlite3.Connection,
    *,
    snapshot: _PlanSnapshot,
    broker: BrokerClient,
    qty: int,
    submitted_at: dt.datetime,
) -> tuple[int, int]:
    """Submit the single disaster-stop SL sized to ``qty``. Returns
    ``(n_submitted, n_failed)`` (each 0 or 1).

    Crash-resilience: the broker submit is wrapped in try/except so a broker
    error (Alpaca APIError held_for_orders / insufficient qty / wash-trade /
    any submit failure) is caught, logged, counted — it does NOT propagate.
    When the submit fails NOTHING is persisted, so the convergence rule in
    ``process_plan_exit`` re-enters next pass and retries until the SL lands.
    """
    if qty <= 0:
        return 0, 0
    try:
        sl_order = broker.submit_stop_order(
            symbol=snapshot.ticker,
            qty=qty,
            stop_price=snapshot.disaster_stop,
            side="sell",
            time_in_force="gtc",
        )
    except Exception as exc:
        logger.warning(
            "exit_manager attach SL FAILED plan_id=%d ticker=%s qty=%d stop=%.2f: %s; "
            "will retry next cycle",
            snapshot.plan_id,
            snapshot.ticker,
            qty,
            snapshot.disaster_stop,
            exc,
        )
        return 0, 1
    insert_order(
        conn,
        plan_id=snapshot.plan_id,
        alpaca_order_id=str(sl_order.id),
        side="SELL",
        order_kind="SL",
        order_type="STOP",
        qty=qty,
        stop_price=snapshot.disaster_stop,
        time_in_force="gtc",
        submitted_at=submitted_at,
        account=snapshot.account,
        platform=snapshot.platform,
    )
    logger.info(
        "exit_manager attach SL plan_id=%d ticker=%s qty=%d stop=%.2f alpaca=%s",
        snapshot.plan_id,
        snapshot.ticker,
        qty,
        snapshot.disaster_stop,
        sl_order.id,
    )
    return 1, 0


def _attach_tps(
    conn: sqlite3.Connection,
    *,
    snapshot: _PlanSnapshot,
    broker: BrokerClient,
    total_filled: int,
    submitted_at: dt.datetime,
) -> tuple[int, int]:
    """Submit one limit-sell per TP tranche, sized to ``total_filled``.
    Returns ``(n_submitted, n_failed)``. Each submit is crash-resilient
    (same contract as :func:`_attach_sl`)."""
    if total_filled <= 0:
        return 0, 0
    submitted = 0
    failed = 0
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
        try:
            tp_order = broker.submit_limit_order(
                symbol=snapshot.ticker,
                qty=qty,
                limit_price=target_price,
                side="sell",
                time_in_force="gtc",
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                "exit_manager attach TP FAILED plan_id=%d tranche=%d qty=%d limit=%.2f: %s; "
                "will retry next cycle",
                snapshot.plan_id,
                int(tranche["tranche_index"]),
                qty,
                target_price,
                exc,
            )
            # Do NOT decrement remaining_qty — the unsold tranche rolls into
            # the residue the last successful tranche absorbs (or is retried).
            continue
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
            platform=snapshot.platform,
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

    return submitted, failed


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


def _cancel_unfilled_entries(
    *,
    snapshot: _PlanSnapshot,
    broker: BrokerClient,
) -> int:
    """Cancel every still-open ENTRY tier on a plan that already has a fill.

    The entry ladder is a 2-3 tier GTC limit BUY. In practice only the
    aggressive tier fills; cheaper tiers sit SUBMITTED and the ladder never
    reaches the all-terminal "settled" state until the TTL sweep cancels
    them ~7 trading days later — so the filled shares carry NO protective
    SELL for up to a week. Worse, the still-open BUY tiers reserve the
    shares (opposite-side hold), so Alpaca rejects the protective SL with
    ``held_for_orders insufficient qty``.

    Cancelling the open tiers on the first fill fixes both: it frees the
    reserved shares (the SL is accepted) and makes the ladder settle (all
    tiers terminal) so exits attach promptly. The aggressive scale-in tiers
    are intentionally abandoned — we keep only the filled qty.

    Best-effort: mirrors ``_cancel_open_exits`` — a failed cancel is
    logged and skipped, never raised. Does NOT update the local order
    status to CANCELED; that lands on the reconciler's next poll (same
    doctrine as ``_cancel_open_exits``: marking CANCELED locally would drop
    the order out of ``fetch_open_orders`` before any final partial fill is
    observed). Returns the count of cancel REQUESTS Alpaca accepted.
    """
    n = 0
    for o in snapshot.entry_orders:
        if o["status"] not in _TERMINAL_ENTRY_STATUSES:
            try:
                broker.cancel_order(o["alpaca_order_id"])
            except Exception as exc:
                logger.warning(
                    "exit_manager cancel unfilled entry failed alpaca=%s plan_id=%d: %s; "
                    "will retry next cycle",
                    o["alpaca_order_id"],
                    snapshot.plan_id,
                    exc,
                )
                continue
            n += 1
            logger.info(
                "exit_manager cancel unfilled entry alpaca=%s plan_id=%d ticker=%s",
                o["alpaca_order_id"],
                snapshot.plan_id,
                snapshot.ticker,
            )
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
    """True when the position is old enough for time-stop AND no PRIOR TIME_STOP
    has either liquidated the position or is still in flight.

    A prior TIME_STOP suppresses a re-fire when it is EITHER:
      * non-terminal (in flight — the market sell hasn't filled yet, e.g.
        submitted while the market is closed); re-firing would cancel and
        re-submit it every pass, OR
      * terminal WITH a fill (filled_qty_observed > 0 — it did its job and
        liquidated the position); re-firing would submit a second market
        sell on top of an already-sold position → oversell-to-short.

    Only a terminal ZERO-fill TIME_STOP (REJECTED / CANCELED, never executed)
    allows a re-fire — that is the rejected-time-stop retry case: the
    liquidation never happened, so the next pass RE-SUBMITS a fresh market
    sell rather than stranding the position forever.

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
    for o in snapshot.exit_orders:
        if o["order_kind"] != "TIME_STOP":
            continue
        non_terminal = o["status"] not in _TERMINAL_ENTRY_STATUSES
        filled = int(o["filled_qty_observed"] or 0) if "filled_qty_observed" in o else 0
        if non_terminal or filled > 0:
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
        platform=snapshot.platform,
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

    # SL-CONVERGENCE rule (the disaster-stop guarantee). A filled position
    # that is not FULLY covered by a disaster-stop must converge to one,
    # independently of ladder settlement and regardless of whether a TP
    # already landed:
    #   (a) best-effort cancel any still-open ENTRY tiers — frees the
    #       reserved-share / opposite-side hold (the held_for_orders cause)
    #       and collapses the ladder to the filled qty (scale-in abandoned),
    #   (b) cancel any UNDER-sized live SL (a gap fill on the cheaper tier
    #       landed after a smaller SL — re-size up to the full position), then
    #   (c) attempt the SL sized to the CURRENT position qty FIRST. SL-PRIORITY:
    #       the TP ladder is attached ONLY once the SL is live this pass; if the
    #       SL submit fails NO TP is submitted (a wash-trade-exempt TP limit
    #       would reserve the shares and block the SL forever — the live defect).
    #       If the SL is blocked by the plan's OWN live TP reserving the shares,
    #       those TPs are cancelled best-effort and the SL is re-attempted the
    #       same pass (the TP yields the shares to the protective SL).
    # If the SL submit is rejected nothing SL-shaped is persisted, so this
    # branch re-enters next pass and retries until the SL lands. Guards:
    #   - ``net_open_qty > 0``: a zero-fill plan is NOT a position, and a plan
    #     whose exits already sold the whole position (net 0, e.g. all TPs
    #     filled) is closed — neither re-arms an SL.
    #   - ``sl_coverage_qty < position``: a position already fully covered by a
    #     live / already-fired SL is protected — idempotent skip. Only UNDER-
    #     coverage (no SL, a canceled-zero-fill SL, or a too-small SL after a
    #     gap fill) re-converges; a partial TP that shrinks the position below
    #     an over-covering SL does NOT (the Phase-A SL-not-resized-down
    #     simplification stands — the SL simply sells remaining inventory).
    # SAFETY: short-circuit SL-convergence whenever the plan is in TIME-STOP
    # territory, i.e. (1) a TIME_STOP market-sell is already in flight, OR
    # (2) the time-stop deadline has passed and this pass is about to fire one.
    #
    # The convergence coverage check (``sl_coverage_qty``) counts only
    # order_kind=='SL', so a pending TIME_STOP contributes ZERO coverage. Once
    # the time-stop has cancelled the live SL (which then polls to a terminal
    # CANCELED with 0 fill) sl_coverage_qty drops to 0 while net_open_qty is
    # still > 0 (the market-sell has not filled — e.g. submitted at/after the
    # close). Without the ``has_live_time_stop`` guard the convergence gate
    # would RE-ARM a fresh full-size SL on top of the live TIME_STOP, so both
    # execute when the market opens and OVER-SELL the position to a short.
    #
    # The ``should_fire_time_stop`` arm of the guard is the COUPLED half of the
    # rejected-time-stop retry change in ``_time_stop_should_fire``: now that a
    # REJECTED / CANCELED (terminal) TIME_STOP no longer suppresses a re-fire,
    # a past-deadline pass with no live TIME_STOP would otherwise (a) arm a
    # fresh SL via convergence and then (b) immediately re-fire the time-stop,
    # cancelling that SL and re-submitting the market sell — churning every
    # pass. Skipping convergence whenever the time-stop is about to fire makes
    # the post-deadline behaviour a clean LIQUIDATION (time-stop retried until
    # it lands), never a convergence-vs-time-stop oscillation. Before the
    # deadline ``should_fire_time_stop`` is False, so convergence keeps the SL
    # fresh exactly as before. The value is computed ONCE here and reused by
    # the time-stop branch below so the skip and the fire stay consistent.
    # The time-stop IS the liquidation; nothing protective should be re-armed
    # against a position that is being market-liquidated.
    pos = _read_position(snap, broker)
    size_qty = pos.qty
    if snap.has_live_time_stop:
        return ExitOutcome(plan_id=plan_id, action="NOOP")
    should_fire_time_stop = _time_stop_should_fire(snap, observed_at)
    # LEDGER<->BROKER DESYNC guard (broker is the SOURCE OF TRUTH for the
    # position). When the broker DEFINITIVELY confirms the position is
    # flat/absent (``_POS_FLAT`` — a clean read returning None, NOT a transient
    # error) WHILE the ledger still believes the plan is filled
    # (net_open_qty > 0, no outcome yet), there is NO position to protect.
    # Falling back to the ledger qty here would size a protective SL for shares
    # that do not exist -> perpetual insufficient-qty rejection + a false
    # filled_without_sl. Instead stop chasing the phantom: submit / cancel
    # NOTHING, write a TERMINAL RECONCILED_FLAT outcome so the plan is not
    # reprocessed every pass, and signal the desync so it self-surfaces.
    #
    # Gated by ``not should_fire_time_stop`` for the same reason as the
    # convergence branch below: once the plan is in time-stop territory the
    # time-stop branch owns the (already-flat) liquidation. A TRANSIENT read
    # error (``_POS_ERROR``) is deliberately NOT a desync — it falls through to
    # the convergence branch and retries next pass, so a real position is never
    # masked behind a blip.
    #
    # CRITICAL data-loss gate (``not snap.exit_orders``): the desync verdict
    # only holds when NO exit order was ever submitted. If an exit (SL / TP /
    # time-stop) IS present, a flat broker most plausibly means THAT EXIT
    # FIRED — its fill may simply not be polled into the ledger yet (a
    # get_order blip on this pass). Writing a terminal RECONCILED_FLAT here
    # would discard the real SL_HIT / TP_HIT outcome with null R, PERMANENTLY.
    # Checking ``total_exit_filled_qty == 0`` is NOT enough — the dangerous
    # case is exactly the one where the exit fill was not recorded (so that
    # count is still 0). When an exit order exists we defer to the normal exit
    # path below (NOOP-retry until the fill is observable, then classify).
    if (
        not should_fire_time_stop
        and pos.state == _POS_FLAT
        and snap.net_open_qty > 0
        and not snap.exit_orders
    ):
        logger.warning(
            "exit_manager LEDGER<->BROKER DESYNC plan_id=%d ticker=%s: broker confirms "
            "FLAT but ledger net_open_qty=%d (plan believed filled). Submitting no "
            "protective order; writing terminal RECONCILED_FLAT.",
            snap.plan_id,
            snap.ticker,
            snap.net_open_qty,
        )
        insert_plan_outcome(
            conn,
            plan_id=plan_id,
            exit_kind="RECONCILED_FLAT",
            first_fill_at=snap.first_entry_fill_at,
            last_exit_at=None,
            blended_entry_price=None,
            blended_exit_price=None,
            realized_r_multiple=None,
            closed_at=observed_at,
        )
        return ExitOutcome(
            plan_id=plan_id,
            action="DESYNC_FLAT",
            exit_kind="RECONCILED_FLAT",
            n_ledger_broker_desync=1,
        )
    if not should_fire_time_stop and snap.net_open_qty > 0 and snap.sl_coverage_qty < size_qty:
        n_canceled = _cancel_unfilled_entries(snapshot=snap, broker=broker)
        # CANCEL-FIRST: cancel the under-sized live SL BEFORE submitting the
        # resized one below, so we never run two live stop-sells on the same
        # shares (both would trigger together → oversell-to-short). This
        # accepts a brief transient window that may be UNDER-covered (the
        # cancel ack and the resized-SL ack race); the next reconcile pass
        # re-runs convergence and re-submits if the resize did not land, so the
        # gap self-heals. We deliberately do NOT protect-first. Best-effort
        # cancel; the ledger status only flips on the reconciler's next poll.
        for sl in snap.live_sl_orders:
            try:
                broker.cancel_order(sl["alpaca_order_id"])
            except Exception as exc:
                logger.warning(
                    "exit_manager cancel under-sized SL failed alpaca=%s plan_id=%d: %s; "
                    "will retry next cycle",
                    sl["alpaca_order_id"],
                    snap.plan_id,
                    exc,
                )
        sl_submitted, sl_failed = _attach_sl(
            conn,
            snapshot=snap,
            broker=broker,
            qty=size_qty,
            submitted_at=observed_at,
        )
        # SL-PRIORITY invariant: the protective disaster-stop must always win
        # the shares; the opportunistic take-profit is strictly secondary.
        # If the SL submit was REJECTED this pass and the plan's OWN live TP
        # order(s) are reserving the shares (the live DLB / S state: a resting
        # TP limit holds the position while no SL covers it), cancel those TPs
        # best-effort so the SL can land — then re-attempt the SL ONCE this
        # pass (the probe shows Alpaca releases the hold ~immediately on cancel,
        # so the re-submit lands same-pass). The safety SL takes the shares; the
        # yielded TP is DROPPED and is NOT re-armed while the SL covers the
        # position (a Phase-A limitation: the convergence block only runs while
        # under-covered, so once the SL is live it does not re-enter to re-attach
        # the TP). The position keeps its disaster-stop; the take-profit is the
        # opportunistic secondary leg, so dropping it is a non-safety trade-off.
        if sl_submitted == 0 and snap.live_tp_orders:
            for tp in snap.live_tp_orders:
                try:
                    broker.cancel_order(tp["alpaca_order_id"])
                except Exception as exc:
                    logger.warning(
                        "exit_manager cancel TP to free shares for SL failed alpaca=%s "
                        "plan_id=%d: %s; will retry next cycle",
                        tp["alpaca_order_id"],
                        snap.plan_id,
                        exc,
                    )
            sl_submitted, sl_failed = _attach_sl(
                conn,
                snapshot=snap,
                broker=broker,
                qty=size_qty,
                submitted_at=observed_at,
            )
        # Attach the TP ladder ONLY when the SL is now live and no TP exists
        # yet. "SL live" means it was just submitted successfully this pass
        # (``sl_submitted == 1``) — the gate guarantees no adequate pre-existing
        # SL reached here. If the SL submit FAILED, submit NO TP: a TP limit is
        # wash-trade-EXEMPT and would be accepted, reserving the shares and
        # blocking the disaster-stop forever (the live defect). The SL retries
        # next pass; the TP waits until it is live.
        tp_submitted = 0
        tp_failed = 0
        if sl_submitted == 1 and not snap.live_tp_orders:
            tp_submitted, tp_failed = _attach_tps(
                conn,
                snapshot=snap,
                broker=broker,
                total_filled=size_qty,
                submitted_at=observed_at,
            )
        # Re-snapshot so the dead-man flag reflects whether the SL actually
        # landed this pass (sl_submitted may be 0 on a rejection). Coverage is
        # re-read against the live position; a still-uncovered position is the
        # transient unprotected state the next pass retries.
        snap = _snapshot(conn, plan_id) or snap
        still_unprotected = (
            1
            if (snap.net_open_qty > 0 and snap.sl_coverage_qty < _position_qty(snap, broker))
            else 0
        )
        return ExitOutcome(
            plan_id=plan_id,
            action="CONVERGE_SL",
            n_exits_submitted=sl_submitted + tp_submitted,
            n_exits_failed=sl_failed + tp_failed,
            n_entries_canceled=n_canceled,
            n_filled_without_sl=still_unprotected,
        )

    # Entry phase still in progress and no exits yet — wait. Once the plan
    # has exit_orders (it converged to a live SL above) the exit-phase
    # machinery below runs even if the entry ladder never formally settles —
    # a never-acking entry cancel must not strand the outcome write forever.
    if not snap.entry_phase_settled and not snap.exit_orders:
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

    # Exit phase active. Check for time-stop first. Reuse the value computed
    # before the convergence gate so the convergence-skip and the actual fire
    # are driven by the SAME decision (no re-eval drift between the two).
    if should_fire_time_stop:
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
