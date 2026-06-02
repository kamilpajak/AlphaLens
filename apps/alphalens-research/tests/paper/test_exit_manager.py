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

from alphalens_pipeline.paper.exit_manager import (
    _classify_exit_kind,
    _PlanSnapshot,
    _RowLike,
    process_plan_exit,
)
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
    """Records every submission + cancellation; returns sequential ids.

    Implements the broker-neutral ``attach_exit_ladder`` OCO-ladder primitive
    (the attach-once exit path) plus ``submit_stop_order`` (the NO_STRUCTURE
    fallback stop). ``attach_exit_ladder`` returns one
    :class:`ExitLadderLeg` per tranche with fake per-tranche tp/sl ids; the
    submitted tranches are recorded so tests can assert qty split. Set
    ``fail_attach_ladder = True`` to simulate a mid-ladder attach failure
    (the adapter is all-or-nothing, so a failure leaves nothing live + nothing
    persisted).
    """

    def __init__(self) -> None:
        self.submissions: list[dict] = []
        self.canceled: list[str] = []
        self._next = 1
        # ticker → live position qty. Default 27 (matches full-fill setup).
        self.position_qty_for: dict[str, int] = {}
        # Captured SUCCESSFUL attach_exit_ladder calls: (symbol, tranches, stop).
        self.ladder_attaches: list[dict] = []
        # Every attach_exit_ladder INVOCATION (incl. ones that raise).
        self.ladder_attempts = 0
        # When True, attach_exit_ladder raises (mid-ladder failure model).
        self.fail_attach_ladder = False

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

    def attach_exit_ladder(self, *, symbol, tranches, stop_price, time_in_force="gtc"):
        from alphalens_pipeline.paper.broker import ExitLadderLeg

        self.ladder_attempts += 1
        if self.fail_attach_ladder:
            raise RuntimeError("stub: attach_exit_ladder failed mid-ladder (all-or-nothing)")
        self.submissions.append(
            {
                "kind": "LADDER",
                "symbol": symbol,
                "stop_price": stop_price,
                "tranche_qtys": [t.qty for t in tranches],
            }
        )
        self.ladder_attaches.append(
            {"symbol": symbol, "tranches": list(tranches), "stop_price": stop_price}
        )
        legs: list[ExitLadderLeg] = []
        for i, tr in enumerate(tranches):
            tp_id = f"tp-{self._next:03d}"
            sl_id = f"sl-{self._next:03d}"
            self._next += 1
            legs.append(
                ExitLadderLeg(
                    tranche_index=i,
                    qty=tr.qty,
                    take_profit_limit=tr.take_profit_limit,
                    stop_price=stop_price,
                    tp_order_id=tp_id,
                    sl_order_id=sl_id,
                )
            )
        return legs

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

    def ladder_call_count(self) -> int:
        return len(self.ladder_attaches)


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


class TestAttachOnceOcoLadder(_ExitTestBase):
    """ATTACH-ONCE OCO-ladder exit path (PR-4). The protective exit is attached
    ONCE on the filled aggregate via ``broker.attach_exit_ladder`` (M TP+SL OCO
    pairs sharing one disaster stop), persisted via ``record_exit_ladder``."""

    def _ladder_rows(self, plan_id: int):
        with open_ledger(self.ledger) as conn:
            return [
                dict(r)
                for r in fetch_orders_for_plan(conn, plan_id)
                if r["order_kind"] in ("TP", "SL")
            ]

    def test_attach_once_records_ladder_on_first_fill(self):
        # Given a fully-filled 27-share aggregate + a 2-tranche plan.
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

        # attach_exit_ladder called exactly once; tranches sum to 27.
        self.assertEqual(outcome.action, "ATTACHED")
        self.assertEqual(self.client.ladder_call_count(), 1)
        attach = self.client.ladder_attaches[0]
        self.assertEqual(sum(t.qty for t in attach["tranches"]), 27)
        self.assertEqual(sorted(t.qty for t in attach["tranches"]), [13, 14])
        self.assertEqual(attach["stop_price"], 80.0)

        # record_exit_ladder persisted 2 TP + 2 SL rows; each pair shares an
        # exit_group_id (the tp_order_id).
        rows = self._ladder_rows(plan_id)
        tp_rows = [r for r in rows if r["order_kind"] == "TP"]
        sl_rows = [r for r in rows if r["order_kind"] == "SL"]
        self.assertEqual(len(tp_rows), 2)
        self.assertEqual(len(sl_rows), 2)
        # n_exits_submitted = 2 * len(legs) = 4.
        self.assertEqual(outcome.n_exits_submitted, 4)
        # Each TP+SL pair shares one exit_group_id; 2 distinct groups overall.
        group_ids = {r["exit_group_id"] for r in rows}
        self.assertEqual(len(group_ids), 2)
        for r in rows:
            self.assertIsNotNone(r["exit_group_id"])

    def test_attach_is_idempotent_second_pass_noop(self):
        """After a ladder is recorded, a 2nd pass does NOT call
        attach_exit_ladder again — has_exit_ladder gates it off (no
        IntegrityError, falls through)."""
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
        # Only ONE ladder attach across both passes.
        self.assertEqual(self.client.ladder_call_count(), 1)

    def test_whole_share_residue_allocation(self):
        # 27 across [50, 50] → [13, 14]; the last tranche absorbs the residue.
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
        qtys = [t.qty for t in self.client.ladder_attaches[0]["tranches"]]
        self.assertEqual(sum(qtys), 27)
        self.assertEqual(qtys, [13, 14])

        # 10 across [60, 40] → [6, 4]; sum == 10, no fractional ExitTranche.
        plan2 = _seed_plan(
            self.ledger,
            ticker="AMD",
            tp_tranches=[(0, 110.0, 60.0, 1.0, "tp-1"), (1, 130.0, 40.0, 3.0, "tp-2")],
        )
        _add_entry(
            self.ledger,
            plan_id=plan2,
            alpaca_id="e2",
            qty=10,
            status="FILLED",
            filled_qty=10,
            filled_price=99.5,
        )
        self.client.position_qty_for["AMD"] = 10
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan2, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        amd_attach = next(a for a in self.client.ladder_attaches if a["symbol"] == "AMD")
        amd_qtys = [t.qty for t in amd_attach["tranches"]]
        self.assertEqual(sum(amd_qtys), 10)
        self.assertEqual(amd_qtys, [6, 4])

    def test_partial_entry_fill_then_cancel_attaches_on_aggregate(self):
        """Only 10 of 27 entry shares filled before the cheaper tier was
        cancelled (#401) — the ladder is sized to the live 10 held, not 27."""
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
        self.client.position_qty_for["NVDA"] = 10

        with open_ledger(self.ledger) as conn:
            outcome = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )

        self.assertEqual(outcome.action, "ATTACHED")
        attach = self.client.ladder_attaches[0]
        # Sized to the live 10 shares, NOT the planned 27.
        self.assertEqual(sum(t.qty for t in attach["tranches"]), 10)

    def test_attach_failure_persists_nothing_and_retries(self):
        """broker.attach_exit_ladder raises → no ledger rows, has_exit_ladder
        False, n_exits_failed > 0, n_filled_without_sl == 1; the next pass
        re-attaches (ladder call count goes 1 → 2)."""
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
        self.client.fail_attach_ladder = True
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o1 = process_plan_exit(
                    conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o1.action, "ATTACHED")
        self.assertEqual(o1.n_exits_submitted, 0)
        self.assertGreater(o1.n_exits_failed, 0)
        self.assertEqual(o1.n_filled_without_sl, 1)
        # Nothing persisted.
        with open_ledger(self.ledger) as conn:
            rows = [
                r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] in ("TP", "SL")
            ]
        self.assertEqual(rows, [])

        # The first pass ATTEMPTED an attach (raised); no ladder recorded yet.
        self.assertEqual(self.client.ladder_attempts, 1)
        self.assertEqual(self.client.ladder_call_count(), 0)

        # Pass 2 with a healthy broker re-attaches (attempt 1 → 2 total).
        self.client.fail_attach_ladder = False
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "ATTACHED")
        self.assertEqual(self.client.ladder_attempts, 2)
        self.assertEqual(self.client.ladder_call_count(), 1)
        self.assertEqual(o2.n_filled_without_sl, 0)


class TestNoStructureFallbackStop(_ExitTestBase):
    """NO_STRUCTURE / defense-in-depth: a filled position whose plan has NO TP
    tranches still gets a single full-size protective stop (with a non-NULL
    exit_group_id so it is attach-once)."""

    def test_no_structure_empty_ladder_still_protected(self):
        plan_id = _seed_plan(self.ledger, tp_tranches=[])
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
        self.assertEqual(o1.action, "ATTACHED")
        # The OCO ladder was NOT used (no tranches); a single fallback STOP was.
        self.assertEqual(self.client.ladder_call_count(), 0)
        self.assertEqual(o1.n_exits_submitted, 1)
        with open_ledger(self.ledger) as conn:
            sl_rows = [
                dict(r) for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"
            ]
        self.assertEqual(len(sl_rows), 1)
        self.assertEqual(sl_rows[0]["qty"], 27)
        # Fallback stop carries a NON-NULL exit_group_id (attach-once).
        self.assertIsNotNone(sl_rows[0]["exit_group_id"])

        # Pass 2: has_exit_ladder is True → no duplicate stop.
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "NOOP")
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
    """OCO-ladder exit closure: the attached ladder is 2 OCO groups (2 TP rows
    + 2 SL rows). When one side of a group fills, the broker auto-cancels its
    sibling; the reconciler polls those terminal transitions, then the
    quantity-weighted classifier writes the outcome."""

    def _attach_and_lookup(self, *, plan_id: int, entry_qty: int, entry_price: float):
        """Attach the OCO ladder, then return (sl_ids, tp_ids) — lists, since
        the ladder produces one SL + one TP per tranche."""
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
        sl_ids = [int(o["order_id"]) for o in orders if o["order_kind"] == "SL"]
        tp_ids = [int(o["order_id"]) for o in orders if o["order_kind"] == "TP"]
        return sl_ids, tp_ids

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

    def _mark_status(self, order_id: int, status: str) -> None:
        with open_ledger(self.ledger) as conn:
            update_order_status(conn, order_id=order_id, status=status)

    def test_oco_sibling_auto_cancel_classifies_tp_hit(self):
        # Both TP legs fill at their targets; the broker auto-CANCELS the
        # sibling SL legs (OCO). All exits terminal → quantity-weighted TP_HIT.
        plan_id = _seed_plan(self.ledger)
        sl_ids, tp_ids = self._attach_and_lookup(plan_id=plan_id, entry_qty=27, entry_price=100.0)
        self._mark_filled(tp_ids[0], qty=13, price=110.0)
        self._mark_filled(tp_ids[1], qty=14, price=130.0)
        # OCO sibling auto-cancel: the SL legs go terminal CANCELED, 0 fill.
        for sl_id in sl_ids:
            self._mark_status(sl_id, "CANCELED")

        with open_ledger(self.ledger) as conn:
            outcome = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )

        self.assertEqual(outcome.action, "CLOSED")
        self.assertEqual(outcome.exit_kind, "TP_HIT")
        self.assertEqual(outcome.n_exits_failed, 0)
        self.assertEqual(outcome.n_ledger_broker_desync, 0)

        with open_ledger(self.ledger) as conn:
            row = conn.execute(
                "SELECT blended_entry_price, blended_exit_price, realized_r_multiple "
                "FROM plan_outcomes WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        self.assertAlmostEqual(row["blended_entry_price"], 100.0)
        self.assertAlmostEqual(row["blended_exit_price"], (13 * 110 + 14 * 130) / 27, places=4)
        self.assertGreater(row["realized_r_multiple"], 0)

    def test_oco_sl_leg_fills_classifies_sl_hit(self):
        # Both SL legs fill (the disaster stop caught the whole position); the
        # sibling TP legs auto-cancel. broker-flat-with-FILLED-SL must NOT
        # write RECONCILED_FLAT (exit_orders present disables the #404 guard).
        plan_id = _seed_plan(self.ledger)
        sl_ids, tp_ids = self._attach_and_lookup(plan_id=plan_id, entry_qty=27, entry_price=100.0)
        # The position is now flat at the broker (both stops fired).
        self.client.position_qty_for["NVDA"] = 0
        self._mark_filled(sl_ids[0], qty=13, price=79.5)
        self._mark_filled(sl_ids[1], qty=14, price=79.5)
        for tp_id in tp_ids:
            self._mark_status(tp_id, "CANCELED")

        with open_ledger(self.ledger) as conn:
            outcome = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(outcome.action, "CLOSED")
        self.assertEqual(outcome.exit_kind, "SL_HIT")
        # NOT misclassified as a phantom-position desync.
        self.assertEqual(outcome.n_ledger_broker_desync, 0)
        with open_ledger(self.ledger) as conn:
            row = conn.execute(
                "SELECT exit_kind, realized_r_multiple FROM plan_outcomes WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        self.assertNotEqual(row["exit_kind"], "RECONCILED_FLAT")
        self.assertAlmostEqual(row["realized_r_multiple"], (79.5 - 100) / (100 - 80))

    def test_sl_fill_cancels_open_sibling_tps_before_terminal(self):
        # When an SL leg fills but the sibling TPs are still SUBMITTED locally
        # (reconciler has not yet polled the auto-cancel), the sl_fired branch
        # issues cancel requests for the open exits; the pass is NOOP until the
        # cancels are observed, then closes SL_HIT.
        plan_id = _seed_plan(self.ledger)
        sl_ids, tp_ids = self._attach_and_lookup(plan_id=plan_id, entry_qty=27, entry_price=100.0)
        self._mark_filled(sl_ids[0], qty=13, price=79.5)
        self._mark_filled(sl_ids[1], qty=14, price=79.5)
        # TPs still SUBMITTED → not all-terminal → NOOP + cancel requests issued.
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "NOOP")
        # The two open TP legs were requested for cancel.
        self.assertEqual(len(self.client.canceled), 2)

        for tp_id in tp_ids:
            self._mark_status(tp_id, "CANCELED")
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "CLOSED")
        self.assertEqual(o2.exit_kind, "SL_HIT")


class TestZenRegressions(_ExitTestBase):
    """Regression tests for issues found in the post-PR #277 zen review."""

    def _attach_and_simulate_exits(self, *, plan_id: int, entry_qty: int, entry_price: float):
        """Attach the OCO ladder then return ([sl_ids], [tp_ids])."""
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
        sl_ids = [int(o["order_id"]) for o in orders if o["order_kind"] == "SL"]
        tp_ids = [int(o["order_id"]) for o in orders if o["order_kind"] == "TP"]
        return sl_ids, tp_ids

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
        """Critical bug zen flagged: an SL leg can PARTIAL-fill remaining
        inventory before Alpaca short-prevents and cancels the rest. SL status
        becomes CANCELED, NOT FILLED. The classifier reads
        ``filled_qty_observed`` (not ``status == 'FILLED'``) so the partial
        SL fill still counts — quantity-weighted, qty_sl (14) >= qty_tp (13)
        → SL_HIT. No state-machine lockup."""
        plan_id = _seed_plan(self.ledger)
        sl_ids, tp_ids = self._attach_and_simulate_exits(
            plan_id=plan_id, entry_qty=27, entry_price=100.0
        )
        # TP tranche #0 fills (13 @ 110).
        self._mark_filled(tp_ids[0], qty=13, price=110.0)
        # SL leg #0 partial-fires for the remaining 14 then Alpaca cancels it.
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            insert_fill(
                conn,
                order_id=sl_ids[0],
                alpaca_fill_id="sl-partial",
                qty=14,
                price=79.5,
                filled_at=ts,
            )
            update_order_status(conn, order_id=sl_ids[0], status="CANCELED")

        # Pass: sl_fired (filled_qty > 0) triggers cancel for the open siblings.
        with open_ledger(self.ledger) as conn:
            process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
            tp2_row = conn.execute(
                "SELECT alpaca_order_id FROM orders WHERE order_id = ?", (tp_ids[1],)
            ).fetchone()
        # The still-open TP tranche #1 cancel was requested at Alpaca.
        self.assertIn(tp2_row["alpaca_order_id"], self.client.canceled)

        # Simulate reconciler picking up CANCELED on the open sibling legs.
        with open_ledger(self.ledger) as conn:
            update_order_status(conn, order_id=tp_ids[1], status="CANCELED")
            update_order_status(conn, order_id=sl_ids[1], status="CANCELED")

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
        sl_ids, _tp_ids = self._attach_and_simulate_exits(
            plan_id=plan_id, entry_qty=27, entry_price=100.0
        )
        # An SL leg fills, triggering cancel for the open sibling TPs.
        self._mark_filled(sl_ids[0], qty=27, price=79.5)
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
        self.assertEqual(o1.action, "ATTACHED")
        self.assertEqual(o1.n_entries_canceled, 1)
        self.assertIn(open_alpaca_id, self.client.canceled)
        # Ladder sized to the 14 filled shares (broker position); cheaper tier
        # abandoned. The disaster_stop is shared across the OCO groups.
        attach = self.client.ladder_attaches[0]
        self.assertEqual(sum(t.qty for t in attach["tranches"]), 14)
        self.assertEqual(attach["stop_price"], 80.0)
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
        self.assertEqual(o.action, "ATTACHED")
        self.assertEqual(len(self.client.canceled), 0)
        # Ladder covers the full 27 filled shares.
        attach = self.client.ladder_attaches[0]
        self.assertEqual(sum(t.qty for t in attach["tranches"]), 27)

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
    """A broker error on the OCO-ladder attach (e.g. Alpaca held_for_orders /
    wash-trade 403 surfacing through the adapter's all-or-nothing wrapper) is
    caught + counted + does NOT propagate, so one bad plan never aborts the
    reconcile pass for plans behind it. The adapter leaves nothing live at the
    broker and the exit_manager persists nothing → retryable next pass."""

    def _seed_filled(self) -> int:
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
        return plan_id

    def test_attach_failure_is_caught_and_counted_not_raised(self):
        plan_id = self._seed_filled()
        self.client.fail_attach_ladder = True
        with open_ledger(self.ledger) as conn:
            # Must NOT raise.
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o.action, "ATTACHED")
        self.assertEqual(o.n_exits_submitted, 0)
        self.assertEqual(o.n_exits_failed, 1)
        # Nothing persisted.
        with open_ledger(self.ledger) as conn:
            kinds = [
                r["order_kind"]
                for r in fetch_orders_for_plan(conn, plan_id)
                if r["order_kind"] in ("SL", "TP")
            ]
        self.assertEqual(kinds, [])
        # The dead-man signal: filled shares with no protective ladder.
        self.assertEqual(o.n_filled_without_sl, 1)

    def test_attach_real_alpaca_wash_trade_403_is_caught_and_counted(self):
        """Fidelity: the ACTUAL production rejection (HTTP 403, code 40310000,
        "potential wash trade detected ... opposite side limit order exists")
        surfacing through the OCO attach is swallowed + counted, never raised.
        The adapter's all-or-nothing rollback means nothing is live; the
        exit_manager persists nothing → retryable once the hold clears."""

        class _WashTradeAttachBroker(_StubBrokerClient):
            def attach_exit_ladder(self, **kwargs):
                self.ladder_attempts += 1
                raise RuntimeError(
                    '403 Client Error: {"code":40310000,"message":'
                    '"potential wash trade detected. opposite side limit order '
                    'exists. use complex orders"}'
                )

        plan_id = self._seed_filled()
        broker = _WashTradeAttachBroker()
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o.action, "ATTACHED")
        self.assertEqual(o.n_exits_failed, 1)
        self.assertEqual(o.n_exits_submitted, 0)
        with open_ledger(self.ledger) as conn:
            kinds = [
                r["order_kind"]
                for r in fetch_orders_for_plan(conn, plan_id)
                if r["order_kind"] in ("SL", "TP")
            ]
        self.assertEqual(kinds, [])
        self.assertEqual(o.n_filled_without_sl, 1)

    def test_broker_ok_ledger_fails_cancels_orphaned_legs_no_double_attach(self):
        """The broker-success / ledger-failure window: ``attach_exit_ladder``
        SUCCEEDS (OCO groups now live at the broker) but ``record_exit_ladder``
        then RAISES (here a duplicate ``alpaca_order_id`` hitting the orders
        UNIQUE constraint, rolling back ALL ledger rows). The adapter's own
        all-or-nothing rollback does NOT cover this — the groups are live but
        nothing is persisted. The exit_manager MUST cancel the just-placed legs
        so has_exit_ladder stays False AND nothing is live; otherwise the next
        pass re-attaches a SECOND ladder on the same shares (double disaster
        stop → over-sell to short)."""

        class _DupLegBroker(_StubBrokerClient):
            """attach_exit_ladder succeeds but returns two legs SHARING one
            tp_order_id, so record_exit_ladder's second insert_order hits the
            alpaca_order_id UNIQUE constraint and the atomic ladder write rolls
            back. Models broker-OK + ledger-raises (sqlite IntegrityError)."""

            def attach_exit_ladder(self, *, symbol, tranches, stop_price, time_in_force="gtc"):
                from alphalens_pipeline.paper.broker import ExitLadderLeg

                self.ladder_attempts += 1
                self.ladder_attaches.append(
                    {"symbol": symbol, "tranches": list(tranches), "stop_price": stop_price}
                )
                # Two legs whose TP ids COLLIDE → second insert_order raises.
                return [
                    ExitLadderLeg(
                        tranche_index=i,
                        qty=tr.qty,
                        take_profit_limit=tr.take_profit_limit,
                        stop_price=stop_price,
                        tp_order_id="dup-tp",
                        sl_order_id=f"sl-{i}",
                    )
                    for i, tr in enumerate(tranches)
                ]

        plan_id = self._seed_filled()
        broker = _DupLegBroker()
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        # Failure is caught + counted, not raised; nothing persisted.
        self.assertEqual(o.action, "ATTACHED")
        self.assertEqual(o.n_exits_submitted, 0)
        self.assertEqual(o.n_exits_failed, 1)
        with open_ledger(self.ledger) as conn:
            kinds = [
                r["order_kind"]
                for r in fetch_orders_for_plan(conn, plan_id)
                if r["order_kind"] in ("SL", "TP")
            ]
        self.assertEqual(kinds, [])
        # The orphaned LIVE legs were cancel-requested (dedup on the shared
        # tp id → one TP cancel + one SL cancel per distinct id).
        self.assertIn("dup-tp", broker.canceled)
        self.assertIn("sl-0", broker.canceled)
        self.assertIn("sl-1", broker.canceled)
        # Dead-man signal: still no protective ladder this pass.
        self.assertEqual(o.n_filled_without_sl, 1)

        # Next pass with a HEALTHY broker attaches the ladder ONCE — proving the
        # orphan cancel prevented a double-attach (no second live ladder lingered).
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "ATTACHED")
        self.assertEqual(o2.n_exits_submitted, 4)
        self.assertEqual(self.client.ladder_call_count(), 1)

    def test_attach_failure_then_healthy_broker_attaches_next_pass(self):
        """A failed attach persists nothing → the next pass re-enters the
        attach branch and a healthy broker lands the full ladder."""
        plan_id = self._seed_filled()
        self.client.fail_attach_ladder = True
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o1 = process_plan_exit(
                    conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o1.action, "ATTACHED")
        self.assertEqual(o1.n_exits_submitted, 0)
        self.assertGreater(o1.n_exits_failed, 0)
        with open_ledger(self.ledger) as conn:
            orders = fetch_orders_for_plan(conn, plan_id)
        self.assertEqual([o for o in orders if o["order_kind"] in ("SL", "TP")], [])

        # Pass 2 with a healthy broker attaches the full 2-tranche ladder
        # (2 TP + 2 SL rows = 4 exit submits).
        self.client.fail_attach_ladder = False
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "ATTACHED")
        self.assertEqual(o2.n_exits_submitted, 4)


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
        self.assertEqual(o_attach.action, "ATTACHED")

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
        # All 4 OCO ladder legs (2 TP + 2 SL) got cancelled.
        self.assertGreaterEqual(len(self.client.canceled), 4)

    def test_time_stop_still_fires_over_attach(self):
        """A past-deadline plan with NO ladder yet fires the time-stop on the
        FIRST pass — the attach-once branch is suppressed
        (should_fire_time_stop True), so NO ladder is attached."""
        plan_id = _seed_plan(self.ledger)
        # Fill is ALREADY past the time-stop deadline (the fixed ancient anchor).
        _add_entry(
            self.ledger,
            plan_id=plan_id,
            alpaca_id="e1",
            qty=27,
            status="FILLED",
            filled_qty=27,
            filled_price=100.0,
        )
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE fills SET filled_at = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE plan_id = ? AND order_kind = 'ENTRY')",
                (_FIRST_FILL_AT_FIXED.isoformat(), plan_id),
            )
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "TIME_STOP")
        # The attach-once branch was suppressed — no ladder attached.
        self.assertEqual(self.client.ladder_call_count(), 0)
        # A market sell was submitted for the liquidation.
        self.assertEqual(len([s for s in self.client.submissions if s["kind"] == "MARKET"]), 1)


class TestTimeStopVsLadderAttachCollision(_ExitTestBase):
    """HIGH safety regression: a live TIME_STOP market-sell must short-circuit
    the ATTACH-ONCE branch so it does NOT attach a fresh exit ladder on top of
    the in-flight liquidation.

    Sequence:
      pass 1  -> attach: an OCO ladder (TP + SL legs) covers the full position.
      pass 2  -> time-stop fires: cancels the live ladder legs + submits a
                 TIME_STOP market-sell (left UNFILLED, e.g. submitted at/after
                 the close).
      between -> the reconciler polls the canceled ladder legs to TERMINAL
                 CANCELED with 0 fill.
      pass 3+ -> net_open_qty is still > 0 (TIME_STOP not yet filled). The
                 ATTACH-ONCE branch must NOT re-attach a ladder while a
                 non-terminal TIME_STOP is in flight, else a live TIME_STOP
                 market-sell AND a fresh ladder both sit on the same shares ->
                 oversell to short.

    The guard makes pass 3 a NOOP (attach skipped while a non-terminal
    TIME_STOP exists), so live sell qty never exceeds the live position qty.
    """

    def _seed_filled_recent_plan(self) -> int:
        """Seed a fully-filled plan whose fill is RECENT (not yet past the
        time-stop deadline), so pass 1 attaches the protective ladder.
        ``_age_plan`` then backdates the fill so the time-stop fires on a
        subsequent pass — preserving the pass1=attach, pass2=fire-time-stop
        sequence (a past-deadline plan fires the time-stop on the very first
        pass with the attach skipped)."""
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
        self.assertEqual(o1.action, "ATTACHED")
        # The 2-tranche default plan attaches 2 OCO SL legs (one per group).
        self.assertEqual(self._live_sl_count(plan_id), 2)
        self.assertEqual(self.client.ladder_call_count(), 1)

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

        # The ladder was attached EXACTLY once (pass 1). The time-stop branch
        # never re-attached a competing ladder on top of the liquidation.
        self.assertEqual(
            self.client.ladder_call_count(), 1, "a fresh ladder was attached during the time-stop"
        )

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


class TestZeroFillStaysUnfilledNoConvergence(_ExitTestBase):
    """A zero-fill plan must NOT trigger the attach-once branch (no position to
    protect) — no cancel, no ladder, no fallback stop."""

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


class TestAttachOnceIdempotentNoEvent(_ExitTestBase):
    """Attach-once idempotency: two consecutive no-event passes on a plan that
    already has an attached OCO ladder create ZERO new orders."""

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


class _PositionReadFailsBroker(_StubBrokerClient):
    """Stub broker whose ``get_position`` ALWAYS raises (Alpaca read error /
    timeout). Forces ``_read_position`` down its fallback path so we can assert
    the ladder sizes to the live ledger position, not the gross entry total.
    """

    def get_position(self, symbol: str):
        raise RuntimeError("stub APIError: position read timed out")


class _PositionFlatBroker(_StubBrokerClient):
    """Stub broker whose ``get_position`` returns None = a DEFINITIVE
    broker-confirmed flat / absent position (the 404 / "does not exist" case).
    Per the BrokerClient contract this is a POSITIVE assertion of flatness,
    not an unknown — so a ledger that believes the plan is filled is a
    ledger<->broker DESYNC, not a sizing-fallback case."""

    def get_position(self, symbol: str):
        return None


class TestPositionQtyFallbackUsesNetOpen(_ExitTestBase):
    """HIGH (oversell risk): when the live broker position read genuinely
    ERRORS (transient), the SL fallback size MUST be ``net_open_qty`` (entry
    fills minus exit fills already SOLD), NOT the gross
    ``total_entry_filled_qty``. Sizing to gross entry fills after a partial TP
    already sold shares oversizes the SL -> persistent insufficient_qty
    rejection + oversell-to-short risk.

    Scenario: 27 shares entered, a 13-share TP already FILLED -> 14 net held.
    The broker position read fails (transient), so the convergence SL must
    size to 14 — and must NOT treat the transient blip as a desync (that would
    mask a real 14-share position and DROP its disaster-stop).
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
        self.assertEqual(o.action, "ATTACHED")
        # A TRANSIENT read error is NOT a desync — must not write a terminal
        # outcome that masks the real 14-share position.
        self.assertEqual(o.n_ledger_broker_desync, 0)
        # Ladder sized to net_open (27 - 13 = 14), NOT the gross 27 entry fills.
        # The 2-tranche default splits 14 across the OCO groups (7 + 7); the
        # SUM is the protected qty.
        self.assertEqual(
            sum(self._live_sl_qty(plan_id)),
            14,
            "ladder fallback oversized to gross entry fills (oversell-to-short risk)",
        )


class TestLedgerBrokerDesync(_ExitTestBase):
    """PHANTOM-position guard: the broker is the SOURCE OF TRUTH for whether a
    position exists. When the broker CONFIRMS flat/absent (definitive None, not
    a transient error) WHILE the ledger believes the plan is filled
    (net_open_qty > 0, no outcome yet), that is a ledger<->broker DESYNC.

    The harness must STOP chasing the phantom: submit NO protective order
    (there is nothing to protect), write a TERMINAL outcome so the plan is not
    reprocessed every pass, and SIGNAL the desync so it self-surfaces.
    """

    def _seed_filled_plan(self) -> int:
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

    def _outcome_kind(self, plan_id: int) -> str | None:
        with open_ledger(self.ledger) as conn:
            row = conn.execute(
                "SELECT exit_kind FROM plan_outcomes WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return row["exit_kind"] if row else None

    def test_broker_flat_while_ledger_filled_is_desync(self):
        plan_id = self._seed_filled_plan()
        broker = _PositionFlatBroker()
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "DESYNC_FLAT")
        # NO protective order submitted into the void.
        self.assertEqual(len(broker.submissions), 0)
        # Desync signalled.
        self.assertEqual(o.n_ledger_broker_desync, 1)
        # A terminal outcome was written so the plan is not reprocessed.
        self.assertEqual(self._outcome_kind(plan_id), "RECONCILED_FLAT")

    def test_desync_outcome_is_idempotent_not_reprocessed(self):
        plan_id = self._seed_filled_plan()
        broker = _PositionFlatBroker()
        with open_ledger(self.ledger) as conn:
            process_plan_exit(conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED)
        # Second pass: the plan already has the terminal DESYNC outcome.
        broker2 = _PositionFlatBroker()
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=broker2, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o2.action, "NOOP")
        self.assertEqual(o2.n_ledger_broker_desync, 0)
        self.assertEqual(len(broker2.submissions), 0)

    def test_flat_broker_and_flat_ledger_stays_unfilled_not_desync(self):
        # Entry CANCELED with zero fills -> net_open_qty == 0. A flat broker
        # here is NOT a desync (the ledger agrees there is no position); the
        # plan settles to UNFILLED exactly as today.
        plan_id = _seed_plan(self.ledger)
        _add_entry(self.ledger, plan_id=plan_id, alpaca_id="e1", qty=27, status="CANCELED")
        broker = _PositionFlatBroker()
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "UNFILLED")
        self.assertEqual(o.n_ledger_broker_desync, 0)
        self.assertEqual(len(broker.submissions), 0)
        self.assertEqual(self._outcome_kind(plan_id), "UNFILLED")

    def test_real_position_keeps_normal_attach_no_desync(self):
        # A broker that returns a REAL position keeps the normal attach-once
        # OCO-ladder path, no desync.
        plan_id = self._seed_filled_plan()
        client = _StubBrokerClient()  # default get_position returns qty 27
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o.action, "ATTACHED")
        self.assertEqual(o.n_ledger_broker_desync, 0)
        # The OCO ladder was attached once; no terminal outcome yet.
        self.assertEqual(client.ladder_call_count(), 1)
        self.assertIsNone(self._outcome_kind(plan_id))

    def test_transient_read_error_writes_no_terminal_outcome(self):
        # A transient read error must NEVER write the terminal DESYNC outcome
        # (that would mask a real position behind a blip). It falls back to the
        # ledger sizing + retries next pass.
        plan_id = self._seed_filled_plan()
        broker = _PositionReadFailsBroker()
        with open_ledger(self.ledger) as conn:
            with self.assertLogs("alphalens_pipeline.paper.exit_manager", level="WARNING"):
                o = process_plan_exit(
                    conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
                )
        self.assertEqual(o.action, "ATTACHED")
        self.assertEqual(o.n_ledger_broker_desync, 0)
        self.assertIsNone(self._outcome_kind(plan_id))

    def test_flat_broker_with_live_exit_order_is_not_desync_preserves_outcome(self):
        # DATA-LOSS GUARD: a flat broker while the ledger believes filled is
        # ONLY a desync when NO exit order was ever submitted. When an exit
        # (SL / TP) IS live, a flat broker most plausibly means that exit
        # FIRED — its fill may simply not be polled into the ledger yet (a
        # get_order blip during the same pass). Declaring desync here would
        # write a terminal RECONCILED_FLAT with null R and PERMANENTLY lose the
        # realized SL_HIT / TP_HIT outcome. The guard must instead defer to the
        # normal exit path (NOOP-retry until the exit fill is observable).
        plan_id = self._seed_filled_plan()
        with open_ledger(self.ledger) as conn:
            insert_order(
                conn,
                plan_id=plan_id,
                alpaca_order_id="sl1",
                side="SELL",
                order_kind="SL",
                tier_index=0,
                order_type="STOP",
                qty=27,
                limit_price=90.0,
                time_in_force="gtc",
                submitted_at=dt.datetime.now(dt.UTC),
                status="SUBMITTED",  # broker filled it; ledger has not polled the fill yet
            )
        broker = _PositionFlatBroker()
        with open_ledger(self.ledger) as conn:
            o = process_plan_exit(
                conn, plan_id=plan_id, broker=broker, observed_at=_OBSERVED_AT_FIXED
            )
        # NOT misclassified as a desync.
        self.assertNotEqual(o.action, "DESYNC_FLAT")
        self.assertEqual(o.n_ledger_broker_desync, 0)
        # NO terminal RECONCILED_FLAT swallowing the real exit outcome.
        self.assertNotEqual(self._outcome_kind(plan_id), "RECONCILED_FLAT")
        # Nothing submitted into the void (broker is flat -> size 0).
        self.assertEqual(len(broker.submissions), 0)

    def test_desync_guard_disabled_for_ladder_plans(self):
        # A plan that already attached an OCO ladder has exit_orders present, so
        # a broker FLAT read (net_open_qty > 0) is NOT a desync — the flat read
        # most plausibly means a ladder leg FIRED and the fill is not yet polled.
        # The #404 RECONCILED_FLAT / DESYNC_FLAT path must stay disabled.
        plan_id = self._seed_filled_plan()
        # Pass 1 attaches the OCO ladder on a real position.
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "ATTACHED")
        # Now the broker reads FLAT while the ledger still believes filled.
        flat = _PositionFlatBroker()
        with open_ledger(self.ledger) as conn:
            o2 = process_plan_exit(
                conn, plan_id=plan_id, broker=flat, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertNotEqual(o2.action, "DESYNC_FLAT")
        self.assertEqual(o2.n_ledger_broker_desync, 0)
        self.assertNotEqual(self._outcome_kind(plan_id), "RECONCILED_FLAT")
        self.assertEqual(len(flat.submissions), 0)


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

        # Pass 1: attach — an OCO ladder covers all 27 (fill not yet aged).
        with open_ledger(self.ledger) as conn:
            o1 = process_plan_exit(
                conn, plan_id=plan_id, broker=self.client, observed_at=_OBSERVED_AT_FIXED
            )
        self.assertEqual(o1.action, "ATTACHED")

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
            # is True again) — never an ATTACHED that arms a competing ladder.
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
        # No fresh ladder was attached after the time-stop deadline passed —
        # only the pass-1 ladder's 2 SL legs ever existed (now terminal
        # CANCELED), and ladder attach fired exactly once.
        with open_ledger(self.ledger) as conn:
            sl_rows = [r for r in fetch_orders_for_plan(conn, plan_id) if r["order_kind"] == "SL"]
        self.assertEqual(len(sl_rows), 2, "a fresh ladder was attached past the time-stop deadline")
        self.assertEqual(self.client.ladder_call_count(), 1)


class TestClassifyExitKindQuantityWeighted(unittest.TestCase):
    """Quantity-weighted exit-kind classification (PR-3 of the OCO-ladder track).

    The classifier picks the canonical label from how the FINAL exit fills
    split across sides. The legacy any-SL-fill rule (R2 bug) mislabeled a
    mostly-take-profit OCO-ladder exit as SL_HIT when one minority stop slice
    caught a dip; these tests pin the majority-of-exited-shares contract.
    """

    @staticmethod
    def _exit(kind: str, filled: int, status: str = "FILLED") -> _RowLike:
        return _RowLike(
            {
                "order_kind": kind,
                "status": status,
                "filled_qty_observed": filled,
            }
        )

    @staticmethod
    def _entry(filled: int) -> _RowLike:
        return _RowLike(
            {
                "order_kind": "ENTRY",
                "status": "FILLED",
                "filled_qty_observed": filled,
            }
        )

    def _snapshot(self, *, entry_qty: int, exit_orders: list[_RowLike]) -> _PlanSnapshot:
        return _PlanSnapshot(
            plan_id=1,
            ticker="NVDA",
            disaster_stop=80.0,
            tp_tranches=(),
            first_entry_fill_at=None,
            entry_orders=(self._entry(entry_qty),) if entry_qty else (),
            exit_orders=tuple(exit_orders),
            has_outcome=False,
            account="test",
            platform="alpaca",
            has_exit_ladder=False,
        )

    def test_given_single_full_tp_no_sl_when_classify_then_tp_hit(self):
        # Given: legacy single TP fully filled, no SL fill.
        snap = self._snapshot(
            entry_qty=27,
            exit_orders=[
                self._exit("TP", 27, status="FILLED"),
                self._exit("SL", 0, status="CANCELED"),
            ],
        )
        # When / Then.
        self.assertEqual(_classify_exit_kind(snap), "TP_HIT")

    def test_given_single_sl_no_tp_when_classify_then_sl_hit(self):
        # Given: legacy single SL filled, no TP fill.
        snap = self._snapshot(
            entry_qty=27,
            exit_orders=[
                self._exit("TP", 0, status="CANCELED"),
                self._exit("SL", 27, status="FILLED"),
            ],
        )
        self.assertEqual(_classify_exit_kind(snap), "SL_HIT")

    def test_given_time_stop_filled_when_classify_then_time_stop_hit_even_with_tp(self):
        # Given: a time-stop liquidation fired alongside a TP fill — precedence.
        snap = self._snapshot(
            entry_qty=27,
            exit_orders=[
                self._exit("TP", 13, status="FILLED"),
                self._exit("TIME_STOP", 14, status="FILLED"),
            ],
        )
        self.assertEqual(_classify_exit_kind(snap), "TIME_STOP_HIT")

    def test_given_mostly_tp_one_small_sl_slice_when_classify_then_not_sl_hit(self):
        # Given (THE R2 FIX): 3 TP tranches mostly filled (qty_tp=24) plus one
        # small SL slice (qty_sl=3); qty_tp > qty_sl. The old any-SL-fill rule
        # would have wrongly returned SL_HIT.
        snap = self._snapshot(
            entry_qty=30,
            exit_orders=[
                self._exit("TP", 9, status="FILLED"),
                self._exit("TP", 9, status="FILLED"),
                self._exit("TP", 6, status="PARTIALLY_FILLED"),
                self._exit("SL", 3, status="FILLED"),
            ],
        )
        kind = _classify_exit_kind(snap)
        self.assertNotEqual(kind, "SL_HIT", "mostly-TP exit must not be labeled SL_HIT (R2 fix)")
        self.assertEqual(kind, "PARTIAL_TP")

    def test_given_mostly_stopped_both_sides_filled_when_classify_then_sl_hit(self):
        # Given: qty_sl >= qty_tp with both > 0 (stop retired the majority).
        snap = self._snapshot(
            entry_qty=27,
            exit_orders=[
                self._exit("TP", 9, status="FILLED"),
                self._exit("SL", 18, status="FILLED"),
            ],
        )
        self.assertEqual(_classify_exit_kind(snap), "SL_HIT")

    def test_given_all_tranches_took_profit_when_classify_then_tp_hit(self):
        # Given: every TP tranche FILLED, no SL fill, qty_tp == total_entry.
        snap = self._snapshot(
            entry_qty=27,
            exit_orders=[
                self._exit("TP", 13, status="FILLED"),
                self._exit("TP", 14, status="FILLED"),
                self._exit("SL", 0, status="CANCELED"),
            ],
        )
        self.assertEqual(_classify_exit_kind(snap), "TP_HIT")

    def test_given_no_exit_fills_when_classify_then_partial_tp_catch_all(self):
        # Given: every exit order terminal with zero observed fill.
        snap = self._snapshot(
            entry_qty=27,
            exit_orders=[
                self._exit("TP", 0, status="CANCELED"),
                self._exit("SL", 0, status="CANCELED"),
            ],
        )
        self.assertEqual(_classify_exit_kind(snap), "PARTIAL_TP")

    def test_given_tie_qty_sl_equals_qty_tp_both_positive_when_classify_then_sl_hit(self):
        # Given: qty_sl == qty_tp > 0 — a tie resolves to SL (conservative).
        snap = self._snapshot(
            entry_qty=24,
            exit_orders=[
                self._exit("TP", 12, status="FILLED"),
                self._exit("SL", 12, status="FILLED"),
            ],
        )
        self.assertEqual(_classify_exit_kind(snap), "SL_HIT")

    def test_given_all_tp_filled_but_observed_underfill_when_classify_then_tp_hit(self):
        # Isolates rule-4's SECOND disjunct: every TP order is terminal-FILLED
        # and no SL filled, but the summed observed fills (qty_tp=24) are BELOW
        # total_entry (27) — so the qty_tp >= total_entry disjunct does NOT
        # apply and the all-FILLED disjunct alone must yield TP_HIT (the
        # intended "all tranches took profit" semantics under observed
        # under-count).
        snap = self._snapshot(
            entry_qty=27,
            exit_orders=[
                self._exit("TP", 12, status="FILLED"),
                self._exit("TP", 12, status="FILLED"),
                self._exit("SL", 0, status="CANCELED"),
            ],
        )
        self.assertEqual(_classify_exit_kind(snap), "TP_HIT")


if __name__ == "__main__":
    unittest.main()
