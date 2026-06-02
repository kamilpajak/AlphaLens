"""Attach an OCO-ladder exit + write plan_outcome.

The protective disaster-stop is a SAFETY-CRITICAL leg: any filled position
MUST carry a LIVE stop. Entry policy is CANCEL-ON-FIRST-FILL (no scale-in), so
once the entries settle the held aggregate only DECREASES — this module
attaches the protective exit ONCE on the filled aggregate (the OCO-ladder),
never a self-healing per-pass convergence loop.

State machine per plan:

  ATTACH-ONCE rule (runs before the settlement gate, while no ladder exists):
    -> the position (entry fills minus exit fills) is open AND the plan has no
       attached exit ladder yet (no exit row carries an exit_group_id):
         (a) best-effort cancel any still-open ENTRY tiers — frees the
             reserved-share / opposite-side hold and collapses the ladder to
             the filled qty (scale-in tiers are intentionally abandoned),
         (b) size the ladder to the LIVE broker position (authoritative),
         (c) build M ExitTranche intents (TP ladder split across the held qty,
             last tranche absorbs the residue) and hand them + the single
             disaster_stop to ``broker.attach_exit_ladder``. The broker (Alpaca
             adapter) decomposes that into M OCO groups, each group's stop_loss
             leg at the SAME disaster_stop — so the held position is fully
             protected regardless of which take-profit tranche fills first.
       The attach is ALL-OR-NOTHING at both ends: the adapter cancels every
       OCO group already placed if any tranche fails (nothing live at the
       broker), and on failure this module persists NOTHING (no ledger rows).
       A failed attach leaves the plan without a ladder, so the SAME branch
       safely re-attaches next pass — never a duplicate live group.
       NO_STRUCTURE / defense-in-depth: a plan with no TP tranches gets a
       SINGLE full-size protective stop instead (carrying a non-NULL
       exit_group_id so it too is attach-once).

  ENTRY phase (no fills yet)
    -> ALL entry orders terminal AND total filled qty == 0:
         -> WRITE plan_outcome(UNFILLED), no exits
    -> otherwise wait (NOOP).

  EXIT phase (OCO-ladder live)
    -> The TP + SL legs flow through reconciler.reconcile_orders() like any
       other order (statuses + fills synthesized from Alpaca polls). When one
       side of an OCO group fills the broker AUTO-CANCELS the sibling.
    -> When all SELL orders reach terminal state:
         -> WRITE plan_outcome(TP_HIT / SL_HIT / PARTIAL_TP / TIME_STOP_HIT)
            via the quantity-weighted classifier, with blended_entry/exit +
            realized R-multiple.
    -> Time-stop: if first fill > TIME_STOP_DAYS ago and exit phase still
       open, cancel pending exits + submit market-sell for the LIVE broker
       position (never the planned qty — a planned-qty time-stop over-sells
       after a partial TP).

Sizing: the ladder + the TIME_STOP are sized to the CURRENT live broker
position qty, never the planned qty. The TIME_STOP reads the live Alpaca
position directly.

Behaviour notes / Known issues (PR-body markers):
  (a) The OCO sibling auto-cancel is the broker's job: when a TP tranche fills,
      its paired stop leg is auto-cancelled by Alpaca (and vice versa). The
      reconciler polls those terminal transitions; the exit_manager does not
      couple TP and SL itself (the convergence-era SL-priority / cancel-first
      machinery is gone — the OCO group owns the coupling).
  (b) The ladder is attached ONCE on the filled aggregate. Because entry policy
      is cancel-on-first-fill the aggregate never grows, so there is no gap-fill
      re-size case (the convergence-era concern). A partial TP simply shrinks
      the held position; the remaining OCO stop legs still cover it.
  (c) The account label is a closed enum {main, test}. Anything that turns it
      into a Prometheus / gauge label needs only simple sanitization of those
      two known values — there is no free-form account string to defend
      against.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from alphalens_pipeline.paper.broker import BrokerClient, ExitLadderLeg, ExitTranche
from alphalens_pipeline.paper.constants import TIME_STOP_DAYS
from alphalens_pipeline.paper.ledger import (
    fetch_orders_for_plan,
    insert_order,
    insert_plan_outcome,
    record_exit_ladder,
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
    # v6 OCO-ladder: True iff ANY exit-order row carries a non-NULL
    # ``exit_group_id`` (an attached OCO-ladder leg OR the defense-in-depth
    # fallback stop). The ATTACH-ONCE gate keys on this: a plan that already
    # has a ladder is never re-attached. Derived from the already-fetched rows
    # in :func:`_snapshot` — no extra query.
    has_exit_ladder: bool

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
        fills. Used as the ATTACH-ONCE GATE — a position whose exits have
        fully sold it (net 0) is closed and must NOT attach a fresh ladder.
        The ladder SIZE still comes from the live broker position
        (:func:`_read_position`), authoritative for a not-yet-reconciled fill;
        this is only the gate.
        """
        return self.total_entry_filled_qty - self.total_exit_filled_qty

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
    # 'ATTACHED' | 'CLOSED' | 'TIME_STOP' | 'UNFILLED' | 'DESYNC_FLAT' | 'NOOP'
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
    # ATTACH-ONCE flag: any exit row tagged with an exit_group_id is an
    # attached OCO-ladder leg (or the fallback stop). ``dict(o)`` already
    # carries the column (orders.SELECT *), so this is a pure row read.
    has_exit_ladder = any(
        ("exit_group_id" in d and d.get("exit_group_id") is not None) for d in exit_orders
    )
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
        has_exit_ladder=has_exit_ladder,
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


def _build_exit_tranches(snapshot: _PlanSnapshot, aggregate_qty: int) -> list[ExitTranche]:
    """Split the held ``aggregate_qty`` across the plan's TP tranches into a
    list of :class:`ExitTranche` INTENTS for the OCO-ladder attach.

    Per-tranche qty is ``aggregate_qty * tranche_pct / 100`` (whole-share
    floor); the LAST non-empty tranche absorbs the rounding residue so
    ``sum(qty) == aggregate_qty`` (the same residue rule the legacy per-TP
    attach used). Tranches whose computed qty is <= 0 are skipped
    (``ExitTranche`` rejects a zero/negative qty). ``take_profit_limit`` is the
    tranche's ``target_price``.

    Returns ``[]`` when ``aggregate_qty <= 0`` OR the plan has no TP tranches;
    the caller treats an empty ladder as the NO_STRUCTURE / defense-in-depth
    case (a single full-size protective stop).
    """
    if aggregate_qty <= 0 or not snapshot.tp_tranches:
        return []
    # Index of the last tranche that will carry a positive qty, so it can
    # absorb the residue. Compute it the same way the legacy attach did:
    # every tranche except the structural last one floors its share; the last
    # one takes whatever remains.
    n = len(snapshot.tp_tranches)
    tranches: list[ExitTranche] = []
    remaining = aggregate_qty
    for idx, tranche in enumerate(snapshot.tp_tranches):
        if idx == n - 1:
            qty = remaining
        else:
            qty = int(aggregate_qty * float(tranche["tranche_pct"]) / 100.0)
        if qty <= 0:
            # A zero-share tranche contributes nothing; its share rolls into
            # the residue the last tranche absorbs (remaining is untouched).
            continue
        tranches.append(ExitTranche(qty=qty, take_profit_limit=float(tranche["target_price"])))
        remaining -= qty
    return tranches


def _attach_fallback_stop(
    conn: sqlite3.Connection,
    *,
    snapshot: _PlanSnapshot,
    broker: BrokerClient,
    qty: int,
    submitted_at: dt.datetime,
) -> tuple[int, int]:
    """Defense-in-depth: attach a SINGLE full-size protective STOP sized to
    ``qty`` when the plan has no TP tranches (so :func:`_build_exit_tranches`
    returned ``[]`` and the OCO-ladder path cannot run). Returns
    ``(n_submitted, n_failed)`` (each 0 or 1).

    CRITICAL: the persisted SL row carries a NON-NULL ``exit_group_id`` (its own
    broker order id), so ``_PlanSnapshot.has_exit_ladder`` becomes True next
    pass and the ATTACH-ONCE gate also covers this fallback — otherwise a fresh
    duplicate stop would be submitted every pass. Crash-resilient: a broker
    error is caught, logged, counted, and NOTHING is persisted, so the attach
    branch re-enters next pass and retries.
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
            "exit_manager attach FALLBACK SL FAILED plan_id=%d ticker=%s qty=%d stop=%.2f: %s; "
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
        # Non-NULL exit_group_id so has_exit_ladder is True next pass (the
        # fallback is attach-once like the OCO ladder). The stop's own id is
        # the stable per-group key (mirrors record_exit_ladder's tp-id key).
        exit_group_id=str(sl_order.id),
    )
    logger.info(
        "exit_manager attach FALLBACK SL plan_id=%d ticker=%s qty=%d stop=%.2f alpaca=%s",
        snapshot.plan_id,
        snapshot.ticker,
        qty,
        snapshot.disaster_stop,
        sl_order.id,
    )
    return 1, 0


# ----- exit lifecycle (write outcome when exits settle) -----


def _classify_exit_kind(snapshot: _PlanSnapshot) -> str:
    """Pick the canonical exit_kind by which side retired the MAJORITY of
    the exited shares (quantity-weighted), called only once every exit order
    is terminal so the observed fill counts are final.

    WHY quantity-weighted: an OCO ladder (PR-1/PR-2) produces MULTIPLE TP rows
    and MULTIPLE SL rows sharing one exit_group_id. With that, mixed fills are
    realistic — several tranches take profit while a price gap fills one stop
    slice. The legacy any-SL-fill rule returned SL_HIT the moment a single SL
    slice caught a dip, mislabeling a mostly-take-profit exit as a stop-out
    (the R2 bug). Summing fills per side and labeling by the majority fixes it.

    Uses ``filled_qty_observed`` (NOT ``status == 'FILLED'``) for the per-side
    counts: the Phase-A SL-not-resized simplification means Alpaca may
    PARTIAL-fill the SL up to remaining inventory (when partial TPs already
    executed) and then transition the order to CANCELED rather than FILLED.
    Looking at the observed fill count avoids that lockup.

    This labels ONLY the categorical exit_kind. The realized-R math in
    ``_write_outcome`` blends fills across all sides and is unchanged.

    Ordered rules (returns one of the plan_outcomes CHECK values
    TP_HIT / SL_HIT / TIME_STOP_HIT / PARTIAL_TP):
      1. qty_ts > 0           → TIME_STOP_HIT (the time-stop liquidation
         dominates regardless of any TP/SL fills — unchanged precedence).
      2. qty_tp == 0 and qty_sl == 0 → PARTIAL_TP (no exit fills observed —
         the catch-all default).
      3. qty_sl >= qty_tp     → SL_HIT (the stop retired at least half the
         exited shares; a tie goes to SL = conservative; this still yields
         SL_HIT for the legacy pure-stop case qty_tp == 0).
      4. qty_sl == 0 and (qty_tp >= total_entry OR all TP terminal-FILLED)
         → TP_HIT (a clean full take-profit with no stop fill — the legacy
         full-TP case + the all-tranches-took-profit OCO case).
      5. otherwise            → PARTIAL_TP (TP retired the majority but it is
         not a clean full take-profit — some shares remain or a minority stop
         slice also filled; this is the R2 fix).
    """
    sl_orders = [o for o in snapshot.exit_orders if o["order_kind"] == "SL"]
    tp_orders = [o for o in snapshot.exit_orders if o["order_kind"] == "TP"]
    time_stop_orders = [o for o in snapshot.exit_orders if o["order_kind"] == "TIME_STOP"]

    qty_ts = sum(int(o["filled_qty_observed"] or 0) for o in time_stop_orders)
    qty_tp = sum(int(o["filled_qty_observed"] or 0) for o in tp_orders)
    qty_sl = sum(int(o["filled_qty_observed"] or 0) for o in sl_orders)
    total_entry = snapshot.total_entry_filled_qty

    if qty_ts > 0:
        return "TIME_STOP_HIT"
    if qty_tp == 0 and qty_sl == 0:
        return "PARTIAL_TP"
    if qty_sl >= qty_tp:
        return "SL_HIT"
    # Here qty_tp > qty_sl.
    if qty_sl == 0 and (
        qty_tp >= total_entry or (tp_orders and all(o["status"] == "FILLED" for o in tp_orders))
    ):
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


def _cancel_orphaned_ladder_legs(
    *,
    legs: Sequence[ExitLadderLeg],
    snapshot: _PlanSnapshot,
    broker: BrokerClient,
) -> None:
    """Best-effort cancel every broker order in an ALREADY-PLACED OCO ladder.

    Called only when ``broker.attach_exit_ladder`` SUCCEEDED (M OCO groups are
    now LIVE at the broker) but the subsequent ``record_exit_ladder`` ledger
    write FAILED and rolled back ALL rows. Without this, ``has_exit_ladder``
    stays False (nothing persisted) while the live groups remain at the broker;
    the next pass re-enters the attach branch and submits a SECOND set of M OCO
    groups on the same shares — two disaster-stop legs at one price → over-sell
    to a short if price gaps through. This closes that broker-success /
    ledger-failure window so a failed-persist leaves NOTHING live, matching the
    adapter's own all-or-nothing rollback at the exit_manager boundary.

    Cancels each leg's take-profit AND stop-loss id. ``sl_order_id`` is
    deduped across legs per the :class:`ExitLadderLeg` doctrine (a future
    shared-stop broker repeats one stop id across tranches). Cancelling either
    side of an OCO group typically auto-cancels its sibling at the broker; the
    dedup + the broker treating an already-cancelled id as a no-op makes the
    redundant cancels harmless. Each cancel is independent best-effort — a
    failed cancel is logged and skipped, never raised (the dead-man
    ``n_filled_without_sl`` signal already flags the still-unprotected state on
    re-snapshot; a broker that ignores these cancels reduces to the prior
    double-attach risk, not worse).
    """
    seen: set[str] = set()
    for leg in legs:
        for order_id in (leg.tp_order_id, leg.sl_order_id):
            if order_id in seen:
                continue
            seen.add(order_id)
            try:
                broker.cancel_order(order_id)
            except Exception as exc:
                logger.warning(
                    "exit_manager cancel orphaned ladder leg failed alpaca=%s plan_id=%d: "
                    "%s; live OCO group may persist until the next reconcile",
                    order_id,
                    snapshot.plan_id,
                    exc,
                )


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

    # ATTACH-ONCE OCO-ladder rule (the disaster-stop guarantee). Entry policy
    # is CANCEL-ON-FIRST-FILL: there is no scale-in, so once the entries settle
    # the held aggregate only DECREASES — the protective exit ladder is attached
    # ONCE on the filled aggregate and never re-converged. The broker (Alpaca
    # adapter) owns the per-tranche OCO decomposition: M take-profit tranches,
    # each an OCO group whose stop_loss leg sits at the SAME disaster_stop, so
    # the held position is fully protected regardless of which tranche fills
    # first. The exit_manager only expresses INTENT (ExitTranche list + one
    # stop price) — no OCO mechanics leak here (the broker-neutral boundary).
    #
    # Gate ``not snap.has_exit_ladder``: a plan that already has an attached
    # ladder (any exit row tagged with an exit_group_id) — OR the defense-in-
    # depth fallback stop — is NEVER re-attached. The attach is all-or-nothing
    # at BOTH ends: the adapter cancels every OCO group already placed if any
    # tranche fails (so nothing is live at the broker), and the caller persists
    # NOTHING on failure (so no ledger rows). The one gap the adapter cannot
    # cover — attach SUCCEEDS (groups live) but the ledger write then fails and
    # rolls back — is closed at this boundary: the except below cancels the
    # just-placed legs (``_cancel_orphaned_ladder_legs``) so nothing stays live.
    # A failed attach therefore leaves has_exit_ladder False AND nothing live,
    # so the SAME branch safely re-attaches next pass — no duplicate live groups.
    #
    # SAFETY: skip the attach whenever the plan is in TIME-STOP territory, i.e.
    # (1) a TIME_STOP market-sell is already in flight (``has_live_time_stop``),
    # OR (2) the time-stop deadline has passed and this pass is about to fire
    # one (``should_fire_time_stop``). The time-stop IS the liquidation; nothing
    # protective should be attached against a position being market-liquidated
    # (a fresh ladder + the in-flight market sell would over-sell to a short).
    # ``should_fire_time_stop`` is computed ONCE here and reused by the time-stop
    # branch below so the skip and the fire stay consistent.
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
    if not should_fire_time_stop and snap.net_open_qty > 0 and not snap.has_exit_ladder:
        # (a) Cancel-on-first-fill: free any still-open entry tier (scale-in is
        # abandoned) so the held aggregate is the filled qty and no opposite-
        # side BUY hold blocks the protective stop. [keeps #401]
        n_canceled = _cancel_unfilled_entries(snapshot=snap, broker=broker)
        # (b) Size the ladder to the LIVE broker position (authoritative
        # inventory; falls back to net-open ledger qty on a transient read
        # error — see _read_position). A FLAT broker (size 0) with no exit
        # orders was already handled by the DESYNC guard above; a size <= 0
        # here is a no-op fall-through.
        n_exits_submitted = 0
        n_exits_failed = 0
        if size_qty > 0:
            tranches = _build_exit_tranches(snap, size_qty)
            if tranches:
                # (d) OCO-ladder attach — all-or-nothing at the broker. On
                # success persist BOTH legs per tranche (2 rows each, sharing
                # an exit_group_id). On failure persist NOTHING (the adapter
                # leaves nothing live), so has_exit_ladder stays False and the
                # next pass safely re-attaches.
                legs: list[ExitLadderLeg] = []
                try:
                    legs = broker.attach_exit_ladder(
                        symbol=snap.ticker,
                        tranches=tranches,
                        stop_price=snap.disaster_stop,
                        time_in_force="gtc",
                    )
                    record_exit_ladder(
                        conn,
                        plan_id=plan_id,
                        legs=legs,
                        submitted_at=observed_at,
                        account=snap.account,
                        platform=snap.platform,
                    )
                    n_exits_submitted = 2 * len(legs)
                    logger.info(
                        "exit_manager attach OCO-ladder plan_id=%d ticker=%s qty=%d "
                        "tranches=%d stop=%.2f",
                        snap.plan_id,
                        snap.ticker,
                        size_qty,
                        len(legs),
                        snap.disaster_stop,
                    )
                except Exception as exc:
                    n_exits_failed = 1
                    logger.warning(
                        "exit_manager attach OCO-ladder FAILED plan_id=%d ticker=%s qty=%d: "
                        "%s; nothing persisted, will retry next cycle",
                        snap.plan_id,
                        snap.ticker,
                        size_qty,
                        exc,
                    )
                    # Broker-success / ledger-failure window: if attach_exit_ladder
                    # already placed live OCO groups (``legs`` non-empty) but
                    # record_exit_ladder then rolled back ALL rows, those groups
                    # are LIVE at the broker yet has_exit_ladder stays False —
                    # the next pass would re-attach a SECOND ladder on the same
                    # shares (double disaster-stop → over-sell to short). Cancel
                    # the orphaned legs so a failed persist leaves nothing live.
                    # When attach_exit_ladder itself raised, ``legs`` is empty
                    # (the adapter's own rollback already cleared the broker), so
                    # this is a no-op for that path.
                    if legs:
                        _cancel_orphaned_ladder_legs(legs=legs, snapshot=snap, broker=broker)
            else:
                # (e) NO_STRUCTURE / defense-in-depth: the plan has no TP
                # tranches, so attach a SINGLE full-size protective stop. It
                # carries a non-NULL exit_group_id so has_exit_ladder is True
                # next pass (attach-once covers the fallback too).
                n_exits_submitted, n_exits_failed = _attach_fallback_stop(
                    conn,
                    snapshot=snap,
                    broker=broker,
                    qty=size_qty,
                    submitted_at=observed_at,
                )
        # (f) Re-snapshot so the dead-man flag reflects whether a ladder
        # actually landed this pass. A still-unprotected filled position is the
        # transient state the next pass retries.
        snap = _snapshot(conn, plan_id) or snap
        n_filled_without_sl = 1 if (snap.net_open_qty > 0 and not snap.has_exit_ladder) else 0
        return ExitOutcome(
            plan_id=plan_id,
            action="ATTACHED",
            n_exits_submitted=n_exits_submitted,
            n_exits_failed=n_exits_failed,
            n_entries_canceled=n_canceled,
            n_filled_without_sl=n_filled_without_sl,
        )

    # Entry phase still in progress and no exits yet — wait. Once the plan
    # has exit_orders (it attached a ladder above) the exit-phase machinery
    # below runs even if the entry ladder never formally settles — a
    # never-acking entry cancel must not strand the outcome write forever.
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
