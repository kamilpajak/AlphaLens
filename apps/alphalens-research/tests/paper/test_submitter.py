"""Tests for the entry-tier submitter.

Uses a stub AlpacaClient that captures every ``submit_limit_order`` call
and returns synthetic order objects so the ledger writes happen
end-to-end without touching the SDK.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from alphalens_pipeline.paper.ledger import (
    fetch_orders_for_plan,
    insert_planned,
    open_ledger,
)
from alphalens_pipeline.paper.submitter import submit_for_date


@dataclass
class _StubOrder:
    """Mimics an Alpaca-py ``Order`` enough that ``str(order.id)`` works."""

    id: str


class _StubAlpacaClient:
    """Records each ``submit_limit_order`` call + returns sequential
    synthetic order ids. The submitter only touches this surface; no
    other AlpacaClient methods are needed here."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._next_id = 1

    def submit_limit_order(self, *, symbol, qty, limit_price, side, time_in_force):
        self.calls.append(
            {
                "symbol": symbol,
                "qty": qty,
                "limit_price": limit_price,
                "side": side,
                "time_in_force": time_in_force,
            }
        )
        oid = f"stub-{self._next_id:03d}"
        self._next_id += 1
        return _StubOrder(id=oid)


def _make_plan(
    ledger_path: Path,
    *,
    brief_date: dt.date,
    ticker: str,
    tiers: list[tuple[int, float, int, float, str]],
) -> int:
    """Create one PLANNED row plus its tiers and return the plan_id."""
    ts = dt.datetime.now(dt.UTC)
    with open_ledger(ledger_path) as conn:
        row = insert_planned(
            conn,
            brief_date=brief_date,
            ticker=ticker,
            theme="ai-infra",
            planned_at=ts,
            suggested_size_pct=5.0,
            scale_factor=0.05,
            final_size_pct=0.25,
            paper_equity=1_000_000.0,
            total_notional=2500.0,
            gross_notional=sum(q * p for _, p, q, _, _ in tiers),
            disaster_stop=80.0,
            order_ttl_days=10,
            tiers=tiers,
            tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
        )
    return row.plan_id


class _SubmitterTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"
        self.d = dt.date(2026, 5, 28)
        self.client = _StubAlpacaClient()

    def tearDown(self):
        self._tmp.cleanup()


class TestSubmitHappyPath(_SubmitterTestBase):
    def test_submits_one_order_per_tier_with_positive_qty(self):
        plan_id = _make_plan(
            self.ledger,
            brief_date=self.d,
            ticker="NVDA",
            tiers=[
                (0, 100.0, 27, 50.0, "t0"),
                (1, 95.0, 14, 30.0, "t1"),
                (2, 90.0, 8, 20.0, "t2"),
            ],
        )

        report = submit_for_date(
            brief_date=self.d, ledger_path=self.ledger, alpaca_client=self.client
        )

        self.assertEqual(report.n_plans_processed, 1)
        self.assertEqual(report.n_orders_submitted, 3)
        self.assertEqual(len(self.client.calls), 3)
        # All three calls were BUY GTC for NVDA at the right prices/qtys.
        self.assertEqual([c["symbol"] for c in self.client.calls], ["NVDA", "NVDA", "NVDA"])
        self.assertEqual([c["qty"] for c in self.client.calls], [27, 14, 8])
        self.assertEqual([c["limit_price"] for c in self.client.calls], [100.0, 95.0, 90.0])
        self.assertEqual({c["side"] for c in self.client.calls}, {"buy"})
        self.assertEqual({c["time_in_force"] for c in self.client.calls}, {"gtc"})

        # Ledger has three ENTRY orders with the stub alpaca_order_id values.
        with open_ledger(self.ledger) as conn:
            orders = fetch_orders_for_plan(conn, plan_id)
        self.assertEqual(len(orders), 3)
        self.assertEqual(
            sorted(o["alpaca_order_id"] for o in orders),
            ["stub-001", "stub-002", "stub-003"],
        )
        self.assertEqual({o["order_kind"] for o in orders}, {"ENTRY"})
        self.assertEqual({o["status"] for o in orders}, {"SUBMITTED"})

    def test_zero_qty_tier_is_skipped(self):
        """A tier with qty=0 (size dropped below one share after global
        scaling) is persisted in plan_entries for audit, but no order is
        submitted for it. The report breaks out the count separately."""
        _make_plan(
            self.ledger,
            brief_date=self.d,
            ticker="NVDA",
            tiers=[
                (0, 500.0, 5, 80.0, "t0"),
                (1, 500.0, 0, 20.0, "t1"),  # zero qty
            ],
        )

        report = submit_for_date(
            brief_date=self.d, ledger_path=self.ledger, alpaca_client=self.client
        )

        self.assertEqual(report.n_orders_submitted, 1)
        self.assertEqual(report.outcomes[0].n_tiers_skipped_zero_qty, 1)


class TestSubmitIdempotency(_SubmitterTestBase):
    def test_rerun_after_partial_submit_only_submits_missing_tiers(self):
        """Real-world crash recovery: the submitter pushed tier 0 + 1 then
        crashed before tier 2. Re-running must only submit tier 2."""
        from alphalens_pipeline.paper.ledger import insert_order

        plan_id = _make_plan(
            self.ledger,
            brief_date=self.d,
            ticker="NVDA",
            tiers=[
                (0, 100.0, 27, 50.0, "t0"),
                (1, 95.0, 14, 30.0, "t1"),
                (2, 90.0, 8, 20.0, "t2"),
            ],
        )

        # Simulate tiers 0 + 1 already submitted before the crash.
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            insert_order(
                conn,
                plan_id=plan_id,
                alpaca_order_id="pre-crash-0",
                side="BUY",
                order_kind="ENTRY",
                tier_index=0,
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=ts,
            )
            insert_order(
                conn,
                plan_id=plan_id,
                alpaca_order_id="pre-crash-1",
                side="BUY",
                order_kind="ENTRY",
                tier_index=1,
                order_type="LIMIT",
                qty=14,
                limit_price=95.0,
                time_in_force="gtc",
                submitted_at=ts,
            )

        report = submit_for_date(
            brief_date=self.d, ledger_path=self.ledger, alpaca_client=self.client
        )

        # Only tier 2 hit the broker — the existing two were skipped.
        self.assertEqual(report.n_orders_submitted, 1)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0]["limit_price"], 90.0)
        self.assertEqual(report.outcomes[0].n_tiers_skipped_existing, 2)

    def test_rerun_with_all_tiers_already_submitted_is_noop(self):
        from alphalens_pipeline.paper.ledger import insert_order

        plan_id = _make_plan(
            self.ledger,
            brief_date=self.d,
            ticker="NVDA",
            tiers=[(0, 100.0, 27, 100.0, "t0")],
        )
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            insert_order(
                conn,
                plan_id=plan_id,
                alpaca_order_id="already-there",
                side="BUY",
                order_kind="ENTRY",
                tier_index=0,
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=ts,
            )

        report = submit_for_date(
            brief_date=self.d, ledger_path=self.ledger, alpaca_client=self.client
        )

        self.assertEqual(report.n_orders_submitted, 0)
        self.assertEqual(len(self.client.calls), 0)


class TestSubmitOnlyProcessesPlanned(_SubmitterTestBase):
    def test_blocked_or_skipped_plans_are_ignored(self):
        """The submitter only operates on plans with status='PLANNED'.
        BLOCKED / SKIPPED plans are excluded so shadow-logged candidates
        never hit Alpaca."""
        plan_id_planned = _make_plan(
            self.ledger,
            brief_date=self.d,
            ticker="NVDA",
            tiers=[(0, 100.0, 27, 100.0, "t0")],
        )
        # Manually rewrite a second plan to BLOCKED so the filter is exercised.
        plan_id_blocked = _make_plan(
            self.ledger,
            brief_date=self.d,
            ticker="AVGO",
            tiers=[(0, 50.0, 50, 100.0, "t0")],
        )
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE plans SET status = 'BLOCKED' WHERE plan_id = ?",
                (plan_id_blocked,),
            )

        report = submit_for_date(
            brief_date=self.d, ledger_path=self.ledger, alpaca_client=self.client
        )
        self.assertEqual(report.n_plans_processed, 1)
        self.assertEqual([o.plan_id for o in report.outcomes], [plan_id_planned])


class TestSubmitMultipleDates(_SubmitterTestBase):
    def test_submit_filters_by_brief_date(self):
        """A submit run for date D ignores plans on date D-1 / D+1."""
        _make_plan(
            self.ledger,
            brief_date=dt.date(2026, 5, 27),
            ticker="OLD",
            tiers=[(0, 100.0, 10, 100.0, "t0")],
        )
        _make_plan(
            self.ledger,
            brief_date=self.d,  # 2026-05-28
            ticker="NEW",
            tiers=[(0, 100.0, 10, 100.0, "t0")],
        )

        report = submit_for_date(
            brief_date=self.d, ledger_path=self.ledger, alpaca_client=self.client
        )
        self.assertEqual(report.n_plans_processed, 1)
        self.assertEqual(self.client.calls[0]["symbol"], "NEW")


if __name__ == "__main__":
    unittest.main()
