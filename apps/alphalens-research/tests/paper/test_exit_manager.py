"""Exit-manager state-machine tests.

Drives a synthetic plan through each phase of the lifecycle:
  entry-phase settled with 0 fills -> UNFILLED outcome
  entry-phase settled with N fills -> attach TPs + SL
  all exits FILLED -> classify TP_HIT / SL_HIT / PARTIAL_TP + write outcome
  60d since first fill -> cancel exits + market-sell remaining
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from alphalens_pipeline.paper.exit_manager import process_plan_exit
from alphalens_pipeline.paper.ledger import (
    fetch_orders_for_plan,
    insert_fill,
    insert_order,
    insert_planned,
    open_ledger,
    update_order_status,
)

# Fixed XNYS anchors for time-stop tests. Fri 2026-01-02 (normal Friday
# session) → Fri 2026-05-29 spans ~100 XNYS trading days (5 months minus
# MLK / Presidents / Good Friday / Memorial Day) — comfortably past
# TIME_STOP_DAYS=42.
#
# The PR-B switch to trading-day arithmetic also exposed a separate
# fragility in the old ``now - timedelta(days=TIME_STOP_DAYS + 5)``
# pattern: the anchor moved every time the test ran (depending on the
# wall-clock day), and the elapsed trading-day count then depended on
# which calendar-week + holiday-density the offset happened to land in.
# Fixed past-dated anchors + an explicit ``observed_at`` threaded into
# ``process_plan_exit`` make the trading-day arithmetic deterministic
# regardless of when CI invokes the suite.
_FIRST_FILL_AT_FIXED = dt.datetime(2026, 1, 2, 16, 0, 0, tzinfo=dt.UTC)
_OBSERVED_AT_FIXED = dt.datetime(2026, 5, 29, 22, 0, 0, tzinfo=dt.UTC)


@dataclass
class _StubOrder:
    id: str


class _StubBrokerClient:
    """Records every submission + cancellation; returns sequential ids."""

    def __init__(self) -> None:
        self.submissions: list[dict] = []
        self.canceled: list[str] = []
        self._next = 1
        # ticker → live position qty. Default 27 (matches full-fill setup).
        self.position_qty_for: dict[str, int] = {}

    def _emit(self, kind: str, kwargs: dict) -> _StubOrder:
        self.submissions.append({"kind": kind, **kwargs})
        oid = f"exit-{kind.lower()}-{self._next:03d}"
        self._next += 1
        return _StubOrder(id=oid)

    def submit_stop_order(self, **kwargs):
        return self._emit("STOP", kwargs)

    def submit_limit_order(self, **kwargs):
        return self._emit("LIMIT", kwargs)

    def submit_market_order(self, **kwargs):
        return self._emit("MARKET", kwargs)

    def cancel_order(self, alpaca_order_id: str) -> None:
        self.canceled.append(alpaca_order_id)

    def get_position(self, symbol: str):
        """Return a stub position with .qty matching the test's expectation.
        Tests can override via .position_qty_for[symbol] = N before the call.
        Default: 27 (matches the standard _seed_plan + full fill setup)."""
        qty = self.position_qty_for.get(symbol, 27)
        if qty <= 0:
            return None

        @dataclass
        class _Pos:
            qty: int

        return _Pos(qty=qty)


def _seed_plan(
    ledger: Path,
    *,
    ticker: str = "NVDA",
    disaster_stop: float = 80.0,
    tp_tranches: list[tuple[int, float, float, float, str]] | None = None,
) -> int:
    """Insert one PLANNED row and return its plan_id."""
    ts = dt.datetime.now(dt.UTC)
    d = dt.date(2026, 5, 28)
    if tp_tranches is None:
        tp_tranches = [
            (0, 110.0, 50.0, 1.0, "tp-1"),
            (1, 130.0, 50.0, 3.0, "tp-2"),
        ]
    with open_ledger(ledger) as conn:
        row = insert_planned(
            conn,
            brief_date=d,
            ticker=ticker,
            theme="ai-infra",
            planned_at=ts,
            suggested_size_pct=5.0,
            scale_factor=0.05,
            final_size_pct=0.25,
            paper_equity=1_000_000.0,
            total_notional=2500.0,
            gross_notional=2700.0,
            disaster_stop=disaster_stop,
            order_ttl_days=10,
            tiers=[(0, 100.0, 27, 100.0, "t0")],
            tp_tranches=tp_tranches,
        )
    return row.plan_id


def _add_entry(
    ledger: Path,
    *,
    plan_id: int,
    alpaca_id: str,
    qty: int,
    limit: float = 100.0,
    status: str = "FILLED",
    filled_qty: int | None = None,
    filled_price: float | None = None,
) -> int:
    ts = dt.datetime.now(dt.UTC)
    with open_ledger(ledger) as conn:
        order_id = insert_order(
            conn,
            plan_id=plan_id,
            alpaca_order_id=alpaca_id,
            side="BUY",
            order_kind="ENTRY",
            tier_index=0,
            order_type="LIMIT",
            qty=qty,
            limit_price=limit,
            time_in_force="gtc",
            submitted_at=ts,
            status=status,
        )
        if filled_qty is not None and filled_price is not None and filled_qty > 0:
            insert_fill(
                conn,
                order_id=order_id,
                alpaca_fill_id=f"{alpaca_id}-fill",
                qty=filled_qty,
                price=filled_price,
                filled_at=ts,
            )
    return order_id


class _ExitTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"
        self.client = _StubBrokerClient()

    def tearDown(self):
        self._tmp.cleanup()


class TestEntryStillOpenNoOp(_ExitTestBase):
    def test_entry_still_submitted_no_op(self):
        plan_id = _seed_plan(self.ledger)
        _add_entry(self.ledger, plan_id=plan_id, alpaca_id="e1", qty=27, status="SUBMITTED")

        with open_ledger(self.ledger) as conn:
            outcome = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )

        self.assertEqual(outcome.action, "NOOP")
        self.assertEqual(len(self.client.submissions), 0)


class TestUnfilledOutcome(_ExitTestBase):
    def test_all_entries_canceled_with_zero_fills_writes_unfilled(self):
        plan_id = _seed_plan(self.ledger)
        _add_entry(self.ledger, plan_id=plan_id, alpaca_id="e1", qty=27, status="CANCELED")

        with open_ledger(self.ledger) as conn:
            outcome = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )

        self.assertEqual(outcome.action, "UNFILLED")
        self.assertEqual(outcome.exit_kind, "UNFILLED")
        # No Alpaca exit calls.
        self.assertEqual(len(self.client.submissions), 0)
        with open_ledger(self.ledger) as conn:
            row = conn.execute(
                "SELECT exit_kind FROM plan_outcomes WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        self.assertEqual(row["exit_kind"], "UNFILLED")


class TestAttachExits(_ExitTestBase):
    def test_full_entry_fill_attaches_sl_plus_two_tps(self):
        plan_id = _seed_plan(self.ledger)
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=27,
            status="FILLED",
            filled_qty=27,
            filled_price=99.5,
        )

        with open_ledger(self.ledger) as conn:
            outcome = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )

        self.assertEqual(outcome.action, "ATTACHED")
        # 1 SL + 2 TP tranches = 3 submissions.
        self.assertEqual(outcome.n_exits_submitted, 3)
        kinds = [s["kind"] for s in self.client.submissions]
        self.assertEqual(kinds.count("STOP"), 1)
        self.assertEqual(kinds.count("LIMIT"), 2)

        # SL is qty=27 at stop=80.
        sl = next(s for s in self.client.submissions if s["kind"] == "STOP")
        self.assertEqual(sl["qty"], 27)
        self.assertEqual(sl["stop_price"], 80.0)

        # TP qtys: 50% of 27 = 13 (floor), last absorbs residue = 14. Sum = 27.
        tp_qtys = [s["qty"] for s in self.client.submissions if s["kind"] == "LIMIT"]
        self.assertEqual(sum(tp_qtys), 27)
        self.assertEqual(sorted(tp_qtys), [13, 14])

    def test_partial_entry_fill_still_attaches_with_lower_total(self):
        """Only 10 of 27 entry shares filled before CANCEL — SL + TPs are
        sized for the 10 actually held."""
        plan_id = _seed_plan(self.ledger)
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=27,
            status="CANCELED",
            filled_qty=10,
            filled_price=99.0,
        )

        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )

        total_exit_qty = sum(s["qty"] for s in self.client.submissions if s["kind"] == "STOP")
        self.assertEqual(total_exit_qty, 10)

    def test_attach_is_idempotent_within_same_run(self):
        """Calling process_plan_exit twice on a settled-with-fills plan does
        NOT submit exits twice — once attached, subsequent passes are
        no-ops until an exit reaches a terminal state."""
        plan_id = _seed_plan(self.ledger)
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=27,
            status="FILLED",
            filled_qty=27,
            filled_price=99.5,
        )
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "ATTACHED")
        self.assertEqual(o2.action, "NOOP")
        # Only one round of submissions.
        self.assertEqual(len([s for s in self.client.submissions if s["kind"] == "STOP"]), 1)


class TestExitOrdersInheritPlatformFromSnapshot(_ExitTestBase):
    """Mirror of TestExitManagerThreadsAccountFromSnapshot for the v5
    ``platform`` axis: a plan seeded with the default platform produces
    exit orders (SL + TPs) all persisted with platform='alpaca' threaded
    from ``_PlanSnapshot.platform`` (NOT merely the orders.platform column
    DEFAULT). Threading is proven by reading the persisted orders rows back.
    """

    def test_exit_orders_inherit_platform_from_snapshot(self):
        plan_id = _seed_plan(self.ledger)
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=27,
            status="FILLED",
            filled_qty=27,
            filled_price=99.5,
        )

        with open_ledger(self.ledger) as conn:
            outcome = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
            orders = fetch_orders_for_plan(conn, plan_id)

        self.assertEqual(outcome.action, "ATTACHED")
        exit_orders = [o for o in orders if o["order_kind"] in ("SL", "TP")]
        self.assertGreater(len(exit_orders), 0)
        for o in exit_orders:
            self.assertEqual(
                o["platform"],
                "alpaca",
                f"exit order kind={o['order_kind']} leaked platform="
                f"{o['platform']!r}, expected 'alpaca'",
            )


class TestExitClosure(_ExitTestBase):
    def _attach_and_simulate_exits(self, *, plan_id: int, entry_qty: int, entry_price: float):
        """Attach the exits, then look them up + return (sl_id, tp_ids)."""
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=entry_qty,
            status="FILLED",
            filled_qty=entry_qty,
            filled_price=entry_price,
        )
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
            orders = fetch_orders_for_plan(conn, plan_id)
        sl_order = next(o for o in orders if o["order_kind"] == "SL")
        tp_orders = [o for o in orders if o["order_kind"] == "TP"]
        return int(sl_order["order_id"]), [int(o["order_id"]) for o in tp_orders]

    def _mark_filled(self, order_id: int, qty: int, price: float) -> None:
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            update_order_status(conn, order_id=order_id, status="FILLED")
            insert_fill(
                conn,
                order_id=order_id,
                alpaca_fill_id=f"exit-fill-{order_id}",
                qty=qty,
                price=price,
                filled_at=ts,
            )

    def test_all_tps_filled_writes_tp_hit_outcome(self):
        plan_id = _seed_plan(self.ledger)
        sl_id, tp_ids = self._attach_and_simulate_exits(
            plan_id=plan_id, entry_qty=27, entry_price=100.0
        )
        # Two TPs fill at their respective targets; SL is still open.
        self._mark_filled(tp_ids[0], qty=13, price=110.0)
        self._mark_filled(tp_ids[1], qty=14, price=130.0)

        with open_ledger(self.ledger) as conn:
            outcome = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )

        # Phase-A simplification: SL stays open until TP-hit reconcile cancels it.
        # The exit-classifier sees TP=FILLED + SL=SUBMITTED → not all-terminal yet → NOOP.
        self.assertEqual(outcome.action, "NOOP")

        # Operator (or next reconcile pass) marks the SL as cancelled — then
        # the all-terminal check passes and outcome is written.
        with open_ledger(self.ledger) as conn:
            update_order_status(conn, order_id=sl_id, status="CANCELED")
            outcome2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(outcome2.action, "CLOSED")
        self.assertEqual(outcome2.exit_kind, "TP_HIT")

        # Outcome row records blended prices + realised R.
        with open_ledger(self.ledger) as conn:
            row = conn.execute(
                "SELECT blended_entry_price, blended_exit_price, realized_r_multiple, exit_kind "
                "FROM plan_outcomes WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        self.assertAlmostEqual(row["blended_entry_price"], 100.0)
        # Blended exit = (13*110 + 14*130) / 27 ≈ 120.37
        self.assertAlmostEqual(row["blended_exit_price"], (13 * 110 + 14 * 130) / 27, places=4)
        # R = (120.37 - 100) / (100 - 80) = 20.37 / 20 = 1.0185
        self.assertGreater(row["realized_r_multiple"], 0)

    def test_sl_fill_classifies_as_sl_hit_and_cancels_tps(self):
        """Per the zen-fixed flow:
        1. SL fills (test marks it via _mark_filled).
        2. First process_plan_exit pass detects sl_fired (filled_qty > 0),
           issues cancel requests to Alpaca for the open TPs but does NOT
           update local status — the reconciler's job. Result: NOOP because
           TPs are still SUBMITTED locally.
        3. Reconciler picks up the CANCELED status on next poll (test
           simulates this by manually updating TP status rows).
        4. Second process_plan_exit pass sees all exits terminal +
           classifies SL_HIT (via filled_qty_observed > 0 on the SL).
        """
        plan_id = _seed_plan(self.ledger)
        sl_id, tp_ids = self._attach_and_simulate_exits(
            plan_id=plan_id, entry_qty=27, entry_price=100.0
        )
        self._mark_filled(sl_id, qty=27, price=79.5)

        # Pass 1: cancel requests issued, TPs still SUBMITTED → NOOP.
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "NOOP")
        # Both TP alpaca ids got a cancel request.
        self.assertEqual(len(self.client.canceled), 2)

        # Simulate the reconciler's next poll picking up the CANCELED status.
        with open_ledger(self.ledger) as conn:
            for tp_id in tp_ids:
                update_order_status(conn, order_id=tp_id, status="CANCELED")

        # Pass 2: all exits now terminal → CLOSED with SL_HIT.
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "CLOSED")
        self.assertEqual(o2.exit_kind, "SL_HIT")

        with open_ledger(self.ledger) as conn:
            row = conn.execute(
                "SELECT realized_r_multiple FROM plan_outcomes WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        # R = (79.5 - 100) / (100 - 80) = -20.5 / 20 = -1.025
        self.assertAlmostEqual(row["realized_r_multiple"], (79.5 - 100) / (100 - 80))


class TestZenRegressions(_ExitTestBase):
    """Regression tests for issues found in the post-PR #277 zen review."""

    def _attach_and_simulate_exits(self, *, plan_id: int, entry_qty: int, entry_price: float):
        """Attach exits then return (sl_id, [tp_ids])."""
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=entry_qty,
            status="FILLED",
            filled_qty=entry_qty,
            filled_price=entry_price,
        )
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
            orders = fetch_orders_for_plan(conn, plan_id)
        sl = next(o for o in orders if o["order_kind"] == "SL")
        tps = [o for o in orders if o["order_kind"] == "TP"]
        return int(sl["order_id"]), [int(o["order_id"]) for o in tps]

    def _mark_filled(self, order_id: int, qty: int, price: float) -> None:
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            update_order_status(conn, order_id=order_id, status="FILLED")
            insert_fill(
                conn,
                order_id=order_id,
                alpaca_fill_id=f"exit-fill-{order_id}",
                qty=qty,
                price=price,
                filled_at=ts,
            )

    def test_time_stop_does_not_refire_when_market_order_already_submitted(self):
        """Critical bug zen flagged: an already-submitted TIME_STOP order that
        hasn't filled yet (e.g. market closed) would otherwise be cancelled
        and resubmitted on every reconcile pass — infinite Alpaca API spam."""
        plan_id = _seed_plan(self.ledger)
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=27,
            status="FILLED",
            filled_qty=27,
            filled_price=100.0,
        )
        ancient = _FIRST_FILL_AT_FIXED
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (ancient.isoformat(), plan_id),
            )

        # Pass 1: attach.
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        # Pass 2: time-stop fires.
        with open_ledger(self.ledger) as conn:
            o_first = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o_first.action, "TIME_STOP")
        n_market_first = len([s for s in self.client.submissions if s["kind"] == "MARKET"])
        self.assertEqual(n_market_first, 1)

        # Pass 3 (same conditions, market order still SUBMITTED) — must NOT
        # submit a second market order.
        with open_ledger(self.ledger) as conn:
            o_second = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        n_market_second = len([s for s in self.client.submissions if s["kind"] == "MARKET"])
        self.assertEqual(n_market_second, 1, "second pass duplicated the TIME_STOP order")
        # The action is NOOP (no new exit kind triggered; everything pending).
        self.assertEqual(o_second.action, "NOOP")

    def test_sl_partial_fill_then_alpaca_cancels_remaining_still_classifies_sl_hit(self):
        """Critical bug zen flagged: when partial TPs have already executed,
        the SL (sized for full position) can only partial-fill the remaining
        inventory before Alpaca short-prevents and cancels the rest. SL
        status becomes CANCELED, NOT FILLED. The old code checked status ==
        'FILLED' so this case caused a state-machine lockup. The fix
        switches detection to filled_qty_observed > 0."""
        plan_id = _seed_plan(self.ledger)
        sl_id, tp_ids = self._attach_and_simulate_exits(
            plan_id=plan_id, entry_qty=27, entry_price=100.0
        )
        # TP1 partial-fills first (13/13 shares at 110).
        self._mark_filled(tp_ids[0], qty=13, price=110.0)
        # SL partial-fires for remaining 14 then Alpaca cancels (status = CANCELED).
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            insert_fill(
                conn,
                order_id=sl_id,
                alpaca_fill_id="sl-partial",
                qty=14,
                price=79.5,
                filled_at=ts,
            )
            update_order_status(conn, order_id=sl_id, status="CANCELED")
            # TP1 was FILLED; TP2 still SUBMITTED.

        # Pass: sl_fired (filled_qty > 0) triggers cancel for TP2.
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
            tp2_row = conn.execute(
                "SELECT alpaca_order_id FROM orders WHERE order_id = ?", (tp_ids[1],)
            ).fetchone()
        # TP2 cancel was requested at Alpaca by alpaca_order_id.
        self.assertIn(tp2_row["alpaca_order_id"], self.client.canceled)

        # Simulate reconciler picking up CANCELED on TP2.
        with open_ledger(self.ledger) as conn:
            update_order_status(conn, order_id=tp_ids[1], status="CANCELED")

        # Now all exits terminal — classify SL_HIT (NOT lockup).
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "CLOSED")
        self.assertEqual(o.exit_kind, "SL_HIT")

    def test_cancel_does_not_eagerly_mark_local_status_canceled(self):
        """HIGH bug zen flagged: marking the local order CANCELED immediately
        on cancel-request drops it from fetch_open_orders, so the reconciler
        never observes final partial fills that landed between our request
        and Alpaca processing it.

        After cancel-request, local status MUST remain SUBMITTED /
        PARTIALLY_FILLED until the reconciler polls and observes the actual
        terminal state from Alpaca.
        """
        plan_id = _seed_plan(self.ledger)
        sl_id, _tp_ids = self._attach_and_simulate_exits(
            plan_id=plan_id, entry_qty=27, entry_price=100.0
        )
        # SL fills, triggering cancel for the TPs.
        self._mark_filled(sl_id, qty=27, price=79.5)
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )

        # Local TP statuses are still SUBMITTED — the cancel went to Alpaca
        # but our ledger waits for the reconciler's poll.
        with open_ledger(self.ledger) as conn:
            rows = fetch_orders_for_plan(conn, plan_id)
        tp_statuses = {r["status"] for r in rows if r["order_kind"] == "TP"}
        self.assertEqual(tp_statuses, {"SUBMITTED"}, "cancel eagerly marked local CANCELED")

    def test_time_stop_queries_alpaca_for_remaining_qty(self):
        """HIGH bug zen flagged: computing remaining = entry_filled -
        exit_filled locally over-sells when an exit fill hasn't been
        reconciled yet. Fix queries Alpaca for the live position."""
        plan_id = _seed_plan(self.ledger)
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=27,
            status="FILLED",
            filled_qty=27,
            filled_price=100.0,
        )
        ancient = _FIRST_FILL_AT_FIXED
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (ancient.isoformat(), plan_id),
            )

        # Tell the stub Alpaca says we have 18 shares (e.g. an exit fill
        # already happened that the reconciler hasn't picked up yet).
        self.client.position_qty_for["NVDA"] = 18

        # Pass 1: attach. Pass 2: time-stop.
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "TIME_STOP")

        market = [s for s in self.client.submissions if s["kind"] == "MARKET"]
        self.assertEqual(market[0]["qty"], 18, "should have used Alpaca position qty, not ledger")


class TestTimeStop(_ExitTestBase):
    def test_position_older_than_time_stop_triggers_market_sell(self):
        plan_id = _seed_plan(self.ledger)
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=27,
            status="FILLED",
            filled_qty=27,
            filled_price=100.0,
        )
        # Backdate the entry fill so first_fill_at is older than TIME_STOP_DAYS.
        ancient = _FIRST_FILL_AT_FIXED
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (ancient.isoformat(), plan_id),
            )

        # First pass attaches the exits.
        with open_ledger(self.ledger) as conn:
            o_attach = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o_attach.action, "ATTACHED")

        # Second pass — time-stop fires before any exit completes.
        with open_ledger(self.ledger) as conn:
            o_ts = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o_ts.action, "TIME_STOP")

        # Verify the market-sell + cancellations happened.
        market = [s for s in self.client.submissions if s["kind"] == "MARKET"]
        self.assertEqual(len(market), 1)
        self.assertEqual(market[0]["qty"], 27)
        # Both the SL + TP got cancelled.
        self.assertGreaterEqual(len(self.client.canceled), 3)


if __name__ == "__main__":
    unittest.main()
