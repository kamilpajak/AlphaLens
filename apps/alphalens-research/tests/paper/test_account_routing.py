"""Schema v4: per-row ``account`` column on plans + orders.

A single canonical ledger file can host plans + orders from both Alpaca
paper accounts ('main' + 'test') simultaneously without collision. The
reconciler / submitter / exit-manager MUST filter by ``account`` so
cross-account UUID lookups never happen.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.paper.ledger import (
    LEDGER_SCHEMA_VERSION,
    VALID_ACCOUNTS,
    fetch_open_orders,
    fetch_plans_for_date,
    insert_order,
    insert_planned,
    open_ledger,
)


def _seed_plan(conn, *, brief_date: dt.date, ticker: str, account: str) -> int:
    plan_row = insert_planned(
        conn,
        brief_date=brief_date,
        ticker=ticker,
        theme="theme-x",
        planned_at=dt.datetime.now(dt.UTC),
        suggested_size_pct=1.0,
        scale_factor=1.0,
        final_size_pct=1.0,
        paper_equity=1_000_000.0,
        total_notional=1000.0,
        gross_notional=1000.0,
        disaster_stop=95.0,
        order_ttl_days=10,
        tiers=[(0, 100.0, 10, 100.0, "shallow")],
        tp_tranches=[(0, 110.0, 100.0, 1.0, "tp1")],
        account=account,
    )
    return plan_row.plan_id


class TestSchemaVersion(unittest.TestCase):
    def test_schema_version_is_4(self):
        self.assertEqual(LEDGER_SCHEMA_VERSION, 4)

    def test_valid_accounts_enum(self):
        self.assertEqual(VALID_ACCOUNTS, frozenset({"main", "test"}))


class TestPlansAccountColumn(unittest.TestCase):
    def test_same_ticker_same_date_different_accounts_coexist(self):
        """``UNIQUE(brief_date, ticker, account)`` allows the same ticker
        on the same date to have one plan per account — useful when both
        accounts are operating in parallel."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                _seed_plan(conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="main")
                _seed_plan(conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="test")

                plans_main = fetch_plans_for_date(conn, dt.date(2026, 5, 27), account="main")
                plans_test = fetch_plans_for_date(conn, dt.date(2026, 5, 27), account="test")
                plans_all = fetch_plans_for_date(conn, dt.date(2026, 5, 27))

                self.assertEqual(len(plans_main), 1)
                self.assertEqual(plans_main[0]["account"], "main")
                self.assertEqual(len(plans_test), 1)
                self.assertEqual(plans_test[0]["account"], "test")
                self.assertEqual(len(plans_all), 2)

    def test_insert_planned_rejects_unknown_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn, self.assertRaises(ValueError):
                _seed_plan(
                    conn,
                    brief_date=dt.date(2026, 5, 27),
                    ticker="X",
                    account="staging",  # unknown
                )

    def test_fetch_plans_for_date_rejects_unknown_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn, self.assertRaises(ValueError):
                fetch_plans_for_date(conn, dt.date(2026, 5, 27), account="staging")


class TestOrdersAccountFiltering(unittest.TestCase):
    """The reconciler MUST filter by account — cross-account UUID lookups
    would 404. This pins that ``fetch_open_orders`` honours the filter."""

    def test_open_orders_scoped_to_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                main_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="main"
                )
                test_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="test"
                )
                insert_order(
                    conn,
                    plan_id=main_id,
                    alpaca_order_id="uuid-main-001",
                    side="BUY",
                    order_kind="ENTRY",
                    order_type="LIMIT",
                    qty=10,
                    time_in_force="gtc",
                    submitted_at=dt.datetime.now(dt.UTC),
                    tier_index=0,
                    limit_price=100.0,
                    account="main",
                )
                insert_order(
                    conn,
                    plan_id=test_id,
                    alpaca_order_id="uuid-test-001",
                    side="BUY",
                    order_kind="ENTRY",
                    order_type="LIMIT",
                    qty=10,
                    time_in_force="gtc",
                    submitted_at=dt.datetime.now(dt.UTC),
                    tier_index=0,
                    limit_price=100.0,
                    account="test",
                )

                main_open = fetch_open_orders(conn, account="main")
                test_open = fetch_open_orders(conn, account="test")
                all_open = fetch_open_orders(conn)

                self.assertEqual(len(main_open), 1)
                self.assertEqual(main_open[0]["account"], "main")
                self.assertEqual(main_open[0]["alpaca_order_id"], "uuid-main-001")
                self.assertEqual(len(test_open), 1)
                self.assertEqual(test_open[0]["account"], "test")
                self.assertEqual(test_open[0]["alpaca_order_id"], "uuid-test-001")
                self.assertEqual(len(all_open), 2)

    def test_insert_order_rejects_unknown_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                plan_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="X", account="main"
                )
                with self.assertRaises(ValueError):
                    insert_order(
                        conn,
                        plan_id=plan_id,
                        alpaca_order_id="uuid-bad",
                        side="BUY",
                        order_kind="ENTRY",
                        order_type="LIMIT",
                        qty=10,
                        time_in_force="gtc",
                        submitted_at=dt.datetime.now(dt.UTC),
                        account="staging",  # unknown
                    )

    def test_fetch_open_orders_rejects_unknown_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn, self.assertRaises(ValueError):
                fetch_open_orders(conn, account="staging")


class TestDefaultAccountIsMain(unittest.TestCase):
    """Default for backward compatibility (in-code, not on-disk): callers
    that omit ``account=`` get 'main'. This pins the safe default so a
    fresh test forgetting the flag tags rows correctly."""

    def test_insert_planned_default_account_is_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                # No account= argument — defaults to 'main'.
                insert_planned(
                    conn,
                    brief_date=dt.date(2026, 5, 27),
                    ticker="AAPL",
                    theme="t",
                    planned_at=dt.datetime.now(dt.UTC),
                    suggested_size_pct=1.0,
                    scale_factor=1.0,
                    final_size_pct=1.0,
                    paper_equity=1_000_000.0,
                    total_notional=1000.0,
                    gross_notional=1000.0,
                    disaster_stop=95.0,
                    order_ttl_days=10,
                    tiers=[(0, 100.0, 10, 100.0, "shallow")],
                    tp_tranches=[(0, 110.0, 100.0, 1.0, "tp1")],
                )
                plans = fetch_plans_for_date(conn, dt.date(2026, 5, 27))
                self.assertEqual(plans[0]["account"], "main")


if __name__ == "__main__":
    unittest.main()
