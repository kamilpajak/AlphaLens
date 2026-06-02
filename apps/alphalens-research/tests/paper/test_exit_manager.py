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

from alphalens_pipeline.paper.exit_manager import _snapshot, process_plan_exit
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

        self.assertEqual(outcome.action, "CONVERGE_SL")
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
        # Live broker position matches the 10 filled shares.
        self.client.position_qty_for["NVDA"] = 10

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
        self.assertEqual(o1.action, "CONVERGE_SL")
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

        self.assertEqual(outcome.action, "CONVERGE_SL")
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
        # Pass 1: attach (fill recent, not yet past the time-stop deadline).
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        # Age past the deadline so the time-stop fires from pass 2 on.
        ancient = _FIRST_FILL_AT_FIXED
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (ancient.isoformat(), plan_id),
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
        # Tell the stub Alpaca says we have 18 shares (e.g. an exit fill
        # already happened that the reconciler hasn't picked up yet).
        self.client.position_qty_for["NVDA"] = 18

        # Pass 1: attach (fill recent, not yet past the time-stop deadline).
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        # Age past the deadline so the time-stop fires on pass 2.
        ancient = _FIRST_FILL_AT_FIXED
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (ancient.isoformat(), plan_id),
            )
        # Pass 2: time-stop.
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "TIME_STOP")

        market = [s for s in self.client.submissions if s["kind"] == "MARKET"]
        self.assertEqual(market[0]["qty"], 18, "should have used Alpaca position qty, not ledger")


class _FailingExitBroker(_StubBrokerClient):
    """Stub broker that raises on selected exit-order submits.

    ``fail_kinds`` selects which submit kinds raise (e.g. {"STOP"} to model
    Alpaca rejecting the SL with held_for_orders / insufficient qty). The
    raised error mimics an Alpaca APIError propagating out of the SDK call.

    ``fail_first_n_stop`` makes the STOP submit fail the first N times then
    succeed — models a transient held_for_orders rejection that clears once
    the reserved-share hold is released, used by the SL-convergence tests.
    """

    def __init__(
        self,
        *,
        fail_kinds: set[str] | None = None,
        fail_first_n_stop: int = 0,
    ) -> None:
        super().__init__()
        self.fail_kinds = fail_kinds or set()
        self._fail_first_n_stop = fail_first_n_stop
        self._stop_attempts = 0

    def _emit(self, kind: str, kwargs: dict):
        if kind == "STOP":
            self._stop_attempts += 1
            if self._stop_attempts <= self._fail_first_n_stop:
                raise RuntimeError(
                    "stub APIError: held_for_orders insufficient qty available for STOP"
                )
        if kind in self.fail_kinds:
            raise RuntimeError(
                f"stub APIError: held_for_orders insufficient qty available for {kind}"
            )
        return super()._emit(kind, kwargs)


class _WashTradeRejectingBroker(_StubBrokerClient):
    """Stub broker whose STOP (SL) submit raises the EXACT Alpaca rejection we
    probed live: HTTP 403, code 40310000, "potential wash trade detected ...
    opposite side limit order exists. use complex orders".

    In production this is what Alpaca returns for a bare protective STOP sell
    while an open opposite-side limit BUY (an unfilled entry tier) still holds
    the shares — the cancel-first step (``_cancel_unfilled_entries``) is what
    frees the hold and avoids it. This stub gives one crash-resilience test the
    real error shape instead of a generic Exception, to prove the broad
    try/except in ``_attach_sl`` catches the actual production failure.
    """

    def _emit(self, kind: str, kwargs: dict):
        if kind == "STOP":
            # Mirrors alpaca.common.exceptions.APIError surfacing the raw 403
            # JSON body. We raise a plain Exception subclass carrying the same
            # message so the production broad-catch is exercised faithfully
            # without importing the Alpaca SDK into the test.
            raise RuntimeError(
                '403 Client Error: {"code":40310000,"message":'
                '"potential wash trade detected. opposite side limit order '
                'exists. use complex orders"}'
            )
        return super()._emit(kind, kwargs)


class _NoCancelAckBroker(_StubBrokerClient):
    """Records cancel requests but the test deliberately NEVER simulates the
    reconciler picking up the CANCELED status — so the entry tier stays
    non-terminal across passes (a broker that never acks a cancel).

    This is the core hardening scenario: the protective SL must converge
    even when the ladder never settles.
    """


def _add_entry_tier(
    ledger: Path,
    *,
    plan_id: int,
    alpaca_id: str,
    tier_index: int,
    qty: int,
    limit: float,
    status: str,
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
            tier_index=tier_index,
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


class TestCancelUnfilledEntriesOnFirstFill(_ExitTestBase):
    """Cancel-on-first-fill: once a plan has any entry fill but still has
    non-terminal entry tiers, those tiers are cancelled via the broker so
    the protective SELL is no longer blocked by the reserved-share /
    opposite-side hold — and the SL converges on the SAME pass (decoupled
    from ladder settlement)."""

    def _seed_partial_ladder(self) -> tuple[int, str]:
        """Tier 0 FILLED (aggressive), tier 1 SUBMITTED (cheaper, unfilled)."""
        plan_id = _seed_plan(self.ledger)
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e0",
            tier_index=0,
            qty=14,
            limit=100.0,
            status="FILLED",
            filled_qty=14,
            filled_price=99.5,
        )
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            tier_index=1,
            qty=13,
            limit=95.0,
            status="SUBMITTED",
        )
        return plan_id, "e1"

    def test_partial_fill_cancels_open_tier_and_attaches_sl_same_pass(self):
        plan_id, open_alpaca_id = self._seed_partial_ladder()
        # Live broker position is the 14 filled shares (cheaper tier unfilled).
        self.client.position_qty_for["NVDA"] = 14

        # Pass 1: a fill exists + an open tier exists → cancel the open tier
        # AND converge the protective SL the same pass (decoupled from ladder
        # settlement — the whole point of the hardening).
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "CONVERGE_SL")
        self.assertEqual(o1.n_entries_canceled, 1)
        self.assertIn(open_alpaca_id, self.client.canceled)
        # SL sized to the 14 filled shares (broker position), cheaper tier abandoned.
        sl = next(s for s in self.client.submissions if s["kind"] == "STOP")
        self.assertEqual(sl["qty"], 14)
        self.assertEqual(o1.n_filled_without_sl, 0)

    def test_zero_fill_plan_is_not_canceled_and_stays_unfilled_path(self):
        """A plan with NO entry fill must NOT trigger cancel-on-first-fill;
        it stays on the NOOP / TTL path (UNFILLED via the sweep backstop)."""
        plan_id = _seed_plan(self.ledger)
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e0",
            tier_index=0,
            qty=14,
            limit=100.0,
            status="SUBMITTED",
        )
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            tier_index=1,
            qty=13,
            limit=95.0,
            status="SUBMITTED",
        )
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "NOOP")
        self.assertEqual(len(self.client.canceled), 0)
        self.assertEqual(len(self.client.submissions), 0)

    def test_fully_filled_ladder_does_not_cancel(self):
        """All tiers FILLED → nothing to cancel; exits attach unchanged."""
        plan_id = _seed_plan(self.ledger)
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e0",
            tier_index=0,
            qty=14,
            limit=100.0,
            status="FILLED",
            filled_qty=14,
            filled_price=99.5,
        )
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            tier_index=1,
            qty=13,
            limit=95.0,
            status="FILLED",
            filled_qty=13,
            filled_price=94.5,
        )
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "CONVERGE_SL")
        self.assertEqual(len(self.client.canceled), 0)
        sl = next(s for s in self.client.submissions if s["kind"] == "STOP")
        self.assertEqual(sl["qty"], 27)

    def test_cancel_is_idempotent_once_exits_exist(self):
        """A plan already in the exit phase (exit_orders present) does NOT
        re-cancel entry tiers nor re-attach exits."""
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
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        n_submitted = len(self.client.submissions)
        n_canceled = len(self.client.canceled)
        # Re-run: exits already attached → NOOP, no extra cancels/submits.
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "NOOP")
        self.assertEqual(len(self.client.submissions), n_submitted)
        self.assertEqual(len(self.client.canceled), n_canceled)


class TestAttachExitsCrashResilience(_ExitTestBase):
    """A broker error on an exit submit (e.g. Alpaca held_for_orders) is
    caught + counted + does NOT propagate, so one bad plan never aborts the
    reconcile pass for plans behind it."""

    def test_sl_submit_failure_is_caught_and_counted_not_raised(self):
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
        broker = _FailingExitBroker(fail_kinds={"STOP"})
        with open_ledger(self.ledger) as conn:
            # Must NOT raise.
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o.action, "CONVERGE_SL")
        # SL-PRIORITY: the SL failed, so NO TP is submitted (a TP would reserve
        # the shares and block the disaster-stop). Nothing submitted, one fail.
        self.assertEqual(o.n_exits_submitted, 0)
        self.assertEqual(o.n_exits_failed, 1)
        kinds = [s["kind"] for s in broker.submissions]
        self.assertEqual(kinds.count("STOP"), 0)
        self.assertEqual(kinds.count("LIMIT"), 0)
        # The dead-man signal: filled shares with no live SL.
        self.assertEqual(o.n_filled_without_sl, 1)

    def test_sl_submit_real_alpaca_wash_trade_403_is_caught_and_counted(self):
        """Fidelity: model the ACTUAL production rejection (HTTP 403, code
        40310000, "potential wash trade detected ... opposite side limit order
        exists") — a bare STOP sell vs an open opposite-side limit BUY. The
        cancel-first step is what avoids it in normal flow; here we prove the
        broad catch in ``_attach_sl`` still swallows + counts this exact error
        (does NOT propagate), so one bad plan never aborts the reconcile pass.
        """
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
        broker = _WashTradeRejectingBroker()
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o.action, "CONVERGE_SL")
        # SL rejected by the wash-trade 403; counted, not raised. SL-PRIORITY:
        # NO TP is submitted while the SL is missing (a TP limit is wash-trade-
        # exempt and would reserve the shares, blocking the SL forever).
        self.assertEqual(o.n_exits_failed, 1)
        self.assertEqual(o.n_exits_submitted, 0)
        # No SL or TP persisted -> retryable next pass once the hold clears.
        with open_ledger(self.ledger) as conn:
            kinds = [
                r["order_kind"]
                for r in fetch_orders_for_plan(conn, plan_id)
                if r["order_kind"] in ("SL", "TP")
            ]
        self.assertEqual(kinds, [])
        self.assertEqual(o.n_filled_without_sl, 1)

    def test_all_submits_fail_nothing_attached_retryable_next_pass(self):
        """When every exit submit fails, no exit_orders are persisted, so the
        next reconcile pass re-enters the attach branch (retryable)."""
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
        broker = _FailingExitBroker(fail_kinds={"STOP", "LIMIT"})
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o1 = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o1.action, "CONVERGE_SL")
        self.assertEqual(o1.n_exits_submitted, 0)
        self.assertGreater(o1.n_exits_failed, 0)
        # No exit orders persisted → still retryable.
        with open_ledger(self.ledger) as conn:
            orders = fetch_orders_for_plan(conn, plan_id)
        self.assertEqual([o for o in orders if o["order_kind"] in ("SL", "TP")], [])

        # Pass 2 with a healthy broker attaches successfully.
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "CONVERGE_SL")
        self.assertEqual(o2.n_exits_submitted, 3)


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
        # First pass attaches the exits (fill is recent, not yet past deadline).
        with open_ledger(self.ledger) as conn:
            o_attach = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o_attach.action, "CONVERGE_SL")

        # Backdate the entry fill so first_fill_at is older than TIME_STOP_DAYS.
        ancient = _FIRST_FILL_AT_FIXED
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (ancient.isoformat(), plan_id),
            )

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


class TestTimeStopVsSlConvergenceCollision(_ExitTestBase):
    """HIGH safety regression: a live TIME_STOP market-sell must short-circuit
    the SL-convergence branch so it does NOT re-arm a fresh SL on top of the
    in-flight liquidation.

    Failure sequence reproduced here (oversell-to-short on the BASE code):
      pass 1  -> attach: live SL covers the full position.
      pass 2  -> time-stop fires: cancels the live SL + submits a TIME_STOP
                 market-sell (left UNFILLED, e.g. submitted at/after the close).
      between -> the reconciler polls the canceled SL to a TERMINAL CANCELED
                 with 0 fill (THIS is the state the old stub never modelled —
                 it left the canceled SL SUBMITTED, masking the bug).
      pass 3  -> sl_coverage_qty has dropped to 0 (only-SL was a canceled-zero-
                 fill), net_open_qty is still > 0 (TIME_STOP not yet filled) ->
                 the BASE convergence gate RE-ARMS a full-size SL. Now a live
                 TIME_STOP market-sell AND a live SL both sit on the same 27
                 shares -> 54 live sell qty vs 27 held -> oversell to short.

    The guard makes pass 3 a NOOP (convergence skipped while a non-terminal
    TIME_STOP exists), so live sell qty never exceeds the live position qty.
    """

    def _seed_filled_recent_plan(self) -> int:
        """Seed a fully-filled plan whose fill is RECENT (not yet past the
        time-stop deadline), so pass 1 arms the protective SL via convergence.
        ``_age_plan`` then backdates the fill so the time-stop fires on a
        subsequent pass — preserving the pass1=arm-SL, pass2=fire-time-stop
        sequence after the rejected-time-stop retry change made a past-deadline
        plan fire the time-stop on the very first pass (convergence skipped)."""
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
        return plan_id

    def _age_plan(self, plan_id: int) -> None:
        """Backdate the entry fill past TIME_STOP_DAYS so the time-stop fires."""
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (_FIRST_FILL_AT_FIXED.isoformat(), plan_id),
            )

    def _live_sell_qty(self, plan_id: int) -> int:
        """Sum the qty of every NON-TERMINAL sell order (SL / TP / TIME_STOP)
        — the shares the broker could still sell. Must never exceed the live
        position qty, else a fill would flip us short."""
        terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        with open_ledger(self.ledger) as conn:
            rows = conn.execute(
                "SELECT qty, status FROM orders WHERE plan_id = ? AND side = 'SELL'",
                (plan_id,),
            ).fetchall()
        return sum(int(r["qty"]) for r in rows if r["status"] not in terminal)

    def _live_sl_count(self, plan_id: int) -> int:
        terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        with open_ledger(self.ledger) as conn:
            rows = conn.execute(
                "SELECT status FROM orders WHERE plan_id = ? AND order_kind = 'SL'",
                (plan_id,),
            ).fetchall()
        return sum(1 for r in rows if r["status"] not in terminal)

    def test_live_time_stop_blocks_sl_rearm_no_oversell(self):
        plan_id = self._seed_filled_recent_plan()
        # Live broker position stays 27 throughout (TIME_STOP unfilled).
        self.client.position_qty_for["NVDA"] = 27

        # Pass 1: attach — one live SL covers all 27 (fill not yet aged).
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "CONVERGE_SL")
        self.assertEqual(self._live_sl_count(plan_id), 1)

        # Now age the fill past the time-stop deadline.
        self._age_plan(plan_id)

        # Pass 2: time-stop fires — submits the TIME_STOP market-sell and
        # requests cancel of the live SL + TPs.
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "TIME_STOP")
        n_market = len([s for s in self.client.submissions if s["kind"] == "MARKET"])
        self.assertEqual(n_market, 1)

        # Model the reconciler's next poll: the canceled exits (SL + TPs)
        # transition to TERMINAL CANCELED with 0 fill. THIS is the state the
        # base stub never reaches; without it the bug stays hidden because the
        # canceled SL keeps counting as coverage.
        with open_ledger(self.ledger) as conn:
            rows = fetch_orders_for_plan(conn, plan_id)
            for o in rows:
                if o["order_kind"] in ("SL", "TP") and o["status"] == "SUBMITTED":
                    update_order_status(conn, order_id=int(o["order_id"]), status="CANCELED")

        # Sanity: SL coverage has now dropped to 0 (canceled-zero-fill) while
        # the position is still 27 — exactly the gap the base code mis-handled.
        self.assertEqual(self._live_sl_count(plan_id), 0)

        # Pass 3 (and a couple more): convergence MUST be skipped because a
        # non-terminal TIME_STOP is in flight. No new SL is armed.
        for _ in range(3):
            with open_ledger(self.ledger) as conn:
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
                )
            self.assertEqual(
                o.action,
                "NOOP",
                "convergence re-armed an SL on top of the live TIME_STOP",
            )
            # No SL was re-armed.
            self.assertEqual(
                self._live_sl_count(plan_id),
                0,
                "a fresh SL was armed against a market-liquidated position",
            )
            # The core safety invariant: live sell qty (TIME_STOP only here)
            # never exceeds the live position qty — no oversell, no short.
            self.assertLessEqual(
                self._live_sell_qty(plan_id),
                27,
                "live sell qty exceeds position qty -> oversell to short",
            )

        # Exactly one SL was ever submitted (the pass-1 one). The TIME_STOP is
        # the only live sell.
        n_stop = len([s for s in self.client.submissions if s["kind"] == "STOP"])
        self.assertEqual(n_stop, 1, "convergence double-armed the SL during the time-stop")

    def test_time_stop_fill_still_closes_after_guard(self):
        """Once the TIME_STOP fills (terminal), the guard no longer blocks and
        the normal exit-phase closure writes a TIME_STOP_HIT outcome."""
        plan_id = self._seed_filled_recent_plan()
        self.client.position_qty_for["NVDA"] = 27

        # Pass 1 arms the SL (fill not yet aged), then age + pass 2 fires.
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self._age_plan(plan_id)
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "TIME_STOP")

        # Reconciler polls: canceled SL/TPs become terminal; the TIME_STOP
        # market-sell fills for the full 27.
        with open_ledger(self.ledger) as conn:
            rows = fetch_orders_for_plan(conn, plan_id)
            for o in rows:
                if o["order_kind"] in ("SL", "TP") and o["status"] == "SUBMITTED":
                    update_order_status(conn, order_id=int(o["order_id"]), status="CANCELED")
            ts_order = next(o for o in rows if o["order_kind"] == "TIME_STOP")
            update_order_status(conn, order_id=int(ts_order["order_id"]), status="FILLED")
            insert_fill(
                conn,
                order_id=int(ts_order["order_id"]),
                alpaca_fill_id="ts-fill",
                qty=27,
                price=95.0,
                filled_at=dt.datetime.now(dt.UTC),
            )

        with open_ledger(self.ledger) as conn:
            o3 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o3.action, "CLOSED")
        self.assertEqual(o3.exit_kind, "TIME_STOP_HIT")


class TestSlConvergenceDecoupledFromSettlement(_ExitTestBase):
    """HARDENING core regression: a filled position MUST converge to a live
    protective SL within a bounded number of reconcile passes, regardless of
    whether the entry ladder ever settles (cancel-ack timing) and regardless
    of a transient SL rejection.

    On the base implementation the attach is gated on entry_phase_settled, so
    a plan whose cancel never acks loops forever in CANCEL_UNFILLED and never
    gets an SL — these tests fail on the base and pass after the decoupling.
    """

    def _seed_partial_ladder(self) -> tuple[int, str]:
        """Tier 0 FILLED (14 shares), tier 1 SUBMITTED (cheaper, unfilled)."""
        plan_id = _seed_plan(self.ledger)
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e0",
            tier_index=0,
            qty=14,
            limit=100.0,
            status="FILLED",
            filled_qty=14,
            filled_price=99.5,
        )
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            tier_index=1,
            qty=13,
            limit=95.0,
            status="SUBMITTED",
        )
        return plan_id, "e1"

    def _live_sl_count(self) -> int:
        with open_ledger(self.ledger) as conn:
            rows = conn.execute("SELECT status FROM orders WHERE order_kind = 'SL'").fetchall()
        terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        return sum(1 for r in rows if r["status"] not in terminal)

    def test_sl_converges_even_when_cancel_never_acks(self):
        """The cancel request goes out but the entry tier NEVER transitions to
        CANCELED (broker never acks). The SL must still be attached, sized to
        the filled qty, within a bounded number of passes."""
        plan_id, open_alpaca_id = self._seed_partial_ladder()
        broker = _NoCancelAckBroker()
        # Live broker position is the 14 filled shares.
        broker.position_qty_for["NVDA"] = 14

        live_sl_at = None
        for i in range(1, 6):  # bounded: must converge well within 5 passes
            with open_ledger(self.ledger) as conn:
                process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
            if self._live_sl_count() == 1:
                live_sl_at = i
                break

        self.assertIsNotNone(live_sl_at, "SL never converged within the bounded passes")
        # The cancel for the open tier was still requested (frees the hold).
        self.assertIn(open_alpaca_id, broker.canceled)
        # SL sized to the 14 filled shares, NOT the 27 planned.
        sl = next(s for s in broker.submissions if s["kind"] == "STOP")
        self.assertEqual(sl["qty"], 14)
        self.assertEqual(sl["stop_price"], 80.0)

    def test_sl_not_double_submitted_once_live(self):
        """After the SL is live, further passes must NOT submit a second SL
        (idempotency under the decoupled convergence rule)."""
        plan_id, _ = self._seed_partial_ladder()
        broker = _NoCancelAckBroker()
        for _ in range(4):
            with open_ledger(self.ledger) as conn:
                process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        n_stop = len([s for s in broker.submissions if s["kind"] == "STOP"])
        self.assertEqual(n_stop, 1, "decoupled convergence double-submitted the SL")


class TestTpWithoutSlClosed(_ExitTestBase):
    """SL-PRIORITY invariant: the protective disaster-stop must always win the
    shares; the opportunistic TP is strictly secondary. When the SL submit is
    rejected this pass, NO TP may be submitted (an accepted TP limit would
    reserve the shares and block the SL forever). Only once the SL is live does
    the TP ladder attach.

    On the base implementation the SL and TP were submitted together regardless
    of whether the SL succeeded, so a rejected SL + accepted TP left the
    position with a take-profit and no disaster stop indefinitely.
    """

    def test_sl_rejected_holds_back_tp_then_both_land_sl_first_next_pass(self):
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
        # Pass 1: SL submit fails (held_for_orders). The TP ladder is HELD BACK
        # so it cannot grab the shares while the SL is missing.
        broker = _FailingExitBroker(fail_first_n_stop=1)
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o1 = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o1.n_exits_failed, 1)
        # No TP submitted (SL-priority invariant), no SL persisted.
        self.assertEqual(o1.n_exits_submitted, 0)
        with open_ledger(self.ledger) as conn:
            kinds = [
                r["order_kind"]
                for r in fetch_orders_for_plan(conn, plan_id)
                if r["order_kind"] in ("SL", "TP")
            ]
        self.assertEqual(kinds.count("TP"), 0)
        self.assertEqual(kinds.count("SL"), 0)
        self.assertEqual(len([s for s in broker.submissions if s["kind"] == "LIMIT"]), 0)
        # The dead-man signal fires: filled shares, no live SL.
        self.assertEqual(o1.n_filled_without_sl, 1)

        # Pass 2: the SL submit now succeeds — convergence lands the SL FIRST,
        # then attaches the TP ladder (both live, SL is the priority leg).
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
            )
        # 1 SL + 2 TP = 3 submits this pass.
        self.assertEqual(o2.n_exits_submitted, 3)
        with open_ledger(self.ledger) as conn:
            sl_rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"]
            tp_rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "TP"]
        self.assertEqual(len(sl_rows), 1)
        self.assertEqual(sl_rows[0]["qty"], 27)
        self.assertEqual(len(tp_rows), 2)
        self.assertEqual(o2.n_filled_without_sl, 0)
        # The SL was submitted BEFORE any TP on this pass (priority ordering).
        pass2_kinds = [s["kind"] for s in broker.submissions]
        self.assertLess(
            pass2_kinds.index("STOP"),
            pass2_kinds.index("LIMIT"),
            "SL must be submitted before any TP",
        )


class TestGapFillSlSizing(_ExitTestBase):
    """HARDENING: if the cheaper tier fills in the race after the cancel
    request, the eventual SL qty == TOTAL observed fills across BOTH tiers,
    not just the first fill."""

    def test_sl_sizes_to_total_fills_after_gap_fill(self):
        plan_id = _seed_plan(self.ledger)
        # Tier 0 filled 14; tier 1 still SUBMITTED at pass 1.
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e0",
            tier_index=0,
            qty=14,
            limit=100.0,
            status="FILLED",
            filled_qty=14,
            filled_price=99.5,
        )
        e1_id = _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            tier_index=1,
            qty=13,
            limit=95.0,
            status="SUBMITTED",
        )
        broker = _NoCancelAckBroker()
        # Pass 1: only the 14 shares of tier 0 are filled; the live position
        # is 14, so the SL lands at 14.
        broker.position_qty_for["NVDA"] = 14
        with open_ledger(self.ledger) as conn:
            process_plan_exit(conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED)
        first_sl = [s for s in broker.submissions if s["kind"] == "STOP"]
        self.assertEqual([s["qty"] for s in first_sl], [14])

        # The cheaper tier fills in the race AFTER the cancel request: the
        # cancel lost, tier 1 got 13 more shares (total 27). The live position
        # is now 27 but the SL only covers 14 — under-protected.
        with open_ledger(self.ledger) as conn:
            insert_fill(
                conn,
                order_id=e1_id,
                alpaca_fill_id="e1-gap-fill",
                qty=13,
                price=94.5,
                filled_at=dt.datetime.now(dt.UTC),
            )
            update_order_status(conn, order_id=e1_id, status="FILLED")
        broker.position_qty_for["NVDA"] = 27

        # Subsequent passes re-converge: cancel the under-sized 14-SL + submit
        # a 27-SL so the eventual coverage equals total fills across BOTH
        # tiers, not just the first.
        for _ in range(4):
            with open_ledger(self.ledger) as conn:
                process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        stops = [s["qty"] for s in broker.submissions if s["kind"] == "STOP"]
        # The under-sized 14-SL was re-sized up to 27 exactly once (no thrash).
        self.assertEqual(stops, [14, 27], f"expected one resize 14 -> 27, got {stops}")
        # The under-sized SL's Alpaca id was requested for cancel.
        with open_ledger(self.ledger) as conn:
            sl_rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"]
        self.assertEqual(len(sl_rows), 2)
        self.assertTrue(any(r["alpaca_order_id"] in broker.canceled for r in sl_rows))


class TestTimeStopSizedToLivePosition(_ExitTestBase):
    """HARDENING/Perplexity gotcha: the TIME_STOP market-sell must be sized to
    the LIVE broker position (post partial-TP), never the planned qty — else
    it over-sells / re-triggers insufficient-qty after a partial TP."""

    def test_time_stop_uses_broker_position_after_partial_tp(self):
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
        # A partial TP already executed 13 shares: live position is 14.
        self.client.position_qty_for["NVDA"] = 14

        # Pass 1 attaches exits while the fill is recent (not yet aged).
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        # Then age the fill past the time-stop deadline.
        ancient = _FIRST_FILL_AT_FIXED
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (ancient.isoformat(), plan_id),
            )
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "TIME_STOP")
        market = [s for s in self.client.submissions if s["kind"] == "MARKET"]
        self.assertEqual(market[0]["qty"], 14, "TIME_STOP must size to live position, not planned")


class TestFilledWithoutSlVisibility(_ExitTestBase):
    """HARDENING visibility: an exit submit rejection is counted in
    ExitOutcome.n_exits_failed, the batch survives, and the same plan retries
    next pass."""

    def test_sl_rejection_counted_and_batch_survives_then_retries(self):
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
        broker = _FailingExitBroker(fail_kinds={"STOP"})
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        # SL rejected -> counted, did not raise. SL-PRIORITY: no TP submitted.
        self.assertEqual(o.n_exits_failed, 1)
        self.assertEqual(o.n_exits_submitted, 0)
        self.assertEqual(o.n_filled_without_sl, 1)
        # Nothing persisted -> retryable. A healthy broker lands the SL FIRST
        # then the TP ladder next pass (1 SL + 2 TP = 3 submits).
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.n_exits_submitted, 3)
        self.assertEqual(o2.n_filled_without_sl, 0)
        with open_ledger(self.ledger) as conn:
            sl = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"]
        self.assertEqual(len(sl), 1)


class TestZeroFillStaysUnfilledNoConvergence(_ExitTestBase):
    """HARDENING regression: a zero-fill plan must NOT trigger the SL
    convergence rule (no position to protect) — no cancel, no SL."""

    def test_zero_fill_plan_no_cancel_no_sl(self):
        plan_id = _seed_plan(self.ledger)
        _add_entry(self.ledger, plan_id=plan_id, alpaca_id="e1", qty=27, status="SUBMITTED")
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "NOOP")
        self.assertEqual(len(self.client.canceled), 0)
        self.assertEqual(len(self.client.submissions), 0)


class TestConvergenceIdempotentNoEvent(_ExitTestBase):
    """HARDENING idempotency: two consecutive no-event passes on a plan that
    already has a live SL create ZERO new orders."""

    def test_two_noevent_passes_create_no_orders(self):
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
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        n_sub = len(self.client.submissions)
        n_can = len(self.client.canceled)
        for _ in range(2):
            with open_ledger(self.ledger) as conn:
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
                )
            self.assertEqual(o.action, "NOOP")
        self.assertEqual(len(self.client.submissions), n_sub)
        self.assertEqual(len(self.client.canceled), n_can)


class TestSlCoverageCountsRemainingOpenQty(_ExitTestBase):
    """LOW: ``sl_coverage_qty`` must count the REMAINING open qty of a
    partially-filled LIVE SL (``qty - filled``), not the original ``qty`` —
    else a partly-consumed SL overstates remaining coverage and delays
    re-convergence on the still-open shares."""

    def test_partially_filled_live_sl_counts_remaining_only(self):
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
        # A live SL of qty 27 that has already partial-filled 10 shares
        # (PARTIALLY_FILLED). Remaining open coverage = 27 - 10 = 17.
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            sl_oid = insert_order(
                conn,
                plan_id=plan_id,
                alpaca_order_id="sl-partial-live",
                side="SELL",
                order_kind="SL",
                order_type="STOP",
                qty=27,
                stop_price=80.0,
                time_in_force="gtc",
                submitted_at=ts,
                status="PARTIALLY_FILLED",
            )
            insert_fill(
                conn,
                order_id=sl_oid,
                alpaca_fill_id="sl-partial-live-fill",
                qty=10,
                price=79.5,
                filled_at=ts,
            )
            snap = _snapshot(conn, plan_id)
        # Remaining open qty (17), NOT the original 27.
        self.assertEqual(snap.sl_coverage_qty, 17)


class _PositionReadFailsBroker(_StubBrokerClient):
    """Stub broker whose ``get_position`` ALWAYS raises (Alpaca read error /
    timeout). Forces ``_position_qty`` down its fallback path so we can assert
    the fallback sizes to the live ledger position, not the gross entry total.
    """

    def get_position(self, symbol: str):
        raise RuntimeError("stub APIError: position read timed out")


class _PositionReadNoneBroker(_StubBrokerClient):
    """Stub broker whose ``get_position`` returns None (no live broker position
    reported). Same fallback path as :class:`_PositionReadFailsBroker`."""

    def get_position(self, symbol: str):
        return None


class TestPositionQtyFallbackUsesNetOpen(_ExitTestBase):
    """HIGH (oversell risk): when the live broker position read fails / returns
    None, the SL fallback size MUST be ``net_open_qty`` (entry fills minus exit
    fills already SOLD), NOT the gross ``total_entry_filled_qty``. Sizing to
    gross entry fills after a partial TP already sold shares oversizes the SL ->
    persistent insufficient_qty rejection + oversell-to-short risk.

    Scenario: 27 shares entered, a 13-share TP already FILLED -> 14 net held.
    The broker position read fails, so the convergence SL must size to 14.
    """

    def _seed_filled_with_partial_tp_sold(self) -> int:
        """27 entered + 1 TP order recorded as FILLED for 13 -> net 14 held."""
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
        # Record a TP exit order that already SOLD 13 shares (partial TP).
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            tp_oid = insert_order(
                conn,
                plan_id=plan_id,
                alpaca_order_id="tp-sold",
                side="SELL",
                order_kind="TP",
                tranche_index=0,
                order_type="LIMIT",
                qty=13,
                limit_price=110.0,
                time_in_force="gtc",
                submitted_at=ts,
                status="FILLED",
            )
            insert_fill(
                conn,
                order_id=tp_oid,
                alpaca_fill_id="tp-sold-fill",
                qty=13,
                price=110.0,
                filled_at=ts,
            )
        return plan_id

    def _live_sl_qty(self, plan_id: int) -> list[int]:
        with open_ledger(self.ledger) as conn:
            rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"]
        return [int(r["qty"]) for r in rows]

    def test_fallback_on_read_error_sizes_sl_to_net_open_not_gross(self):
        plan_id = self._seed_filled_with_partial_tp_sold()
        broker = _PositionReadFailsBroker()
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o.action, "CONVERGE_SL")
        # SL sized to net_open (27 - 13 = 14), NOT the gross 27 entry fills.
        self.assertEqual(
            self._live_sl_qty(plan_id),
            [14],
            "SL fallback oversized to gross entry fills (oversell-to-short risk)",
        )

    def test_fallback_on_none_position_sizes_sl_to_net_open_not_gross(self):
        plan_id = self._seed_filled_with_partial_tp_sold()
        broker = _PositionReadNoneBroker()
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "CONVERGE_SL")
        self.assertEqual(
            self._live_sl_qty(plan_id),
            [14],
            "SL fallback oversized to gross entry fills (oversell-to-short risk)",
        )


class TestRejectedTimeStopRetriesNoOscillation(_ExitTestBase):
    """HIGH: a TIME_STOP that is submitted then REJECTED (terminal, 0 fill) on
    a past-deadline plan must be RETRIED on subsequent passes — not permanently
    suppressed by the bare 'a TIME_STOP exists' guard. AND the SL-convergence
    branch must NOT arm a competing SL each pass once the plan is in time-stop
    territory (which would churn cancel/re-submit every pass and risk oversell).

    Failure on the base code:
      - ``_time_stop_should_fire`` counts ANY TIME_STOP incl. the terminal
        REJECTED one -> returns False forever -> liquidation never retried
        (the position is stranded with no live sell at all).
      - If (a) alone is applied without the interaction guard, a single pass
        would arm a fresh SL (convergence) and then re-fire the time-stop which
        cancels it and re-submits -> oscillation, churn, transient double sell.
    """

    def _seed_filled_recent_plan(self) -> int:
        """27 shares filled with a RECENT fill (pass 1 arms the SL); call
        ``_age_plan`` after to push the fill past the time-stop deadline."""
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
        return plan_id

    def _age_plan(self, plan_id: int) -> None:
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (_FIRST_FILL_AT_FIXED.isoformat(), plan_id),
            )

    def _live_sells(self, plan_id: int) -> int:
        """Count of NON-TERMINAL sell orders (SL / TP / TIME_STOP)."""
        terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        with open_ledger(self.ledger) as conn:
            rows = conn.execute(
                "SELECT status FROM orders WHERE plan_id = ? AND side = 'SELL'",
                (plan_id,),
            ).fetchall()
        return sum(1 for r in rows if r["status"] not in terminal)

    def _live_sell_qty(self, plan_id: int) -> int:
        terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        with open_ledger(self.ledger) as conn:
            rows = conn.execute(
                "SELECT qty, status FROM orders WHERE plan_id = ? AND side = 'SELL'",
                (plan_id,),
            ).fetchall()
        return sum(int(r["qty"]) for r in rows if r["status"] not in terminal)

    def _reject_open_time_stops(self, plan_id: int) -> None:
        """Model the reconciler polling the just-submitted TIME_STOP to a
        terminal REJECTED with 0 fill (e.g. Alpaca rejected the market sell)."""
        with open_ledger(self.ledger) as conn:
            rows = fetch_orders_for_plan(conn, plan_id)
            for o in rows:
                if o["order_kind"] == "TIME_STOP" and o["status"] == "SUBMITTED":
                    update_order_status(conn, order_id=int(o["order_id"]), status="REJECTED")

    def _cancel_open_sl_tp(self, plan_id: int) -> None:
        """Model the reconciler polling the time-stop-cancelled SL/TPs to
        terminal CANCELED with 0 fill."""
        with open_ledger(self.ledger) as conn:
            rows = fetch_orders_for_plan(conn, plan_id)
            for o in rows:
                if o["order_kind"] in ("SL", "TP") and o["status"] == "SUBMITTED":
                    update_order_status(conn, order_id=int(o["order_id"]), status="CANCELED")

    def test_rejected_time_stop_is_retried_and_no_competing_sl(self):
        plan_id = self._seed_filled_recent_plan()
        self.client.position_qty_for["NVDA"] = 27

        # Pass 1: attach — one live SL covers all 27 (fill not yet aged).
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "CONVERGE_SL")

        # Age past the deadline so the time-stop fires from pass 2 on.
        self._age_plan(plan_id)

        # Pass 2: time-stop fires — submits the TIME_STOP, cancels SL + TPs.
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "TIME_STOP")
        # Reconciler: the SL/TPs go terminal CANCELED, the TIME_STOP is REJECTED.
        self._cancel_open_sl_tp(plan_id)
        self._reject_open_time_stops(plan_id)
        n_stop_after_pass2 = len([s for s in self.client.submissions if s["kind"] == "MARKET"])
        self.assertEqual(n_stop_after_pass2, 1)

        # Passes 3..6: the rejected TIME_STOP must be RETRIED (a fresh market
        # sell submitted) AND convergence must NOT arm a competing SL.
        retried = False
        for _ in range(4):
            with open_ledger(self.ledger) as conn:
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
                )
            # Each retry submits a fresh TIME_STOP (terminal, so should-fire
            # is True again) — never a CONVERGE_SL that arms an SL.
            if o.action == "TIME_STOP":
                retried = True
                # The just-submitted TIME_STOP gets rejected again next poll.
                self._reject_open_time_stops(plan_id)
            # Safety invariant: at most ONE live sell-side order at any time
            # (the in-flight TIME_STOP between submit and reject), never an SL
            # stacked on top of a TIME_STOP.
            self.assertLessEqual(
                self._live_sells(plan_id),
                1,
                "convergence armed a competing SL alongside the time-stop (churn/oversell)",
            )
            self.assertLessEqual(
                self._live_sell_qty(plan_id),
                27,
                "live sell qty exceeds position qty -> oversell to short",
            )

        self.assertTrue(retried, "rejected TIME_STOP was permanently suppressed, never retried")
        # No SL was ever re-armed after the time-stop deadline passed.
        with open_ledger(self.ledger) as conn:
            sl_rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"]
        # Only the single pass-1 SL ever existed (now terminal CANCELED).
        self.assertEqual(
            len(sl_rows), 1, "convergence armed a fresh SL past the time-stop deadline"
        )


class _ShareReservingBroker(_StubBrokerClient):
    """Faithful model of the two probed Alpaca paper rejections (memory
    ``reference_alpaca_paper_order_behavior_2026_06_02``) that the old stub did
    NOT model, so the TP-blocks-SL defect could never reproduce hermetically.

      WASH-TRADE guard (always modelled): a BARE STOP sell submitted while an
      opposite-side limit BUY is open (symbol in ``open_buy_symbols``) is
      rejected — HTTP 403, code 40310000, "potential wash trade detected ...
      opposite side limit order exists". A LIMIT sell is EXEMPT (it coexists
      with the open BUY). This is the real cause of the prod SL rejection.
      Cancelling the entry tier (``cancel_order``) clears the hold immediately
      (probe: re-submit accepted ~0.30 s after the cancel → same-pass).

      SHARE RESERVATION (opt-in via ``reserve_shares=True``): a resting
      (non-terminal) SELL reserves the position shares, so a SECOND sell whose
      cumulative qty would exceed the live position is rejected with
      ``held_for_orders`` / insufficient-qty until an earlier resting sell is
      canceled. Used to reproduce the live DLB / S state where a resting TP
      limit holds the shares and blocks a fresh disaster-stop SL. Defaults OFF
      so an SL + TP ladder coexist on the happy path (matches the bare-order
      harness, where the protective SL and the TP ladder both rest live).

    ``position_qty_for`` (inherited) bounds the reservable shares.
    """

    def __init__(self, *, reserve_shares: bool = False) -> None:
        super().__init__()
        # Symbols with an open opposite-side limit BUY (unfilled entry tier).
        # While present, a bare STOP sell on that symbol is wash-trade-rejected.
        self.open_buy_symbols: set[str] = set()
        self._reserve_shares = reserve_shares
        # alpaca_order_id -> (symbol, qty) for every resting (non-terminal)
        # SELL order this stub accepted. Cleared on cancel_order.
        self._resting_sells: dict[str, tuple[str, int]] = {}

    def _reserved_qty(self, symbol: str) -> int:
        return sum(q for (s, q) in self._resting_sells.values() if s == symbol)

    def _check_and_reserve(self, *, symbol: str, qty: int, is_stop: bool) -> None:
        """Raise the faithful Alpaca rejection if this sell cannot rest."""
        if is_stop and symbol in self.open_buy_symbols:
            raise RuntimeError(
                '403 Client Error: {"code":40310000,"message":'
                '"potential wash trade detected. opposite side limit order '
                'exists. use complex orders"}'
            )
        if self._reserve_shares:
            position = self.position_qty_for.get(symbol, 27)
            if self._reserved_qty(symbol) + qty > position:
                raise RuntimeError(
                    "stub APIError: held_for_orders insufficient qty available "
                    f"for {symbol} (code 40310000)"
                )

    def submit_stop_order(self, **kwargs):
        symbol = kwargs["symbol"]
        qty = int(kwargs["qty"])
        self._check_and_reserve(symbol=symbol, qty=qty, is_stop=True)
        order = super().submit_stop_order(**kwargs)
        self._resting_sells[order.id] = (symbol, qty)
        return order

    def submit_limit_order(self, **kwargs):
        symbol = kwargs["symbol"]
        qty = int(kwargs["qty"])
        # The exit manager only ever submits SELL limits (TP); a limit sell is
        # exempt from the wash-trade rule but still reserves shares (rule 2).
        self._check_and_reserve(symbol=symbol, qty=qty, is_stop=False)
        order = super().submit_limit_order(**kwargs)
        self._resting_sells[order.id] = (symbol, qty)
        return order

    def cancel_order(self, alpaca_order_id: str) -> None:
        super().cancel_order(alpaca_order_id)
        # Cancelling a resting sell frees its reserved shares immediately
        # (faithful: the broker releases the hold on cancel-ack).
        self._resting_sells.pop(alpaca_order_id, None)


class TestTpBlocksSlReproduction(_ExitTestBase):
    """SL-PRIORITY reproduction of the LIVE defect (DLB / S had a TP and 0 SL,
    never self-healing). With faithful share reservation: a position whose
    entry tiers are still open has its bare STOP SL wash-trade-rejected, while
    a TP limit is exempt and would reserve the shares — permanently blocking
    the SL.

    RED on base: the TP lands, reserves the shares, and the SL is blocked
    forever. GREEN after: NO TP is left live without an SL.
    """

    def _seed_open_entry_with_fill(self) -> tuple[int, str]:
        """A plan with a FILLED tier 0 + a still-open tier 1 (opposite-side
        limit BUY) so the bare STOP SL is wash-trade-rejected."""
        plan_id = _seed_plan(self.ledger)
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e0",
            tier_index=0,
            qty=14,
            limit=100.0,
            status="FILLED",
            filled_qty=14,
            filled_price=99.5,
        )
        _add_entry_tier(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            tier_index=1,
            qty=13,
            limit=95.0,
            status="SUBMITTED",
        )
        return plan_id, "e1"

    def test_open_entry_blocks_sl_but_no_tp_left_without_sl(self):
        plan_id, _open_id = self._seed_open_entry_with_fill()
        broker = _ShareReservingBroker()
        broker.position_qty_for["NVDA"] = 14
        # The cancel for the open entry tier does NOT ack this pass (broker
        # never transitions it), so the opposite-side hold persists and the
        # bare STOP SL is wash-trade-rejected on this pass.
        broker.open_buy_symbols.add("NVDA")

        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o1 = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )

        # The SL was rejected (wash trade) and NO TP was submitted to grab the
        # shares while the SL is missing — the invariant.
        self.assertEqual(o1.n_exits_failed, 1)
        self.assertEqual(o1.n_exits_submitted, 0)
        with open_ledger(self.ledger) as conn:
            rows = [
                r["order_kind"]
                for r in fetch_orders_for_plan(conn, plan_id)
                if r["order_kind"] in ("SL", "TP")
            ]
        self.assertEqual(rows.count("TP"), 0, "a TP was left live without an SL")
        self.assertEqual(rows.count("SL"), 0)
        self.assertEqual(o1.n_filled_without_sl, 1)

        # Next pass: the cancel has now freed the opposite-side hold (entry
        # tier acked CANCELED + the wash-trade condition cleared). The SL lands
        # FIRST, then the TP ladder attaches.
        with open_ledger(self.ledger) as conn:
            for r in fetch_orders_for_plan(conn, plan_id):
                if r["order_kind"] == "ENTRY" and r["status"] == "SUBMITTED":
                    update_order_status(conn, order_id=int(r["order_id"]), status="CANCELED")
        broker.open_buy_symbols.discard("NVDA")

        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
            )
        with open_ledger(self.ledger) as conn:
            sl_rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"]
            tp_rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "TP"]
        self.assertEqual(len(sl_rows), 1)
        self.assertEqual(sl_rows[0]["qty"], 14)
        self.assertGreaterEqual(len(tp_rows), 1)
        self.assertEqual(o2.n_filled_without_sl, 0)


class TestTpYieldsToSl(_ExitTestBase):
    """SL-PRIORITY: a position that already has a LIVE TP but NO SL (the live
    DLB / S state) must converge by CANCELLING the TP so the protective SL can
    land. End state: a live SL, and the invariant (no TP-without-SL) restored.

    With faithful share reservation the resting TP holds all the shares, so the
    SL is rejected with held_for_orders until the TP is canceled first.
    """

    def _seed_filled_with_live_tp_no_sl(self) -> int:
        """Seed a plan with 27 filled shares and NO SL. The caller seeds the
        live TP limits inline (the broken live DLB/S state: a resting TP holds
        the position while no SL covers it)."""
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
        return plan_id

    def test_live_tp_without_sl_is_canceled_so_sl_can_land(self):
        plan_id = self._seed_filled_with_live_tp_no_sl()
        broker = _ShareReservingBroker(reserve_shares=True)
        broker.position_qty_for["NVDA"] = 27
        # Pre-seed two LIVE TP limits at the broker AND in the ledger (the
        # broken live state: a TP holds all 27 shares, no SL).
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            for idx, (q, px) in enumerate([(13, 110.0), (14, 130.0)]):
                tp = broker.submit_limit_order(
                    symbol="NVDA", qty=q, limit_price=px, side="sell", time_in_force="gtc"
                )
                insert_order(
                    conn,
                    plan_id=plan_id,
                    alpaca_order_id=str(tp.id),
                    side="SELL",
                    order_kind="TP",
                    tranche_index=idx,
                    order_type="LIMIT",
                    qty=q,
                    limit_price=px,
                    time_in_force="gtc",
                    submitted_at=ts,
                    status="SUBMITTED",
                )
        # Sanity: all 27 shares are reserved by the resting TPs, so a fresh SL
        # would be rejected without cancelling them first.
        self.assertEqual(broker._reserved_qty("NVDA"), 27)

        # Pass 1: convergence must cancel the live TP(s) so the SL can land.
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "CONVERGE_SL")
        # The two live TPs were requested for cancel (yielding the shares).
        self.assertEqual(len(broker.canceled), 2)
        # Cancelling the TPs freed the shares, so the SL landed THIS pass.
        with open_ledger(self.ledger) as conn:
            sl_rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"]
        self.assertEqual(len(sl_rows), 1)
        self.assertEqual(sl_rows[0]["qty"], 27)
        self.assertEqual(o1.n_filled_without_sl, 0)


class TestHappyPathSlBeforeTp(_ExitTestBase):
    """SL-PRIORITY happy path: with freed shares (fully-filled ladder, no
    opposite-side hold), the SL lands FIRST and the TP ladder attaches after,
    both live, in that order."""

    def test_freed_shares_sl_lands_first_then_tp(self):
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
        broker = _ShareReservingBroker()
        broker.position_qty_for["NVDA"] = 27
        # No open opposite-side BUY — shares are freed.

        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "CONVERGE_SL")
        self.assertEqual(o.n_exits_submitted, 3)  # 1 SL + 2 TP
        kinds = [s["kind"] for s in broker.submissions]
        self.assertEqual(kinds.count("STOP"), 1)
        self.assertEqual(kinds.count("LIMIT"), 2)
        # SL submitted before any TP.
        self.assertLess(
            kinds.index("STOP"),
            kinds.index("LIMIT"),
            "SL must be submitted before any TP on the happy path",
        )
        self.assertEqual(o.n_filled_without_sl, 0)


if __name__ == "__main__":
    unittest.main()
