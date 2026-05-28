"""Tests for the reconciler fills tracker.

Uses a stub AlpacaClient whose ``get_order`` returns synthetic Alpaca
order objects with configurable status + filled_qty + filled_avg_price.
Each test exercises one transition path through the reconciler.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from alphalens_pipeline.paper.ledger import (
    fetch_fills_for_order,
    fetch_orders_for_plan,
    insert_order,
    insert_planned,
    open_ledger,
)
from alphalens_pipeline.paper.reconciler import reconcile_orders


@dataclass
class _StubAlpacaOrder:
    """Mimics the alpaca-py Order object enough for the reconciler.

    Status is passed as a string ('new', 'partially_filled', 'filled', ...)
    matching what the reconciler's ``_map_alpaca_status`` extracts from
    the SDK enum.
    """

    id: str
    status: str
    filled_qty: int = 0
    filled_avg_price: float | None = None


@dataclass
class _StubExitOrder:
    """Mirror of _StubAlpacaOrder for synthetic exit submissions."""

    id: str


@dataclass
class _StubAccount:
    """Minimal account snapshot the gross_guard reads."""

    equity: float = 1_000_000.0
    long_market_value: float = 0.0


class _StubAlpacaClient:
    def __init__(self) -> None:
        self.orders_by_id: dict[str, _StubAlpacaOrder] = {}
        self.get_order_calls: list[str] = []
        self.fail_for: set[str] = set()
        # Captures exit submissions so the exit-aware tests can assert
        # what got submitted. Existing reconciler tests don't inspect
        # these but the methods need to exist so process_plan_exit can
        # be called transitively from the reconciler.
        self.exit_submissions: list[dict] = []
        self.canceled_orders: list[str] = []
        self._next_exit_id = 1
        # Gross-guard reads account.equity + account.long_market_value
        # post-reconcile per memo §6.1 Path B.
        self.account = _StubAccount()

    def add(self, order: _StubAlpacaOrder) -> None:
        self.orders_by_id[order.id] = order

    def get_order(self, alpaca_order_id: str):
        self.get_order_calls.append(alpaca_order_id)
        if alpaca_order_id in self.fail_for:
            raise RuntimeError(f"stub: simulated SDK error for {alpaca_order_id}")
        return self.orders_by_id[alpaca_order_id]

    def submit_stop_order(self, **kwargs):
        self.exit_submissions.append({"kind": "STOP", **kwargs})
        oid = f"exit-stop-{self._next_exit_id:03d}"
        self._next_exit_id += 1
        return _StubExitOrder(id=oid)

    def submit_limit_order(self, **kwargs):
        self.exit_submissions.append({"kind": "LIMIT", **kwargs})
        oid = f"exit-limit-{self._next_exit_id:03d}"
        self._next_exit_id += 1
        return _StubExitOrder(id=oid)

    def submit_market_order(self, **kwargs):
        self.exit_submissions.append({"kind": "MARKET", **kwargs})
        oid = f"exit-market-{self._next_exit_id:03d}"
        self._next_exit_id += 1
        return _StubExitOrder(id=oid)

    def cancel_order(self, alpaca_order_id: str) -> None:
        self.canceled_orders.append(alpaca_order_id)

    def get_account(self) -> _StubAccount:
        return self.account


def _make_plan_with_order(
    ledger_path: Path,
    *,
    alpaca_order_id: str,
    qty: int = 27,
    limit_price: float = 100.0,
) -> tuple[int, int]:
    """Seed a PLANNED row + one open ENTRY order. Returns (plan_id, order_id)."""
    ts = dt.datetime.now(dt.UTC)
    d = dt.date(2026, 5, 28)
    with open_ledger(ledger_path) as conn:
        row = insert_planned(
            conn,
            brief_date=d,
            ticker="NVDA",
            theme="ai-infra",
            planned_at=ts,
            suggested_size_pct=5.0,
            scale_factor=0.05,
            final_size_pct=0.25,
            paper_equity=1_000_000.0,
            total_notional=2500.0,
            gross_notional=qty * limit_price,
            disaster_stop=80.0,
            order_ttl_days=10,
            tiers=[(0, limit_price, qty, 100.0, "t0")],
            tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
        )
        order_id = insert_order(
            conn,
            plan_id=row.plan_id,
            alpaca_order_id=alpaca_order_id,
            side="BUY",
            order_kind="ENTRY",
            tier_index=0,
            order_type="LIMIT",
            qty=qty,
            limit_price=limit_price,
            time_in_force="gtc",
            submitted_at=ts,
        )
    return row.plan_id, order_id


class _ReconcilerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"
        self.client = _StubAlpacaClient()

    def tearDown(self):
        self._tmp.cleanup()


class TestStatusTransitions(_ReconcilerTestBase):
    def test_status_unchanged_when_alpaca_still_new(self):
        _make_plan_with_order(self.ledger, alpaca_order_id="aaa")
        self.client.add(_StubAlpacaOrder(id="aaa", status="new"))

        report = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)

        self.assertEqual(report.n_orders_checked, 1)
        self.assertEqual(report.n_orders_transitioned, 0)
        self.assertEqual(report.n_fills_appended, 0)

    def test_new_to_filled_transitions_status(self):
        _, order_id = _make_plan_with_order(self.ledger, alpaca_order_id="bbb")
        self.client.add(
            _StubAlpacaOrder(id="bbb", status="filled", filled_qty=27, filled_avg_price=99.5)
        )

        report = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)
        self.assertEqual(report.n_orders_transitioned, 1)
        self.assertEqual(report.outcomes[0].prev_status, "SUBMITTED")
        self.assertEqual(report.outcomes[0].new_status, "FILLED")
        self.assertEqual(report.n_fills_appended, 1)

        with open_ledger(self.ledger) as conn:
            fills = fetch_fills_for_order(conn, order_id)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["qty"], 27)
        self.assertAlmostEqual(fills[0]["price"], 99.5)

    def test_canceled_transition_appends_no_fill(self):
        _make_plan_with_order(self.ledger, alpaca_order_id="ccc")
        self.client.add(_StubAlpacaOrder(id="ccc", status="canceled"))

        report = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)
        self.assertEqual(report.outcomes[0].new_status, "CANCELED")
        self.assertEqual(report.n_fills_appended, 0)

        with open_ledger(self.ledger) as conn:
            orders = fetch_orders_for_plan(conn, 1)
        self.assertEqual(orders[0]["status"], "CANCELED")

    def test_unknown_alpaca_status_falls_through_to_submitted_with_warning(self):
        _, _ = _make_plan_with_order(self.ledger, alpaca_order_id="ddd")
        # 'wat' is not in _ALPACA_STATUS_MAP — reconciler logs warning and
        # treats as SUBMITTED so the ledger stays consistent.
        self.client.add(_StubAlpacaOrder(id="ddd", status="wat"))

        with self.assertLogs("alphalens_pipeline.paper.reconciler", level="WARNING") as cm:
            report = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)
        self.assertEqual(report.outcomes[0].new_status, "SUBMITTED")
        self.assertTrue(any("unknown Alpaca status" in m for m in cm.output))


class TestPartialFills(_ReconcilerTestBase):
    def test_partial_fill_appends_one_fill_row(self):
        _, order_id = _make_plan_with_order(self.ledger, alpaca_order_id="eee", qty=27)
        self.client.add(
            _StubAlpacaOrder(
                id="eee", status="partially_filled", filled_qty=10, filled_avg_price=99.0
            )
        )

        report = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)
        self.assertEqual(report.n_fills_appended, 1)
        self.assertEqual(report.outcomes[0].new_status, "PARTIALLY_FILLED")

        with open_ledger(self.ledger) as conn:
            fills = fetch_fills_for_order(conn, order_id)
        self.assertEqual(fills[0]["qty"], 10)

    def test_repoll_with_more_fills_appends_delta_only(self):
        """First poll observes 10 / 27 filled. Second poll observes 27 / 27.
        The reconciler must append ONLY the delta (17), not re-create the
        first 10."""
        _, order_id = _make_plan_with_order(self.ledger, alpaca_order_id="fff", qty=27)

        # Poll 1: 10 filled
        self.client.add(
            _StubAlpacaOrder(
                id="fff", status="partially_filled", filled_qty=10, filled_avg_price=99.0
            )
        )
        reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)

        # Poll 2: now 27 filled (full)
        self.client.orders_by_id["fff"] = _StubAlpacaOrder(
            id="fff", status="filled", filled_qty=27, filled_avg_price=99.4
        )
        report = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)

        self.assertEqual(report.n_fills_appended, 1)
        with open_ledger(self.ledger) as conn:
            fills = fetch_fills_for_order(conn, order_id)
        self.assertEqual(len(fills), 2)
        self.assertEqual([f["qty"] for f in fills], [10, 17])

    def test_repoll_with_unchanged_state_is_idempotent(self):
        """Reconciler called twice with identical Alpaca state must not
        create duplicate fill rows. The synthetic fill_id keyed on
        cumulative qty enforces this."""
        _make_plan_with_order(self.ledger, alpaca_order_id="ggg", qty=27)
        self.client.add(
            _StubAlpacaOrder(id="ggg", status="filled", filled_qty=27, filled_avg_price=99.5)
        )
        r1 = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)
        r2 = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)
        self.assertEqual(r1.n_fills_appended, 1)
        self.assertEqual(r2.n_fills_appended, 0)
        # Second pass also reports zero transitions — order already terminal.
        self.assertEqual(r2.n_orders_checked, 0)


class TestSdkFailureGracefullyContinues(_ReconcilerTestBase):
    def test_failed_get_order_logs_and_skips_rest_continue(self):
        """One open order fails to fetch; reconciler logs + skips it and
        still processes the next open order in the same pass."""
        _, _ = _make_plan_with_order(self.ledger, alpaca_order_id="bad")
        self.client.add(_StubAlpacaOrder(id="bad", status="new"))
        self.client.fail_for.add("bad")

        # Seed a second open order whose plan_id is the same — just need
        # another row in fetch_open_orders.
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            insert_order(
                conn,
                plan_id=1,
                alpaca_order_id="good",
                side="BUY",
                order_kind="ENTRY",
                tier_index=1,
                order_type="LIMIT",
                qty=5,
                limit_price=95.0,
                time_in_force="gtc",
                submitted_at=ts,
            )
        self.client.add(
            _StubAlpacaOrder(id="good", status="filled", filled_qty=5, filled_avg_price=94.9)
        )

        with self.assertLogs("alphalens_pipeline.paper.reconciler", level="WARNING") as cm:
            report = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)

        self.assertEqual(report.n_orders_checked, 1)
        self.assertEqual(report.outcomes[0].alpaca_order_id, "good")
        self.assertTrue(any("reconcile failed to fetch" in m for m in cm.output))


class TestNoOpenOrders(_ReconcilerTestBase):
    def test_empty_ledger_runs_clean(self):
        report = reconcile_orders(ledger_path=self.ledger, alpaca_client=self.client)
        self.assertEqual(report.n_orders_checked, 0)
        self.assertEqual(report.n_orders_transitioned, 0)
        self.assertEqual(report.n_fills_appended, 0)


if __name__ == "__main__":
    unittest.main()
